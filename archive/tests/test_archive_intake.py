import hashlib
import os
import shutil
from pathlib import Path
from unittest import mock

from django.test import override_settings

from archive.models import ArchiveOperation, ManagedOriginal, SourceObservation, Job
from archive.services.intake import scan_once, refresh_original_availability
from archive.services.jobs import recover_running
from archive.services.paths import open_root, fingerprint, RootsNotIsolated
from archive.tests.test_confirmation import ConfirmationFixture
from archive.tests.fixtures import jpeg


class ArchiveIntakeTests(ConfirmationFixture):
    def archive_worker(self, **extra):
        from django.core.management import call_command
        archive, claims = self.root/'archive', self.root/'claims'
        archive.mkdir(exist_ok=True); claims.mkdir(exist_ok=True)
        with override_settings(BIRDS_ARCHIVE_ROOT=str(archive), BIRDS_CLAIM_ROOT=str(claims), **extra):
            call_command('runworker', '--once', verbosity=0)

    def confirmed(self):
        photo, path = self.imported()
        self.assertEqual(self.post(photo).status_code, 303)
        return photo, path, ArchiveOperation.objects.get(photo=photo)

    def registered(self):
        photo, path, operation = self.confirmed()
        archive = self.root/'archive'; archive.mkdir()
        target = archive/'one.jpg'; shutil.copyfile(path, target)
        managed = ManagedOriginal.objects.create(photo=photo, operation=operation, root_key='archive',
            relative_path='one.jpg', actual_basename='one.jpg', fingerprint=fingerprint(target.stat()),
            sha256=photo.sha256, byte_size=photo.byte_size)
        return photo, path, operation, archive, target, managed

    def test_scan_does_not_stale_archive_job(self):
        photo, path, operation = self.confirmed()
        # 来源失效也不能跨 kind 干预已经由未来执行器领取的任务。
        Job.objects.filter(pk=operation.job_id).update(state='running')
        path.unlink(); scan_once()
        operation.job.refresh_from_db()
        self.assertEqual(operation.job.state, 'running')
        photo.refresh_from_db(); self.assertEqual(photo.state, 'confirmed')
        operation.refresh_from_db(); self.assertEqual(operation.stage, 'queued')

    def test_import_recovery_does_not_touch_archive_operation(self):
        _, _, operation = self.confirmed()
        before = dict(ArchiveOperation.objects.values().get(pk=operation.pk))
        data_fd = open_root(self.data)
        try:
            recover_running(data_fd)
        finally:
            os.close(data_fd)
        self.assertEqual(ArchiveOperation.objects.values().get(pk=operation.pk), before)
        operation.job.refresh_from_db()
        self.assertEqual(operation.job.state, 'awaiting_executor')

    def test_new_input_same_path_gets_observation(self):
        photo, path, operation = self.confirmed()
        original_observation = operation.source_observation_id
        path.unlink(); jpeg(path, color='blue'); scan_once()
        source = operation.source_observation.source
        source.refresh_from_db()
        self.assertNotEqual(source.current_observation_id, original_observation)
        self.assertEqual(source.observed_version, 2)
        self.assertEqual(SourceObservation.objects.get(pk=original_observation).photo_id, photo.pk)
        operation.job.refresh_from_db(); self.assertEqual(operation.job.state, 'awaiting_executor')
        self.assertTrue(path.exists())

    def test_registered_original_availability_independent_of_inbox(self):
        photo, path, _, archive, target, _ = self.registered()
        with override_settings(BIRDS_ARCHIVE_ROOT=str(archive)):
            path.unlink(); scan_once(); refresh_original_availability([photo.pk])
        photo.refresh_from_db()
        self.assertTrue(photo.original_available)
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), photo.sha256)

    def test_registered_original_rejects_corruption_missing_root_and_symlink(self):
        photo, _, _, archive, target, managed = self.registered()
        from archive.services.intake import managed_original_available
        with override_settings(BIRDS_ARCHIVE_ROOT=str(archive)):
            self.assertTrue(managed_original_available(managed))
            content = target.read_bytes(); target.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
            managed.fingerprint = fingerprint(target.stat()); managed.save(update_fields=['fingerprint'])
            self.assertFalse(managed_original_available(managed))
            target.unlink(); target.symlink_to(self.inbox/'one.jpg')
            self.assertFalse(managed_original_available(managed))
        with override_settings(BIRDS_ARCHIVE_ROOT=''):
            refresh_original_availability([photo.pk])
            self.assertFalse(managed_original_available(managed))
        photo.refresh_from_db(); self.assertFalse(photo.original_available)

    def test_scan_rejects_archive_and_claim_root_identity(self):
        for setting in ('BIRDS_ARCHIVE_ROOT', 'BIRDS_CLAIM_ROOT'):
            nested = self.inbox/setting
            nested.mkdir(); jpeg(nested/'do-not-import.jpg')
            with override_settings(**{setting: str(nested)}):
                with self.assertRaises(RootsNotIsolated):
                    scan_once()
            shutil.rmtree(nested)
        self.assertFalse(SourceObservation.objects.exists())

    def test_missing_configured_managed_roots_do_not_block_import(self):
        jpeg(self.inbox/'input.jpg')
        with override_settings(BIRDS_ARCHIVE_ROOT=str(self.root/'missing'), BIRDS_CLAIM_ROOT=''):
            self.assertEqual(scan_once(), 1)

    def test_managed_original_detects_path_replacement_during_hash(self):
        _, _, _, archive, target, managed = self.registered()
        from archive.services.intake import managed_original_available
        from archive.services.images import content_hash
        def replace_after_hash(original):
            result = content_hash(original)
            target.rename(archive/'retained-original.jpg')
            jpeg(target, color='blue')
            return result
        with override_settings(BIRDS_ARCHIVE_ROOT=str(archive)), \
             mock.patch('archive.services.intake.content_hash', side_effect=replace_after_hash):
            self.assertFalse(managed_original_available(managed))

    def test_handoff_reconfigure_resume_same_operation(self):
        import config.settings_test as isolated
        self.assertEqual(Path(isolated.BIRDS_ARCHIVE_ROOT), isolated.BIRDS_TEST_ROOT/'archive')
        self.assertEqual(Path(isolated.BIRDS_CLAIM_ROOT), isolated.BIRDS_TEST_ROOT/'claims')
        photo, path, operation = self.confirmed()
        original_id = operation.pk
        self.archive_worker(BIRDS_HANDOFF_MODE='copy_compatible')
        operation.refresh_from_db(); operation.job.refresh_from_db()
        self.assertEqual(operation.job.state, 'blocked')
        self.assertEqual(operation.roots_snapshot, {'binding_state': 'unbound'})
        self.assertTrue(path.exists())
        self.archive_worker()
        operation.refresh_from_db(); photo.refresh_from_db()
        self.assertEqual(operation.pk, original_id)
        self.assertEqual((operation.stage, photo.state), ('complete', 'archived'))
        self.assertEqual(ArchiveOperation.objects.count(), 1)
        self.assertFalse(path.exists())

    def test_claim_root_not_scanned(self):
        archive, claims = self.root/'archive', self.root/'claims'
        archive.mkdir(); claims.mkdir()
        jpeg(archive/'archive.jpg'); jpeg(claims/'claim.jpg')
        with override_settings(BIRDS_ARCHIVE_ROOT=str(archive), BIRDS_CLAIM_ROOT=str(claims)):
            self.assertEqual(scan_once(), 0)
        self.assertFalse(SourceObservation.objects.exists())

    def test_claim_cleanup_keeps_reused_input(self):
        photo, path, operation = self.confirmed()
        replacement_hash = None
        def replace(stage):
            nonlocal replacement_hash
            if stage == 'archive_claimed':
                replacement_hash = jpeg(path, color='blue')
                scan_once()
        self.archive_worker(BIRDS_TEST_FAULT_CALLBACK=replace)
        operation.refresh_from_db(); photo.refresh_from_db()
        self.assertEqual((operation.stage, photo.state), ('complete', 'archived'))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), replacement_hash)
        current = operation.source_observation.source
        current.refresh_from_db()
        self.assertNotEqual(current.current_observation_id, operation.source_observation_id)
        self.assertEqual(SourceObservation.objects.get(pk=operation.source_observation_id).photo_id, photo.pk)

    def test_duplicate_input_retained(self):
        photo, path, operation = self.confirmed()
        duplicate = self.inbox/'duplicate.jpg'; duplicate.write_bytes(path.read_bytes())
        scan_once()
        before = duplicate.read_bytes()
        self.archive_worker()
        self.archive_worker()  # --once may first service the duplicate's earlier import job.
        operation.refresh_from_db()
        self.assertEqual(operation.stage, 'complete')
        self.assertEqual(duplicate.read_bytes(), before)
        self.assertEqual(ManagedOriginal.objects.filter(photo=photo).count(), 1)

    def test_availability_after_cleanup(self):
        photo, path, operation = self.confirmed()
        self.archive_worker()
        managed = ManagedOriginal.objects.get(photo=photo)
        archive = self.root/'archive'
        target = archive/managed.relative_path
        self.assertFalse(path.exists())
        before = target.read_bytes()
        with override_settings(BIRDS_ARCHIVE_ROOT=str(archive)):
            refresh_original_availability([photo.pk])
            photo.refresh_from_db()
            self.assertTrue(photo.original_available)
            target.rename(target.with_suffix('.retained'))
            refresh_original_availability([photo.pk])
        photo.refresh_from_db(); operation.refresh_from_db()
        self.assertFalse(photo.original_available)
        self.assertEqual(operation.last_error, 'original_unavailable')
        self.assertEqual(operation.stage, 'complete')
        self.assertEqual(target.with_suffix('.retained').read_bytes(), before)
