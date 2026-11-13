"""Resume only verified import/recognition intent after an isolated restore."""
import errno
import os
import sys
import time
import uuid
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from archive.models import (ArchiveOperation, ImportSource, Job, ManagedOriginal, ManagementOperation,
                            ModelArtifact, Photo, RecognitionRun, SourceSnapshot, Species,
                            SpeciesConcept)
from archive.services import management, model_packages, recognition
from archive.services.archiving import process_archive
from archive.services.intake import scan_once
from archive.services.jobs import claim_job, process_source
from archive.services.paths import open_root
from archive.services.recognition_process import InferenceChannel, InferenceLimits
from archive.tests.fixtures import jpeg
from archive.tests.restore_fixtures import restored_backup
from archive.tests.test_download import DownloadFixture
from archive.tests.test_local_model import PackageFixture


class SourceResumeTests(PackageFixture, DownloadFixture):
    def setUp(self):
        super().setUp()
        self.inbox = self.inbox.resolve()
        self.data = self.data.resolve()
        self.archive = self.archive.resolve()
        self.claims = self.claims.resolve()
        canonical = override_settings(
            BIRDS_INBOX_ROOT=str(self.inbox), BIRDS_DATA_ROOT=str(self.data),
            BIRDS_ARCHIVE_ROOT=str(self.archive), BIRDS_CLAIM_ROOT=str(self.claims))
        canonical.enable()
        self.addCleanup(canonical.disable)

    def _artifact(self):
        snapshot = SourceSnapshot.objects.create(
            source='AviList', version='2025b', sha256='a' * 64,
            acquired_at='2026-10-19', download_page_url='https://example.org/catalog',
            license_url='https://example.org/license', attribution='restore control',
            row_count=3, species_count=3, manifest={})
        species = Species.objects.create(
            scientific_name=self.labels[0]['scientific_name'], scientific_key='anas platyrhynchos',
            search_key='anas platyrhynchos', source_snapshot=snapshot)
        SpeciesConcept.objects.create(
            snapshot=snapshot, species=species, external_id=self.labels[0]['external_id'],
            evidence='synthetic exact concept')
        artifact = ModelArtifact.objects.create(
            provider=model_packages.PROVIDER, version='restore-control', revision=model_packages.REVISION,
            manifest_digest=model_packages.digest(self.manifest), manifest=self.manifest,
            selection_state='selected')
        recognition.set_current_model(artifact.pk)
        candidate = {'rank': 1, 'raw_label': self.labels[0]['scientific_name'], 'score': .9,
                     'species_id': self.labels[0]['species_id'], 'mapping_status': 'mapped',
                     'mapping_evidence': self.labels[0]['mapping_evidence']}
        return artifact, candidate

    def _process_pending_imports(self):
        inbox_fd, data_fd = open_root(self.inbox), open_root(self.data)
        try:
            for job in Job.objects.filter(kind='import', state='pending'):
                self.assertTrue(process_source(job, inbox_fd, data_fd))
        finally:
            os.close(inbox_fd)
            os.close(data_fd)

    def _dispatch(self, candidate, relative_path):
        channel = InferenceChannel(
            python=sys.executable, lock_path=self.data / 'recognition.lock',
            limits=InferenceLimits(512, 5), test_provider={'candidates': [candidate]})
        channel.start().wait_ready()
        inbox_fd, data_fd = open_root(self.inbox), open_root(self.data)
        dispatcher = recognition.RecognitionDispatcher(channel, inbox_fd, data_fd)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                dispatcher.tick()
                run = RecognitionRun.objects.get(
                    source_observation__source__relative_path=relative_path)
                if run.state not in ('queued', 'running', 'retry_wait'):
                    return run
                time.sleep(.01)
            self.fail('restored recognition control did not finish')
        finally:
            dispatcher.close_input()
            channel.close()
            os.close(inbox_fd)
            os.close(data_fd)

    def test_queued_import_continues_against_verified_restored_inode(self):
        path = self.inbox / 'queued.jpg'
        digest = jpeg(path, color='red')
        scan_once()
        source = ImportSource.objects.get(relative_path='queued.jpg')
        original_fingerprint = source.fingerprint

        with restored_backup(self):
            restored = ImportSource.objects.get(relative_path='queued.jpg')
            self.assertNotEqual(restored.fingerprint, original_fingerprint)
            self._process_pending_imports()
            photo = Photo.objects.get(sha256=digest)
            self.assertEqual(Job.objects.get(kind='import').state, 'succeeded')
            self.assertTrue(photo.original_available)

    def test_queued_recognition_keeps_payload_and_uses_control_provider(self):
        completed, _, completed_managed = self.completed('completed.jpg', 'red')
        trashed, _, trashed_managed = self.completed('trashed.jpg', 'green')
        trashed.refresh_from_db()
        trash = management.accept_confirmed_trash(trashed.pk, management.trash_confirmation(trashed))
        self.assertTrue(management.process_management(trash.pk))
        trashed.refresh_from_db()
        self.assertIsNotNone(trashed.trashed_at)

        paused, _, paused_operation = self.confirmed('paused.jpg', 'yellow')
        Job.objects.filter(pk=paused_operation.job_id).update(state='queued', next_attempt_at=timezone.now())
        paused_job = claim_job(paused_operation.job_id)
        self.assertIsNotNone(paused_job)

        def interrupt_claim(stage):
            if stage == 'claimed':
                raise OSError(errno.EIO, 'mixed restore interruption')

        with mock.patch('archive.services.archiving.archive_fault_hook', side_effect=interrupt_claim):
            self.assertFalse(process_archive(paused_job))
        paused_operation.refresh_from_db()
        self.assertEqual(paused_operation.stage, 'claimed')

        correction_photo, _, _ = self.completed('correction.jpg', 'purple')
        correction_photo.refresh_from_db()
        correction = management.accept_management(
            correction_photo.pk, correction_photo.metadata_version, uuid.uuid4(), 'capture',
            {'capture': None, 'mode': 'unknown'})
        self.assertEqual((correction.stage, correction.status), ('accepted', 'pending'))

        artifact, candidate = self._artifact()
        jpeg(self.inbox / 'queued-recognition.jpg', color='blue')
        scan_once()
        self._process_pending_imports()
        original = RecognitionRun.objects.get(
            source_observation__source__relative_path='queued-recognition.jpg')
        payload_digest = original.payload_digest
        source_fingerprint = original.source_fingerprint
        metadata_version = original.metadata_version

        with restored_backup(self):
            scan_once()
            current_source = ImportSource.objects.get(relative_path='queued-recognition.jpg')
            self.assertEqual(current_source.current_observation_id, original.source_observation_id)
            self.assertEqual(current_source.observed_version, original.source_version)
            restored_paused = ArchiveOperation.objects.get(pk=paused_operation.pk)
            self.assertTrue(process_archive(restored_paused.job))
            restored_paused.refresh_from_db()
            self.assertEqual(restored_paused.stage, 'complete')

            restored_correction = ManagementOperation.objects.get(pk=correction.pk)
            corrected = management.process_management(restored_correction.pk)
            restored_correction.refresh_from_db()
            self.assertTrue(corrected, (restored_correction.stage, restored_correction.status,
                                        restored_correction.error_code))
            self.assertEqual((restored_correction.stage, restored_correction.status),
                             ('completed', 'succeeded'))
            self.assertEqual(Photo.objects.get(pk=correction_photo.pk).capture_override_mode, 'unknown')

            for photo_id, original_managed in (
                    (completed.pk, completed_managed), (trashed.pk, trashed_managed)):
                current_photo = Photo.objects.get(pk=photo_id)
                current_managed = ManagedOriginal.objects.get(photo_id=photo_id)
                target = self.archive / current_managed.relative_path
                self.assertEqual((current_managed.sha256, current_managed.byte_size),
                                 (original_managed.sha256, original_managed.byte_size))
                self.assertEqual(current_photo.trashed_at is not None, photo_id == trashed.pk)
                self.assert_download(current_photo, target, current_managed)
            paused_managed = ManagedOriginal.objects.get(photo_id=paused.pk)
            self.assert_download(Photo.objects.get(pk=paused.pk),
                                 self.archive / paused_managed.relative_path, paused_managed)

            restored = RecognitionRun.objects.select_related('source_observation__source').get(
                source_observation__source__relative_path='queued-recognition.jpg')
            self.assertEqual((restored.state, restored.payload_digest, restored.source_fingerprint,
                              restored.metadata_version),
                             ('queued', payload_digest, source_fingerprint, metadata_version))
            self.assertNotEqual(restored.source_observation.fingerprint, restored.source_fingerprint)
            finished = self._dispatch(candidate, 'queued-recognition.jpg')
            self.assertEqual((finished.state, finished.payload_digest, finished.artifact_id),
                             ('succeeded', payload_digest, artifact.pk))
            self.assertEqual(finished.candidates.get().raw_label, candidate['raw_label'])

            rerun = recognition.request_rerun(finished.photo_id, metadata_version, uuid.uuid4())
            self.assertEqual(recognition.claim_next_run().pk, rerun.pk)
            Photo.objects.filter(pk=finished.photo_id).update(metadata_version=metadata_version + 1)
            self.assertFalse(recognition.publish_result(
                rerun.pk, [candidate], artifact_id=rerun.artifact_id))
            rerun.refresh_from_db()
            self.assertEqual((rerun.state, rerun.safe_error), ('stale', 'result_stale'))
            self.assertFalse(rerun.candidates.exists())

            restored_path = self.inbox / 'queued-recognition.jpg'
            content = restored_path.read_bytes()
            restored_path.unlink()
            restored_path.write_bytes(content)
            inbox_fd = open_root(self.inbox)
            try:
                with self.assertRaisesRegex(ValueError, 'source_changed'):
                    recognition.validate_source(finished, inbox_fd)
            finally:
                os.close(inbox_fd)

    def test_interrupted_running_recognition_retries_from_verified_restore(self):
        _, candidate = self._artifact()
        path = self.inbox / 'running-recognition.jpg'
        jpeg(path, color='green')
        scan_once()
        self._process_pending_imports()
        running = recognition.claim_next_run()
        self.assertEqual(running.state, 'running')

        with restored_backup(self):
            recognition.recover_runs(lock_held=True)
            run = RecognitionRun.objects.get()
            self.assertEqual(run.state, 'retry_wait')
            RecognitionRun.objects.filter(pk=run.pk).update(next_attempt_at=timezone.now())
            finished = self._dispatch(candidate, 'running-recognition.jpg')
            self.assertEqual((finished.state, finished.attempts, finished.cycle_attempts),
                             ('succeeded', 2, 2))
