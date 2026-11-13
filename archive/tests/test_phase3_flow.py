"""独立软件闭环：合成 JPEG/候选，经真实发布、HTTP 人工决定及普通文件 worker。

不加载模型，不把这些控制样本算入公开模型效果或 NAS 性能分母。
"""
import hashlib
import copy
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone

from archive.models import (ArchiveCompletion, ArchiveOperation, ManagedOriginal,
                            ModelArtifact, Photo, PhotoSpecies, RecognitionCandidate,
                            RecognitionRun, RecognitionState, SourceObservation,
                            SourceSnapshot, Species, SpeciesConcept)
from archive.services import recognition
from archive.services import model_packages
from archive.services.paths import open_root
from archive.tests.fixtures import disk_hashes, jpeg
from archive.tests.test_archive_recovery import ArchiveFixture
from archive.tests.test_recognition_state import RecognitionFixture


class Phase3FlowTests(ArchiveFixture):
    def setUp(self):
        super().setUp()
        self.client = Client(enforce_csrf_checks=True)
        self.artifact = self.model('a', 'control-one')
        recognition.set_current_model(self.artifact.pk)

    def model(self, letter, revision):
        return ModelArtifact.objects.create(manifest_digest=letter*64,
            provider='synthetic-flow-control-only', version='1', revision=revision,
            selection_state='selected', manifest={
                'score_kind': 'similarity', 'label_digest': letter*64,
                'threshold_version': 'synthetic-only', 'margin_threshold': .1,
                'calibration_evidence': 'synthetic contract; not model evidence'})

    def imported(self, index):
        path = self.inbox/f'{index:02d}.jpg'
        digest = jpeg(path, color=(index*7 % 255, index*13 % 255, 51))
        self.subprocess_worker()
        return Photo.objects.get(sha256=digest), path, digest

    def publish_next(self, photo):
        run = recognition.claim_next_run()
        self.assertEqual(run.photo_id, photo.pk, '新导入不能越过旧的就绪意图')
        output = [dict(rank=1, species_id=str(self.species.pk), raw_label='Ardea alba',
            score=.9, mapping_status='mapped', mapping_evidence='synthetic exact identity'),
            dict(rank=2, species_id=None, raw_label='unmapped synthetic', score=.2,
                mapping_status='unmapped', mapping_evidence='')]
        self.assertTrue(recognition.publish_result(run.pk, output, artifact_id=run.artifact_id))
        return run, output

    def post(self, url, data):
        if 'csrftoken' not in self.client.cookies:
            self.client.get('/review/')
        return self.client.post(url, data, HTTP_X_CSRFTOKEN=self.client.cookies['csrftoken'].value)

    def confirm(self, photo, *, ids=None, names=None, classification='species'):
        return self.post(f'/photos/{photo.pk}/', {
            'metadata_version': photo.metadata_version, 'request_id': str(uuid.uuid4()),
            'species_ids': ids or [], 'local_names': names or [], 'classification': classification})

    def assert_archived(self, photo, path, digest):
        self.subprocess_worker()
        photo.refresh_from_db()
        operation = ArchiveOperation.objects.get(photo=photo)
        completion = ArchiveCompletion.objects.get(photo=photo, operation=operation)
        managed = ManagedOriginal.objects.get(photo=photo)
        self.assertEqual(photo.state, 'archived')
        self.assertEqual(completion.operation_id, operation.pk)
        self.assertFalse(path.exists())
        self.assertEqual([name for name, sha in disk_hashes(self.archive).items() if sha == digest],
            [managed.relative_path])
        self.assertEqual(disk_hashes(self.claims), {})
        response = self.client.get(f'/photos/{photo.pk}/download/')
        self.assertEqual(response.status_code, 200)
        try:
            downloaded = b''.join(response.streaming_content)
        finally:
            response.close()
        self.assertEqual(hashlib.sha256(downloaded).hexdigest(), digest)
        self.assertIn('no-store', response['Cache-Control'])
        connection.close()
        with sqlite3.connect(settings.DATABASES['default']['NAME']) as db:
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM archive_archivecompletion WHERE photo_id=?',
                (photo.pk.hex,)).fetchone(), (1,))
            self.assertEqual(db.execute('SELECT sha256 FROM archive_managedoriginal WHERE photo_id=?',
                (photo.pk.hex,)).fetchone(), (digest,))

    def test_fifo_candidates_need_explicit_single_human_then_archive_original(self):
        first, path, digest = self.imported(1)
        second, second_path, second_digest = self.imported(2)
        self.publish_next(first)
        self.publish_next(second)
        self.assertFalse(PhotoSpecies.objects.exists())
        self.assertFalse(ArchiveOperation.objects.exists())
        page = self.client.get(f'/photos/{first.pk}/')
        self.assertContains(page, 'Ardea alba')
        self.assertEqual(self.client.post(f'/photos/{first.pk}/', {}).status_code, 403)
        self.assertEqual(self.confirm(first, ids=[str(self.species.pk)]).status_code, 303)
        self.assertTrue(path.exists(), '人工受理与文件执行分开')
        self.assertFalse(ArchiveCompletion.objects.exists())
        self.assert_archived(first, path, digest)
        self.assertEqual(list(first.confirmed_species.values_list('species_id', flat=True)), [self.species.pk])
        self.assertTrue(second_path.exists())
        self.assertEqual(hashlib.sha256(second_path.read_bytes()).hexdigest(), second_digest)
        self.assertFalse(ArchiveOperation.objects.filter(photo=second).exists())

    def test_crosspage_own_and_uniform_preview_mixed_skip_replay_and_bytes(self):
        # Actual 24-row pagination; a selected ID on page 2 is outside page 1.
        pairs = [self.imported(index) for index in range(25)]
        for photo, _, _ in pairs:
            self.publish_next(photo)
        first, stale, last = pairs[0][0], pairs[1][0], pairs[-1][0]
        page1, page2 = self.client.get('/review/'), self.client.get('/review/?page=2')
        self.assertContains(page1, str(first.pk))
        self.assertNotContains(page1, f'data-photo-id="{last.pk}"')
        self.assertContains(page2, str(last.pk))
        selected = [str(first.pk), str(stale.pk), str(last.pk)]
        created = self.post('/review/batch/preview/', {'mode': 'own', 'photo_ids': selected})
        self.assertEqual(created.status_code, 303)
        page = self.client.get(created.url)
        preview, token = page.context['preview'], page.context['token']
        self.assertEqual(preview.selection_ids, selected)
        self.assertFalse(ArchiveOperation.objects.exists())
        # Another explicit human decision makes only this frozen row stale.
        self.assertEqual(self.confirm(stale, classification='undetermined').status_code, 303)
        accepted = self.post(created.url+'submit/', {'token': token, 'confirm': 'yes'})
        self.assertEqual(accepted.status_code, 303)
        preview.refresh_from_db()
        rows = list(preview.results.order_by('id').values('photo_id', 'status'))
        self.assertEqual({str(row['photo_id']): row['status'] for row in rows},
            {str(first.pk): 'accepted', str(stale.pk): 'skipped', str(last.pk): 'accepted'})
        before = list(ArchiveOperation.objects.values_list('id', flat=True))
        self.assertEqual(self.post(created.url+'submit/', {'token': token, 'confirm': 'yes'}).status_code, 303)
        self.assertEqual(list(ArchiveOperation.objects.values_list('id', flat=True)), before)
        # Drain only already accepted operations with the ordinary worker.
        for _ in range(3):
            self.subprocess_worker()
        for index in (0, 1, 24):
            self.assert_archived(*pairs[index])
        self.assertEqual(stale.confirmed_species.count(), 0)
        self.assertEqual(ArchiveOperation.objects.get(photo=stale).classification, 'undetermined')
        # Explicit uniform complete set still works without usable candidates.
        other = [pairs[2][0], pairs[23][0]]
        created = self.post('/review/batch/preview/', {'mode': 'uniform_species',
            'photo_ids': [str(photo.pk) for photo in other],
            'species_ids': [str(self.species.pk)], 'local_names': ['人工补种']})
        self.assertEqual(created.status_code, 303)
        page = self.client.get(created.url)
        self.assertTrue(all(row['manual_override'] for row in page.context['preview'].frozen_rows))
        self.assertEqual(self.post(created.url+'submit/', {'token': page.context['token'], 'confirm': 'yes'}).status_code, 303)
        for _ in other:
            self.subprocess_worker()
        for index in (2, 23):
            self.assert_archived(*pairs[index])
            self.assertEqual(pairs[index][0].confirmed_species.count(), 2)
        self.assertEqual(ArchiveCompletion.objects.count(), 5)
        self.assertTrue(all(path.exists() for i, (_, path, _) in enumerate(pairs) if i not in (0, 1, 2, 23, 24)))

    def test_missing_model_manual_multi_custom_and_undetermined(self):
        ModelArtifact.objects.update(is_current=False)
        for index, (ids, names, classification) in enumerate((
            ([str(self.species.pk)], ['手动补种'], 'species'),
            ([], ['仅自定义鸟'], 'species'), ([], [], 'undetermined')), 30):
            photo, path, digest = self.imported(index)
            self.assertEqual(photo.recognition_runs.get().safe_error, 'model_not_configured')
            self.assertEqual(self.confirm(photo, ids=ids, names=names, classification=classification).status_code, 303)
            self.assert_archived(photo, path, digest)
        self.assertEqual(PhotoSpecies.objects.count(), 3)
        self.assertFalse(RecognitionCandidate.objects.exists())

    def test_switch_rerun_retry_and_late_results_preserve_human_and_old_snapshots(self):
        candidate, path, digest = self.imported(40)
        old, output = self.publish_next(candidate)
        queued, _, _ = self.imported(41)
        new_artifact = self.model('b', 'control-two')
        recognition.set_current_model(new_artifact.pk)
        new, _, _ = self.imported(42)
        self.assertEqual(queued.recognition_runs.get().artifact_id, self.artifact.pk)
        self.assertEqual(new.recognition_runs.get().artifact_id, new_artifact.pk)
        self.assertEqual(RecognitionState.objects.get(photo=candidate).active_run_id, old.pk)
        # FIFO + bounded retry retains the old immutable artifact, even after switch.
        run = recognition.claim_next_run()
        self.assertEqual(run.photo_id, queued.pk)
        with override_settings(RECOGNITION_RETRY_LIMIT=2, RECOGNITION_RETRY_BASE_MS=1):
            recognition.fail_run(run.pk, 'temporarily_unavailable')
            run = recognition.claim_next_run(timezone.now()+timedelta(seconds=1))
            self.assertEqual((run.photo_id, run.artifact_id), (queued.pk, self.artifact.pk))
            recognition.fail_run(run.pk, 'temporarily_unavailable')
        run.refresh_from_db()
        self.assertEqual((run.state, run.cycle_attempts), ('failed', 2))
        self.publish_next(new)
        key = uuid.uuid4()
        rerun = recognition.request_rerun(candidate.pk, 1, key)
        self.assertEqual(recognition.request_rerun(candidate.pk, 1, key).pk, rerun.pk)
        self.assertEqual((rerun.generation, rerun.artifact_id), (2, new_artifact.pk))
        self.assertEqual(RecognitionState.objects.get(photo=candidate).active_run_id, old.pk)
        self.assertFalse(recognition.publish_result(old.pk, output, artifact_id=old.artifact_id))
        claimed = recognition.claim_next_run()
        self.assertEqual(claimed.pk, rerun.pk)
        self.assertEqual(self.confirm(candidate, names=['人工最后决定']).status_code, 303)
        human = list(candidate.confirmed_species.values_list('species_id', flat=True))
        self.assertFalse(recognition.publish_result(rerun.pk, output, artifact_id=new_artifact.pk))
        self.assertEqual(list(candidate.confirmed_species.values_list('species_id', flat=True)), human)
        self.assert_archived(candidate, path, digest)

    def test_source_replacement_rejects_late_candidate_without_human_or_archive(self):
        photo, path, digest = self.imported(50)
        run = recognition.claim_next_run()
        source = SourceObservation.objects.get(photo=photo).source
        source.observed_version += 1
        source.save(update_fields=['observed_version'])
        output = [dict(rank=1, raw_label='Ardea alba', species_id=str(self.species.pk), score=.9,
            mapping_status='mapped', mapping_evidence='synthetic control')]
        self.assertFalse(recognition.publish_result(run.pk, output, artifact_id=run.artifact_id))
        run.refresh_from_db()
        self.assertEqual(run.state, 'stale')
        self.assertFalse(PhotoSpecies.objects.exists())
        self.assertFalse(ArchiveOperation.objects.exists())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)


class SelectedPackageMappingTests(RecognitionFixture):
    """One-label metadata fixture; weights/model/global accuracy are not mocked evidence."""
    def setUp(self):
        super().setUp()
        self.catalog = SourceSnapshot.objects.create(source='AviList', version='2025b', sha256='a'*64,
            acquired_at='2026-10-19', download_page_url='https://example.org/catalog',
            license_url='https://example.org/license', attribution='synthetic mapping control',
            row_count=1, species_count=1, manifest={})
        self.species = Species.objects.create(scientific_name='Ardea alba', scientific_key='ardea alba',
            source_snapshot=self.catalog, search_key='ardea alba')
        self.concept = SpeciesConcept.objects.create(snapshot=self.catalog, species=self.species,
            external_id='avibase-control', evidence='synthetic exact concept')
        self.package_uuid = uuid.uuid4()
        self.label = {'identity_kind':'catalog', 'external_id':'avibase-control',
            'scientific_name':'Ardea alba', 'species_id':str(self.package_uuid),
            'mapping_evidence':'synthetic fixed vocabulary concept avibase-control'}
        self.package = (self.data/'control-package').resolve()
        self.package.mkdir()
        manifest = {'provider':model_packages.PROVIDER, 'local_root':str(self.package), 'files':[]}
        for role, field, value in (
            ('labels','label_digest',[self.label]),
            ('snapshot','snapshot_digest',{'source':'AviList','version':'2025b','sha256':self.catalog.sha256,
                'species_count':1,'concepts':[self.label]})):
            raw = json.dumps(value, sort_keys=True).encode()
            (self.package/(role+'.json')).write_bytes(raw)
            sha = hashlib.sha256(raw).hexdigest()
            manifest[field] = sha
            manifest['files'].append({'role':role,'path':role+'.json','sha256':sha,'size_bytes':len(raw)})
        self.artifact = ModelArtifact.objects.create(provider=model_packages.PROVIDER, version='control-only',
            revision='fixed', manifest_digest=model_packages.digest(manifest), manifest=manifest,
            selection_state='selected')
        recognition.set_current_model(self.artifact.pk)
        self.output = [{'rank':1,'raw_label':'Ardea alba','score':.9,
            'species_id':str(self.package_uuid),'mapping_status':'mapped',
            'mapping_evidence':self.label['mapping_evidence']}]
        # Only vocabulary cardinality is reduced; real byte/digest/identity and
        # catalog checks run. No verify_manifest/mapper/publication mock.
        config = patch.object(model_packages, 'GLOBAL_SPECIES_COUNT', 1)
        config.start(); self.addCleanup(config.stop)

    def dispatch(self, output=None, before=None):
        photo, path = self.imported()
        run = recognition.claim_next_run()
        if before:
            before(photo)
        inbox_fd, data_fd = open_root(self.inbox), open_root(self.data)
        try:
            channel = SimpleNamespace(ready=False, poll=lambda:None)
            dispatcher = recognition.RecognitionDispatcher(channel, inbox_fd, data_fd)
            dispatcher.run = run
            dispatcher.reply = {'type':'result','candidates':self.output if output is None else output}
            dispatcher.tick()
            self.assertIsNone(dispatcher.run)
        finally:
            os.close(inbox_fd); os.close(data_fd)
        run.refresh_from_db()
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), photo.sha256)
        return photo, run

    def rejected(self, output=None):
        photo, run = self.dispatch(output)
        self.assertEqual((run.state, run.safe_error), ('failed', 'invalid_result'))
        self.assertFalse(run.candidates.exists())
        self.assertFalse(PhotoSpecies.objects.exists())
        self.assertFalse(ArchiveOperation.objects.exists())
        return photo, run

    def test_ordinary_import_maps_fixed_package_uuid_to_same_concept_in_new_database(self):
        self.assertNotEqual(self.package_uuid, self.species.pk)
        photo, run = self.dispatch()
        self.assertEqual(run.reason, 'import')
        self.assertEqual(run.state, 'succeeded')
        candidate = run.candidates.get()
        self.assertEqual(candidate.species_id, self.species.pk)
        self.assertIn('avibase-control', candidate.mapping_evidence)
        self.assertEqual((candidate.raw_label, candidate.score), ('Ardea alba', .9))
        self.assertFalse(PhotoSpecies.objects.exists())
        self.assertFalse(ArchiveOperation.objects.exists())

    def test_real_channel_oversized_integer_score_is_invalid_result_and_closes_input(self):
        from archive.services.recognition_process import InferenceChannel, InferenceLimits
        photo, path = self.imported()
        original = path.read_bytes()
        output = copy.deepcopy(self.output)
        output[0]['score'] = 10**400
        channel = InferenceChannel(python=sys.executable, lock_path=self.data/'recognition.lock',
            limits=InferenceLimits(512, 5), test_provider={'candidates':output})
        self.addCleanup(channel.close)
        channel.start().wait_ready()
        inbox_fd, data_fd = open_root(self.inbox), open_root(self.data)
        self.addCleanup(os.close, inbox_fd)
        self.addCleanup(os.close, data_fd)
        dispatcher = recognition.RecognitionDispatcher(channel, inbox_fd, data_fd)
        self.addCleanup(dispatcher.close_input)
        dispatcher.tick()
        run = RecognitionRun.objects.get(photo=photo)
        self.assertEqual(run.state, 'running')
        deadline = time.monotonic()+4
        while time.monotonic() < deadline:
            dispatcher.tick()
            run.refresh_from_db()
            if run.state != 'running':
                break
            time.sleep(.01)
        self.assertEqual((run.state, run.safe_error), ('failed', 'invalid_result'))
        self.assertIsNone(channel.pending)
        self.assertIsNone(dispatcher.run)
        self.assertIsNone(dispatcher.input)
        self.assertFalse(list(self.data.glob('.recognition-*')))
        self.assertFalse(run.candidates.exists())
        self.assertFalse(PhotoSpecies.objects.exists())
        self.assertFalse(ArchiveOperation.objects.exists())
        self.assertEqual(path.read_bytes(), original)

    def test_wrong_snapshot_cannot_borrow_same_named_species(self):
        SourceSnapshot.objects.filter(pk=self.catalog.pk).update(sha256='9'*64)
        self.rejected()

    def test_foreign_concept_cannot_fall_back_to_scientific_name(self):
        SpeciesConcept.objects.filter(pk=self.concept.pk).update(external_id='foreign-concept')
        self.rejected()

    def test_tampered_provider_uuid_is_rejected_even_when_it_is_local_valid_uuid(self):
        output = copy.deepcopy(self.output)
        output[0]['species_id'] = str(self.species.pk)
        self.rejected(output)

    def test_unknown_label_is_not_promoted_or_fuzzily_mapped(self):
        output = copy.deepcopy(self.output)
        output[0]['raw_label'] = 'Unknown model label'
        self.rejected(output)

    def test_duplicate_candidate_concept_rejects_whole_result(self):
        output = copy.deepcopy(self.output)
        output.append({**output[0], 'rank':2, 'score':.8})
        self.rejected(output)

    def test_changed_label_bytes_fail_manifest_binding(self):
        with (self.package/'labels.json').open('ab') as stream:
            stream.write(b' ')
        self.rejected()

    def test_provider_mapping_evidence_must_match_frozen_vocabulary(self):
        output = copy.deepcopy(self.output)
        output[0]['mapping_evidence'] = 'unverified provider assertion'
        self.rejected(output)

    def test_valid_rebinding_cannot_overwrite_later_explicit_human(self):
        from archive.services.confirmation import confirm_photo
        photo, run = self.dispatch(before=lambda photo:confirm_photo(photo.pk, 1, uuid.uuid4(),
            'species', [], ['人工最后决定']))
        self.assertEqual(run.state, 'stale')
        self.assertEqual(photo.confirmed_species.get().species.local_name, '人工最后决定')
        self.assertFalse(run.candidates.exists())
        self.assertEqual(ArchiveOperation.objects.count(), 1)
