"""Persistent file intentions. File I/O runs only under runworker's lifetime lock.

The first archive journal is immutable provenance; current registration and formal
metadata are committed at different boundaries. All paths here come from journals.
"""
import errno
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing
from urllib.parse import quote

from django.conf import settings
from django.db import transaction, IntegrityError, OperationalError
from django.db.models import F
from django.utils import timezone

from archive.models import Photo, ManagedOriginal, ManagementOperation, MetadataReceipt, Species, PhotoSpecies
from archive.services.archiving import (_directory, _directory_existing, _sync,
    _same_content_identity, _same_inode, ArchiveBlocked, archive_error_code)
from archive.services.images import content_hash, ImageRejected
from archive.services.paths import open_root, open_regular_beneath, fingerprint, root_identity, UnsafePath

UNRESOLVED = ('pending', 'running', 'failed', 'blocked')
STAGES = ('accepted', 'prepared', 'published', 'registered', 'cleaned', 'completed')
STAGE_LABELS = dict(zip(STAGES, ('已受理', '目标已固定', '原图已发布', '当前位置已核验', '旧位置已清理', '更正已完成')))
STATUS_LABELS = {'pending': '等待更正', 'running': '更正中', 'failed': '更正失败',
    'blocked': '更正未完成', 'succeeded': '更正已完成', 'stale': '原意图已失效'}


def describe_operation(operation):
    from archive.services.archiving import MESSAGES
    operation.stage_label = STAGE_LABELS.get(operation.stage, '阶段未知')
    operation.status_label = STATUS_LABELS.get(operation.status, '状态未知')
    if operation.kind in ('trash', 'restore'):
        operation.status_label = {'pending':'等待移入回收站', 'running':'移入回收站中',
            'blocked':'移入回收站未完成', 'failed':'移入回收站失败',
            'succeeded':'已回收', 'stale':'原回收意图已失效'}.get(operation.status, '状态未知')
        if operation.stage == 'completed':
            operation.stage_label = '回收已完成'
        if operation.kind == 'restore':
            operation.status_label = {'pending':'等待恢复', 'running':'恢复中', 'blocked':'恢复未完成',
                'failed':'恢复失败', 'succeeded':'已恢复', 'stale':'原恢复意图已失效'}.get(operation.status, '状态未知')
            if operation.stage == 'completed':
                operation.stage_label = '恢复已完成'
    operation.display_error = ('照片意图已变化，不能继续旧操作。' if operation.error_code == 'generation_changed'
        else MESSAGES.get(operation.error_code, '请核对存储与原操作。')) if operation.error_code else ''
    return operation


def retry_management(photo_id, operation_id, request_id):
    """Independent retry receipt: the same intention, stage and target survive replay."""
    try:
        operation_id, request_id = uuid.UUID(str(operation_id)), uuid.UUID(str(request_id))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('重试请求标识无效。') from None
    digest = _digest(photo_id, 'management_retry', {'operation': str(operation_id)})
    with transaction.atomic():
        operation = ManagementOperation.objects.filter(pk=operation_id, photo_id=photo_id).first()
        if operation is None:
            raise ManagementConflict('未找到本照片的原操作。')
        photo = Photo.objects.get(pk=photo_id)
        if photo.management_generation != operation.generation or operation.status == 'stale':
            raise ManagementConflict('照片意图已变化，不能重试旧操作。')
        if operation.kind not in ('trash', 'restore'):
            require_editable(photo)
        previous = MetadataReceipt.objects.filter(request_id=request_id).first()
        if previous:
            if previous.photo_id != photo_id or previous.payload_digest != digest:
                raise ManagementConflict('请求标识已用于其他操作。')
            return operation
        if operation.status in ('blocked', 'failed'):
            _save(operation, status='pending', error_code='')
        MetadataReceipt.objects.create(request_id=request_id, photo_id=photo_id,
            payload_digest=digest, result={'operation_id': str(operation.pk)})
        return operation


class ManagementConflict(ValueError):
    pass


def editable_lifecycle(photo):
    """The tombstone lasts through trash, failed moves and pending restoration."""
    return not (photo.trash_requested_at or photo.trashed_at)


def require_editable(photo):
    if not editable_lifecycle(photo):
        raise ManagementConflict('照片已进入回收流程，恢复后再修改。')


TRASH_SALT = 'birds-single-trash-v1'
RESTORE_SALT = 'birds-single-restore-v1'


def restore_origin(photo):
    if not photo.trashed_at:
        raise ManagementConflict('回收完成后才可恢复。')
    operation = photo.management_operations.filter(kind='trash', status='succeeded',
        stage='completed').order_by('-created_at', '-id').first()
    if operation is None:
        raise ManagementConflict('未找到成功回收的原位置，请核对原操作。')
    relative = operation.source_snapshot.get('relative_path', '')
    from pathlib import PurePosixPath
    parts = PurePosixPath(relative).parts
    if len(parts) != 3 or any(part in ('', '.', '..') or '\\' in part or '\x00' in part for part in parts) or relative.startswith('/') or parts[0].startswith('.'):
        raise ManagementConflict('回收前位置无效。')
    return relative


def restore_confirmation(photo):
    from django.core import signing
    origin = restore_origin(photo)
    if photo.management_operations.filter(status__in=UNRESOLVED).exists():
        raise ManagementConflict('已有操作尚未完成，请先处理原操作。')
    return signing.dumps({'photo':str(photo.pk), 'version':photo.metadata_version,
        'generation':photo.management_generation, 'trashed_at':photo.trashed_at.isoformat(),
        'origin':origin, 'request_id':str(uuid.uuid4())}, salt=RESTORE_SALT)


def accept_confirmed_restore(photo_id, token):
    from django.core import signing
    try:
        frozen = signing.loads(token, salt=RESTORE_SALT, max_age=3600)
        if frozen['photo'] != str(photo_id):
            raise ValueError
        with transaction.atomic():
            previous = ManagementOperation.objects.filter(request_id=frozen['request_id']).first()
            photo = Photo.objects.get(pk=photo_id)
            if previous is None and (photo.management_generation != frozen['generation'] or
                    not photo.trashed_at or photo.trashed_at.isoformat() != frozen['trashed_at'] or
                    restore_origin(photo) != frozen['origin']):
                raise ManagementConflict('照片已变化，请重新核对恢复。')
            return accept_management(photo_id, frozen['version'], frozen['request_id'], 'restore', {})
    except (signing.BadSignature, KeyError, TypeError, ValueError) as error:
        if isinstance(error, ManagementConflict):
            raise
        raise ManagementConflict('恢复核对已失效，请重新核对。') from None


def trash_confirmation(photo):
    from django.core import signing
    return signing.dumps({'photo':str(photo.pk), 'version':photo.metadata_version,
        'generation':photo.management_generation, 'request_id':str(uuid.uuid4())}, salt=TRASH_SALT)


def accept_confirmed_trash(photo_id, token):
    from django.core import signing
    try:
        frozen = signing.loads(token, salt=TRASH_SALT, max_age=3600)
        if frozen['photo'] != str(photo_id):
            raise ValueError
        with transaction.atomic():
            previous = ManagementOperation.objects.filter(request_id=frozen['request_id']).first()
            if previous is None and Photo.objects.get(pk=photo_id).management_generation != frozen['generation']:
                raise ManagementConflict('照片意图已变化，请重新核对。')
            return accept_management(photo_id, frozen['version'], frozen['request_id'], 'trash', {})
    except (signing.BadSignature, KeyError, TypeError, ValueError) as error:
        if isinstance(error, ManagementConflict):
            raise
        raise ManagementConflict('回收核对已失效，请重新核对。') from None


def _digest(photo_id, kind, payload):
    return hashlib.sha256(json.dumps({'photo': str(photo_id), 'kind': kind, 'payload': payload},
        sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def species_payload(payload):
    """Normalize an explicit operation without creating local identities."""
    from archive.services.batch_review import _manual_set
    if not isinstance(payload, dict) or set(payload) - {'mode', 'classification', 'species_ids', 'local_names'}:
        raise ValueError('鸟种更正无效。')
    mode, classification = payload.get('mode'), payload.get('classification', 'identified')
    if mode not in ('replace', 'add', 'remove') or classification not in ('identified', 'unidentified'):
        raise ValueError('请选择替换、添加或移除及明确分类。')
    ids, names = payload.get('species_ids', []), payload.get('local_names', [])
    if not isinstance(ids, list) or not isinstance(names, list) or len(ids)+len(names) > 100:
        raise ValueError('鸟种集合无效。')
    names = [name for name in names if not isinstance(name, str) or name.strip()]
    if classification == 'unidentified':
        if mode != 'replace' or ids or names:
            raise ValueError('无法确定须单独明确选择，并清空鸟种。')
        return dict(mode=mode, classification=classification, species_ids=[], local_names=[])
    ids, names = _manual_set(ids, names)
    return dict(mode=mode, classification=classification, species_ids=ids, local_names=sorted(names))


def species_target(photo, payload, *, create=False):
    from archive.services.confirmation import local_name_key
    from archive.services.archiving import _component
    import unicodedata
    selected = {str(item.pk): item for item in Species.objects.filter(pk__in=payload['species_ids'])}
    for name in payload['local_names']:
        key = local_name_key(name)
        defaults = dict(id=uuid.uuid5(uuid.NAMESPACE_URL, 'birds-archive:local:'+key),
            source_kind='local', local_name=name, search_key=key)
        if create and payload['mode'] != 'remove':
            item, _ = Species.objects.get_or_create(local_key=key, defaults=defaults)
        else:
            item = Species.objects.filter(local_key=key).first() or Species(local_key=key, **defaults)
        selected[str(item.pk)] = item
    current = {str(link.species_id): link.species for link in photo.confirmed_species.select_related('species')}
    mode = payload['mode']
    final = selected if mode == 'replace' else ({**current, **selected} if mode == 'add' else
        {ident: item for ident, item in current.items() if ident not in selected})
    if payload['classification'] == 'identified' and not final:
        raise ValueError('不能移除最后一个鸟种；如需无法确定，请另行明确选择。')
    if payload['classification'] == 'unidentified':
        directory = '无法确定'
    elif len(final) > 1:
        directory = '多鸟种'
    else:
        item = next(iter(final.values()))
        directory = item.archive_directory
        if not directory:
            for length in (0, 8, 12, 32):
                candidate = _component(item.display_name, stable_id=item.pk.hex[:8],
                    suffix=('_'+item.pk.hex[:length]) if length else '')
                key = unicodedata.normalize('NFC', candidate).casefold()
                if key in ('多鸟种', '无法确定', '.archive-ops', '.archive-trash') or Species.objects.exclude(pk=item.pk).filter(directory_key=key).exists():
                    continue
                directory = candidate
                if create:
                    Species.objects.filter(pk=item.pk, archive_directory__isnull=True).update(
                        archive_directory=directory, directory_key=key)
                break
            else:
                raise ValueError('鸟种目录无法分配，请重新核对。')
    return {'classification': payload['classification'], 'species_ids': sorted(final),
        'directory': directory, 'labels': [final[key].display_name for key in sorted(final)]}


def accept_management(photo_id, expected_version, request_id, kind, payload):
    """Receipt lookup precedes CAS; acceptance never changes formal metadata."""
    try:
        request_id = uuid.UUID(str(request_id))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('请求标识无效。') from None
    if kind not in ('capture', 'species', 'trash', 'restore') or not isinstance(payload, dict):
        raise ValueError('更正操作无效。')
    if kind == 'species':
        payload = species_payload(payload)
    elif (kind in ('trash', 'restore') and payload) or (kind == 'capture' and set(payload) - {'capture', 'mode'}):
        raise ValueError('更正操作无效。')
    mode = payload.get('mode', 'manual')
    if kind == 'capture' and (mode not in ('manual', 'unknown', 'inherit') or 'capture' not in payload):
        raise ValueError('时间来源无效。')
    capture = Photo(manual_capture=payload.get('capture')).effective_capture()
    if kind == 'capture' and mode == 'manual' and not capture['local']:
        raise ValueError('请填写有效拍摄日期。')
    if kind == 'capture' and mode != 'manual' and payload['capture'] is not None:
        raise ValueError('此时间模式不能同时填写日期。')
    digest = _digest(photo_id, kind, payload)
    try:
        with transaction.atomic():
            photo = Photo.objects.get(pk=photo_id)
            if kind not in ('trash', 'restore'):
                require_editable(photo)
            previous = ManagementOperation.objects.filter(request_id=request_id).first()
            if previous:
                if previous.photo_id != photo_id or previous.payload_digest != digest:
                    raise ManagementConflict('请求标识已用于其他更正。')
                return previous
            if kind == 'capture' and mode != 'manual':
                capture = Photo(capture_override_mode=mode, captured_local=photo.captured_local,
                    capture_quality=photo.capture_quality, capture_precision=photo.capture_precision,
                    capture_offset=photo.capture_offset).effective_capture()
            managed = ManagedOriginal.objects.select_related('operation', 'last_management_operation').filter(photo=photo).first()
            if (not photo.current_classification or not hasattr(photo, 'archive_completion') or managed is None
                    or (not photo.trashed_at if kind == 'restore' else not editable_lifecycle(photo))):
                raise ManagementConflict('照片当前不可更正。')
            if photo.management_operations.filter(status__in=UNRESOLVED).exists():
                raise ManagementConflict('已有更正尚未完成，请先处理原操作。')
            if type(expected_version) is not int or expected_version != photo.metadata_version:
                raise ManagementConflict('照片信息已变化，请核对当前值和草稿。')
            target = (species_target(photo, payload, create=True) if kind == 'species' else
                {'restore_path':restore_origin(photo)} if kind == 'restore' else {} if kind == 'trash' else
                {'capture': capture, 'manual_capture': payload['capture'], 'capture_override_mode': mode})
            lifecycle = {'trash_requested_at':timezone.now()} if kind == 'trash' else {}
            cas = Photo.objects.filter(pk=photo_id, metadata_version=expected_version,
                management_generation=photo.management_generation)
            cas = cas.filter(trashed_at=photo.trashed_at) if kind == 'restore' else cas.filter(trash_requested_at__isnull=True)
            changed = cas.update(
                metadata_version=F('metadata_version')+1, management_generation=F('management_generation')+1,
                **lifecycle)
            if changed != 1:
                raise ManagementConflict('照片信息已变化，请重新核对。')
            binding = (managed.last_management_operation.root_identity if managed.location_revision else
                managed.operation.roots_snapshot['archive'])
            from archive.services.restore import effective_managed_location, RestoreError
            try:
                binding, managed_fingerprint = effective_managed_location(managed, binding)
            except RestoreError:
                raise ManagementConflict('恢复后的原图证据不一致，不能创建更正。') from None
            return ManagementOperation.objects.create(photo=photo, kind=kind, request_id=request_id,
                payload_digest=digest, generation=photo.management_generation+1,
                source_location_revision=managed.location_revision,
                source_snapshot={'root_key': managed.root_key, 'relative_path': managed.relative_path,
                    'actual_basename': managed.actual_basename, 'fingerprint': managed_fingerprint,
                    'sha256': managed.sha256, 'byte_size': managed.byte_size, 'root_identity': binding},
                target_snapshot=target)
    except IntegrityError:
        raise ManagementConflict('已有更正尚未完成，请刷新后核对。') from None


def _save(operation, **values):
    values['updated_at'] = timezone.now()
    ManagementOperation.objects.filter(pk=operation.pk).update(**values)
    for key, value in values.items():
        setattr(operation, key, value)


def _context(operation, **values):
    _save(operation, retry_context={**operation.retry_context, **values})


def management_fault_hook(stage):
    from archive.services.jobs import fault_hook
    fault_hook('management_'+stage)


def _effective_root(binding):
    from archive.services.restore import effective_replay_root
    return effective_replay_root('archive', binding)


def _effective_fingerprint(relative, value, *, ignore_ctime=False):
    from archive.services.restore import effective_replay_fingerprint
    return effective_replay_fingerprint('archive', relative, value, ignore_ctime=ignore_ctime)


def _guard(operation):
    photo = Photo.objects.get(pk=operation.photo_id)
    lifecycle_valid = (bool(photo.trash_requested_at) and not photo.trashed_at
        if operation.kind == 'trash' else bool(photo.trashed_at) if operation.kind == 'restore' else editable_lifecycle(photo))
    if photo.management_generation != operation.generation or not lifecycle_valid:
        raise ManagementConflict('照片意图已变化。')
    managed = ManagedOriginal.objects.get(photo_id=operation.photo_id)
    revision = operation.source_location_revision + (operation.stage in ('registered', 'cleaned', 'completed'))
    if managed.location_revision != revision:
        raise ArchiveBlocked('ownership_changed')
    if revision != operation.source_location_revision and managed.last_management_operation_id != operation.pk:
        raise ArchiveBlocked('ownership_changed')
    return managed


def _verified(root, relative, operation, expected):
    expected = _effective_fingerprint(relative, expected, ignore_ctime=True)
    with open_regular_beneath(root, relative) as stream:
        before = fingerprint(os.fstat(stream.fileno()))
        if not _same_content_identity(before, expected):
            raise ArchiveBlocked('ownership_changed')
        source = operation.source_snapshot
        if content_hash(stream) != (source['sha256'], source['byte_size']):
            raise ArchiveBlocked('source_changed')
        if fingerprint(os.fstat(stream.fileno())) != before:
            raise ArchiveBlocked('ownership_changed')
        with open_regular_beneath(root, relative) as current:
            if fingerprint(os.fstat(current.fileno())) != before:
                raise ArchiveBlocked('ownership_changed')
        return before


def _root_recheck(operation, held):
    binding = _effective_root(operation.root_identity or operation.source_snapshot['root_identity'])
    configured = str(settings.BIRDS_ARCHIVE_ROOT)
    if os.path.abspath(configured) != binding['path']:
        raise ArchiveBlocked('root_changed')
    current = open_root(configured)
    try:
        if root_identity(current) != binding['identity'] or root_identity(held) != binding['identity']:
            raise ArchiveBlocked('root_changed')
    finally:
        os.close(current)


def _target_path(operation, *, collision_counter=None):
    source = operation.source_snapshot
    counter = operation.collision_counter if collision_counter is None else collision_counter
    if operation.kind == 'restore':
        directory, year, original_basename = operation.target_snapshot['restore_path'].rsplit('/', 2)
    else:
        original_basename = source['actual_basename']
        directory = (operation.target_snapshot['directory'] if operation.kind == 'species' else
            source['relative_path'].rsplit('/', 2)[0])
        year = (Photo.objects.get(pk=operation.photo_id).current_capture if operation.kind in ('species', 'trash')
            else operation.target_snapshot['capture']).get('year') or '未知年份'
    basename = original_basename
    if counter:
        from archive.services.archiving import _component
        stem, extension = os.path.splitext(basename)
        suffix = '_'+operation.pk.hex[:8]+(f'_{counter}' if counter > 1 else '')
        basename = _component(stem, stable_id=operation.pk.hex[:8], limit=180-len(extension.encode()), suffix=suffix)+extension
    if operation.kind == 'trash':
        return f'.archive-trash/{operation.photo_id}/{basename}'
    return f'{directory}/{year}/{basename}'


def _prepare(operation, root):
    _verified(root, operation.source_snapshot['relative_path'], operation, operation.source_snapshot['fingerprint'])
    target = _target_path(operation)
    target_dir = _directory(root, target.rsplit('/', 1)[0]); os.close(target_dir)
    _save(operation, target_relative_path=target,
        root_identity=operation.source_snapshot['root_identity'], stage='prepared')


def _copy_temporary(operation, root, target_dir):
    """EXDEV fallback: exclusive file on the target filesystem, never adopt by SHA."""
    relative = operation.target_relative_path.rsplit('/', 1)[0]+'/.management-'+str(operation.pk)+'.tmp'
    name = relative.rsplit('/', 1)[1]
    identity = operation.retry_context.get('temporary_identity')
    if identity is None:
        try:
            descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600, dir_fd=target_dir)
        except FileExistsError:
            raise ArchiveBlocked('ownership_changed') from None
        try:
            _context(operation, temporary_relative_path=relative, temporary_identity=fingerprint(os.fstat(descriptor)))
            _sync(target_dir)
        finally:
            os.close(descriptor)
    with open_regular_beneath(root, relative) as temporary:
        temporary_identity = _effective_fingerprint(
            relative, operation.retry_context['temporary_identity'], ignore_ctime=True)
        if not _same_inode(fingerprint(os.fstat(temporary.fileno())), temporary_identity):
            raise ArchiveBlocked('ownership_changed')
    descriptor = os.open(name, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=target_dir)
    try:
        if not _same_inode(fingerprint(os.fstat(descriptor)), temporary_identity):
            raise ArchiveBlocked('ownership_changed')
        os.ftruncate(descriptor, 0)
        with open_regular_beneath(root, operation.source_snapshot['relative_path']) as stream:
            source_identity = _effective_fingerprint(
                operation.source_snapshot['relative_path'], operation.source_snapshot['fingerprint'],
                ignore_ctime=True)
            if not _same_content_identity(fingerprint(os.fstat(stream.fileno())), source_identity):
                raise ArchiveBlocked('source_changed')
            for chunk in iter(lambda: stream.read(1024*1024), b''):
                remaining = memoryview(chunk)
                while remaining:
                    remaining = remaining[os.write(descriptor, remaining):]
        _sync(descriptor)
        saved = fingerprint(os.fstat(descriptor))
    finally:
        os.close(descriptor)
    _verified(root, relative, operation, saved)
    _context(operation, temporary_fingerprint=saved)
    return relative, saved


def _publish(operation, root):
    source = operation.source_snapshot
    # A restore suffix allocated by an earlier attempt is an immutable target.
    # A new foreign occupant must block that retry rather than redirect it again.
    fixed_restore_target = operation.kind == 'restore' and operation.collision_counter > 0
    if operation.target_relative_path == source['relative_path']:
        verified = _verified(root, source['relative_path'], operation, source['fingerprint'])
        _save(operation, stage='published', target_fingerprint=verified)
        return
    for _ in range(100):
        _guard(operation); _root_recheck(operation, root)
        target = operation.target_relative_path
        target_dir = _directory(root, target.rsplit('/', 1)[0])
        try:
            if (operation.retry_context.get('temporary_relative_path')
                    and not operation.retry_context.get('temporary_fingerprint')):
                _copy_temporary(operation, root, target_dir)
            owned = operation.retry_context.get('publish_identity')
            if owned:
                try:
                    verified = _verified(root, target, operation, owned)
                except FileNotFoundError:
                    pass
                except ArchiveBlocked:
                    # A foreign inode is a collision, never adopt matching bytes.
                    owned = None
                else:
                    _sync(target_dir)
                    _save(operation, stage='published', target_fingerprint=verified)
                    return
            link_source = operation.retry_context.get('temporary_relative_path') or source['relative_path']
            identity = operation.retry_context.get('temporary_fingerprint') or source['fingerprint']
            _verified(root, link_source, operation, identity)
            _context(operation, publish_identity=identity)
            source_dir = _directory_existing(root, link_source.rsplit('/', 1)[0])
            try:
                try:
                    os.link(link_source.rsplit('/', 1)[1], target.rsplit('/', 1)[1],
                        src_dir_fd=source_dir, dst_dir_fd=target_dir, follow_symlinks=False)
                except OSError as error:
                    if error.errno == errno.EXDEV:
                        relative, identity = _copy_temporary(operation, root, target_dir)
                        _context(operation, publish_identity=identity)
                        os.link(relative.rsplit('/', 1)[1], target.rsplit('/', 1)[1],
                            src_dir_fd=target_dir, dst_dir_fd=target_dir, follow_symlinks=False)
                    else:
                        raise
            finally:
                os.close(source_dir)
            _sync(target_dir)
            verified = _verified(root, target, operation, operation.retry_context['publish_identity'])
            _save(operation, stage='published', target_fingerprint=verified)
            management_fault_hook('published')
            return
        except FileExistsError:
            if fixed_restore_target:
                raise ArchiveBlocked('target_conflict') from None
            counter = operation.collision_counter+1
            _save(operation, collision_counter=counter,
                target_relative_path=_target_path(operation, collision_counter=counter),
                retry_context={key: value for key, value in operation.retry_context.items() if key != 'publish_identity'})
        finally:
            os.close(target_dir)
    raise ArchiveBlocked('target_conflict')


def _registered_readback(operation):
    filename = str(settings.DATABASES['default']['NAME'])
    with closing(sqlite3.connect('file:'+quote(filename)+'?mode=ro', uri=True, timeout=5)) as db:
        row = db.execute('SELECT relative_path, location_revision, last_management_operation_id, sha256, byte_size, root_key, fingerprint '
            'FROM archive_managedoriginal WHERE photo_id=?', (operation.photo_id.hex,)).fetchone()
        generation = db.execute('SELECT management_generation FROM archive_photo WHERE id=?',
            (operation.photo_id.hex,)).fetchone()
    expected = (operation.target_relative_path, operation.source_location_revision+1,
        operation.pk.hex, operation.source_snapshot['sha256'], operation.source_snapshot['byte_size'], 'archive')
    target_fingerprint = _effective_fingerprint(operation.target_relative_path, operation.target_fingerprint)
    registered_fingerprint = (None if row is None else
        _effective_fingerprint(operation.target_relative_path, json.loads(row[6])))
    if (row is None or row[:6] != expected or registered_fingerprint != target_fingerprint
            or generation != (operation.generation,)):
        raise ArchiveBlocked('ownership_changed')


def _register(operation, root):
    verified = _verified(root, operation.target_relative_path, operation, operation.target_fingerprint)
    with transaction.atomic():
        managed = _guard(operation)
        changed = ManagedOriginal.objects.filter(pk=managed.pk,
            location_revision=operation.source_location_revision).update(
            relative_path=operation.target_relative_path,
            actual_basename=operation.target_relative_path.rsplit('/', 1)[1], fingerprint=verified,
            location_revision=F('location_revision')+1, last_management_operation=operation)
        if changed != 1:
            raise ArchiveBlocked('ownership_changed')
        _save(operation, stage='registered', target_fingerprint=verified)
    _registered_readback(operation)
    management_fault_hook('registered')


def _cleanup(operation, root):
    _guard(operation); _registered_readback(operation); _root_recheck(operation, root)
    _verified(root, operation.target_relative_path, operation, operation.target_fingerprint)
    source = operation.source_snapshot
    if source['relative_path'] != operation.target_relative_path:
        try:
            _verified(root, source['relative_path'], operation, source['fingerprint'])
        except FileNotFoundError:
            pass  # Registered phase authorizes replay after this operation's unlink.
        else:
            parent = _directory_existing(root, source['relative_path'].rsplit('/', 1)[0])
            try:
                _guard(operation); _root_recheck(operation, root)
                _verified(root, source['relative_path'], operation, source['fingerprint'])
                _verified(root, operation.target_relative_path, operation, operation.target_fingerprint)
                os.unlink(source['relative_path'].rsplit('/', 1)[1], dir_fd=parent)
                _sync(parent)
            finally:
                os.close(parent)
    temporary = operation.retry_context.get('temporary_relative_path')
    if temporary:
        try:
            _verified(root, temporary, operation, operation.retry_context['temporary_fingerprint'])
        except FileNotFoundError:
            pass
        else:
            parent = _directory_existing(root, temporary.rsplit('/', 1)[0])
            try:
                os.unlink(temporary.rsplit('/', 1)[1], dir_fd=parent); _sync(parent)
            finally:
                os.close(parent)
    verified = _verified(root, operation.target_relative_path, operation, operation.target_fingerprint)
    with transaction.atomic():
        _guard(operation)
        ManagedOriginal.objects.filter(photo_id=operation.photo_id,
            last_management_operation=operation).update(fingerprint=verified)
        _save(operation, stage='cleaned', target_fingerprint=verified)
    management_fault_hook('cleaned')


def process_management(operation_id):
    operation = ManagementOperation.objects.get(pk=operation_id)
    if operation.status in ('succeeded', 'stale'):
        return operation.status == 'succeeded'
    root = None
    try:
        _guard(operation)
        root = open_root(settings.BIRDS_ARCHIVE_ROOT)
        _root_recheck(operation, root)
        _save(operation, status='running', error_code='')
        if operation.stage == 'accepted':
            _prepare(operation, root)
        if operation.stage == 'prepared':
            _publish(operation, root)
        if operation.stage == 'published':
            _register(operation, root)
        if operation.stage == 'registered':
            _cleanup(operation, root)
        if operation.stage == 'cleaned':
            _verified(root, operation.target_relative_path, operation, operation.target_fingerprint)
            _registered_readback(operation); _root_recheck(operation, root)
            with transaction.atomic():
                _guard(operation)
                fields = operation.target_snapshot
                photo_query = Photo.objects.filter(pk=operation.photo_id, management_generation=operation.generation)
                if operation.kind == 'trash':
                    photo_query.filter(trash_requested_at__isnull=False).update(
                        trashed_at=timezone.now(), original_available=True)
                elif operation.kind == 'restore':
                    photo_query.update(trash_requested_at=None, trashed_at=None, original_available=True)
                elif operation.kind == 'species':
                    photo_query.update(current_classification=fields['classification'], original_available=True)
                    PhotoSpecies.objects.filter(photo_id=operation.photo_id).delete()
                    PhotoSpecies.objects.bulk_create([PhotoSpecies(photo_id=operation.photo_id, species_id=ident)
                        for ident in fields['species_ids']])
                else:
                    photo_query.update(manual_capture=fields['manual_capture'], current_capture=fields['capture'],
                        capture_override_mode=fields['capture_override_mode'], original_available=True)
                _save(operation, stage='completed', status='succeeded', error_code='',
                    result={'relative_path': operation.target_relative_path, **fields})
        return True
    except ManagementConflict:
        # A guard can fail inside an atomic registration/complete block: persist
        # the terminal diagnosis only after that transaction has rolled back.
        _save(operation, status='stale', error_code='generation_changed')
        return False
    except (OSError, UnsafePath, ImageRejected, OperationalError, sqlite3.DatabaseError) as error:
        _save(operation, status='blocked', error_code=str(error) if isinstance(error, ArchiveBlocked)
            else archive_error_code(error))
        return False
    finally:
        if root is not None:
            os.close(root)
