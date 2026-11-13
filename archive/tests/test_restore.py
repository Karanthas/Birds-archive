import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, SimpleTestCase

from archive.models import ImportSource, Job, RestoreBinding, RestoreSession
from archive.services import backup, restore


class RestoreCopyTests(SimpleTestCase):
    def test_prepare_preserves_hardlinks_retries_and_rejects_same_byte_replacement(self):
        temporary = tempfile.TemporaryDirectory(prefix='birds-restore-core-')
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        set_id = str(uuid.uuid4())
        source = base / set_id
        (source / 'roots' / 'inbox').mkdir(parents=True)
        (source / 'roots' / 'claim').mkdir(parents=True)
        (source / 'database').mkdir()
        first = source / 'roots' / 'inbox' / 'bird.jpg'
        first.write_bytes(b'same-bird-bytes')
        second = source / 'roots' / 'claim' / 'bird.jpg'
        os.link(first, second)
        database = source / 'database' / 'archive.sqlite3'
        database.write_bytes(b'database-snapshot')
        (source / backup.MANIFEST).write_text('{}')
        digest = hashlib.sha256(first.read_bytes()).hexdigest()
        info = first.stat()
        manifest = {'set_id': set_id, 'models': [], 'source_roots': {}, 'deployment': {},
            'database': {'path': 'database/archive.sqlite3', 'size': database.stat().st_size,
                         'sha256': hashlib.sha256(database.read_bytes()).hexdigest(), 'tables': []},
            'files': [
                {'root_role': 'inbox', 'relative_path': 'bird.jpg', 'size': info.st_size,
                 'sha256': digest, 'source_fingerprint': {}, 'hardlink_group': 'link-1'},
                {'root_role': 'claim', 'relative_path': 'bird.jpg', 'size': info.st_size,
                 'sha256': digest, 'source_fingerprint': {}, 'hardlink_group': 'link-1'}]}
        roots = {role: base / ('new-' + role) for role in restore.ROLES}
        for role, root in roots.items():
            root.mkdir()
            (root / restore.OWNER).write_text(json.dumps({'deployment_id': 'new', 'role': role}))
        with patch.object(backup, '_verify_set', return_value=manifest):
            prepared = restore.prepare_restore(source, roots)
            self.assertEqual((roots['inbox'] / 'bird.jpg').stat().st_ino,
                             (roots['claim'] / 'bird.jpg').stat().st_ino)
            restore.prepare_restore(source, roots, prepared['session_id'])
            replaced = roots['inbox'] / 'bird.jpg'
            replaced.unlink(); replaced.write_bytes(b'same-bird-bytes')
            with self.assertRaisesRegex(restore.RestoreError, 'restore_target_collision'):
                restore.prepare_restore(source, roots, prepared['session_id'])


class RestoreBindingTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='birds-restore-binding-')
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.roots = {role: base / role for role in restore.ROLES}
        for root in self.roots.values(): root.mkdir()

    def session(self, status='activated'):
        return RestoreSession.objects.create(backup_set_id=uuid.uuid4(), backup_manifest_sha256='a' * 64,
            target_roots={role: str(root) for role, root in self.roots.items()}, status=status)

    def test_effective_reads_are_strict_only_for_an_active_session(self):
        original = {'root_key': 'archive', 'relative_path': 'bird.jpg'}
        self.assertEqual(restore.effective_binding('managed_original', 'photo', original), original)
        session = self.session()
        RestoreBinding.objects.create(session=session, kind='managed_original', object_id='photo', role='archive',
            source_relative_path='bird.jpg', target_relative_path='bird.jpg',
            source_fingerprint={'ino': 1}, target_fingerprint={'ino': 2}, sha256='b' * 64,
            byte_size=1, hardlink_group='one')
        self.assertEqual(restore.effective_fingerprint(
            'managed_original', 'photo', 'archive', {'ino': 1}), {'ino': 2})
        with self.assertRaisesRegex(restore.RestoreError, 'restore_binding_missing'):
            restore.effective_binding('managed_original', 'missing', original)
        with self.assertRaisesRegex(restore.RestoreError, 'restore_source_provenance_mismatch'):
            restore.effective_fingerprint('managed_original', 'photo', 'archive', {'ino': 9})

    def test_verify_accepts_succeeded_import_and_blocks_missing_pending_version(self):
        session = self.session(status='prepared')
        for role, root in self.roots.items():
            (root / restore.MARKER).write_text(json.dumps({'schema': 1, 'session_id': str(session.pk),
                'backup_set_id': str(session.backup_set_id),
                'backup_manifest_sha256': session.backup_manifest_sha256, 'role': role}))
            RestoreBinding.objects.create(session=session, kind='root', object_id=role, role=role,
                source_relative_path='.', target_relative_path='.', source_fingerprint={},
                target_fingerprint=restore._root_fingerprint(root), sha256='', byte_size=0,
                hardlink_group='root-' + role)
        source = ImportSource.objects.create(relative_path='bird.jpg')
        job = Job.objects.create(source=source, source_version=1, state='succeeded')
        self.assertEqual(restore.verify_restore(session.pk).status, 'verified')
        job.state = 'pending'; job.save(update_fields=['state'])
        with self.assertRaisesRegex(restore.RestoreError, 'restore_import_version_missing'):
            restore.verify_restore(session.pk)
