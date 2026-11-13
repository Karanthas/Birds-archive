"""Retries preserve the confirmed journal and never grant new file ownership."""
import errno
from datetime import timedelta
from unittest import mock

from django.db import OperationalError
from django.test import Client, override_settings
from django.utils import timezone

from archive.models import ArchiveCompletion, Job
from archive.services.archiving import process_archive
from archive.services.jobs import claim_job, claim_next, retry_job
from archive.tests.test_archive_recovery import ArchiveFixture


class ArchiveRetryTests(ArchiveFixture):
    def test_retry_rejects_objects_without_persisted_ownership(self):
        from archive.services.archiving import bind_archive_roots
        from archive.services.paths import root_identity, open_root
        from archive.tests.fixtures import disk_hashes
        import os
        cases = ('temporary_file', 'temporary_directory', 'claim_directory', 'invalid_temporary_identity')
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                _, path, operation = self.confirmed(case+'.jpg', color=(index*40, 20, 30))
                bind_archive_roots(operation)
                directory = (self.claims/str(operation.pk) if case == 'claim_directory' else
                             self.archive/'.archive-ops'/str(operation.pk))
                directory.mkdir(parents=True)
                context = operation.retry_context.copy()
                if case in ('temporary_file', 'invalid_temporary_identity'):
                    fd = open_root(directory)
                    try:
                        context['temporary_directory_identity'] = root_identity(fd)
                    finally:
                        os.close(fd)
                    (directory/'original').write_bytes(b'unknown object')
                if case == 'invalid_temporary_identity':
                    context['temporary_identity'] = {'dev': None, 'ino': None}
                operation.stage = 'protecting'
                operation.retry_context = context
                operation.temporary_relative_path = '.archive-ops/'+str(operation.pk)+'/original'
                operation.save(update_fields=['stage', 'retry_context', 'temporary_relative_path'])
                Job.objects.filter(pk=operation.job_id).update(state='blocked', error_code='target_conflict')
                before = (path.read_bytes(), disk_hashes(self.archive), disk_hashes(self.claims), directory.stat().st_ino)
                job, accepted, _ = retry_job(operation.job_id, operation.source_observation.version)
                self.assertFalse(accepted)
                self.assertEqual(job.state, 'blocked')
                operation.refresh_from_db()
                self.assertEqual(operation.retry_context, context)
                self.assertEqual((path.read_bytes(), disk_hashes(self.archive), disk_hashes(self.claims),
                                  directory.stat().st_ino), before)
                self.assertFalse(ArchiveCompletion.objects.filter(operation=operation).exists())

    def attempt(self, operation, now=None):
        job = claim_job(operation.job_id, now or timezone.now())
        self.assertIsNotNone(job)
        return process_archive(job)

    @override_settings(RETRY_LIMIT=3, RETRY_BASE_MS=1000)
    def test_transient_bounded_backoff(self):
        photo, path, operation = self.confirmed()
        Job.objects.filter(pk=operation.job_id).update(state='queued')
        for attempt in range(1, 4):
            before = timezone.now()
            with mock.patch('archive.services.archiving._protect', side_effect=OSError(errno.EAGAIN, 'private path')):
                self.assertFalse(self.attempt(operation, before + timedelta(seconds=10)))
            job = Job.objects.get(pk=operation.job_id)
            self.assertEqual(job.state, 'retry_wait' if attempt < 3 else 'failed')
            self.assertEqual(job.attempts, attempt)
            self.assertEqual(job.error_code, 'temporarily_unavailable')
            if attempt < 3:
                self.assertGreaterEqual((job.next_attempt_at-before).total_seconds(), 2**(attempt-1))
                self.assertIsNone(claim_job(job.pk, before))
        operation.refresh_from_db()
        self.assertEqual(operation.retry_context['cycle_attempts'], 3)
        self.assertIn('next_photo_id', operation.retry_context)
        self.assertTrue(path.exists())
        self.assertIsNone(claim_next(timezone.now()+timedelta(days=1)))

    def test_manual_retry_idempotent(self):
        photo, path, operation = self.confirmed()
        Job.objects.filter(pk=operation.job_id).update(state='queued')
        with mock.patch('archive.services.archiving._protect', side_effect=OSError(errno.ENOSPC, 'private path')):
            self.assertFalse(self.attempt(operation))
        original_context = operation.retry_context.copy()
        job, queued, _ = retry_job(operation.job_id, operation.source_observation.version)
        self.assertTrue(queued)
        self.assertEqual(job.attempts, 1)
        self.assertTrue(retry_job(job.pk, job.source_version)[1])
        self.assertEqual(Job.objects.filter(kind='archive').count(), 1)
        self.assertTrue(self.attempt(operation))
        operation.refresh_from_db(); job.refresh_from_db()
        self.assertEqual(job.attempts, 2)
        self.assertEqual(ArchiveCompletion.objects.filter(operation=operation).count(), 1)
        self.assertEqual(operation.retry_context['next_photo_id'], original_context['next_photo_id'])
        self.assert_complete(photo, path, operation)

    def test_changed_source_not_retried(self):
        photo, path, operation = self.confirmed()
        Job.objects.filter(pk=operation.job_id).update(state='queued')
        path.write_bytes(b'changed source')
        self.assertFalse(self.attempt(operation))
        self.assertFalse(retry_job(operation.job_id, operation.source_observation.version)[1])
        self.assertIsNone(claim_next(timezone.now()+timedelta(days=1)))
        self.assertEqual(path.read_bytes(), b'changed source')

    def test_root_changed_not_rebound(self):
        _, _, operation = self.confirmed()
        Job.objects.filter(pk=operation.job_id).update(state='queued')
        with mock.patch('archive.services.archiving._protect', side_effect=OSError(errno.ENOSPC, 'full')):
            self.attempt(operation)
        operation.refresh_from_db(); snapshot = operation.roots_snapshot.copy()
        with override_settings(BIRDS_ARCHIVE_ROOT=str(self.root/'other')):
            self.assertFalse(retry_job(operation.job_id, operation.source_observation.version)[1])
            self.assertIsNone(claim_next(timezone.now()+timedelta(days=1)))
        operation.refresh_from_db()
        self.assertEqual(operation.roots_snapshot, snapshot)

    def test_registered_cleanup_resume(self):
        photo, path, operation = self.confirmed()
        Job.objects.filter(pk=operation.job_id).update(state='queued')
        with mock.patch('archive.services.archiving._cleanup', side_effect=OSError(errno.EIO, 'fsync')):
            self.assertFalse(self.attempt(operation))
        operation.refresh_from_db()
        self.assertEqual(operation.stage, 'registered')
        self.assertFalse(ArchiveCompletion.objects.exists())
        self.assertTrue(retry_job(operation.job_id, operation.source_observation.version)[1])
        self.assertTrue(self.attempt(operation))
        self.assert_complete(photo, path, operation)

    def test_retry_requires_csrf(self):
        _, _, operation = self.confirmed()
        response = Client(enforce_csrf_checks=True).post(f'/tasks/{operation.job_id}/retry/',
            {'version': operation.source_observation.version})
        self.assertEqual(response.status_code, 403)

    def test_unsafe_errors_stop(self):
        for number in (errno.EIO, errno.ENOSPC, errno.EDQUOT, errno.EACCES):
            with self.subTest(errno=number):
                _, path, operation = self.confirmed(name=f'{number}.jpg', color=(number, 10, 20))
                Job.objects.filter(pk=operation.job_id).update(state='queued')
                with mock.patch('archive.services.archiving._protect', side_effect=OSError(number, 'private path')):
                    self.assertFalse(self.attempt(operation))
                self.assertEqual(Job.objects.get(pk=operation.job_id).state, 'failed')
                self.assertTrue(path.exists())

    def test_database_busy_safe_replay(self):
        photo, path, operation = self.confirmed()
        Job.objects.filter(pk=operation.job_id).update(state='queued')
        with mock.patch('archive.services.archiving._protect', side_effect=OperationalError('database is locked')):
            self.assertFalse(self.attempt(operation))
        job = Job.objects.get(pk=operation.job_id)
        self.assertEqual((job.state, job.error_code), ('retry_wait', 'database_busy'))
        self.assertTrue(self.attempt(operation, job.next_attempt_at))
        self.assert_complete(photo, path, operation)

    def test_public_status_no_store(self):
        _, _, operation = self.confirmed()
        for endpoint in ('/tasks/', '/tasks/status/', '/tasks/status/?scope=archive'):
            response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 200)
            self.assertIn('no-store', response['Cache-Control'])
            self.assertNotContains(response, str(self.root))

    def test_manual_cycle_retains_cumulative_attempts(self):
        _, _, operation = self.confirmed()
        Job.objects.filter(pk=operation.job_id).update(state='queued')
        for _ in range(3):
            with mock.patch('archive.services.archiving._protect', side_effect=OSError(errno.EINTR, 'interrupted')):
                self.assertFalse(self.attempt(operation, timezone.now()+timedelta(days=1)))
        self.assertTrue(retry_job(operation.job_id, operation.source_observation.version)[1])
        with mock.patch('archive.services.archiving._protect', side_effect=OSError(errno.EBUSY, 'busy')):
            self.assertFalse(self.attempt(operation))
        operation.refresh_from_db(); operation.job.refresh_from_db()
        self.assertEqual(operation.job.attempts, 4)
        self.assertEqual(operation.retry_context['cycle_start_attempt'], 3)
        self.assertEqual(operation.retry_context['cycle_attempts'], 1)
        self.assertEqual(operation.job.state, 'retry_wait')

    def test_repeated_crash_stops_at_cycle_limit(self):
        from archive.services.jobs import recover_running
        from archive.services.paths import open_root
        import os
        _, path, operation = self.confirmed()
        Job.objects.filter(pk=operation.job_id).update(state='running', attempts=3)
        operation.retry_context = {**operation.retry_context, 'cycle_attempts': 3}
        operation.save(update_fields=['retry_context'])
        fd = open_root(self.data)
        try:
            recover_running(fd)
        finally:
            os.close(fd)
        self.assertEqual(Job.objects.get(pk=operation.job_id).state, 'failed')
        self.assertIsNone(claim_next(timezone.now()+timedelta(days=1)))
        self.assertTrue(path.exists())

    def test_retry_after_new_inbox_version_uses_frozen_evidence(self):
        from archive.services.intake import scan_once
        from archive.tests.fixtures import jpeg
        photo, path, operation = self.confirmed()
        Job.objects.filter(pk=operation.job_id).update(state='queued')
        with mock.patch('archive.services.archiving._cleanup', side_effect=OSError(errno.EIO, 'fsync')):
            self.assertFalse(self.attempt(operation))
        operation.refresh_from_db(); frozen = operation.retry_context.copy()
        jpeg(path, color='green'); new_bytes = path.read_bytes(); scan_once()
        self.assertNotEqual(operation.source_observation.version,
            operation.source_observation.source.__class__.objects.get(pk=operation.source_observation.source_id).observed_version)
        self.assertTrue(retry_job(operation.job_id, operation.source_observation.version)[1])
        operation.refresh_from_db()
        self.assertEqual({key: operation.retry_context[key] for key in frozen if not key.startswith('cycle_')},
                         {key: value for key, value in frozen.items() if not key.startswith('cycle_')})
        self.assertTrue(self.attempt(operation))
        self.assertEqual(path.read_bytes(), new_bytes)
        self.assertEqual(ArchiveCompletion.objects.filter(photo=photo).count(), 1)

    def test_concurrent_manual_retry_queues_single_operation(self):
        from concurrent.futures import ThreadPoolExecutor
        from django.db import connections
        _, _, operation = self.confirmed()
        Job.objects.filter(pk=operation.job_id).update(state='queued')
        with mock.patch('archive.services.archiving._protect', side_effect=OSError(errno.ENOSPC, 'full')):
            self.assertFalse(self.attempt(operation))
        def retry(_):
            try:
                return Client().post(f'/tasks/{operation.job_id}/retry/',
                    {'version': operation.source_observation.version}).status_code
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(retry, range(2))), [302, 302])
        self.assertEqual(Job.objects.filter(kind='archive').count(), 1)
        self.assertEqual(Job.objects.get(pk=operation.job_id).attempts, 1)
        self.assertTrue(self.attempt(operation))

    def test_fsync_eagain_is_not_transient(self):
        _, _, operation = self.confirmed()
        Job.objects.filter(pk=operation.job_id).update(state='queued')
        with mock.patch('archive.services.archiving.os.fsync', side_effect=OSError(errno.EAGAIN, 'sync')):
            self.assertFalse(self.attempt(operation))
        self.assertEqual(Job.objects.get(pk=operation.job_id).error_code, 'sync_failed')
        self.assertEqual(Job.objects.get(pk=operation.job_id).state, 'failed')
