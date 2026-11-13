"""Restore a complete backup into isolated roots without rewriting historical journals."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import uuid
from pathlib import Path, PurePosixPath

from django.db import transaction
from django.utils import timezone

from archive.models import (ArchiveOperation, ImportSource, Job, ManagedOriginal, ManagementOperation,
                            ModelArtifact, RecognitionRun, RestoreBinding, RestoreSession, SourceObservation)
from archive.services import backup, model_packages
from archive.services.deployment import filesystem_identity

MARKER = '.birds-restore-v1.json'
JOURNAL = '.birds-restore-files-v1.json'
OWNER = '.birds-tracer-root.json'
SKIPPED_DATA = {'inbox-root.json', 'deployment-v1.json'}
ROLES = ('inbox', 'data', 'archive', 'claim')


class RestoreError(RuntimeError):
    pass


def _fail(code):
    raise RestoreError(code)


def _digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def _fingerprint(path):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        _fail('restore_target_special_file')
    return {'dev': info.st_dev, 'ino': info.st_ino, 'size': info.st_size,
            'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns}


def _root_fingerprint(path):
    info = os.stat(path, follow_symlinks=False)
    return {'dev': info.st_dev, 'ino': info.st_ino}


def _owned_fingerprint(path):
    value = _fingerprint(path)
    return {key: value[key] for key in ('dev', 'ino', 'size', 'mtime_ns')}


def _relative(value):
    if not isinstance(value, str) or not value or '\\' in value:
        _fail('restore_manifest_path_invalid')
    result = PurePosixPath(value)
    if (result.is_absolute() or not result.parts or result.as_posix() != value
            or '..' in result.parts):
        _fail('restore_manifest_path_invalid')
    return Path(*result.parts)


def _roots(values):
    if set(values) != set(ROLES):
        _fail('restore_target_roots_invalid')
    result = {}
    for role in ROLES:
        raw = Path(values[role])
        if not raw.is_absolute() or raw.is_symlink():
            _fail('restore_target_root_invalid')
        path = raw.resolve(strict=True)
        if path != raw or not path.is_dir():
            _fail('restore_target_root_invalid')
        result[role] = path
    identities = [_root_fingerprint(path) for path in result.values()]
    if len({(row['dev'], row['ino']) for row in identities}) != len(identities):
        _fail('restore_target_roots_overlap')
    paths = list(result.values())
    for index, left in enumerate(paths):
        for right in paths[index + 1:]:
            if left in right.parents or right in left.parents:
                _fail('restore_target_roots_overlap')
    return result


def _read_marker(path):
    try:
        return json.loads((path / MARKER).read_text())
    except (OSError, ValueError, TypeError):
        _fail('restore_marker_invalid')


def _write_marker(path, value):
    fd = os.open(path / MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def _journal(path):
    try:
        value = json.loads((path / JOURNAL).read_text())
        return value if isinstance(value, dict) else _fail('restore_journal_invalid')
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError):
        _fail('restore_journal_invalid')


def _write_journal(path, value):
    temporary = path / (JOURNAL + '.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path / JOURNAL)
    finally:
        try: temporary.unlink()
        except FileNotFoundError: pass


def _ensure_parent(root, relative):
    current = root
    for part in relative.parent.parts:
        current = current / part
        try:
            info = os.lstat(current)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                _fail('restore_target_ancestor_invalid')
        except FileNotFoundError:
            current.mkdir(mode=0o700)


def _registered_database(path, session_id):
    try:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as database:
            return database.execute('select 1 from archive_restoresession where id=?',
                                    (session_id.replace('-', ''),)).fetchone() is not None
    except sqlite3.Error:
        return False


def _migrated_unregistered_database(path, original):
    """Accept only the known 0012 schema-only migration interruption window."""
    try:
        current = backup._database_baseline(path)
        expected = {row['table']: row for row in original['tables'] if row['table'] != 'django_migrations'}
        actual = {row['table']: row for row in current['tables'] if row['table'] != 'django_migrations'}
        extras = set(actual) - set(expected)
        if extras != {'archive_restoresession', 'archive_restorebinding'}:
            return False
        if any(actual[name] != row for name, row in expected.items()):
            return False
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as database:
            migration = database.execute(
                "select count(*) from django_migrations where app='archive' and name='0012_restore_bindings'").fetchone()
            return migration == (1,) and database.execute(
                'select count(*) from archive_restoresession').fetchone() == (0,)
    except (backup.BackupError, sqlite3.Error, KeyError, TypeError):
        return False


def _skip(role, relative):
    return relative == OWNER or (role == 'data' and relative in SKIPPED_DATA)


def _payload_rows(manifest):
    yield from manifest['files']
    for model in manifest.get('models', []):
        digest = model['manifest_digest']
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            _fail('restore_model_identity_invalid')
        if model['relative_root'] != f'dependencies/models/{digest}':
            _fail('restore_model_identity_invalid')
        for item in model['files']:
            name = _relative(item['path'])
            if len(name.parts) != 1:
                _fail('restore_model_identity_invalid')
            yield {'root_role': 'data', 'relative_path': f'.restored-models/{digest}/{name}',
                   'backup_path': f"{model['relative_root']}/{name}", 'size': item['size_bytes'],
                   'sha256': item['sha256'], 'source_fingerprint': {},
                   'hardlink_group': 'model:' + model_packages.digest([digest, name.as_posix()])}


def _restore_directories(manifest, roots, journals):
    seen = set()
    for row in sorted(manifest.get('directories', []), key=lambda item: (
            item['root_role'], item['relative_path'])):
        role, name = row['root_role'], row['relative_path']
        identity = row['identity']
        if (role not in roots or (role, name) in seen or not isinstance(identity, dict)
                or set(identity) != {'dev', 'ino'} or any(type(value) is not int for value in identity.values())):
            _fail('restore_directory_manifest_invalid')
        seen.add((role, name))
        relative = _relative(name)
        _ensure_parent(roots[role], relative)
        target = roots[role] / relative
        entry = journals[role].get(name)
        if target.exists() or target.is_symlink():
            if (target.is_symlink() or not target.is_dir()
                    or entry != {'state': 'directory', 'fingerprint': _root_fingerprint(target)}):
                _fail('restore_directory_collision')
        else:
            if entry is not None:
                _fail('restore_directory_missing')
            target.mkdir(mode=0o700)
            journals[role][name] = {'state': 'directory', 'fingerprint': _root_fingerprint(target)}
            _write_journal(roots[role], journals[role])


def prepare_restore(backup_set, target_roots, session_id=None):
    """Verify then copy once; same-session markers permit an exact interrupted retry."""
    manifest = backup._verify_set(backup_set)
    source = Path(backup_set)
    roots = _roots(target_roots)
    for target in roots.values():
        if target == source or target in source.parents or source in target.parents:
            _fail('restore_target_overlaps_backup')
    backup_identity = filesystem_identity(source)
    target_identities = {role: filesystem_identity(path) for role, path in roots.items()}
    for identity in target_identities.values():
        if backup._identity_overlap(identity, backup_identity):
            _fail('restore_target_overlaps_backup')
        for source_root in manifest.get('source_roots', {}).values():
            old_identity = source_root.get('filesystem', {})
            if old_identity and backup._identity_overlap(identity, old_identity):
                _fail('restore_target_overlaps_source')
    session_id = str(session_id or uuid.uuid4())
    manifest_digest = _digest(source / backup.MANIFEST)
    marker_base = {'schema': 1, 'session_id': session_id, 'backup_set_id': manifest['set_id'],
                   'backup_manifest_sha256': manifest_digest}
    retry = {}
    for role, root in roots.items():
        names = set(os.listdir(root))
        if MARKER in names:
            if _read_marker(root) != {**marker_base, 'role': role}:
                _fail('restore_target_owned_by_other_session')
            retry[role] = True
        else:
            if names - {OWNER}:
                _fail('restore_target_not_empty')
            retry[role] = False
    for role, root in roots.items():
        if not retry[role]:
            _write_marker(root, {**marker_base, 'role': role})

    copied_groups = {}
    journals = {role: _journal(root) for role, root in roots.items()}
    _restore_directories(manifest, roots, journals)
    for row in _payload_rows(manifest):
        role, relative_name = row['root_role'], row['relative_path']
        if _skip(role, relative_name):
            continue
        relative = _relative(relative_name)
        target = roots[role] / relative
        _ensure_parent(roots[role], relative)
        prior = copied_groups.get(row['hardlink_group'])
        entry = journals[role].get(relative_name)
        if target.exists():
            if (not retry[role] or not isinstance(entry, dict)
                    or entry.get('fingerprint') != _owned_fingerprint(target)
                    or target.stat().st_size != row['size'] or _digest(target) != row['sha256']):
                _fail('restore_target_collision')
            if entry.get('state') == 'prepared':
                entry['state'] = 'published'; _write_journal(roots[role], journals[role])
        elif prior is not None:
            expected = _owned_fingerprint(prior)
            journals[role][relative_name] = {'state': 'prepared', 'fingerprint': expected}
            _write_journal(roots[role], journals[role])
            try:
                os.link(prior, target, follow_symlinks=False)
            except OSError as error:
                if error.errno == errno.EXDEV:
                    _fail('restore_hardlink_cross_device')
                raise
            journals[role][relative_name]['state'] = 'published'
            _write_journal(roots[role], journals[role])
        else:
            source_file = (source / _relative(row['backup_path']) if 'backup_path' in row
                           else source / 'roots' / role / relative)
            temporary = target.parent / ('.restore-' + session_id + '-' + hashlib.sha256(
                relative_name.encode()).hexdigest() + '.partial')
            if temporary.exists():
                if (not isinstance(entry, dict) or entry.get('state') != 'prepared'
                        or entry.get('fingerprint') != _owned_fingerprint(temporary)
                        or _digest(temporary) != row['sha256']):
                    _fail('restore_temporary_mismatch')
            else:
                with source_file.open('rb') as input_stream, open(temporary, 'xb') as output_stream:
                    shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
                if temporary.stat().st_size != row['size'] or _digest(temporary) != row['sha256']:
                    _fail('restore_copy_mismatch')
                journals[role][relative_name] = {'state': 'prepared', 'fingerprint': _owned_fingerprint(temporary)}
                _write_journal(roots[role], journals[role])
            os.link(temporary, target, follow_symlinks=False)
            temporary.unlink()
            journals[role][relative_name]['state'] = 'published'
            _write_journal(roots[role], journals[role])
        copied_groups.setdefault(row['hardlink_group'], target)

    database_target = roots['data'] / 'archive.sqlite3'
    database_source = source / manifest['database']['path']
    if database_target.exists():
        exact = (database_target.stat().st_size == manifest['database']['size']
                 and _digest(database_target) == manifest['database']['sha256'])
        owned_exact = (exact and isinstance(journals['data'].get('@database'), dict)
                       and journals['data']['@database'].get('fingerprint') == _owned_fingerprint(database_target))
        if not owned_exact and not _registered_database(database_target, session_id) \
                and not _migrated_unregistered_database(database_target, manifest['database']):
            _fail('restore_database_collision')
    else:
        temporary = roots['data'] / ('.restore-' + session_id + '-database.partial')
        entry = journals['data'].get('@database')
        if temporary.exists():
            if (not isinstance(entry, dict) or entry.get('state') != 'prepared'
                    or entry.get('fingerprint') != _owned_fingerprint(temporary)
                    or _digest(temporary) != manifest['database']['sha256']):
                _fail('restore_database_temporary_mismatch')
        else:
            with database_source.open('rb') as input_stream, open(temporary, 'xb') as output_stream:
                shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
                output_stream.flush(); os.fsync(output_stream.fileno())
            journals['data']['@database'] = {'state': 'prepared', 'fingerprint': _owned_fingerprint(temporary)}
            _write_journal(roots['data'], journals['data'])
        os.link(temporary, database_target, follow_symlinks=False)
        temporary.unlink()
        journals['data']['@database']['state'] = 'published'
        _write_journal(roots['data'], journals['data'])
    old_inbox_identity = {}
    try:
        record = json.loads((source / 'roots' / 'data' / 'inbox-root.json').read_text())
        old_inbox_identity = record.get('identity', record)
        if not isinstance(old_inbox_identity, dict): old_inbox_identity = {}
    except (OSError, ValueError, TypeError):
        pass
    return {'schema': 1, 'session_id': session_id, 'backup_set': str(source),
            'backup_set_id': manifest['set_id'], 'backup_manifest_sha256': manifest_digest,
            'target_roots': {role: str(path) for role, path in roots.items()},
            'manifest': manifest, 'source_inbox_identity': old_inbox_identity}


def _binding_values(session, row, roots, *, kind, object_id, source_fingerprint=None):
    target = roots[row['root_role']] / _relative(row['relative_path'])
    return dict(session=session, kind=kind, object_id=str(object_id), role=row['root_role'],
                source_relative_path=row['relative_path'], target_relative_path=row['relative_path'],
                source_fingerprint=source_fingerprint if source_fingerprint is not None else row['source_fingerprint'],
                target_fingerprint=_fingerprint(target), sha256=row['sha256'], byte_size=row['size'],
                hardlink_group=row['hardlink_group'], verification_reason='backup_manifest_verified')


def _record_binding(values):
    key = {name: values[name] for name in ('session', 'kind', 'object_id', 'role')}
    defaults = {name: value for name, value in values.items() if name not in key}
    binding, created = RestoreBinding.objects.get_or_create(**key, defaults=defaults)
    if not created and any(getattr(binding, name) != value for name, value in defaults.items()):
        _fail('restore_binding_replay_mismatch')
    return binding


@transaction.atomic
def register_prepared_restore(prepared):
    """Append bindings after the copied database has been migrated to schema 0012."""
    roots = _roots(prepared['target_roots'])
    manifest = prepared.get('manifest')
    if not isinstance(manifest, dict) or manifest.get('set_id') != prepared.get('backup_set_id'):
        _fail('restore_manifest_changed')
    session, created = RestoreSession.objects.get_or_create(pk=prepared['session_id'], defaults={
        'backup_set_id': manifest['set_id'], 'backup_manifest_sha256': prepared['backup_manifest_sha256'],
        'source_deployment_id': manifest.get('deployment', {}).get('deployment_id') or '',
        'source_roots': manifest.get('source_roots', {}), 'target_roots': prepared['target_roots'],
        'manifest': manifest})
    if not created and (str(session.backup_set_id) != manifest['set_id']
                        or session.backup_manifest_sha256 != prepared['backup_manifest_sha256']
                        or session.target_roots != prepared['target_roots']):
        _fail('restore_session_mismatch')
    rows = {(row['root_role'], row['relative_path']): row for row in _payload_rows(manifest)
            if not _skip(row['root_role'], row['relative_path'])}
    for row in rows.values():
        _record_binding(_binding_values(session, row, roots, kind='path',
                                        object_id=f"{row['root_role']}:{row['relative_path']}"))

    old_inbox_identity = prepared.get('source_inbox_identity', {})
    for role in ROLES:
        source_identity = manifest.get('source_roots', {}).get(role, {}).get('identity', {})
        if role == 'inbox' and not source_identity:
            source_identity = old_inbox_identity
        _record_binding(dict(session=session, kind='root', object_id=role, role=role,
            source_relative_path='.', target_relative_path='.', source_fingerprint=source_identity,
            target_fingerprint=_root_fingerprint(roots[role]), sha256='', byte_size=0,
            hardlink_group=f'root-{role}', verification_reason='isolated_root_verified'))

    for row in manifest.get('directories', []):
        role, relative = row['root_role'], row['relative_path']
        target = roots[role] / _relative(relative)
        _record_binding(dict(session=session, kind='directory', object_id=f'{role}:{relative}', role=role,
            source_relative_path=relative, target_relative_path=relative, source_fingerprint=row['identity'],
            target_fingerprint=_root_fingerprint(target), sha256='', byte_size=0, hardlink_group='directory',
            verification_reason='backup_directory_verified'))

    for source_row in ImportSource.objects.select_related('current_observation').all():
        row = rows.get((source_row.root_key, source_row.relative_path))
        if row is None:
            continue
        original_source = source_row.fingerprint
        existing = RestoreBinding.objects.filter(session=session, kind='import_source',
                                                  object_id=str(source_row.pk), role=source_row.root_key).first()
        if existing is not None:
            original_source = existing.source_fingerprint
        if not _fingerprint_matches(original_source, row['source_fingerprint']):
            continue
        _record_binding(_binding_values(session, row, roots, kind='import_source', object_id=source_row.pk,
                                        source_fingerprint=original_source))
        observation = source_row.current_observation
        if observation is not None:
            values = _binding_values(session, row, roots, kind='source_observation',
                                     object_id=observation.pk, source_fingerprint=observation.fingerprint)
            existing = RestoreBinding.objects.filter(session=session, kind='source_observation',
                                                      object_id=str(observation.pk), role=source_row.root_key).first()
            if existing is not None:
                values['source_fingerprint'] = existing.source_fingerprint
            if not _fingerprint_matches(values['source_fingerprint'], row['source_fingerprint']):
                continue
            _record_binding(values)
            SourceObservation.objects.filter(pk=observation.pk).update(fingerprint=values['target_fingerprint'])
            ImportSource.objects.filter(pk=source_row.pk).update(fingerprint=values['target_fingerprint'])
    for managed in ManagedOriginal.objects.all():
        row = rows.get((managed.root_key, managed.relative_path))
        if row is not None:
            _record_binding(_binding_values(session, row, roots, kind='managed_original',
                                            object_id=managed.photo_id, source_fingerprint=managed.fingerprint))
    return session


def _verify_marker(session, role, root):
    expected = {'schema': 1, 'session_id': str(session.pk), 'backup_set_id': str(session.backup_set_id),
                'backup_manifest_sha256': session.backup_manifest_sha256, 'role': role}
    if _read_marker(root) != expected:
        _fail('restore_marker_mismatch')


def _verify_inflight(session, roots):
    paths = {(row.kind, row.role, row.source_relative_path): row for row in
             session.bindings.filter(kind__in=('path', 'directory'))}

    def matches(role, relative, old, *, digest=None, size=None, kind='path'):
        row = paths.get((kind, role, relative))
        return (row is not None and _fingerprint_matches(old, row.source_fingerprint, ignore_ctime=True)
                and (not digest or row.sha256 == digest) and (size is None or row.byte_size == size))

    def require(role, relative, old, code, **evidence):
        if not matches(role, relative, old, **evidence):
            _fail(code)

    def require_root(role, old, code):
        expected = _replay_root(session, role, old)
        if expected == old and (not isinstance(expected, dict)
                or expected.get('identity') != _root_fingerprint(roots[role])):
            _fail(code)

    terminal = ('succeeded', 'failed', 'stale')
    for job in Job.objects.exclude(state__in=terminal).select_related('source__current_observation', 'archive_operation'):
        if job.kind == 'archive':
            if job.state not in ('awaiting_executor', 'pending', 'queued', 'running', 'retry_wait', 'blocked') \
                    or not hasattr(job, 'archive_operation'):
                _fail('restore_archive_job_unsupported')
            continue
        if job.kind != 'import' or job.state not in ('pending', 'queued', 'running', 'retry_wait'):
            _fail('restore_unsupported_inflight')
        source, observation = job.source, job.source.current_observation
        if observation is None or observation.version != job.source_version:
            _fail('restore_import_version_missing')
        original = session.bindings.filter(kind='source_observation', object_id=str(observation.pk),
                                           role='inbox').first()
        if source.root_key != 'inbox' or original is None or observation.fingerprint != original.target_fingerprint:
            _fail('restore_import_source_missing')
        require('inbox', source.relative_path, original.source_fingerprint, 'restore_import_source_missing',
                digest=job.expected_sha256 or observation.sha256, size=observation.byte_size)

    archive_before_claim = ('queued', 'protecting', 'protected')
    archive_claimed = ('claimed', 'verified', 'publish_intent', 'published', 'registered', 'temp_cleaned')
    archive_published = ('published', 'registered', 'temp_cleaned', 'source_cleanup_intent', 'source_cleaned')
    for operation in ArchiveOperation.objects.exclude(stage='complete').select_related('source_observation__source'):
        stage, journal = operation.stage, operation.retry_context
        if operation.schema_version != 1 or stage not in {
                *archive_before_claim, 'claim_intent', *archive_claimed, *archive_published}:
            _fail('restore_archive_stage_unsupported')
        snapshot = operation.roots_snapshot
        if snapshot.get('binding_state') == 'bound':
            for role in ROLES:
                require_root(role, snapshot.get(role), 'restore_archive_root_missing')
        elif stage != 'queued' or snapshot.get('binding_state') != 'unbound':
            _fail('restore_archive_root_missing')
        for kind, role, name in (('claim', 'claim', str(operation.pk)),
                                 ('temporary', 'archive', '.archive-ops/' + str(operation.pk))):
            identity = journal.get(kind + '_directory_identity')
            if identity:
                require(role, name, identity, 'restore_archive_directory_missing', kind='directory')
            elif (kind == 'claim' and stage in (*archive_claimed, *archive_published, 'claim_intent')
                    or kind == 'temporary' and stage not in ('queued', 'protecting')):
                _fail('restore_archive_directory_missing')
        evidence = {'digest': operation.expected_sha256, 'size': operation.byte_size}
        inbox = operation.source_observation.source.relative_path
        if stage in archive_before_claim:
            require('inbox', inbox, operation.source_fingerprint, 'restore_archive_source_missing', **evidence)
        elif stage == 'claim_intent':
            if not (matches('inbox', inbox, operation.source_fingerprint, **evidence)
                    or matches('claim', operation.claim_relative_path, operation.source_fingerprint, **evidence)):
                _fail('restore_archive_claim_missing')
        elif stage in archive_claimed or ('path', 'claim', operation.claim_relative_path) in paths:
            require('claim', operation.claim_relative_path, operation.claim_fingerprint,
                    'restore_archive_claim_missing', **evidence)
        if stage in ('protected', 'claim_intent', 'claimed', 'verified', 'publish_intent', 'published'):
            require('archive', operation.temporary_relative_path, operation.protected_fingerprint,
                    'restore_archive_temporary_missing', **evidence)
        elif ('path', 'archive', operation.temporary_relative_path) in paths:
            temporary = operation.protected_fingerprint or journal.get('temporary_identity')
            require('archive', operation.temporary_relative_path, temporary, 'restore_archive_temporary_missing',
                    **(evidence if operation.protected_fingerprint else {}))
        if stage in archive_published:
            require('archive', operation.target_relative_path, operation.published_fingerprint,
                    'restore_archive_target_missing', **evidence)

    for operation in ManagementOperation.objects.exclude(status__in=('succeeded', 'stale')):
        stage, source, journal = operation.stage, operation.source_snapshot, operation.retry_context
        if (operation.status not in ('pending', 'running', 'failed', 'blocked') or operation.kind not in (
                'capture', 'species', 'trash', 'restore') or stage not in (
                'accepted', 'prepared', 'published', 'registered', 'cleaned')):
            _fail('restore_management_stage_unsupported')
        require_root('archive', operation.root_identity or source.get('root_identity'),
                     'restore_management_root_missing')
        evidence = {'digest': source.get('sha256'), 'size': source.get('byte_size')}
        relative = source.get('relative_path')
        if stage in ('accepted', 'prepared', 'published') or ('path', 'archive', relative) in paths:
            require('archive', relative, source.get('fingerprint'), 'restore_management_source_missing', **evidence)
        if stage in ('published', 'registered', 'cleaned'):
            require('archive', operation.target_relative_path, operation.target_fingerprint,
                    'restore_management_target_missing', **evidence)
        temporary = journal.get('temporary_relative_path')
        if temporary and (stage in ('prepared', 'published') or ('path', 'archive', temporary) in paths):
            complete = journal.get('temporary_fingerprint')
            require('archive', temporary, complete or journal.get('temporary_identity'),
                    'restore_management_temporary_missing', **(evidence if complete else {}))

    for run in RecognitionRun.objects.exclude(state__in=('succeeded', 'failed', 'unavailable', 'stale')).select_related(
            'source_observation__source'):
        if run.state not in ('queued', 'running', 'retry_wait'):
            _fail('restore_recognition_stage_unsupported')
        observation, source = run.source_observation, run.source_observation.source
        if source.current_observation_id != observation.pk or observation.version != run.source_version:
            _fail('restore_recognition_version_missing')
        evidence = {'digest': run.photo_sha256, 'size': run.byte_size}
        if matches('inbox', source.relative_path, run.source_fingerprint, **evidence):
            continue
        operations = ArchiveOperation.objects.filter(source_observation=observation)
        if operations.filter(stage__in=(*archive_claimed, *archive_published, 'complete')).exists():
            continue  # Normal recognition CAS rejects a consumed source.
        if any(matches('claim', op.claim_relative_path, op.source_fingerprint, **evidence)
               for op in operations.filter(stage='claim_intent')):
            continue
        _fail('restore_recognition_source_missing')

    for binding in session.bindings.filter(kind='path', role='data', source_relative_path__startswith='uploads/'):
        if not binding.source_relative_path.endswith('.json'):
            continue
        try:
            intent = json.loads((roots['data'] / _relative(binding.target_relative_path)).read_text())
        except (OSError, ValueError, TypeError):
            _fail('restore_upload_intent_invalid')
        if intent.get('version') != 1 or intent.get('state') not in ('complete', 'failed'):
            _fail('restore_unsupported_inflight')


def _verify_target_tree(session, roots):
    controls = {MARKER, JOURNAL, OWNER}
    for role, root in roots.items():
        expected = set(session.bindings.filter(kind='path', role=role).values_list(
            'target_relative_path', flat=True))
        allowed = expected | controls
        if role == 'data':
            allowed |= {'archive.sqlite3', 'archive.sqlite3-wal', 'archive.sqlite3-shm',
                        'inbox-root.json', 'deployment-v1.json', 'restore-lifecycle.lock', 'worker.lock'}
        observed = set()
        for directory, directories, files in os.walk(root, followlinks=False):
            base = Path(directory)
            if any((base / name).is_symlink() for name in directories):
                _fail('restore_target_extra_or_special')
            for name in files:
                path = base / name
                if path.is_symlink() or not path.is_file():
                    _fail('restore_target_extra_or_special')
                observed.add(path.relative_to(root).as_posix())
        if observed - allowed:
            _fail('restore_target_extra_or_special')


def _relocated_model_manifest(session, model, artifact):
    original = artifact.manifest
    digest = artifact.manifest_digest
    fields = ('role', 'path', 'sha256', 'size_bytes')
    if (model_packages.digest(original) != digest or model['manifest_digest'] != digest
            or any(model[key] != getattr(artifact, key) for key in ('provider', 'version', 'revision'))):
        _fail('restore_model_identity_mismatch')
    expected = sorted(tuple(item[key] for key in fields) for item in original['files'])
    backed = sorted(tuple(item[key] for key in fields) for item in model['files'])
    if not expected or expected != backed or len({item[1] for item in expected}) != len(expected):
        _fail('restore_model_inventory_mismatch')
    relative_root = _relative(f'.restored-models/{digest}')
    root = Path(session.target_roots['data']) / relative_root
    bindings = {row.source_relative_path: row for row in session.bindings.filter(
        kind='path', role='data', source_relative_path__startswith=relative_root.as_posix() + '/')}
    if root.resolve() != root:
        _fail('restore_model_file_missing')
    for _, name, sha256, size in expected:
        filename = _relative(name)
        relative = (relative_root / filename).as_posix()
        binding = bindings.get(relative)
        path = root / filename
        if (len(filename.parts) != 1 or binding is None or binding.target_relative_path != relative
                or binding.sha256 != sha256 or binding.byte_size != size):
            _fail('restore_model_file_missing')
        try:
            if _fingerprint(path) != binding.target_fingerprint:
                _fail('restore_model_file_missing')
        except OSError:
            _fail('restore_model_file_missing')
    return {**original, 'local_root': str(root)}


def _verify_model_dependencies(session):
    active_runs = RecognitionRun.objects.filter(state__in=('queued', 'running', 'retry_wait'))
    required = set(active_runs.values_list('artifact_id', flat=True))
    required.update(ModelArtifact.objects.filter(is_current=True).values_list('pk', flat=True))
    models = session.manifest.get('models', [])
    by_digest = ModelArtifact.objects.in_bulk([row['manifest_digest'] for row in models],
                                            field_name='manifest_digest')
    for row in models:
        artifact = by_digest.get(row['manifest_digest'])
        if artifact is None:
            _fail('restore_model_identity_mismatch')
        _relocated_model_manifest(session, row, artifact)
        required.discard(artifact.pk)
    if required:
        _fail('restore_recognition_model_missing')


@transaction.atomic
def verify_restore(session_id, *, for_activation=False):
    """Re-check target evidence only; source backup is not required after registration."""
    session = RestoreSession.objects.select_for_update().get(pk=session_id)
    if session.status == 'failed':
        _fail('restore_session_failed')
    if for_activation and session.status not in ('verified', 'activated'):
        _fail('restore_session_not_verified')
    roots = _roots(session.target_roots)
    for role, root in roots.items():
        _verify_marker(session, role, root)
        try:
            root_binding = session.bindings.get(kind='root', object_id=role, role=role)
        except RestoreBinding.DoesNotExist:
            _fail('restore_root_binding_missing')
        if root_binding.target_fingerprint != _root_fingerprint(root):
            _fail('restore_root_binding_mismatch')
    _verify_target_tree(session, roots)
    for binding in session.bindings.filter(kind='directory'):
        path = roots[binding.role] / _relative(binding.target_relative_path)
        if path.is_symlink() or not path.is_dir() or _root_fingerprint(path) != binding.target_fingerprint:
            _fail('restore_directory_binding_mismatch')
    groups = {}
    for binding in session.bindings.filter(kind='path').order_by('role', 'source_relative_path'):
        path = roots[binding.role] / _relative(binding.target_relative_path)
        if not path.is_file():
            _fail('restore_binding_missing')
        if (_fingerprint(path) != binding.target_fingerprint
                or (not for_activation and _digest(path) != binding.sha256)):
            _fail('restore_binding_mismatch')
        groups.setdefault(binding.hardlink_group, []).append(path)
    for paths in groups.values():
        identities = {(path.stat().st_dev, path.stat().st_ino) for path in paths}
        if len(identities) != 1 or paths[0].stat().st_nlink != len(paths):
            _fail('restore_hardlink_mismatch')
    _verify_inflight(session, roots)
    _verify_model_dependencies(session)
    if session.status == 'prepared':
        session.status, session.verified_at = 'verified', timezone.now()
        session.save(update_fields=['status', 'verified_at'])
    return session


@transaction.atomic
def activate_restore(session_id):
    session = RestoreSession.objects.select_for_update().get(pk=session_id)
    if session.status == 'activated':
        return session
    if session.status != 'verified':
        _fail('restore_session_not_verified')
    if RestoreSession.objects.filter(status='activated').exclude(pk=session.pk).exists():
        _fail('restore_other_session_active')
    session.status, session.activated_at = 'activated', timezone.now()
    session.save(update_fields=['status', 'activated_at'])
    return session


def _active_session():
    return RestoreSession.objects.filter(status='activated').first()


def _fingerprint_matches(saved, backed, *, ignore_ctime=False):
    if not isinstance(saved, dict) or not isinstance(backed, dict):
        return False
    keys = set(saved)
    inode = {'dev', 'ino'}
    content = inode | {'size', 'mtime_ns'}
    if keys not in (inode, content, content | {'ctime_ns'}):
        return False
    compared = keys - {'ctime_ns'} if ignore_ctime else keys
    return all(type(saved[key]) is int and saved[key] == backed.get(key) for key in compared)


def effective_replay_fingerprint(role, relative_path, original, *, ignore_ctime=False):
    """Translate only an exact backed identity; fresh journals retain strict native checks."""
    if not original or not relative_path:
        return original
    session = _active_session()
    if session is None:
        return original
    return _replay_fingerprint(session, role, relative_path, original, ignore_ctime=ignore_ctime)


def _replay_fingerprint(session, role, relative_path, original, *, ignore_ctime=False):
    if relative_path == '.':
        binding = session.bindings.filter(kind='root', object_id=role, role=role).first()
    else:
        binding = session.bindings.filter(kind__in=('path', 'directory'),
            object_id=f'{role}:{relative_path}', role=role, source_relative_path=relative_path).first()
    if binding is None or not _fingerprint_matches(original, binding.source_fingerprint,
                                                   ignore_ctime=ignore_ctime):
        return original
    return binding.target_fingerprint


def _replay_root(session, role, original_binding):
    if not isinstance(original_binding, dict):
        return original_binding
    binding = session.bindings.filter(kind='root', object_id=role, role=role).first()
    source_path = session.source_roots.get(role, {}).get('path')
    if (binding is None or not source_path or original_binding.get('path') != source_path
            or not _fingerprint_matches(original_binding.get('identity'), binding.source_fingerprint)):
        return original_binding
    return {'path': session.target_roots[role], 'identity': binding.target_fingerprint}


def effective_replay_root(role, original_binding):
    session = _active_session()
    return original_binding if session is None else _replay_root(session, role, original_binding)


def effective_managed_location(managed, original_root):
    session = _active_session()
    if session is None:
        return original_root, managed.fingerprint
    root = session.bindings.filter(kind='root', object_id='archive', role='archive').first()
    target = session.target_roots.get('archive')
    if root is None or not target:
        _fail('restore_binding_missing')
    restored_root = {'path': target, 'identity': root.target_fingerprint}
    if original_root == restored_root:
        return restored_root, managed.fingerprint
    if _replay_root(session, 'archive', original_root) == restored_root:
        return restored_root, _replay_fingerprint(
            session, 'archive', managed.relative_path, managed.fingerprint, ignore_ctime=True)

    # Original v1 sets used a verified per-object binding without an archive root snapshot.
    if session.source_roots.get('archive', {}).get('identity') or (
            root.source_fingerprint and not _fingerprint_matches(
                original_root.get('identity'), root.source_fingerprint)):
        _fail('restore_binding_mismatch')
    evidence = session.bindings.filter(role='archive', source_relative_path=managed.relative_path,
                                       target_relative_path=managed.relative_path)
    recorded = evidence.filter(kind='managed_original', object_id=str(managed.photo_id)).first()
    path = evidence.filter(kind='path', object_id='archive:' + managed.relative_path).first()
    if (recorded is None or path is None or recorded.source_fingerprint != managed.fingerprint
            or recorded.target_fingerprint != path.target_fingerprint
            or (path.sha256, path.byte_size) != (managed.sha256, managed.byte_size)):
        _fail('restore_binding_mismatch')
    return restored_root, recorded.target_fingerprint


def effective_model_manifest(artifact):
    """Use relocated offline material without modifying the recorded artifact identity."""
    session = _active_session()
    if session is None:
        return artifact.manifest
    model = next((row for row in session.manifest.get('models', [])
                  if row['manifest_digest'] == artifact.manifest_digest), None)
    if model is None:
        return artifact.manifest
    return _relocated_model_manifest(session, model, artifact)


def effective_path_binding(role, relative_path):
    session = _active_session()
    if session is None:
        return None
    try:
        return session.bindings.get(kind='path', object_id=f'{role}:{relative_path}', role=role,
                                    source_relative_path=relative_path)
    except RestoreBinding.DoesNotExist:
        _fail('restore_binding_missing')


def effective_binding(kind, object_id, original_binding):
    session = _active_session()
    if session is None:
        return original_binding
    role = original_binding.get('role', original_binding.get('root_key'))
    relative = original_binding.get('relative_path')
    try:
        binding = session.bindings.get(kind=kind, object_id=str(object_id), role=role,
                                       source_relative_path=relative)
    except RestoreBinding.DoesNotExist:
        _fail('restore_binding_missing')
    return {'root_key': binding.role, 'role': binding.role, 'relative_path': binding.target_relative_path}


def effective_fingerprint(kind, object_id, role, original_fingerprint):
    session = _active_session()
    if session is None:
        return original_fingerprint
    try:
        binding = session.bindings.get(kind=kind, object_id=str(object_id), role=role)
    except RestoreBinding.DoesNotExist:
        _fail('restore_binding_missing')
    if binding.source_fingerprint != original_fingerprint:
        _fail('restore_source_provenance_mismatch')
    return binding.target_fingerprint
