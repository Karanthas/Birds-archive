"""Journalled, no-overwrite original transfer. Called only under the worker lifetime lock.

Published handoff and exclusive worker write permission on archive/claim roots are
deployment prerequisites: rename does not close a producer's old writable FD.
Never remove a published target or the original inbox pathname during recovery.
"""
import errno
import os
import re
import stat
import sqlite3
import unicodedata
from contextlib import ExitStack, closing, contextmanager
from urllib.parse import quote

from django.conf import settings
from django.db import transaction
from django.db import OperationalError
from django.utils import timezone

from archive.models import ArchiveOperation, ArchiveCompletion, ManagedOriginal, Photo, Job, Species
from archive.services.images import content_hash, ImageRejected
from archive.services.intake import managed_original_available, open_managed_original
from archive.services.paths import (UnsafePath, open_root, open_regular_beneath, parts_for,
                                    fingerprint, root_identity, validate_root_isolation)
from archive.services.restore import effective_replay_fingerprint, effective_replay_root


class ArchiveBlocked(UnsafePath):
    pass


MESSAGES = {
    'handoff_not_ready': '交接未就绪，请检查关闭后发布约定与归档目录。',
    'root_changed': '归档目录身份已变化，请核对原目录后继续。',
    'source_changed': '来源已变化，原图保护副本与已占用输入均保留，请核对。',
    'ownership_changed': '文件身份不一致，已保留现有文件，请核对。',
    'space_exhausted': '存储空间不足，原图已保留，请释放空间后重试。',
    'permission_denied': '存储权限不足，原图已保留，请检查权限后重试。',
    'sync_failed': '存储同步未完成，原图已保留，请检查存储后重试。',
    'archive_root_unavailable': '归档目录暂不可用，请恢复原目录后重试。',
    'target_conflict': '文件身份不一致，已保留现有文件，请核对原日志与文件。',
    'database_busy': '数据库暂忙，将有限重试；持续失败请检查其他写入者。',
    'temporarily_unavailable': '归档暂不可用，将有限重试；持续失败请检查存储。',
}


def _sync(fd):
    try:
        os.fsync(fd)
    except OSError:
        # Synchronization failure never authorizes automatic replay, including EAGAIN.
        raise ArchiveBlocked('sync_failed') from None


def archive_fault_hook(stage):
    from archive.services.jobs import fault_hook
    fault_hook('archive_'+stage)


def _save(operation, stage=None, **values):
    if stage:
        values['stage'] = stage
    values['updated_at'] = timezone.now()
    ArchiveOperation.objects.filter(pk=operation.pk).update(**values)
    for key, value in values.items():
        setattr(operation, key, value)
    Job.objects.filter(pk=operation.job_id).update(stage=operation.stage, updated_at=timezone.now())


def _context(operation, **values):
    _save(operation, retry_context={**operation.retry_context, **values})


def _same_inode(first, second):
    return (isinstance(first, dict) and isinstance(second, dict)
            and all(type(identity.get(key)) is int and identity[key] >= 0
                    for identity in (first, second) for key in ('dev', 'ino'))
            and all(first[key] == second[key] for key in ('dev', 'ino')))


def _same_content_identity(first, second):
    return _same_inode(first, second) and all(first.get(key) == second.get(key) for key in ('size', 'mtime_ns'))


def _root_paths():
    return {name: str(getattr(settings, 'BIRDS_'+name.upper()+'_ROOT', ''))
            for name in ('inbox', 'data', 'archive', 'claim')}


def _replay_root(operation, role):
    return effective_replay_root(role, operation.roots_snapshot[role])


def _replay_fingerprint(role, relative, original):
    if not original:
        return original
    return effective_replay_fingerprint(role, relative, original, ignore_ctime=True)


@contextmanager
def _roots(operation, *, bind=False, validate_only=False):
    paths = _root_paths()
    if any(not value for value in paths.values()):
        raise ArchiveBlocked('handoff_not_ready')
    snapshot = operation.roots_snapshot
    bound = snapshot.get('binding_state') == 'bound'
    effective_roots = ({key: _replay_root(operation, key) for key in paths} if bound else snapshot)
    if bound and any(os.path.abspath(paths[key]) != effective_roots[key]['path'] for key in paths):
        raise ArchiveBlocked('root_changed')
    with ExitStack() as stack:
        fds = {}
        for name, path in paths.items():
            try:
                fd = open_root(path)
            except OSError as error:
                # A retained claim or published original can survive an unavailable inbox.
                if name == 'inbox' and bound and operation.stage not in ('queued', 'protecting', 'protected'):
                    continue
                if isinstance(error, PermissionError):
                    raise ArchiveBlocked('permission_denied') from None
                raise ArchiveBlocked('archive_root_unavailable' if bound else 'handoff_not_ready') from None
            stack.callback(os.close, fd)
            fds[name] = fd
            if bound and root_identity(fd) != effective_roots[name]['identity']:
                raise ArchiveBlocked('root_changed')
        for i, name in enumerate(fds):
            for other in list(fds)[i+1:]:
                validate_root_isolation(fds[name], fds[other], paths[name], paths[other])
        if 'inbox' in fds and os.fstat(fds['inbox']).st_dev != os.fstat(fds['claim']).st_dev:
            raise ArchiveBlocked('handoff_not_ready')
        for name in ('archive', 'claim'):
            if os.fstat(fds[name]).st_mode & 0o022:
                raise ArchiveBlocked('handoff_not_ready')
        if not bound and not validate_only:
            if not bind or operation.stage != 'queued' or operation.temporary_relative_path or operation.claim_relative_path:
                raise ArchiveBlocked('root_changed')
            snapshot = {'binding_state': 'bound', **{
                key: {'path': os.path.abspath(paths[key]), 'identity': root_identity(fd)} for key, fd in fds.items()}}
            with transaction.atomic():
                current = ArchiveOperation.objects.get(pk=operation.pk)
                if current.roots_snapshot.get('binding_state') != 'unbound':
                    raise ArchiveBlocked('root_changed')
                _save(operation, roots_snapshot=snapshot)
        yield fds
        # Reopen configured paths, including after destructive boundaries.
        for name, fd in fds.items():
            current = open_root(paths[name])
            try:
                if root_identity(current) != root_identity(fd):
                    raise ArchiveBlocked('root_changed')
            finally:
                os.close(current)


def bind_archive_roots(operation):
    with _roots(operation, bind=True):
        pass
    return operation.roots_snapshot


def _recheck_roots(operation, roots):
    for name, held in roots.items():
        try:
            current = open_root(_root_paths()[name])
        except OSError:
            if name == 'inbox' and operation.stage not in ('queued', 'protecting', 'protected'):
                continue
            raise ArchiveBlocked('root_changed') from None
        try:
            if (root_identity(current) != root_identity(held)
                    or root_identity(current) != _replay_root(operation, name)['identity']):
                raise ArchiveBlocked('root_changed')
        finally:
            os.close(current)


def _directory(root_fd, relative):
    current = os.dup(root_fd)
    try:
        for part in parts_for(relative):
            created = False
            try:
                os.mkdir(part, mode=0o700, dir_fd=current)
                created = True
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current)
            if created:
                _sync(child); _sync(current)
            os.close(current); current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _name_limit(root_fd):
    limit = os.fpathconf(root_fd, 'PC_NAME_MAX')
    # Private directories retain their full operation UUID.
    if 0 < limit < 40:
        raise ArchiveBlocked('handoff_not_ready')
    return min(180, limit) if limit > 0 else 180


def _component(value, *, stable_id, limit=180, suffix=''):
    value = unicodedata.normalize('NFC', value)
    value = ''.join('_' if unicodedata.category(char).startswith('C') else char for char in value)
    value = re.sub(r'[/\\:*?"<>|]', '_', value).strip(' .')
    value = value or '未知'
    device = unicodedata.normalize('NFKC', value).casefold().split('.')[0].rstrip(' ')
    if device in ('con', 'prn', 'aux', 'nul') or re.fullmatch(r'(com|lpt)[1-9]', device):
        value = '_'+value
        suffix = suffix or '_'+stable_id
    if len((value+suffix).encode('utf-8')) > limit:
        suffix = suffix or '_'+stable_id
        budget = limit-len(suffix.encode('utf-8'))
        if budget < 1:
            raise ArchiveBlocked('handoff_not_ready')
        value = value.encode('utf-8')[:budget].decode('utf-8', errors='ignore').rstrip(' .') or '_'
    return value+suffix


def _basename(operation, limit, suffix=''):
    value = operation.original_basename
    stem, extension = os.path.splitext(value)
    if extension.casefold() not in ('.jpg', '.jpeg'):
        stem, extension = value, ''
    return _component(stem, stable_id=operation.pk.hex[:8],
        limit=limit-len(extension.encode('utf-8')), suffix=suffix)+extension


def ensure_archive_directory(operation, *, root_fd=None):
    """Allocate once under a short transaction; record the mapping in the operation."""
    limit = _name_limit(root_fd) if root_fd is not None else 180
    if operation.retry_context.get('directory'):
        return operation.retry_context['directory']
    with transaction.atomic():
        if operation.classification == 'undetermined':
            directory = '无法确定'
        elif len(operation.species_ids_snapshot) > 1:
            directory = '多鸟种'
        else:
            species = Species.objects.get(pk=operation.species_ids_snapshot[0])
            if not species.archive_directory:
                base = _component(species.display_name, stable_id=species.pk.hex[:8], limit=limit)
                reserved = base.casefold() in ('多鸟种', '无法确定', '.archive-ops', '.archive-trash')
                for length in (0, 8, 12, 32):
                    if not length and reserved:
                        continue
                    candidate = _component(species.display_name, stable_id=species.pk.hex[:8], limit=limit,
                        suffix=('_'+species.pk.hex[:length]) if length else '')
                    key = unicodedata.normalize('NFC', candidate).casefold()
                    if not Species.objects.exclude(pk=species.pk).filter(directory_key=key).exists():
                        species.archive_directory, species.directory_key = candidate, key
                        species.save(update_fields=['archive_directory', 'directory_key'])
                        break
                else:
                    raise ArchiveBlocked('ownership_changed')
            directory = species.archive_directory
        _context(operation, directory=directory, directory_species_ids=operation.species_ids_snapshot,
                 allocated_basename=_basename(operation, limit), collision_counter=0, name_limit=limit)
    return directory


def _private_directory(operation, root_fd, kind):
    relative = str(operation.pk) if kind == 'claim' else '.archive-ops/'+str(operation.pk)
    key = kind+'_directory_identity'
    saved = operation.retry_context.get(key)
    if saved:
        fd = _directory_existing(root_fd, relative)
        role = 'claim' if kind == 'claim' else 'archive'
        if not _same_inode(root_identity(fd), effective_replay_fingerprint(role, relative, saved)):
            os.close(fd)
            raise ArchiveBlocked('ownership_changed')
        return fd
    parent = os.dup(root_fd) if kind == 'claim' else _directory(root_fd, '.archive-ops')
    try:
        # Never adopt an existing foreign directory, even when empty.
        os.mkdir(str(operation.pk), mode=0o700, dir_fd=parent)
        fd = os.open(str(operation.pk), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        _sync(fd); _sync(parent)
        _context(operation, **{key: root_identity(fd)})
        return fd
    finally:
        os.close(parent)


def _directory_existing(root_fd, relative):
    current = os.dup(root_fd)
    try:
        for component in parts_for(relative):
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current)
            os.close(current); current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _verified(role, root_fd, relative, operation, expected=None, *, inode_only=False):
    expected = _replay_fingerprint(role, relative, expected)
    with open_regular_beneath(root_fd, relative) as stream:
        before = fingerprint(os.fstat(stream.fileno()))
        if expected and not (_same_inode(before, expected) if inode_only else _same_content_identity(before, expected)):
            raise ArchiveBlocked('ownership_changed')
        digest, size = content_hash(stream)
        if (digest, size) != (operation.expected_sha256, operation.byte_size) or fingerprint(os.fstat(stream.fileno())) != before:
            raise ArchiveBlocked('source_changed')
        with open_regular_beneath(root_fd, relative) as reopened:
            if fingerprint(os.fstat(reopened.fileno())) != before:
                raise ArchiveBlocked('ownership_changed')
        return before


def _protect(operation, roots):
    source_path = operation.source_observation.source.relative_path
    with open_regular_beneath(roots['inbox'], source_path) as source:
        before = fingerprint(os.fstat(source.fileno()))
        if before != _replay_fingerprint('inbox', source_path, operation.source_fingerprint):
            raise ArchiveBlocked('source_changed')
        if content_hash(source) != (operation.expected_sha256, operation.byte_size):
            raise ArchiveBlocked('source_changed')
        archive_fault_hook('before_protecting')
        _save(operation, 'protecting')
        archive_fault_hook('protecting')
        temp_dir = _private_directory(operation, roots['archive'], 'temporary')
        try:
            relative = '.archive-ops/'+str(operation.pk)+'/original'
            if not operation.temporary_relative_path:
                _save(operation, temporary_relative_path=relative)
            try:
                fd = os.open('original', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=temp_dir)
            except FileExistsError:
                fd = os.open('original', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=temp_dir)
                if not stat.S_ISREG(os.fstat(fd).st_mode) or not _same_inode(fingerprint(os.fstat(fd)), operation.retry_context.get('temporary_identity')):
                    os.close(fd); raise ArchiveBlocked('ownership_changed')
            else:
                _context(operation, temporary_identity=root_identity(fd))
            with os.fdopen(fd, 'w+b') as target:
                target.truncate(0); source.seek(0)
                copied = 0
                while True:
                    block = source.read(min(1024*1024, max(1, operation.byte_size//2)))
                    if not block:
                        break
                    copied += len(block)
                    if copied > operation.byte_size or copied > settings.MAX_FILE_BYTES:
                        raise ArchiveBlocked('source_changed')
                    target.write(block); target.flush()
                    archive_fault_hook('partial_copy')
                if fingerprint(os.fstat(source.fileno())) != before:
                    raise ArchiveBlocked('source_changed')
                target.seek(0)
                if content_hash(target) != (operation.expected_sha256, operation.byte_size):
                    raise ArchiveBlocked('source_changed')
                archive_fault_hook('after_hash'); archive_fault_hook('before_file_fsync')
                _sync(target.fileno())
                archive_fault_hook('after_file_fsync')
                _sync(temp_dir)
                _save(operation, 'protected', protected_fingerprint=fingerprint(os.fstat(target.fileno())),
                      temporary_fingerprint=fingerprint(os.fstat(target.fileno())))
            archive_fault_hook('protected')
        finally:
            os.close(temp_dir)


def _claim(operation, roots):
    claim_dir = _private_directory(operation, roots['claim'], 'claim')
    try:
        relative = str(operation.pk)+'/source'
        if operation.stage == 'protected':
            _save(operation, 'claim_intent', claim_relative_path=relative)
        try:
            claimed = _verified('claim', roots['claim'], relative, operation, operation.source_fingerprint)
        except FileNotFoundError:
            if 'inbox' not in roots:
                raise ArchiveBlocked('root_changed')
            source_path = operation.source_observation.source.relative_path
            with open_regular_beneath(roots['inbox'], source_path) as source:
                before = fingerprint(os.fstat(source.fileno()))
                expected = _replay_fingerprint('inbox', source_path, operation.source_fingerprint)
                if before != expected or content_hash(source) != (operation.expected_sha256, operation.byte_size):
                    raise ArchiveBlocked('source_changed')
                archive_fault_hook('before_claim')
                if os.listdir(claim_dir):
                    raise ArchiveBlocked('ownership_changed')
                parent_path, basename = source_path.rsplit('/', 1) if '/' in source_path else ('', source_path)
                parent = _directory_existing(roots['inbox'], parent_path) if parent_path else os.dup(roots['inbox'])
                try:
                    os.rename(basename, 'source', src_dir_fd=parent, dst_dir_fd=claim_dir)
                    archive_fault_hook('after_claim_rename')
                    # Reopen the directory entry, never just rehash the pre-rename FD.
                    try:
                        claimed = _verified('claim', roots['claim'], relative, operation, before)
                    except ArchiveBlocked:
                        raise ArchiveBlocked('source_changed') from None
                    if not _same_inode(claimed, fingerprint(os.fstat(source.fileno()))):
                        raise ArchiveBlocked('source_changed')
                    _sync(parent); _sync(claim_dir)
                finally:
                    os.close(parent)
        _save(operation, 'claimed', claim_fingerprint=claimed)
        archive_fault_hook('claimed')
    except ArchiveBlocked as error:
        if str(error) == 'ownership_changed':
            raise ArchiveBlocked('source_changed') from None
        raise
    finally:
        os.close(claim_dir)


def _target(operation, roots):
    directory = ensure_archive_directory(operation, root_fd=roots['archive'])
    if operation.retry_context['directory_species_ids'] != operation.species_ids_snapshot:
        raise ArchiveBlocked('ownership_changed')
    if operation.classification != 'undetermined' and len(operation.species_ids_snapshot) == 1:
        if Species.objects.get(pk=operation.species_ids_snapshot[0]).archive_directory != directory:
            raise ArchiveBlocked('ownership_changed')
    year = operation.capture_snapshot.get('year') or '未知年份'
    if year != '未知年份' and not re.fullmatch(r'[0-9]{4}', year):
        raise ArchiveBlocked('ownership_changed')
    relative_dir = directory+'/'+year
    target_dir = _directory(roots['archive'], relative_dir)
    os.close(target_dir)
    basename = operation.retry_context['allocated_basename']
    limit = _name_limit(roots['archive'])
    if any(len(part.encode('utf-8')) > limit for part in (directory, year, basename)):
        raise ArchiveBlocked('ownership_changed')
    return relative_dir+'/'+basename


def _publish(operation, roots):
    _verified('claim', roots['claim'], operation.claim_relative_path, operation, operation.claim_fingerprint)
    temporary = _verified('archive', roots['archive'], operation.temporary_relative_path,
                          operation, operation.protected_fingerprint)
    if operation.stage == 'claimed':
        _save(operation, 'verified', temporary_fingerprint=temporary)
    if not operation.target_relative_path:
        _save(operation, target_relative_path=_target(operation, roots))
    _save(operation, 'publish_intent')
    temp_dir = _private_directory(operation, roots['archive'], 'temporary')
    try:
        for _ in range(100):
            _target(operation, roots)  # Verify the persistent species mapping before every publication.
            parent, basename = operation.target_relative_path.rsplit('/', 1)
            target_dir = _directory_existing(roots['archive'], parent)
            try:
                try:
                    os.link('original', basename, src_dir_fd=temp_dir, dst_dir_fd=target_dir, follow_symlinks=False)
                    archive_fault_hook('after_link')
                except FileExistsError:
                    try:
                        candidate = _verified('archive', roots['archive'], operation.target_relative_path, operation)
                    except (OSError, UnsafePath):
                        candidate = None
                    if not candidate or not _same_inode(candidate, temporary):
                        # Hash equality never grants ownership. Persist allocation before retry.
                        counter = operation.retry_context.get('collision_counter', 0)+1
                        suffix = '_'+operation.pk.hex[:8]+(('_'+str(counter)) if counter > 1 else '')
                        _context(operation, collision_counter=counter)
                        limit = min(operation.retry_context.get('name_limit', 180), _name_limit(roots['archive']))
                        _save(operation, target_relative_path=parent+'/'+_basename(operation, limit, suffix))
                        continue
                _sync(target_dir)
                published = _verified('archive', roots['archive'], operation.target_relative_path,
                                      operation, temporary)
                _save(operation, 'published', published_fingerprint=published)
                return
            finally:
                os.close(target_dir)
        raise ArchiveBlocked('ownership_changed')
    finally:
        os.close(temp_dir)


def _register(operation, roots):
    published = _verified('archive', roots['archive'], operation.target_relative_path,
                          operation, operation.published_fingerprint)
    with transaction.atomic():
        managed, created = ManagedOriginal.objects.get_or_create(operation=operation, defaults={
            'photo_id': operation.photo_id, 'root_key': 'archive', 'relative_path': operation.target_relative_path,
            'actual_basename': operation.target_relative_path.rsplit('/', 1)[1], 'fingerprint': published,
            'sha256': operation.expected_sha256, 'byte_size': operation.byte_size})
        if not created and (managed.photo_id != operation.photo_id or managed.relative_path != operation.target_relative_path):
            raise ArchiveBlocked('ownership_changed')
        _save(operation, 'registered')
    archive_fault_hook('registered')


def _cleanup(operation, roots):
    _registered_readback(operation)
    published = _verified('archive', roots['archive'], operation.target_relative_path,
                          operation, operation.published_fingerprint)
    temp_dir = _private_directory(operation, roots['archive'], 'temporary')
    claim_dir = _private_directory(operation, roots['claim'], 'claim')
    try:
        if operation.stage == 'registered':
            _recheck_roots(operation, roots)
            try:
                temporary = _verified('archive', roots['archive'], operation.temporary_relative_path,
                                      operation, operation.protected_fingerprint)
            except FileNotFoundError:
                pass  # Registered journal proves unlink may already have happened.
            else:
                if not _same_inode(temporary, published):
                    raise ArchiveBlocked('ownership_changed')
                os.unlink('original', dir_fd=temp_dir)
                archive_fault_hook('after_temp_unlink')
            _sync(temp_dir)
            _save(operation, 'temp_cleaned')
        if operation.stage == 'temp_cleaned':
            _save(operation, 'source_cleanup_intent')
        if operation.stage == 'source_cleanup_intent':
            _recheck_roots(operation, roots)
            try:
                _verified('claim', roots['claim'], operation.claim_relative_path,
                          operation, operation.claim_fingerprint)
            except FileNotFoundError:
                pass
            else:
                # Check target immediately before deleting the private claim.
                _verified('archive', roots['archive'], operation.target_relative_path,
                          operation, operation.published_fingerprint)
                os.unlink('source', dir_fd=claim_dir)
                archive_fault_hook('after_claim_unlink')
            _sync(claim_dir)
            _save(operation, 'source_cleaned')
        published = _verified('archive', roots['archive'], operation.target_relative_path,
                              operation, operation.published_fingerprint)
        if os.listdir(temp_dir) or os.listdir(claim_dir):
            raise ArchiveBlocked('ownership_changed')
        _recheck_roots(operation, roots)
        _registered_readback(operation)
        with transaction.atomic():
            managed = ManagedOriginal.objects.get(operation=operation)
            if managed.relative_path != operation.target_relative_path or managed.sha256 != operation.expected_sha256:
                raise ArchiveBlocked('ownership_changed')
            ManagedOriginal.objects.filter(pk=managed.pk).update(fingerprint=published)
            _save(operation, 'complete', last_error='')
            Photo.objects.filter(pk=operation.photo_id).update(state='archived', original_available=True)
            Job.objects.filter(pk=operation.job_id).update(state='succeeded', stage='complete', error_code='', safe_message='')
            _, created = ArchiveCompletion.objects.get_or_create(operation=operation, photo_id=operation.photo_id)
            if created:
                Photo.objects.filter(pk=operation.photo_id, current_capture__isnull=True).update(
                    current_capture=operation.capture_snapshot,
                    current_classification='identified' if operation.classification == 'species' else 'unidentified',
                    capture_override_mode='manual' if operation.photo.manual_capture is not None else 'inherit')
    finally:
        os.close(temp_dir); os.close(claim_dir)


def _registered_readback(operation):
    """Observe the committed registration through an independent read-only SQLite connection."""
    filename = str(settings.DATABASES['default']['NAME'])
    try:
        with closing(sqlite3.connect('file:'+quote(filename)+'?mode=ro', uri=True, timeout=5)) as db:
            record = db.execute('SELECT photo_id, relative_path, sha256, byte_size, root_key '
                'FROM archive_managedoriginal WHERE operation_id=?', (operation.pk.hex,)).fetchone()
    except sqlite3.OperationalError as error:
        raise ArchiveBlocked('database_busy' if any(word in str(error).lower() for word in ('busy', 'locked')) else 'sync_failed') from None
    except sqlite3.DatabaseError:
        raise ArchiveBlocked('archive_io') from None
    if record != (operation.photo_id.hex, operation.target_relative_path, operation.expected_sha256,
                  operation.byte_size, 'archive'):
        raise ArchiveBlocked('ownership_changed')


def verify_managed_original(photo):
    managed = ManagedOriginal.objects.filter(photo_id=photo.pk).first()
    return managed is not None and managed_original_available(managed)


def open_current_original(photo):
    """只为一致性完成的主文件交接FD；不依赖inbox，也不回退来源文件。"""
    completion = ArchiveCompletion.objects.select_related('operation').filter(photo_id=photo.pk).first()
    if completion is None:
        raise ArchiveCompletion.DoesNotExist
    operation = completion.operation
    managed = ManagedOriginal.objects.select_related('operation').filter(photo_id=photo.pk).first()
    if (managed is None or managed.operation_id != operation.pk or operation.photo_id != photo.pk
            or operation.stage != 'complete' or operation.schema_version != 1
            or operation.roots_snapshot.get('binding_state') != 'bound'
            or not operation.roots_snapshot.get('archive')
            or managed.actual_basename != parts_for(managed.relative_path)[-1]
            or managed.sha256 != operation.expected_sha256 or managed.byte_size != operation.byte_size):
        raise UnsafePath('original_unavailable')
    if managed.location_revision == 0:
        if managed.last_management_operation_id or managed.relative_path != operation.target_relative_path:
            raise UnsafePath('original_unavailable')
    else:
        latest = managed.last_management_operation
        if (latest is None or latest.photo_id != photo.pk
                or latest.source_location_revision + 1 != managed.location_revision
                or latest.stage not in ('registered', 'cleaned', 'completed')
                or latest.target_relative_path != managed.relative_path
                or latest.target_fingerprint != managed.fingerprint):
            raise UnsafePath('original_unavailable')
    return open_managed_original(managed), managed.actual_basename


def open_completed_original(photo):
    """Compatibility entry point; the original completion remains provenance only."""
    return open_current_original(photo)


def record_original_unavailable(photo):
    """保存固定待对账事实，不重放归档、不重写完成凭据、不修改外来文件。"""
    Photo.objects.filter(pk=photo.pk).update(original_available=False)
    ArchiveOperation.objects.filter(photo_id=photo.pk, stage='complete').update(
        last_error='original_unavailable', updated_at=timezone.now())


def check_archive_retry(operation):
    """Read-only reconciliation: never bind roots or adopt a new source/target."""
    if operation.schema_version != 1:
        raise ArchiveBlocked('target_conflict')
    if operation.stage in ('queued', 'protecting', 'protected') and (
            settings.BIRDS_HANDOFF_MODE != 'published' or not settings.BIRDS_PUBLISHED_CONTRACT):
        raise ArchiveBlocked('handoff_not_ready')
    with _roots(operation, validate_only=True) as roots:
        for kind, root_name, relative in (
                ('temporary', 'archive', '.archive-ops/'+str(operation.pk)),
                ('claim', 'claim', str(operation.pk))):
            identity = operation.retry_context.get(kind+'_directory_identity')
            try:
                fd = _directory_existing(roots[root_name], relative)
            except FileNotFoundError:
                if identity is not None:
                    raise
                continue
            try:
                effective = effective_replay_fingerprint(root_name, relative, identity)
                if not _same_inode(root_identity(fd), effective):
                    raise ArchiveBlocked('ownership_changed')
            finally:
                os.close(fd)
        if operation.stage in ('queued', 'protecting'):
            # A crash can leave O_EXCL's file before its inode reaches the journal.
            # A read-only retry must not grant ownership of that existing object.
            try:
                with open_regular_beneath(roots['archive'], '.archive-ops/'+str(operation.pk)+'/original') as stream:
                    if not _same_inode(fingerprint(os.fstat(stream.fileno())),
                                       operation.retry_context.get('temporary_identity')):
                        raise ArchiveBlocked('ownership_changed')
            except FileNotFoundError:
                pass
        if operation.stage in ('queued', 'protecting', 'protected'):
            _verified('inbox', roots['inbox'], operation.source_observation.source.relative_path,
                      operation, operation.source_fingerprint)
        elif operation.stage == 'claim_intent':
            try:
                _verified('claim', roots['claim'], operation.claim_relative_path,
                          operation, operation.source_fingerprint)
            except FileNotFoundError:
                _verified('inbox', roots['inbox'], operation.source_observation.source.relative_path,
                          operation, operation.source_fingerprint)
        elif operation.stage in ('claimed', 'verified', 'publish_intent'):
            _verified('claim', roots['claim'], operation.claim_relative_path,
                      operation, operation.claim_fingerprint)
        if operation.stage in ('protected', 'claim_intent', 'claimed', 'verified', 'publish_intent'):
            _verified('archive', roots['archive'], operation.temporary_relative_path,
                      operation, operation.protected_fingerprint)
        if operation.stage in ('published', 'registered', 'temp_cleaned', 'source_cleanup_intent', 'source_cleaned', 'complete'):
            _verified('archive', roots['archive'], operation.target_relative_path,
                      operation, operation.published_fingerprint)
    return True


def archive_error_code(error):
    if isinstance(error, ArchiveBlocked):
        return {'ownership_changed': 'target_conflict', 'archive_io': 'sync_failed'}.get(str(error), str(error))
    if isinstance(error, ImageRejected):
        return 'source_changed'
    if isinstance(error, UnsafePath):
        return 'target_conflict'
    if isinstance(error, OperationalError):
        return 'database_busy' if any(word in str(error).lower() for word in ('locked', 'busy')) else 'sync_failed'
    if isinstance(error, PermissionError):
        return 'permission_denied'
    if isinstance(error, OSError):
        if error.errno in (errno.ENOSPC, errno.EDQUOT):
            return 'space_exhausted'
        if error.errno in (errno.EINTR, errno.EAGAIN, errno.EBUSY):
            return 'temporarily_unavailable'
    return 'sync_failed'


def process_archive(job):
    operation = ArchiveOperation.objects.select_related('source_observation__source', 'photo').get(job=job)
    if operation.stage == 'complete' or ArchiveCompletion.objects.filter(operation=operation).exists():
        return verify_managed_original(operation.photo)
    try:
        if operation.schema_version != 1:
            raise ArchiveBlocked('ownership_changed')
        if operation.stage in ('queued', 'protecting', 'protected') and (
                settings.BIRDS_HANDOFF_MODE != 'published' or not settings.BIRDS_PUBLISHED_CONTRACT):
            raise ArchiveBlocked('handoff_not_ready')
        with _roots(operation, bind=True) as roots:
            Photo.objects.filter(pk=operation.photo_id).update(state='archiving')
            if operation.stage in ('queued', 'protecting'):
                _protect(operation, roots)
        # Reopen roots at every persistent boundary; changed paths never silently rebind.
        with _roots(operation) as roots:
            if operation.stage in ('protected', 'claim_intent'):
                _verified('archive', roots['archive'], operation.temporary_relative_path,
                          operation, operation.protected_fingerprint)
                _claim(operation, roots)
        with _roots(operation) as roots:
            if operation.stage in ('claimed', 'verified', 'publish_intent'):
                _publish(operation, roots)
        with _roots(operation) as roots:
            if operation.stage == 'published':
                _register(operation, roots)
        with _roots(operation) as roots:
            if operation.stage in ('registered', 'temp_cleaned', 'source_cleanup_intent', 'source_cleaned'):
                _cleanup(operation, roots)
        return True
    except (OSError, UnsafePath, ImageRejected, OperationalError) as error:
        from archive.services.jobs import archive_failure
        archive_failure(operation, archive_error_code(error))
        return False


def recover_archive(operation):
    """Reconcile the same journal, without rebuilding identities from current paths."""
    return process_archive(operation.job)
