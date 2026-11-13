"""Batch decisions use synthetic JPEGs and isolated roots only."""
import hashlib
import uuid
from unittest import mock
from datetime import timedelta
from importlib.util import find_spec

from django.apps import apps
from django.db import transaction, OperationalError
from django.test import Client
from django.utils import timezone
from archive.models import (ModelArtifact, RecognitionRun, RecognitionState, RecognitionCandidate,
    SourceSnapshot, Species, Photo, ArchiveOperation, ManagedOriginal, ArchiveCompletion)
from archive.tests.test_recognition_state import RecognitionFixture
from archive.services.jobs import claim_next
from archive.services.archiving import process_archive


class BatchReviewTests(RecognitionFixture):
    def pair(self):
        snapshot = SourceSnapshot.objects.create(source='synthetic-batch', version='1', sha256='b'*64,
            acquired_at='2026-10-17', download_page_url='https://example.org/catalog',
            license_url='https://example.org/license', attribution='synthetic', row_count=2,
            species_count=2, manifest={})
        species = [Species.objects.create(scientific_name=f'Birdus {n}', scientific_key=f'birdus {n}',
            chinese_name=f'鸟{n}', search_key=f'birdus {n} 鸟{n}', source_snapshot=snapshot) for n in (1, 2)]
        policy = dict(score_kind='similarity', label_digest='a'*64, threshold_version='synthetic-v1',
            margin_threshold=0.1, calibration_evidence='synthetic contract, not model evidence',
            prompt_version='synthetic-prompt')
        artifact = ModelArtifact.objects.create(manifest_digest='d'*64, provider='synthetic-only',
            version='1', revision='fixed', manifest=policy, selection_state='selected')
        pairs = [self.imported(), self.imported('two.jpg', 'blue')]
        for index, (photo, _) in enumerate(pairs):
            run = RecognitionRun.objects.get(photo=photo)
            run.state, run.artifact = 'succeeded', artifact
            run.save(update_fields=['state', 'artifact'])
            RecognitionState.objects.filter(photo=photo).update(active_run=run)
            for rank, item in enumerate([species[index], species[1-index]], 1):
                RecognitionCandidate.objects.create(run=run, rank=rank, species=item,
                    raw_label=item.scientific_name, score=0.9 if rank == 1 else 0.2,
                    mapping_status='mapped', mapping_evidence='synthetic exact concept')
        return pairs, species

    def service(self):
        self.assertIsNotNone(find_spec('archive.services.batch_review'), '必须有持久冻结预览和逐行确认边界')
        from archive.services import batch_review
        return batch_review

    def test_two_own_candidates_archive_original_bytes_and_replay_receipts(self):
        pairs, species = self.pair()
        service = self.service()
        preview = service.build_preview([str(photo.pk) for photo, _ in pairs], mode='own')
        self.assertEqual(len(preview.frozen_rows), 2)
        self.assertEqual([row['final_species_ids'] for row in preview.frozen_rows], [[str(s.pk)] for s in species])
        self.assertFalse(ArchiveOperation.objects.exists())
        token = service.preview_token(preview)
        results = service.submit_preview(preview.pk, token)
        self.assertEqual([r['status'] for r in results], ['accepted', 'accepted'])
        for photo, path in pairs:
            before = path.read_bytes()
            self.assertTrue(process_archive(claim_next()))
            photo.refresh_from_db()
            managed = ManagedOriginal.objects.get(photo=photo)
            actual = (self.archive/managed.relative_path).read_bytes()
            self.assertEqual(actual, before)
            self.assertEqual(hashlib.sha256(actual).hexdigest(), photo.sha256)
            self.assertTrue(ArchiveCompletion.objects.filter(photo=photo).exists())
        self.assertEqual(service.submit_preview(preview.pk, token), results)
        self.assertEqual(ArchiveOperation.objects.count(), 2)

    def test_changed_row_skipped_while_other_archives(self):
        pairs, _ = self.pair()
        service = self.service()
        preview = service.build_preview([str(photo.pk) for photo, _ in pairs], mode='own')
        Photo.objects.filter(pk=pairs[0][0].pk).update(metadata_version=2)
        results = service.submit_preview(preview.pk, service.preview_token(preview))
        self.assertEqual([r['status'] for r in results], ['skipped', 'accepted'])
        before = pairs[1][1].read_bytes()
        self.assertTrue(process_archive(claim_next()))
        managed = ManagedOriginal.objects.get(photo=pairs[1][0])
        self.assertEqual((self.archive/managed.relative_path).read_bytes(), before)
        self.assertTrue(pairs[0][1].exists())

    def test_explicit_selection_bounds_and_missing_row_preserved(self):
        pairs, _ = self.pair()
        service = self.service()
        for ids in ([], [str(uuid.uuid4())]*201, ['invalid'], [str(pairs[0][0].pk)]*2):
            with self.assertRaises(ValueError):
                service.build_preview(ids, mode='own')
        with self.assertRaises(ValueError):
            service.build_preview([str(pairs[0][0].pk)], mode='implicit')
        missing = str(uuid.uuid4())
        preview = service.build_preview([missing, str(pairs[0][0].pk)], mode='own')
        self.assertEqual(preview.selection_ids, [missing, str(pairs[0][0].pk)])
        self.assertEqual([r['status'] for r in service.submit_preview(preview.pk, service.preview_token(preview))],
            ['skipped', 'accepted'])

    def test_unknown_close_unmapped_and_failed_are_never_undetermined(self):
        pairs, _ = self.pair()
        service = self.service()
        photo = pairs[0][0]
        run = RecognitionRun.objects.get(photo=photo)
        cases = [lambda: ModelArtifact.objects.filter(pk=run.artifact_id).update(manifest={}),
            lambda: RecognitionCandidate.objects.filter(run=run, rank=2).update(score=0.85),
            lambda: RecognitionCandidate.objects.filter(run=run, rank=1).update(mapping_status='unmapped', species=None),
            lambda: RecognitionRun.objects.filter(pk=run.pk).update(state='failed'),
            lambda: RecognitionCandidate.objects.filter(run=run).delete(),
            lambda: RecognitionCandidate.objects.filter(run=run, rank=2).delete()]
        for mutate in cases:
            with self.subTest(mutate=mutate), transaction.atomic():
                mutate()
                preview = service.build_preview([str(photo.pk)], mode='own')
                self.assertEqual(preview.frozen_rows[0]['final_species_ids'], [])
                self.assertEqual(service.submit_preview(preview.pk, service.preview_token(preview))[0]['status'], 'skipped')
                self.assertFalse(ArchiveOperation.objects.exists())
                transaction.set_rollback(True)

    def test_all_frozen_identity_changes_skip_only_changed_row(self):
        pairs, _ = self.pair()
        service = self.service()
        photo = pairs[0][0]
        run = RecognitionRun.objects.get(photo=photo)
        observation = photo.observations.get()
        from archive.models import SourceObservation, ImportSource
        mutations = [lambda: Photo.objects.filter(pk=photo.pk).update(metadata_version=2),
            lambda: Photo.objects.filter(pk=photo.pk).update(state='confirmed'),
            lambda: SourceObservation.objects.filter(pk=observation.pk).update(fingerprint={'changed': 1}),
            lambda: SourceObservation.objects.filter(pk=observation.pk).update(available=False),
            lambda: ImportSource.objects.filter(pk=observation.source_id).update(observed_version=2),
            lambda: ImportSource.objects.filter(pk=observation.source_id).update(current_observation=None),
            lambda: RecognitionState.objects.filter(photo=photo).update(requested_generation=2),
            lambda: RecognitionState.objects.filter(photo=photo).update(active_run=None),
            lambda: RecognitionCandidate.objects.filter(run=run, rank=1).update(score=0.8)]
        for mutate in mutations:
            with self.subTest(mutate=mutate), transaction.atomic():
                preview = service.build_preview([str(p.pk) for p, _ in pairs], mode='own')
                mutate()
                result = service.submit_preview(preview.pk, service.preview_token(preview))
                self.assertEqual([r['status'] for r in result], ['skipped', 'accepted'])
                transaction.set_rollback(True)

    def test_changed_shared_policy_invalidates_both_frozen_rows(self):
        pairs, _ = self.pair()
        service = self.service()
        preview = service.build_preview([str(p.pk) for p, _ in pairs], mode='own')
        ModelArtifact.objects.update(manifest={'threshold_version': 'changed'})
        self.assertEqual([r['status'] for r in service.submit_preview(preview.pk, service.preview_token(preview))],
            ['skipped', 'skipped'])

    def test_unknown_failure_rolls_back_current_row_and_original_preview_can_resume(self):
        pairs, _ = self.pair()
        service = self.service()
        preview = service.build_preview([str(p.pk) for p, _ in pairs], mode='own')
        real = service._record
        def fail_accepted(preview, row, status, reason, operation=None):
            result = real(preview, row, status, reason, operation)
            if row['photo_id'] == str(pairs[1][0].pk) and status == 'accepted':
                raise RuntimeError('synthetic failure after confirm')
            return result
        token = service.preview_token(preview)
        with self.assertLogs('archive.services.batch_review', level='ERROR') as logs:
            with mock.patch.object(service, '_record', side_effect=fail_accepted):
                with self.assertRaisesRegex(RuntimeError, 'synthetic failure after confirm'):
                    service.submit_preview(preview.pk, token)
        record = logs.records[0]
        self.assertIsInstance(record.exc_info[1], RuntimeError)
        for value in (str(preview.pk), str(pairs[1][0].pk), preview.frozen_rows[1]['request_id']):
            self.assertIn(value, record.getMessage())
        self.assertNotIn(token, record.getMessage())
        first, second = [p for p, _ in pairs]
        first.refresh_from_db(); second.refresh_from_db()
        self.assertEqual(first.state, 'confirmed')
        self.assertEqual(second.state, 'review_pending')
        self.assertEqual(preview.results.count(), 1)
        self.assertEqual(preview.results.get().status, 'accepted')
        self.assertFalse(preview.results.filter(photo_id=second.pk).exists())
        self.assertFalse(second.confirmed_species.exists())
        self.assertFalse(ArchiveOperation.objects.filter(photo=second).exists())
        first_operation = ArchiveOperation.objects.get(photo=first)
        results = service.submit_preview(preview.pk, token)
        self.assertEqual([r['status'] for r in results], ['accepted', 'accepted'])
        self.assertEqual(results[0]['operation_id'], str(first_operation.pk))
        self.assertEqual(ArchiveOperation.objects.count(), 2)
        self.assertEqual(preview.results.count(), 2)
        self.assertEqual(service.submit_preview(preview.pk, token), results)
        for photo, path in pairs:
            original = path.read_bytes()
            self.assertTrue(process_archive(claim_next()))
            managed = ManagedOriginal.objects.get(photo=photo)
            self.assertEqual((self.archive/managed.relative_path).read_bytes(), original)
            self.assertTrue(ArchiveCompletion.objects.filter(photo=photo).exists())
        self.assertEqual(ArchiveOperation.objects.count(), 2)
        self.assertEqual(service.submit_preview(preview.pk, token), results)

    def test_known_conflict_and_validation_failures_keep_partial_success(self):
        from archive.services.confirmation import ConfirmationConflict, ConfirmationValidation
        pairs, _ = self.pair()
        service = self.service()
        real = service.confirm_photo
        for error, status, reason in [(ConfirmationConflict('control conflict'), 'skipped', 'confirmation_conflict'),
                (Photo.DoesNotExist('control missing'), 'skipped', 'confirmation_conflict'),
                (ConfirmationValidation('control validation'), 'failed', 'confirmation_failed')]:
            with self.subTest(error=type(error).__name__), transaction.atomic():
                preview = service.build_preview([str(p.pk) for p, _ in pairs], mode='own')
                def fail_first(photo_id, *args):
                    if photo_id == str(pairs[0][0].pk):
                        raise error
                    return real(photo_id, *args)
                with mock.patch.object(service, 'confirm_photo', side_effect=fail_first):
                    results = service.submit_preview(preview.pk, service.preview_token(preview))
                self.assertEqual([r['status'] for r in results], [status, 'accepted'])
                self.assertEqual(results[0]['reason_code'], reason)
                self.assertEqual(preview.results.count(), 2)
                self.assertEqual(ArchiveOperation.objects.count(), 1)
                self.assertFalse(pairs[0][0].confirmed_species.exists())
                self.assertEqual(service.submit_preview(preview.pk, service.preview_token(preview)), results)
                transaction.set_rollback(True)

    def test_missing_selected_species_is_known_validation_but_plain_valueerror_propagates(self):
        pairs, species = self.pair()
        service = self.service()
        preview = service.build_preview([str(p.pk) for p, _ in pairs], mode='own')
        with mock.patch.object(service, 'confirm_photo', side_effect=ValueError('unexpected programming value')):
            with self.assertLogs('archive.services.batch_review', level='ERROR'):
                with self.assertRaisesRegex(ValueError, 'unexpected programming value'):
                    service.submit_preview(preview.pk, service.preview_token(preview))
        self.assertFalse(preview.results.exists())
        self.assertFalse(ArchiveOperation.objects.exists())
        # Delete after CAS identity was read, so confirm_photo itself detects the
        # vanished selection inside the row transaction. This is a domain failure.
        selected = Species.objects.create(scientific_name='Control vanish', scientific_key='control vanish',
            source_snapshot=species[0].source_snapshot)
        preview = service.build_preview([str(p.pk) for p, _ in pairs], mode='uniform_species',
            species_ids=[str(selected.pk)])
        real = service.confirm_photo
        def vanish_first(photo_id, *args):
            if photo_id == str(pairs[0][0].pk):
                Species.objects.filter(pk=selected.pk).delete()
            return real(photo_id, *args)
        with mock.patch.object(service, 'confirm_photo', side_effect=vanish_first):
            results = service.submit_preview(preview.pk, service.preview_token(preview))
        self.assertEqual([r['status'] for r in results], ['failed', 'accepted'])
        self.assertEqual(results[0]['reason_code'], 'confirmation_failed')
        self.assertEqual(preview.results.count(), 2)
        self.assertTrue(Species.objects.filter(pk=selected.pk).exists(), '领域失败也须回滚当前行')
        self.assertEqual(ArchiveOperation.objects.count(), 1)

    def test_database_failure_reports_remaining_unprocessed(self):
        pairs, _ = self.pair()
        service = self.service()
        preview = service.build_preview([str(p.pk) for p, _ in pairs], mode='own')
        real = service.confirm_photo
        def fail_second(photo_id, *args):
            if photo_id == str(pairs[1][0].pk):
                raise OperationalError('synthetic database unavailable')
            return real(photo_id, *args)
        with mock.patch.object(service, 'confirm_photo', side_effect=fail_second):
            results = service.submit_preview(preview.pk, service.preview_token(preview))
        self.assertEqual([r['status'] for r in results], ['accepted', 'unprocessed'])
        self.assertEqual(preview.results.count(), 1)

    def test_unknown_failure_while_recording_known_error_is_logged_and_retryable(self):
        from archive.services.confirmation import ConfirmationValidation
        pairs, _ = self.pair()
        service = self.service()
        preview = service.build_preview([str(p.pk) for p, _ in pairs], mode='own')
        real_confirm, real_record = service.confirm_photo, service._record
        def known_second(photo_id, *args):
            if photo_id == str(pairs[1][0].pk):
                raise ConfirmationValidation('control validation')
            return real_confirm(photo_id, *args)
        def fail_terminal(preview, row, status, reason, operation=None):
            result = real_record(preview, row, status, reason, operation)
            if status == 'failed':
                raise RuntimeError('control terminal receipt error')
            return result
        with mock.patch.object(service, 'confirm_photo', side_effect=known_second), \
                mock.patch.object(service, '_record', side_effect=fail_terminal):
            with self.assertLogs('archive.services.batch_review', level='ERROR') as logs:
                with self.assertRaisesRegex(RuntimeError, 'control terminal receipt error'):
                    service.submit_preview(preview.pk, service.preview_token(preview))
        self.assertIsInstance(logs.records[0].exc_info[1], RuntimeError)
        for value in (str(preview.pk), str(pairs[1][0].pk), preview.frozen_rows[1]['request_id']):
            self.assertIn(value, logs.records[0].getMessage())
        self.assertEqual(preview.results.count(), 1)
        self.assertEqual(ArchiveOperation.objects.count(), 1)
        self.assertFalse(pairs[1][0].confirmed_species.exists())
        results = service.submit_preview(preview.pk, service.preview_token(preview))
        self.assertEqual([r['status'] for r in results], ['accepted', 'accepted'])
        self.assertEqual(ArchiveOperation.objects.count(), 2)

    def test_interrupted_second_transaction_rolls_back_and_resume_replays_first(self):
        pairs, _ = self.pair()
        service = self.service()
        preview = service.build_preview([str(p.pk) for p, _ in pairs], mode='own')
        real = service._record
        def interrupt(preview, row, status, reason, operation=None):
            if row['photo_id'] == str(pairs[1][0].pk) and status == 'accepted':
                raise KeyboardInterrupt('synthetic process interruption')
            return real(preview, row, status, reason, operation)
        with mock.patch.object(service, '_record', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            service.submit_preview(preview.pk, service.preview_token(preview))
        self.assertEqual(preview.results.count(), 1)
        self.assertEqual(ArchiveOperation.objects.count(), 1)
        self.assertFalse(pairs[1][0].confirmed_species.exists())
        resumed = service.submit_preview(preview.pk, service.preview_token(preview))
        self.assertEqual([r['status'] for r in resumed], ['accepted', 'accepted'])
        self.assertEqual(preview.results.count(), 2)
        self.assertEqual(ArchiveOperation.objects.count(), 2)

    def test_policy_is_shared_and_tied_to_frozen_manifest_and_prompt(self):
        from archive.services.recognition_policy import candidate_ambiguity
        from archive.views import candidates_context
        pairs, _ = self.pair()
        service = self.service()
        photo = pairs[0][0]
        run = RecognitionRun.objects.get(photo=photo)
        self.assertEqual(candidates_context(photo)['ambiguity'], 'separated')
        for manifest, scores, expected in [({}, [0.8, 0.2], 'unknown'),
            (run.artifact.manifest, [0.8], 'unknown'),
            (run.artifact.manifest, [0.8, float('nan')], 'unknown'),
            (run.artifact.manifest, [0.8, 0.75], 'close'),
            (run.artifact.manifest, [0.8, 0.2], 'separated')]:
            self.assertEqual(candidate_ambiguity(manifest, scores), expected)
        preview = service.build_preview([str(photo.pk)], mode='own')
        changed = dict(run.artifact.manifest, prompt_version='changed')
        ModelArtifact.objects.filter(pk=run.artifact_id).update(manifest=changed)
        self.assertEqual(service.submit_preview(preview.pk, service.preview_token(preview))[0]['status'], 'skipped')

    def test_manual_override_multispecies_local_names_and_exclusion(self):
        pairs, species = self.pair()
        service = self.service()
        ModelArtifact.objects.update(manifest={})
        preview = service.build_preview([str(p.pk) for p, _ in pairs], mode='own')
        changes = {str(pairs[0][0].pk): {'species_ids': [str(s.pk) for s in species], 'local_names': ['本地鸟甲', '本地鸟乙']},
            str(pairs[1][0].pk): {'excluded': True}}
        revised = service.revise_preview(preview.pk, service.preview_token(preview), changes)
        self.assertEqual(revised.revision, 2)
        self.assertEqual(preview.frozen_rows[0]['final_species_ids'], [])
        with self.assertRaises(service.PreviewConflict):
            service.submit_preview(preview.pk, service.preview_token(preview))
        results = service.submit_preview(revised.pk, service.preview_token(revised))
        self.assertEqual([r['status'] for r in results], ['accepted', 'skipped'])
        self.assertEqual(pairs[0][0].confirmed_species.count(), 4)
        self.assertFalse(pairs[1][0].confirmed_species.exists())

    def test_token_tamper_expiry_revision_and_empty_manual_set_rejected(self):
        pairs, _ = self.pair()
        service = self.service()
        preview = service.build_preview([str(pairs[0][0].pk)], mode='own')
        token = service.preview_token(preview)
        with self.assertRaises(service.PreviewConflict):
            service.submit_preview(preview.pk, token+'bad')
        with self.assertRaises(ValueError):
            service.revise_preview(preview.pk, token, {str(pairs[0][0].pk): {'species_ids': [], 'local_names': []}})
        with mock.patch('archive.services.batch_review.timezone.now', return_value=timezone.now()+timedelta(hours=2)):
            with self.assertRaises(service.PreviewConflict):
                service.submit_preview(preview.pk, token)
        self.assertFalse(ArchiveOperation.objects.exists())

    def test_uniform_rejects_empty_invalid_and_oversized_sets_without_writes(self):
        pairs, species = self.pair()
        service = self.service()
        from archive.models import BatchReviewPreview
        for ids, names in [([], []), (['invalid'], []), ([str(uuid.uuid4())], []),
                ([], ['x'*301]), ([], ['x\x00']), ([], [' ']),
                ([str(species[0].pk)]*101, [])]:
            with self.subTest(ids=ids, names=names), self.assertRaises(ValueError):
                service.build_preview([str(pairs[0][0].pk)], mode='uniform_species',
                    species_ids=ids, local_names=names)
        self.assertFalse(BatchReviewPreview.objects.exists())
        self.assertFalse(ArchiveOperation.objects.exists())

    def test_uniform_single_and_multiple_sets_do_not_copy_candidates(self):
        pairs, species = self.pair()
        service = self.service()
        for ids, names in [([str(species[1].pk)], []),
                ([str(s.pk) for s in species], []), ([], ['名录外甲', '名录外乙'])]:
            preview = service.build_preview([str(p.pk) for p, _ in pairs],
                mode='uniform_species', species_ids=ids, local_names=names)
            self.assertTrue(all(row['final_species_ids'] == sorted(ids) and row['local_names'] == names
                and row['manual_override'] and row['reason_code'] == '' for row in preview.frozen_rows))
        own = service.build_preview([str(p.pk) for p, _ in pairs], mode='own_candidates')
        self.assertEqual([r['final_species_ids'] for r in own.frozen_rows], [[str(s.pk)] for s in species])
        self.assertFalse(ArchiveOperation.objects.exists())

    def test_uniform_existing_local_id_and_same_name_show_one_final_species(self):
        pairs, _ = self.pair()
        local = Species.objects.create(source_kind='local', local_name='池边鸟', local_key='池边鸟',
            scientific_name=None, scientific_key=None, source_snapshot=None, search_key='池边鸟')
        service = self.service()
        preview = service.build_preview([str(pairs[0][0].pk)], mode='uniform_species',
            species_ids=[str(local.pk)], local_names=['池边鸟'])
        self.assertEqual(preview.frozen_rows[0]['local_names'], [],
            '已显式选择同一本地身份时，预览不能把它重复显示为多鸟种')
        self.assertEqual(service.submit_preview(preview.pk, service.preview_token(preview))[0]['status'], 'accepted')
        self.assertEqual(pairs[0][0].confirmed_species.count(), 1)

    def test_uniform_catalog_homonym_keeps_local_identity_and_reuses_name(self):
        pairs, species = self.pair()
        service = self.service()
        Species.objects.filter(pk__in=[s.pk for s in species]).update(chinese_name='同名鸟')
        preview = service.build_preview([str(p.pk) for p, _ in pairs], mode='uniform_species',
            species_ids=[str(species[1].pk)], local_names=['  同名鸟  ', '同名鸟'])
        self.assertEqual(preview.frozen_rows[0]['local_names'], ['同名鸟'])
        results = service.submit_preview(preview.pk, service.preview_token(preview))
        self.assertEqual([r['status'] for r in results], ['accepted', 'accepted'])
        local = Species.objects.get(source_kind='local', local_name='同名鸟')
        self.assertIsNone(local.scientific_name)
        for photo, _ in pairs:
            self.assertEqual(set(photo.confirmed_species.values_list('species_id', flat=True)),
                {local.pk, species[1].pk})
        self.assertEqual(Species.objects.filter(source_kind='catalog').count(), 2)

    def test_uniform_failed_close_missing_candidates_manual_and_stale_cas(self):
        pairs, species = self.pair()
        service = self.service()
        first = pairs[0][0]
        for mutation in [lambda: RecognitionRun.objects.update(state='failed'),
                lambda: RecognitionCandidate.objects.filter(rank=2).update(score=0.89),
                lambda: RecognitionState.objects.update(active_run=None)]:
            with self.subTest(mutation=mutation), transaction.atomic():
                mutation()
                preview = service.build_preview([str(p.pk) for p, _ in pairs], mode='uniform_species',
                    species_ids=[str(species[0].pk)], local_names=[])
                Photo.objects.filter(pk=first.pk).update(metadata_version=2)
                self.assertEqual([r['status'] for r in service.submit_preview(preview.pk, service.preview_token(preview))],
                    ['skipped', 'accepted'])
                transaction.set_rollback(True)

    def test_uniform_revision_exclusion_manual_adjustment_and_confirmed_skip(self):
        pairs, species = self.pair()
        service = self.service()
        preview = service.build_preview([str(p.pk) for p, _ in pairs], mode='uniform_species',
            species_ids=[str(species[0].pk)], local_names=[])
        revised = service.revise_preview(preview.pk, service.preview_token(preview), {
            str(pairs[0][0].pk): {'species_ids': [str(species[1].pk)], 'local_names': ['补种']},
            str(pairs[1][0].pk): {'excluded': True}})
        self.assertEqual(revised.revision, 2)
        with self.assertRaises(service.PreviewConflict):
            service.submit_preview(preview.pk, service.preview_token(preview))
        token = service.preview_token(revised)
        results = service.submit_preview(revised.pk, token)
        self.assertEqual([r['status'] for r in results], ['accepted', 'skipped'])
        self.assertEqual(service.submit_preview(revised.pk, token), results)
        confirmed = service.build_preview([str(pairs[0][0].pk)], mode='uniform_species',
            species_ids=[str(species[0].pk)], local_names=[])
        confirmed = service.revise_preview(confirmed.pk, service.preview_token(confirmed), {
            str(pairs[0][0].pk): {'species_ids': [str(species[0].pk)]}})
        self.assertEqual(confirmed.frozen_rows[0]['reason_code'], 'not_review_pending')
        self.assertEqual(service.submit_preview(confirmed.pk, service.preview_token(confirmed))[0]['status'], 'skipped')
        self.assertEqual(set(pairs[0][0].confirmed_species.values_list('species_id', flat=True)),
            {species[1].pk, Species.objects.get(local_name='补种').pk})


class BatchReviewViewTests(RecognitionFixture):
    pair = BatchReviewTests.pair
    service = BatchReviewTests.service
    def test_uniform_without_model_has_explicit_final_set_and_archives(self):
        pairs, species = self.pair()
        RecognitionState.objects.update(active_run=None)
        response = self.client.post('/review/batch/preview/', {
            'mode': 'uniform_species', 'photo_ids': [str(p.pk) for p, _ in pairs],
            'species_ids': [str(species[0].pk)], 'local_names': ['池边鸟甲', '池边鸟乙']})
        self.assertEqual(response.status_code, 303, '缺模型时明确人工统一集合仍须建立预览')
        page = self.client.get(response.url)
        self.assertContains(page, '统一设置鸟种集合')
        self.assertContains(page, '替换为', count=2)
        preview = page.context['preview']
        for row in preview.frozen_rows:
            self.assertEqual(row['final_species_ids'], [str(species[0].pk)])
            self.assertEqual(row['local_names'], ['池边鸟甲', '池边鸟乙'])
            self.assertTrue(row['manual_override'])
        service = self.service()
        results = service.submit_preview(preview.pk, service.preview_token(preview))
        self.assertEqual([r['status'] for r in results], ['accepted', 'accepted'])
        for photo, path in pairs:
            original = path.read_bytes()
            self.assertTrue(process_archive(claim_next()))
            managed = ManagedOriginal.objects.get(photo=photo)
            self.assertEqual((self.archive/managed.relative_path).read_bytes(), original)
            self.assertTrue(ArchiveCompletion.objects.filter(photo=photo).exists())
            self.assertEqual(photo.confirmed_species.count(), 3)
        self.assertEqual(Species.objects.filter(source_kind='local').count(), 2)

    def test_uniform_native_setup_search_preserves_ids_draft_and_empty_rejected(self):
        from archive.tests.test_confirmation import ConfirmationPage
        pairs, species = self.pair()
        ids = [str(p.pk) for p, _ in pairs]
        response = self.client.post('/review/batch/preview/?mode=uniform_species', {'photo_ids': ids})
        self.assertContains(response, '已选 2 张')
        self.assertFalse(ArchiveOperation.objects.exists())
        parser = ConfirmationPage(response)
        for field in parser.inputs:
            if field.get('name') == 'q':
                field['value'] = 'Birdus'
            if field.get('aria-label') == '添加本地鸟名':
                field['value'] = '未保存鸟'
        response = parser.submit(self.client, '/review/batch/preview/', '搜索')
        self.assertContains(response, '未保存鸟')
        parser = ConfirmationPage(response)
        chosen = next(field for field in parser.inputs if field.get('name') == 'species_ids')
        chosen['checked'] = ''
        response = parser.submit(self.client, '/review/batch/preview/', '核对全部 2 张')
        self.assertEqual(response.status_code, 303)
        page = self.client.get(response.url)
        self.assertEqual(page.context['preview'].selection_ids, ids)
        self.assertContains(page, '替换为', count=2)
        empty = self.client.post('/review/batch/preview/', {
            'mode': 'uniform_species', 'photo_ids': ids, 'action': 'preview'})
        self.assertContains(empty, '空集合不会归档', status_code=400)
        self.assertFalse(ArchiveOperation.objects.exists())

    def test_uniform_form_is_separate_from_current_page_location_limits(self):
        from archive.forms import BatchSpeciesForm, BatchLocationForm
        from django.http import QueryDict
        values = QueryDict('', mutable=True)
        values.setlist('photo_ids', [str(uuid.uuid4()) for _ in range(25)])
        values.setlist('local_names', ['人工鸟'])
        self.assertTrue(BatchSpeciesForm(values).is_valid())
        self.assertFalse(BatchLocationForm(values).is_valid())

    def test_native_preview_requires_post_and_explicit_action(self):
        pairs, _ = self.pair()
        self.assertEqual(self.client.get('/review/batch/preview/').status_code, 405,
            '建预览必须是明确POST且GET不写决定')
        response = self.client.post('/review/batch/preview/', {
            'mode': 'own', 'photo_ids': [str(p.pk) for p, _ in pairs]})
        self.assertEqual(response.status_code, 303)
        page = self.client.get(response.url)
        self.assertContains(page, '共 2 张')
        self.assertContains(page, '鸟1')
        self.assertContains(page, '鸟2')
        self.assertFalse(ArchiveOperation.objects.exists())

    def test_native_search_paginate_multilocal_save_exclude_and_confirm(self):
        from archive.tests.test_confirmation import ConfirmationPage
        pairs, species = self.pair()
        ModelArtifact.objects.update(manifest={})  # Manual override works without any usable policy.
        for n in range(30):
            Species.objects.create(scientific_name=f'Globalus {n}', scientific_key=f'globalus {n}',
                chinese_name=f'全球鸟{n}', search_key='全球鸟', source_snapshot=species[0].source_snapshot)
        created = self.client.post('/review/batch/preview/', {'mode': 'own', 'photo_ids': [str(p.pk) for p, _ in pairs]})
        page = self.client.get(created.url)
        parser = ConfirmationPage(page)
        first_form = parser.forms[0]
        for field in parser.inputs:
            if field['form'] is first_form and field.get('name') == 'q':
                field['value'] = '全球鸟'
            if field['form'] is first_form and field.get('name') == 'local_names':
                field['value'] = '池边鸟'
        searched = parser.submit(self.client, created.url, '搜索')
        self.assertContains(searched, '池边鸟')
        self.assertFalse(ArchiveOperation.objects.exists())
        parsed = ConfirmationPage(searched)
        second_page = parsed.submit(self.client, created.url, '下一页鸟种')
        self.assertContains(second_page, '池边鸟')
        parsed = ConfirmationPage(second_page)
        selected = next(field for field in parsed.inputs if field.get('name') == 'species_ids')
        selected['checked'] = ''
        name_fields = [field for field in parsed.inputs if field['form'] is parsed.forms[0] and field.get('name') == 'local_names']
        name_fields[0]['value'] = '池边鸟甲'
        name_fields[1]['value'] = '池边鸟乙'
        saved = parsed.submit(self.client, created.url, '保存补选并核对')
        self.assertEqual(saved.status_code, 303)
        revised_page = self.client.get(saved.url)
        self.assertContains(revised_page, '池边鸟甲')
        self.assertContains(revised_page, '池边鸟乙')
        self.assertContains(revised_page, '共 2 张')
        preview = revised_page.context['preview']
        self.assertEqual(preview.revision, 2)
        second_id = str(pairs[1][0].pk)
        excluded = self.client.post(f'/review/batch/{preview.pk}/revise/', {'token': revised_page.context['token'],
            'photo_id': second_id, 'action': 'exclude'})
        final = self.client.get(excluded.url)
        self.assertContains(final, '跳过 1 张')
        confirmed = ConfirmationPage(final).submit(self.client, excluded.url, '确认以上 2 张')
        self.assertEqual(confirmed.status_code, 303)
        result = self.client.get(confirmed.url)
        self.assertContains(result, '已受理')
        self.assertContains(result, '已跳过')
        self.assertNotContains(result, '确认以上')
        self.assertEqual(pairs[0][0].confirmed_species.count(), 3)
        self.assertFalse(pairs[1][0].confirmed_species.exists())

    def test_csrf_methods_get_readonly_and_no_store(self):
        pairs, _ = self.pair()
        from archive.models import BatchReviewPreview
        from archive.tests.test_confirmation import ConfirmationPage
        secured = Client(enforce_csrf_checks=True)
        denied = secured.post('/review/batch/preview/', {'mode': 'own', 'photo_ids': [str(pairs[0][0].pk)]})
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(BatchReviewPreview.objects.exists())
        workbench = secured.get('/review/')
        parser = ConfirmationPage(workbench)
        for field in parser.inputs:
            if field.get('name') == 'photo_ids':
                field['checked'] = ''
        created = parser.submit(secured, '/review/', '确认各自候选')
        self.assertEqual(created.status_code, 303)
        page = secured.get(created.url)
        preview = page.context['preview']
        self.assertEqual(BatchReviewPreview.objects.count(), 1)
        self.assertIn('no-store', page['Cache-Control'])
        self.assertFalse(ArchiveOperation.objects.exists())
        for suffix in ('revise/', 'submit/'):
            url = created.url+suffix
            self.assertEqual(secured.get(url).status_code, 405)
            self.assertEqual(secured.post(url, {'token': page.context['token'], 'confirm': 'yes'}).status_code, 403)
        final = ConfirmationPage(page).submit(secured, created.url, '确认以上 2 张')
        self.assertEqual(final.status_code, 303)
        self.assertIn('no-store', final['Cache-Control'])
        self.assertEqual(preview.results.count(), 2)

    def test_invalid_old_token_preserves_posted_draft_and_no_decision(self):
        pairs, _ = self.pair()
        service = self.service()
        preview = service.build_preview([str(pairs[0][0].pk)], mode='own')
        original_token = service.preview_token(preview)
        current = service.revise_preview(preview.pk, original_token, {str(pairs[0][0].pk): {'excluded': True}})
        response = self.client.post(f'/review/batch/{preview.pk}/revise/', {'token': original_token,
            'photo_id': str(pairs[0][0].pk), 'action': 'save', 'local_names': ['<草稿鸟>']})
        self.assertContains(response, '&lt;草稿鸟&gt;', status_code=409)
        for token in (original_token, original_token+'tamper'):
            self.assertEqual(self.client.post(f'/review/batch/{preview.pk}/submit/', {
                'token': token, 'confirm': 'yes'}).status_code, 409)
        self.assertFalse(ArchiveOperation.objects.exists())
        self.assertEqual(current.frozen_rows[0]['excluded'], True)

    def test_explicit_final_confirm_empty_set_and_missing_objects_visible(self):
        pairs, _ = self.pair()
        missing = str(uuid.uuid4())
        created = self.client.post('/review/batch/preview/', {'mode': 'own', 'photo_ids': [missing, str(pairs[0][0].pk)]})
        page = self.client.get(created.url)
        self.assertContains(page, missing)
        self.assertContains(page, '共 2 张')
        preview = page.context['preview']
        response = self.client.post(created.url+'submit/', {'token': page.context['token']})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ArchiveOperation.objects.exists())
        invalid = self.client.post(created.url+'revise/', {'token': page.context['token'], 'action': 'save',
            'photo_id': str(pairs[0][0].pk), 'species_ids': [], 'local_names': []})
        self.assertContains(invalid, '空集合不会归档', status_code=400)
        self.assertEqual(preview.results.count(), 0)

    def test_database_outage_result_lists_all_rows_without_more_database_reads(self):
        pairs, _ = self.pair()
        service = self.service()
        preview = service.build_preview([str(p.pk) for p, _ in pairs], mode='own')
        results = [{'photo_id': str(p.pk), 'status': 'unprocessed',
            'reason_code': 'database_unavailable', 'operation_id': None} for p, _ in pairs]
        from archive import batch_views
        from django.test import RequestFactory
        from django.db import connection
        def no_database(*args, **kwargs):
            raise AssertionError('结果已知后不可再次依赖故障数据库')
        with connection.execute_wrapper(no_database):
            response = batch_views._page(RequestFactory().post('/review/batch/'), preview,
                results=results, status=503)
        self.assertContains(response, '未处理 ·', count=2, status_code=503)
        self.assertContains(response, '共 2 张', status_code=503)

    def test_get_after_partial_interruption_marks_missing_receipts_unprocessed(self):
        pairs, _ = self.pair()
        service = self.service()
        preview = service.build_preview([str(p.pk) for p, _ in pairs], mode='own')
        real = service.confirm_photo
        def fail_second(photo_id, *args):
            if photo_id == str(pairs[1][0].pk):
                raise OperationalError('synthetic unavailable')
            return real(photo_id, *args)
        with mock.patch.object(service, 'confirm_photo', side_effect=fail_second):
            service.submit_preview(preview.pk, service.preview_token(preview))
        response = self.client.get(f'/review/batch/{preview.pk}/')
        self.assertContains(response, '已受理')
        self.assertContains(response, '未处理 ·')
        self.assertEqual(preview.results.count(), 1)
