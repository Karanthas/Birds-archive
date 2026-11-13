"""Synthetic JPEGs only: durable intent must never replace a human decision."""
import hashlib
import os
import tempfile
import uuid
from pathlib import Path

from django.apps import apps
from django.db import connection, transaction, IntegrityError
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings

from archive.models import Photo, Job, ArchiveOperation, PhotoSpecies, ManagedOriginal, ArchiveCompletion
from archive.services.confirmation import confirm_photo
from archive.services.intake import scan_once
from archive.services.jobs import process_source, claim_next
from archive.services.archiving import process_archive
from archive.services.paths import open_root
from archive.tests.fixtures import jpeg


class RecognitionFixture(TransactionTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='birds-recognition-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.inbox, self.data, self.archive, self.claims = [self.root/name for name in
            ('inbox', 'data', 'archive', 'claims')]
        for path in (self.inbox, self.data, self.archive, self.claims):
            path.mkdir(mode=0o700)
        config = override_settings(BIRDS_INBOX_ROOT=self.inbox, BIRDS_DATA_ROOT=self.data,
            BIRDS_ARCHIVE_ROOT=self.archive, BIRDS_CLAIM_ROOT=self.claims,
            BIRDS_HANDOFF_MODE='published', BIRDS_PUBLISHED_CONTRACT=True)
        config.enable()
        self.addCleanup(config.disable)

    def imported(self, name='one.jpg', color='red', content=None):
        path = self.inbox/name
        if content is None:
            jpeg(path, color=color)
        else:
            path.write_bytes(content)
        scan_once()
        inbox_fd, data_fd = open_root(self.inbox), open_root(self.data)
        try:
            for job in Job.objects.filter(kind='import', state='pending'):
                self.assertTrue(process_source(job, inbox_fd, data_fd))
        finally:
            os.close(inbox_fd)
            os.close(data_fd)
        return Photo.objects.get(observations__source__relative_path=name), path


class RecognitionImportTests(RecognitionFixture):
    def test_fifo_claim_is_single_inflight_and_backoff_does_not_block(self):
        from archive.services import recognition
        from archive.models import ModelArtifact, RecognitionRun
        artifact = ModelArtifact.objects.create(manifest_digest='f'*64, provider='test-only',
            version='1', revision='fifo', manifest={}, selection_state='selected')
        recognition.set_current_model(artifact.pk)
        first, _ = self.imported()
        second, _ = self.imported('two.jpg', 'blue')
        self.assertTrue(callable(getattr(recognition, 'claim_next_run', None)),
            'FIFO必须领取最早就绪识别且只允许一个in-flight')
        run = recognition.claim_next_run()
        self.assertEqual(run.photo_id, first.pk)
        self.assertIsNone(recognition.claim_next_run())
        recognition.fail_run(run.pk, 'temporarily_unavailable')
        newer = recognition.claim_next_run()
        self.assertEqual(newer.photo_id, second.pk)
        run.refresh_from_db()
        self.assertEqual((run.state, run.attempts, run.cycle_attempts), ('retry_wait', 1, 1))
        self.assertEqual(RecognitionRun.objects.count(), 2)

    def test_real_import_persists_unavailable_then_manual_archive_keeps_bytes(self):
        photo, path = self.imported()
        # Keep RED an actual behavioral assertion, even before the schema exists.
        models = {model.__name__: model for model in apps.get_app_config('archive').get_models()}
        self.assertIn('RecognitionRun', models, '新导入必须持久保存识别意图与未配置原因')
        run = models['RecognitionRun'].objects.get(photo=photo)
        self.assertEqual((run.state, run.safe_error), ('unavailable', 'model_not_configured'))
        self.assertIsNone(run.artifact_id)
        self.assertEqual((run.photo_sha256, run.byte_size, run.metadata_version),
            (photo.sha256, photo.byte_size, 1))
        self.assertEqual(run.source_observation_id, photo.observations.get().pk)
        self.assertEqual(run.source_fingerprint, photo.observations.get().fingerprint)
        self.assertEqual(models['RecognitionState'].objects.get(photo=photo).requested_generation, 1)
        self.assertFalse(models['RecognitionCandidate'].objects.exists())
        self.assertFalse(PhotoSpecies.objects.exists())
        self.assertFalse(ArchiveOperation.objects.exists())
        before = path.read_bytes()
        duplicate, duplicate_path = self.imported('duplicate.jpg', content=before)
        self.assertEqual(duplicate.pk, photo.pk)
        self.assertEqual(models['RecognitionRun'].objects.count(), 1)
        self.assertEqual(duplicate_path.read_bytes(), before)
        operation, _ = confirm_photo(photo.pk, 1, uuid.uuid4(), 'species', [], ['池边小鸟'])
        self.assertTrue(process_archive(claim_next()))
        photo.refresh_from_db()
        managed = ManagedOriginal.objects.get(photo=photo)
        self.assertEqual(photo.state, 'archived')
        self.assertEqual((self.archive/managed.relative_path).read_bytes(), before)
        self.assertEqual(managed.sha256, hashlib.sha256(before).hexdigest())
        self.assertEqual(managed.byte_size, len(before))
        self.assertEqual(photo.confirmed_species.get().species.local_name, '池边小鸟')
        self.assertTrue(ArchiveCompletion.objects.filter(photo=photo, operation=operation).exists())
        self.assertFalse(path.exists())
        self.assertEqual(duplicate_path.read_bytes(), before)

    def test_new_model_selection_only_applies_to_future_imports(self):
        from archive.models import ModelArtifact, RecognitionRun
        from archive.services.recognition import set_current_model
        old, _ = self.imported()
        artifact = ModelArtifact.objects.create(manifest_digest='a'*64, provider='synthetic-only',
            version='1', revision='test', manifest={'synthetic': True}, selection_state='selected')
        set_current_model(artifact.pk)
        new, _ = self.imported('new.jpg', 'blue')
        old_run, new_run = RecognitionRun.objects.get(photo=old), RecognitionRun.objects.get(photo=new)
        self.assertEqual((old_run.state, old_run.artifact_id), ('unavailable', None))
        self.assertEqual((new_run.state, new_run.artifact_id), ('queued', artifact.pk))
        self.assertEqual(RecognitionRun.objects.count(), 2)

    def test_replaying_import_intent_is_idempotent(self):
        from archive.models import RecognitionRun
        from archive.services.recognition import enqueue_import
        photo, _ = self.imported()
        first = RecognitionRun.objects.get(photo=photo)
        replay = enqueue_import(photo, photo.observations.get())
        self.assertEqual(replay.pk, first.pk)
        self.assertEqual(RecognitionRun.objects.count(), 1)

    def test_artifact_identity_and_unique_current_are_protected(self):
        from archive.models import ModelArtifact
        from archive.services.recognition import set_current_model
        first = ModelArtifact.objects.create(manifest_digest='a'*64, provider='synthetic',
            version='1', revision='one', manifest={'synthetic': True})
        with self.assertRaises(ValueError):
            set_current_model(first.pk)
        first.selection_state = 'selected'
        first.save(update_fields=['selection_state'])
        set_current_model(first.pk)
        second = ModelArtifact.objects.create(manifest_digest='b'*64, provider='synthetic',
            version='2', revision='two', manifest={}, selection_state='selected')
        with self.assertRaises(IntegrityError), transaction.atomic():
            ModelArtifact.objects.filter(pk=second.pk).update(is_current=True)
        set_current_model(second.pk)
        self.assertEqual(list(ModelArtifact.objects.filter(is_current=True).values_list('pk', flat=True)), [second.pk])
        first.version = 'changed'
        with self.assertRaises(ValueError):
            first.save()

    def test_generation_and_request_uniqueness(self):
        from archive.models import RecognitionRun
        photo, _ = self.imported()
        run = RecognitionRun.objects.get(photo=photo)
        fields = {field.name: getattr(run, field.name) for field in run._meta.fields
            if field.name not in ('id', 'created_at', 'updated_at')}
        with self.assertRaises(IntegrityError), transaction.atomic():
            RecognitionRun.objects.create(**{**fields, 'request_id': uuid.uuid4()})
        with self.assertRaises(IntegrityError), transaction.atomic():
            RecognitionRun.objects.create(**{**fields, 'generation': 2})

    def test_duplicate_of_history_without_recognition_does_not_backfill(self):
        from archive.models import RecognitionRun, RecognitionState
        temporary = self.inbox/'fixture.jpg'
        jpeg(temporary, color='blue')
        content = temporary.read_bytes()
        temporary.unlink()
        old = Photo.objects.create(sha256=hashlib.sha256(content).hexdigest(), byte_size=len(content),
            preview_key='historical-preview')
        photo, _ = self.imported(content=content)
        self.assertEqual(photo.pk, old.pk)
        self.assertFalse(RecognitionState.objects.exists())
        self.assertFalse(RecognitionRun.objects.exists())


class RecognitionMigrationTests(RecognitionFixture):
    def test_reversible_additive_migration_preserves_archive_and_manual_records(self):
        photo, path = self.imported()
        before = path.read_bytes()
        operation, _ = confirm_photo(photo.pk, 1, uuid.uuid4(), 'species', [], ['迁移测试鸟'])
        self.assertTrue(process_archive(claim_next()))
        ids = (photo.pk, operation.pk, operation.job_id, photo.confirmed_species.get().species_id)
        saved = {model.__name__: list(model.objects.values()) for model in
            (Photo, Job, PhotoSpecies, ArchiveOperation, ManagedOriginal, ArchiveCompletion)}
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(latest))
        executor.migrate([('archive', '0006_local_species')])
        self.assertNotIn('archive_recognitionrun', connection.introspection.table_names())
        MigrationExecutor(connection).migrate([('archive', '0007_recognition')])
        from archive.models import RecognitionRun, RecognitionState
        self.assertFalse(RecognitionRun.objects.exists(), '迁移不得给历史图库排队')
        self.assertFalse(RecognitionState.objects.exists())
        for model in (Photo, Job, PhotoSpecies, ArchiveOperation, ManagedOriginal, ArchiveCompletion):
            self.assertEqual(list(model.objects.values()), saved[model.__name__])
        self.assertEqual(ids, (photo.pk, operation.pk, operation.job_id, photo.confirmed_species.get().species_id))
        self.assertEqual((self.archive/ManagedOriginal.objects.get(photo=photo).relative_path).read_bytes(), before)


class RecognitionQueueTests(RecognitionFixture):
    def setUp(self):
        super().setUp()
        from archive.models import ModelArtifact
        from archive.services.recognition import set_current_model
        self.artifact = ModelArtifact.objects.create(manifest_digest='a'*64, provider='test-only',
            version='1', revision='queue', manifest={}, selection_state='selected')
        set_current_model(self.artifact.pk)

    def test_retry_whitelist_budget_and_cumulative_history(self):
        from archive.models import RecognitionRun
        from archive.services.recognition import claim_next_run, fail_run
        from django.utils import timezone
        from datetime import timedelta
        self.imported()
        now = timezone.now()
        with override_settings(RECOGNITION_RETRY_LIMIT=3, RECOGNITION_RETRY_BASE_MS=1000):
            for attempt in range(1, 4):
                run = claim_next_run(now)
                self.assertEqual(run.attempts, attempt)
                fail_run(run.pk, 'temporarily_unavailable', now)
                run.refresh_from_db()
                self.assertEqual(run.state, 'retry_wait' if attempt < 3 else 'failed')
                if attempt < 3:
                    self.assertEqual(run.next_attempt_at-now, timedelta(seconds=2**(attempt-1)))
                    self.assertIsNone(claim_next_run(now))
                now += timedelta(seconds=10)
        self.assertEqual(RecognitionRun.objects.get().cycle_attempts, 3)

    def test_permanent_errors_and_unknown_private_errors_do_not_retry(self):
        from archive.services.recognition import claim_next_run, fail_run
        from archive.models import RecognitionRun
        for index, code in enumerate(('model_package_missing', 'model_package_invalid', 'source_permission',
                'model_memory_exceeded', 'model_timeout', '/private/secret', {}, [])):
            with self.subTest(code=code):
                photo, _ = self.imported(str(index)+'.jpg', color=(index*30, 50, 80))
                run = claim_next_run()
                fail_run(run.pk, code)
                run.refresh_from_db()
                self.assertIn(run.state, ('failed', 'unavailable'))
                self.assertNotIn('/private', run.safe_error)
                self.assertIsNone(claim_next_run())

    def test_recovery_requires_lock_and_rejects_changed_human_version(self):
        from archive.services.recognition import claim_next_run, recover_runs
        from archive.models import RecognitionRun
        photo, _ = self.imported()
        run = claim_next_run()
        with self.assertRaises(ValueError):
            recover_runs()
        recover_runs(lock_held=True)
        run.refresh_from_db()
        self.assertEqual((run.state, run.attempts), ('retry_wait', 1))
        RecognitionRun.objects.filter(pk=run.pk).update(state='running')
        Photo.objects.filter(pk=photo.pk).update(metadata_version=2)
        recover_runs(lock_held=True)
        run.refresh_from_db()
        self.assertEqual(run.state, 'stale')

    def test_old_ready_item_wins_after_continuous_new_imports(self):
        from archive.services.recognition import claim_next_run, fail_run
        from archive.models import RecognitionRun
        from django.utils import timezone
        old, _ = self.imported()
        run = claim_next_run()
        fail_run(run.pk, 'database_busy')
        age = run.queue_age
        for index in range(5):
            self.imported(str(index)+'.jpg', color=(index*30, 10, 20))
        RecognitionRun.objects.filter(pk=run.pk).update(next_attempt_at=timezone.now())
        next_run = claim_next_run()
        self.assertEqual((next_run.photo_id, next_run.queue_age), (old.pk, age))

    def test_input_is_anonymous_readonly_exact_bytes_and_replacement_is_rejected(self):
        import fcntl
        from archive.services.recognition import claim_next_run, prepare_input, validate_source
        photo, path = self.imported()
        run = claim_next_run()
        before = path.read_bytes()
        inbox_fd, data_fd = open_root(self.inbox), open_root(self.data)
        try:
            with prepare_input(run, inbox_fd, data_fd) as stream:
                self.assertEqual(stream.read(), before)
                self.assertEqual(os.fstat(stream.fileno()).st_nlink, 0)
                self.assertEqual(fcntl.fcntl(stream.fileno(), fcntl.F_GETFL) & os.O_ACCMODE, os.O_RDONLY)
                with self.assertRaises(OSError):
                    os.write(stream.fileno(), b'bad')
            self.assertFalse(list(self.data.glob('.recognition-*')))
            self.assertEqual(path.read_bytes(), before)
            replacement = self.inbox/'replacement.jpg'
            jpeg(replacement, color='blue')
            replacement.replace(path)
            with self.assertRaises(ValueError):
                validate_source(run, inbox_fd)
        finally:
            os.close(inbox_fd)
            os.close(data_fd)

    def test_explicit_rerun_preserves_snapshot_and_old_queued_model(self):
        from archive.services.recognition import claim_next_run, publish_result, request_rerun, set_current_model
        from archive.models import ModelArtifact, RecognitionState, RecognitionRun
        photo, _ = self.imported()
        run = claim_next_run()
        output = [{'rank': 1, 'raw_label': 'old candidate', 'score': .5, 'species_id': None,
            'mapping_status': 'unmapped', 'mapping_evidence': ''}]
        self.assertTrue(publish_result(run.pk, output, artifact_id=run.artifact_id))
        old_queued, _ = self.imported('queued.jpg', 'blue')
        newer = ModelArtifact.objects.create(manifest_digest='b'*64, provider='test-only',
            version='2', revision='next', manifest={}, selection_state='selected')
        set_current_model(newer.pk)
        newest, _ = self.imported('newest.jpg', 'green')
        self.assertEqual(old_queued.recognition_runs.get().artifact_id, self.artifact.pk)
        self.assertEqual(newest.recognition_runs.get().artifact_id, newer.pk)
        key = uuid.uuid4()
        rerun = request_rerun(photo.pk, 1, key)
        self.assertEqual(request_rerun(photo.pk, 1, key).pk, rerun.pk)
        self.assertEqual((rerun.generation, rerun.artifact_id), (2, newer.pk))
        self.assertEqual(RecognitionState.objects.get(photo=photo).active_run_id, run.pk)
        RecognitionRun.objects.filter(pk=rerun.pk).update(state='failed')
        self.assertEqual(RecognitionState.objects.get(photo=photo).active_run_id, run.pk)
        with self.assertRaises(ValueError):
            request_rerun(photo.pk, 2, key)

    def test_retry_is_new_idempotent_frozen_cycle_with_cumulative_attempts(self):
        from archive.services.recognition import claim_next_run, retry_run, set_current_model
        from archive.models import ModelArtifact, RecognitionRun
        photo, _ = self.imported()
        run = claim_next_run()
        RecognitionRun.objects.filter(pk=run.pk).update(state='failed', safe_error='temporarily_unavailable',
            attempts=3, cycle_attempts=3)
        newer = ModelArtifact.objects.create(manifest_digest='b'*64, provider='test-only',
            version='2', revision='next', manifest={}, selection_state='selected')
        set_current_model(newer.pk)
        key = uuid.uuid4()
        retry = retry_run(photo.pk, 1, key)
        self.assertEqual((retry.artifact_id, retry.attempts, retry.cycle_attempts), (run.artifact_id, 3, 0))
        self.assertEqual(retry_run(photo.pk, 1, key).pk, retry.pk)
        claimed = claim_next_run()
        self.assertEqual((claimed.attempts, claimed.cycle_attempts), (4, 1))
        self.assertEqual(RecognitionRun.objects.get(pk=run.pk).state, 'failed')

    def test_retry_rejects_permanent_errors_and_changed_source(self):
        from archive.services.recognition import claim_next_run, retry_run, request_rerun
        from archive.models import RecognitionRun
        photo, _ = self.imported()
        run = claim_next_run()
        RecognitionRun.objects.filter(pk=run.pk).update(state='failed', safe_error='source_changed')
        with self.assertRaises(ValueError):
            retry_run(photo.pk, 1, uuid.uuid4())
        rerun = request_rerun(photo.pk, 1, uuid.uuid4())
        self.assertEqual(rerun.generation, 2)
        confirm_photo(photo.pk, 1, uuid.uuid4(), 'undetermined', [], [])
        with self.assertRaises(ValueError):
            request_rerun(photo.pk, 2, uuid.uuid4())

    def test_repeated_child_exit_stops_after_one_automatic_retry(self):
        from archive.services.recognition import claim_next_run, fail_run
        from django.utils import timezone
        from datetime import timedelta
        self.imported()
        run = claim_next_run()
        fail_run(run.pk, 'child_exited')
        again = claim_next_run(timezone.now()+timedelta(seconds=10))
        fail_run(again.pk, 'child_exited')
        again.refresh_from_db()
        self.assertEqual((again.state, again.cycle_attempts), ('failed', 2))


class RecognitionPublicationTests(RecognitionFixture):
    """DB publication contract only; candidates are explicitly synthetic, not model output."""
    def setUp(self):
        super().setUp()
        from archive.models import ModelArtifact, RecognitionRun
        from archive.services.recognition import set_current_model
        self.artifact = ModelArtifact.objects.create(manifest_digest='c'*64, provider='synthetic-test-only',
            version='1', revision='unit-test', manifest={'synthetic': True}, selection_state='selected')
        set_current_model(self.artifact.pk)
        self.photo, self.path = self.imported()
        self.run = RecognitionRun.objects.get(photo=self.photo)
        RecognitionRun.objects.filter(pk=self.run.pk).update(state='running', attempts=1, cycle_attempts=1)
        self.run.refresh_from_db()
        self.output = [{'rank': 1, 'raw_label': 'Unknown test label', 'score': 0.7,
            'species_id': None, 'mapping_status': 'unmapped', 'mapping_evidence': ''}]

    def publish(self, run=None, output=None, artifact_id=None):
        from archive.services import recognition
        publish = getattr(recognition, 'publish_result', None)
        self.assertTrue(callable(publish), '识别发布必须以版本校验拒绝晚到结果')
        return publish((run or self.run).pk, self.output if output is None else output,
            artifact_id=self.artifact.pk if artifact_id is None else artifact_id)

    def test_late_result_after_human_confirmation_never_overwrites_manual_decision(self):
        before = self.path.read_bytes()
        operation, _ = confirm_photo(self.photo.pk, 1, uuid.uuid4(), 'species', [], ['人工鸟'])
        saved = list(PhotoSpecies.objects.values())
        self.assertFalse(self.publish())
        self.photo.refresh_from_db()
        self.run.refresh_from_db()
        self.assertEqual((self.photo.state, self.photo.metadata_version), ('confirmed', 2))
        self.assertEqual(self.run.state, 'stale')
        self.assertEqual(list(PhotoSpecies.objects.values()), saved)
        self.assertEqual(ArchiveOperation.objects.get().pk, operation.pk)
        self.assertFalse(self.run.candidates.exists())
        self.assertEqual(self.path.read_bytes(), before)

    def test_valid_result_publishes_unknown_label_without_human_or_file_side_effects(self):
        from archive.models import RecognitionState
        before = self.path.read_bytes()
        self.assertTrue(self.publish())
        self.run.refresh_from_db()
        candidate = self.run.candidates.get()
        self.assertEqual((candidate.raw_label, candidate.species_id, candidate.mapping_status),
            ('Unknown test label', None, 'unmapped'))
        self.assertEqual(self.run.state, 'succeeded')
        self.assertEqual(RecognitionState.objects.get(photo=self.photo).active_run_id, self.run.pk)
        self.assertFalse(PhotoSpecies.objects.exists())
        self.assertFalse(ArchiveOperation.objects.exists())
        self.photo.refresh_from_db()
        self.assertEqual((self.photo.state, self.photo.metadata_version), ('review_pending', 1))
        self.assertEqual(self.path.read_bytes(), before)

    def test_actual_top_three_and_catalog_mapping_keep_provenance(self):
        from archive.models import Species, SourceSnapshot
        snapshot = SourceSnapshot.objects.create(source='synthetic', version='1', sha256='e'*64,
            acquired_at='2026-10-17', download_page_url='https://example.org/catalog',
            license_url='https://example.org/license', attribution='synthetic', row_count=1,
            species_count=1, manifest={})
        species = Species.objects.create(scientific_name='Ardea alba', scientific_key='ardea alba',
            chinese_name='', search_key='ardea alba', source_snapshot=snapshot)
        output = [{**self.output[0], 'raw_label': 'Ardea alba', 'score': 0.8,
            'species_id': str(species.pk), 'mapping_status': 'mapped', 'mapping_evidence': 'synthetic concept mapping'},
            {**self.output[0], 'rank': 2, 'raw_label': '<Unknown second>', 'score': 0.6},
            {**self.output[0], 'rank': 3, 'raw_label': 'Unknown third', 'score': -0.1}]
        self.assertTrue(self.publish(output=output))
        self.assertEqual(list(self.run.candidates.values_list('rank', flat=True)), [1, 2, 3])
        mapped = self.run.candidates.get(rank=1)
        self.assertEqual((mapped.species_id, mapped.mapping_evidence), (species.pk, 'synthetic concept mapping'))
        self.assertEqual(self.run.candidates.get(rank=2).raw_label, '<Unknown second>')
        self.assertFalse(PhotoSpecies.objects.exists())

    def test_each_frozen_source_and_photo_identity_conflict_is_stale(self):
        from archive.models import RecognitionState, SourceObservation, ImportSource
        observation = self.run.source_observation
        cases = [(Photo, self.photo.pk, values) for values in (
            {'metadata_version': 2}, {'sha256': 'd'*64}, {'byte_size': 1},
            *({'state': state} for state in ('confirmed', 'archiving', 'archived', 'archive_failed', 'trashed')))]
        cases += [(SourceObservation, observation.pk, values) for values in (
            {'available': False}, {'photo_id': None}, {'version': 2}, {'sha256': 'd'*64},
            {'byte_size': 1}, {'fingerprint': {'ino': 999}})]
        cases += [(ImportSource, observation.source_id, values) for values in (
            {'current_observation_id': None}, {'observed_version': 2}, {'photo_id': None},
            {'sha256': 'd'*64}, {'fingerprint': {'ino': 999}}, {'state': 'unavailable'})]
        state = RecognitionState.objects.get(photo=self.photo)
        cases.append((RecognitionState, state.pk, {'requested_generation': 2}))
        for model, ident, values in cases:
            with self.subTest(model=model.__name__, values=values), transaction.atomic():
                model.objects.filter(pk=ident).update(**values)
                self.assertFalse(self.publish())
                self.run.refresh_from_db()
                self.assertEqual((self.run.state, self.run.safe_error), ('stale', 'result_stale'))
                self.assertFalse(self.run.candidates.exists())
                self.assertIsNone(RecognitionState.objects.get(photo=self.photo).active_run_id)
                self.assertFalse(ArchiveOperation.objects.exists())
                transaction.set_rollback(True)

    def next_generation(self):
        from archive.models import RecognitionRun, RecognitionState
        from archive.services.recognition import run_payload_digest
        first = RecognitionRun.objects.get(pk=self.run.pk)
        values = {field.name: getattr(first, field.name) for field in first._meta.fields
            if field.name not in ('id', 'created_at', 'updated_at')}
        values.update(generation=first.generation+1, request_id=uuid.uuid4(), state='running', reason='explicit_retry')
        run = RecognitionRun(**values)
        run.payload_digest = run_payload_digest(run)
        run.save()
        RecognitionState.objects.filter(photo=self.photo).update(requested_generation=run.generation)
        return run

    def test_new_generation_failure_retains_previous_candidates_and_active_pointer(self):
        from archive.models import RecognitionState
        self.assertTrue(self.publish())
        saved = list(self.run.candidates.values())
        new = self.next_generation()
        self.assertFalse(self.publish(run=new, output=[{**self.output[0], 'score': float('nan')}]))
        self.assertEqual(RecognitionState.objects.get(photo=self.photo).active_run_id, self.run.pk)
        self.assertEqual(list(self.run.candidates.values()), saved)
        new.refresh_from_db()
        self.assertEqual((new.state, new.safe_error), ('failed', 'invalid_result'))

    def test_out_of_order_and_changed_duplicate_results_cannot_replace_current_snapshot(self):
        from archive.models import RecognitionState
        new = self.next_generation()
        output = [{**self.output[0], 'raw_label': 'New generation'}]
        self.assertTrue(self.publish(run=new, output=output))
        self.assertFalse(self.publish())
        self.assertTrue(self.publish(run=new, output=output))
        self.assertFalse(self.publish(run=new, output=self.output))
        self.assertEqual(new.candidates.count(), 1)
        self.assertEqual(new.candidates.get().raw_label, 'New generation')
        self.assertEqual(RecognitionState.objects.get(photo=self.photo).active_run_id, new.pk)

    def test_invalid_output_is_rejected_atomically(self):
        invalid = []
        invalid.extend([None, {}, [], self.output*4, self.output*2,
            [{**self.output[0], 'raw_label': 'x'*301}], [{**self.output[0], 'raw_label': ''}],
            [{**self.output[0], 'raw_label': 'bad\x00label'}],
            [{**self.output[0], 'mapping_status': 'mapped'}],
            [{**self.output[0], 'mapping_status': 'invented'}],
            [{**self.output[0], 'mapping_evidence': 'x'*1001}],
            [{**self.output[0], 'species_id': str(uuid.uuid4()), 'mapping_status': 'mapped'}],
            [self.output[0], {**self.output[0], 'rank': 2, 'score': float('nan')}],
            [{**self.output[0], 'extra': 'untrusted'}]])
        invalid.extend([[{**self.output[0], 'score': value}] for value in
            (float('nan'), float('inf'), -float('inf'), True, '0.7', None, 10**1000)])
        invalid.extend([[{**self.output[0], 'rank': value}] for value in (0, 4, 1.5, True, '1')])
        for output in invalid:
            with self.subTest(output=repr(output)[:100]), transaction.atomic():
                # None here is an explicitly malformed payload, not the helper's default.
                from archive.services.recognition import publish_result
                self.assertFalse(publish_result(self.run.pk, output, artifact_id=self.artifact.pk))
                self.run.refresh_from_db()
                self.assertEqual(self.run.safe_error, 'invalid_result')
                self.assertFalse(self.run.candidates.exists())
                self.assertFalse(PhotoSpecies.objects.exists())
                self.assertFalse(ArchiveOperation.objects.exists())
                transaction.set_rollback(True)

    def test_artifact_argument_and_frozen_package_identity_must_match(self):
        from archive.models import ModelArtifact, RecognitionRun
        for model, ident, changes in ((ModelArtifact, self.artifact.pk, {'manifest_digest': 'd'*64}),
                (RecognitionRun, self.run.pk, {'payload_digest': '0'*64}),
                (RecognitionRun, self.run.pk, {'artifact_id': None})):
            with self.subTest(model=model.__name__, changes=changes), transaction.atomic():
                model.objects.filter(pk=ident).update(**changes)
                self.assertFalse(self.publish())
                self.run.refresh_from_db()
                self.assertEqual(self.run.state, 'stale')
                transaction.set_rollback(True)
        self.assertFalse(self.publish(artifact_id=uuid.uuid4()))

    def test_current_model_switch_does_not_invalidate_frozen_old_package(self):
        from archive.models import ModelArtifact
        from archive.services.recognition import set_current_model
        newer = ModelArtifact.objects.create(manifest_digest='d'*64, provider='synthetic-test-only',
            version='2', revision='two', manifest={}, selection_state='selected')
        set_current_model(newer.pk)
        self.assertTrue(self.publish())
        self.assertEqual(self.run.candidates.count(), 1)

    def test_cross_photo_active_pointer_is_rejected_and_never_exposed(self):
        from archive.models import RecognitionState, RecognitionRun
        from archive.services.recognition import recognition_summary
        other, _ = self.imported('other.jpg', 'blue')
        foreign = RecognitionRun.objects.get(photo=other)
        RecognitionState.objects.filter(photo=self.photo).update(active_run=foreign)
        self.assertIsNone(recognition_summary(self.photo)['active_run'])
        self.assertFalse(self.publish())
        self.assertFalse(self.run.candidates.exists())

    def test_two_connections_publish_only_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from django.db import close_old_connections
        from archive.services.recognition import publish_result
        def submit(_):
            close_old_connections()
            try:
                return publish_result(self.run.pk, self.output, artifact_id=self.artifact.pk)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(submit, range(2))), [True, True])
        self.assertEqual(self.run.candidates.count(), 1)

    def test_concurrent_different_outputs_have_one_winner_and_no_partial_snapshot(self):
        from concurrent.futures import ThreadPoolExecutor
        from django.db import close_old_connections
        from archive.services.recognition import publish_result
        outputs = [self.output, [{**self.output[0], 'raw_label': 'Competing output'}]]
        def submit(output):
            close_old_connections()
            try:
                return publish_result(self.run.pk, output, artifact_id=self.artifact.pk)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(submit, outputs)), [False, True])
        self.assertEqual(self.run.candidates.count(), 1)
