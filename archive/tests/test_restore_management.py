import errno
import uuid
from unittest import mock

from django.test import override_settings

from archive.models import ArchiveOperation, ManagedOriginal, ManagementOperation, Photo
from archive.services import management
from archive.tests.restore_fixtures import restored_backup
from archive.tests.test_download import DownloadFixture


class ManagementResumeTests(DownloadFixture):
    def setUp(self):
        super().setUp()
        self.inbox = self.inbox.resolve()
        self.data = self.data.resolve()
        self.archive = self.archive.resolve()
        self.claims = self.claims.resolve()
        canonical = override_settings(
            BIRDS_INBOX_ROOT=str(self.inbox), BIRDS_DATA_ROOT=str(self.data),
            BIRDS_ARCHIVE_ROOT=str(self.archive), BIRDS_CLAIM_ROOT=str(self.claims),
        )
        canonical.enable()
        self.addCleanup(canonical.disable)

    def interrupt_at(self, operation, expected_stage):
        def interrupt(stage):
            if stage == expected_stage:
                raise OSError(errno.EIO, 'backup fixture interruption')

        with mock.patch('archive.services.management.management_fault_hook', side_effect=interrupt):
            self.assertFalse(management.process_management(operation.pk))
        operation.refresh_from_db()
        self.assertEqual((operation.stage, operation.status), (expected_stage, 'blocked'))

    def test_registered_operation_resumes_without_rewriting_history_or_user_data(self):
        photo, source, _ = self.completed()
        photo.refresh_from_db()
        operation = management.accept_management(
            photo.pk, photo.metadata_version, uuid.uuid4(), 'capture',
            {'capture': {'local': '2035-03-04', 'precision': 'date', 'offset': None}},
        )

        self.interrupt_at(operation, 'registered')
        original_journal = operation.source_snapshot
        archive_history = list(ArchiveOperation.objects.filter(photo=photo).values())
        Photo.objects.filter(pk=photo.pk).update(
            user_location='后来地点', notes='保留用户备注', favorite=True, rating=5, metadata_version=99)

        with restored_backup(self):
            restored = ManagementOperation.objects.get(pk=operation.pk)
            self.assertTrue(management.process_management(restored.pk))
            restored.refresh_from_db()
            current_photo = Photo.objects.get(pk=photo.pk)
            managed = ManagedOriginal.objects.get(photo=photo)
            target = self.archive / managed.relative_path

            self.assertEqual((restored.stage, restored.status), ('completed', 'succeeded'))
            self.assertEqual(restored.source_snapshot, original_journal)
            self.assertEqual(
                (current_photo.user_location, current_photo.notes, current_photo.favorite,
                 current_photo.rating, current_photo.metadata_version),
                ('后来地点', '保留用户备注', True, 5, 99),
            )
            self.assertEqual(list(ArchiveOperation.objects.filter(photo=photo).values()), archive_history)
            self.assertEqual(current_photo.current_capture['year'], '2035')
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_published_trash_and_restore_operations_resume(self):
        restoring, original, _ = self.completed('restore-me.jpg', 'red')
        trashing, _, _ = self.completed('trash-me.jpg', 'blue')
        restoring.refresh_from_db()
        initial_trash = management.accept_confirmed_trash(
            restoring.pk, management.trash_confirmation(restoring))
        self.assertTrue(management.process_management(initial_trash.pk))
        restoring.refresh_from_db()
        old_restore = management.accept_confirmed_restore(
            restoring.pk, management.restore_confirmation(restoring))
        self.interrupt_at(old_restore, 'published')

        trashing.refresh_from_db()
        old_trash = management.accept_confirmed_trash(
            trashing.pk, management.trash_confirmation(trashing))
        self.interrupt_at(old_trash, 'published')
        restore_journal = old_restore.source_snapshot
        trash_journal = old_trash.source_snapshot

        with restored_backup(self):
            restored_trash = ManagementOperation.objects.get(pk=old_trash.pk)
            restored_restore = ManagementOperation.objects.get(pk=old_restore.pk)
            self.assertTrue(management.process_management(restored_trash.pk))
            self.assertTrue(management.process_management(restored_restore.pk))

            current_trash = Photo.objects.get(pk=trashing.pk)
            current_restore = Photo.objects.get(pk=restoring.pk)
            trash_managed = ManagedOriginal.objects.get(photo=trashing)
            restore_managed = ManagedOriginal.objects.get(photo=restoring)
            restored_trash.refresh_from_db()
            restored_restore.refresh_from_db()
            self.assertEqual(restored_trash.source_snapshot, trash_journal)
            self.assertEqual(restored_restore.source_snapshot, restore_journal)
            self.assertIsNotNone(current_trash.trashed_at)
            self.assertIsNone(current_restore.trashed_at)
            self.assertTrue(trash_managed.relative_path.startswith('.archive-trash/'))
            self.assertEqual((self.archive / restore_managed.relative_path).read_bytes(), original.read_bytes())

    def test_new_http_correction_trash_and_restore_use_the_restored_root(self):
        photo, source, _ = self.completed()
        Photo.objects.filter(pk=photo.pk).update(notes='恢复后仍保留', rating=4)
        archive_history = list(ArchiveOperation.objects.filter(photo=photo).values())

        with restored_backup(self):
            current_photo = Photo.objects.get(pk=photo.pk)
            request_id = uuid.uuid4()
            response = self.client.post(f'/photos/{current_photo.pk}/', {
                'management_action': 'capture', 'metadata_version': current_photo.metadata_version,
                'request_id': str(request_id), 'mode': 'manual', 'date': '2034-05-06',
                'time': '', 'offset': '',
            })
            self.assertEqual(response.status_code, 303)
            correction = ManagementOperation.objects.get(request_id=request_id)
            self.assertEqual(correction.source_snapshot['root_identity']['path'], str(self.archive))
            self.assertTrue(management.process_management(correction.pk))

            current_photo.refresh_from_db()
            response = self.client.post(f'/photos/{current_photo.pk}/', {
                'management_action': 'trash',
                'trash_token': management.trash_confirmation(current_photo),
            })
            self.assertEqual(response.status_code, 303)
            trash = current_photo.management_operations.filter(kind='trash').latest('created_at')
            self.assertEqual(trash.source_snapshot['root_identity']['path'], str(self.archive))
            completed = management.process_management(trash.pk)
            trash.refresh_from_db()
            self.assertTrue(completed, (trash.stage, trash.status, trash.error_code))

            current_photo.refresh_from_db()
            managed = ManagedOriginal.objects.get(photo=current_photo)
            self.assertIsNotNone(current_photo.trashed_at)
            self.assertTrue(managed.relative_path.startswith('.archive-trash/'))
            self.assertTrue((self.archive / managed.relative_path).is_file())

            response = self.client.post(f'/photos/{current_photo.pk}/', {
                'management_action': 'restore',
                'restore_token': management.restore_confirmation(current_photo),
            })
            self.assertEqual(response.status_code, 303)
            restore = current_photo.management_operations.filter(kind='restore').latest('created_at')
            self.assertEqual(restore.source_snapshot['root_identity']['path'], str(self.archive))
            completed = management.process_management(restore.pk)
            restore.refresh_from_db()
            self.assertTrue(completed, (restore.stage, restore.status, restore.error_code))

            current_photo.refresh_from_db()
            managed.refresh_from_db()
            self.assertIsNone(current_photo.trashed_at)
            self.assertIsNone(current_photo.trash_requested_at)
            self.assertEqual((current_photo.notes, current_photo.rating), ('恢复后仍保留', 4))
            self.assertEqual(list(ArchiveOperation.objects.filter(photo=photo).values()), archive_history)
            self.assertEqual((self.archive / managed.relative_path).read_bytes(), source.read_bytes())
