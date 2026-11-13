"""Current projection is initialized from history, without accessing asset paths."""
from unittest import mock
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from archive.models import Photo, ArchiveOperation
from archive.tests.test_download import DownloadFixture


class ProjectionMigrationTests(DownloadFixture):
    def test_old_completion_round_trip_and_gallery(self):
        photo, target, managed = self.completed()
        history = ArchiveOperation.objects.get(photo=photo).capture_snapshot
        before = target.read_bytes()
        pending = Photo.objects.create(sha256='b'*64, byte_size=11, preview_key='')
        executor = MigrationExecutor(connection)
        executor.migrate([('archive', '0008_batch_review')])
        try:
            executor = MigrationExecutor(connection)
            with mock.patch('archive.services.paths.open_root', side_effect=AssertionError('migration file IO')):
                executor.migrate([('archive', '0009_photo_management')])
        finally:
            MigrationExecutor(connection).migrate([('archive', '0009_photo_management')])
        photo.refresh_from_db(); pending.refresh_from_db()
        self.assertEqual(photo.current_capture, history)
        self.assertEqual(photo.current_classification, 'identified')
        self.assertIsNone(pending.current_capture)
        self.assertEqual(ArchiveOperation.objects.get(photo=photo).capture_snapshot, history)
        self.assertEqual(target.read_bytes(), before)
        self.assertContains(self.client.get('/'), str(photo.pk))

    def test_first_completion_initializes_once_and_reads_current(self):
        photo, target, managed = self.completed()
        photo.refresh_from_db()
        history = photo.archive_operation.capture_snapshot
        self.assertEqual(photo.current_capture, history)
        current = {**history, 'local':'2030-01-01', 'month':'2030-01', 'year':'2030', 'sort_key':'2030-01-01'}
        Photo.objects.filter(pk=photo.pk).update(current_capture=current)
        self.run_worker()
        response = self.client.get('/')
        self.assertContains(response, '2030 年 1 月')
        photo.refresh_from_db()
        self.assertEqual(photo.current_capture, current)
        self.assertEqual(ArchiveOperation.objects.get(photo=photo).capture_snapshot, history)
