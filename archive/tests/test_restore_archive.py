import copy
import hashlib
from pathlib import Path
from unittest import mock

from archive.models import ArchiveCompletion, ArchiveOperation, ManagedOriginal, Photo
from archive.services import archiving, restore
from archive.tests.fixtures import disk_hashes
from archive.tests.restore_fixtures import restored_backup
from archive.tests.test_archive_recovery import ArchiveFixture


class ArchiveResumeTests(ArchiveFixture):
    def test_published_before_registration_resumes_in_restored_roots(self):
        photo, _, operation = self.confirmed('restore-published.jpg', color='blue')
        worker = self.start_paused('archive_after_link')
        operation.refresh_from_db()
        self.assertEqual(operation.stage, 'publish_intent')
        worker.kill()
        worker.communicate(timeout=3)

        original_roots = tuple(Path(value).resolve() for value in
                               (self.inbox, self.data, self.archive, self.claims))
        original_files = {
            'inbox': disk_hashes(self.inbox),
            'archive': disk_hashes(self.archive),
            'claim': disk_hashes(self.claims),
        }
        history = {
            'roots_snapshot': copy.deepcopy(operation.roots_snapshot),
            'source_fingerprint': copy.deepcopy(operation.source_fingerprint),
            'protected_fingerprint': copy.deepcopy(operation.protected_fingerprint),
            'temporary_fingerprint': copy.deepcopy(operation.temporary_fingerprint),
            'claim_fingerprint': copy.deepcopy(operation.claim_fingerprint),
            'claim_relative_path': operation.claim_relative_path,
            'temporary_relative_path': operation.temporary_relative_path,
            'target_relative_path': operation.target_relative_path,
        }
        journal_keys = ('temporary_directory_identity', 'claim_directory_identity', 'temporary_identity',
                        'directory', 'directory_species_ids', 'allocated_basename', 'collision_counter',
                        'name_limit')
        journal_context = {key: copy.deepcopy(operation.retry_context.get(key)) for key in journal_keys}

        with restored_backup(self):
            restored = ArchiveOperation.objects.get(pk=operation.pk)
            temporary = self.archive / restored.temporary_relative_path
            published = self.archive / restored.target_relative_path
            self.assertEqual((temporary.stat().st_dev, temporary.stat().st_ino),
                             (published.stat().st_dev, published.stat().st_ino))

            real_open_root = archiving.open_root

            def restored_only(path):
                if Path(path).resolve() in original_roots:
                    raise AssertionError('归档恢复访问了原根')
                return real_open_root(path)

            with mock.patch.object(archiving, 'open_root', side_effect=restored_only):
                self.run_worker()
                restored.refresh_from_db()
                if restored.stage != 'complete':
                    self.run_worker()

            restored.refresh_from_db()
            current_photo = Photo.objects.get(pk=photo.pk)
            managed = ManagedOriginal.objects.get(photo=current_photo)
            target = self.archive / managed.relative_path
            self.assertEqual((restored.stage, current_photo.state), ('complete', 'archived'))
            self.assertEqual(ManagedOriginal.objects.filter(photo=current_photo).count(), 1)
            self.assertEqual(ArchiveCompletion.objects.filter(photo=current_photo).count(), 1)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), current_photo.sha256)

            response = self.client.get(f'/photos/{current_photo.pk}/download/')
            self.assertEqual(response.status_code, 200)
            try:
                downloaded = b''.join(response.streaming_content)
            finally:
                response.close()
            self.assertEqual(hashlib.sha256(downloaded).hexdigest(), current_photo.sha256)

            for field, value in history.items():
                self.assertEqual(getattr(restored, field), value)
            self.assertEqual({key: restored.retry_context.get(key) for key in journal_context}, journal_context)

        self.assertEqual(disk_hashes(original_roots[0]), original_files['inbox'])
        self.assertEqual(disk_hashes(original_roots[2]), original_files['archive'])
        self.assertEqual(disk_hashes(original_roots[3]), original_files['claim'])

    def test_missing_claimed_protected_copy_rejects_activation(self):
        _, _, operation = self.confirmed('restore-missing-protected.jpg', color='green')
        worker = self.start_paused('archive_claimed')
        operation.refresh_from_db()
        self.assertEqual(operation.stage, 'claimed')
        worker.kill()
        worker.communicate(timeout=3)

        with restored_backup(self, activate=False) as session:
            restored = ArchiveOperation.objects.get(pk=operation.pk)
            (self.archive / restored.temporary_relative_path).unlink()
            with self.assertRaisesRegex(restore.RestoreError, 'restore_binding_missing'):
                restore.verify_restore(session.pk)
            session.refresh_from_db()
            self.assertEqual(session.status, 'prepared')
            self.assertFalse(ManagedOriginal.objects.filter(photo_id=restored.photo_id).exists())
            self.assertFalse(ArchiveCompletion.objects.filter(photo_id=restored.photo_id).exists())

