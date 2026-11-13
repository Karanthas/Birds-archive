"""Read-only recognition fallback with the existing anonymous confirmation form."""
import uuid
from django.test import Client

from archive.models import Photo, RecognitionRun, RecognitionState, ArchiveOperation
from archive.tests.test_recognition_state import RecognitionFixture


class RecognitionCandidateViewTests(RecognitionFixture):
    def candidates(self, count=3, policy=None):
        from archive.models import ModelArtifact, RecognitionCandidate, SourceSnapshot, Species
        from archive.services.recognition import set_current_model
        from django.utils import timezone
        snapshot = SourceSnapshot.objects.create(source='AviList', version='fixture-1', sha256='a'*64,
            acquired_at='2026-10-17', download_page_url='https://example.org/catalog',
            license_url='https://example.org/license', attribution='Test fixture', row_count=3,
            species_count=3, manifest={})
        artifact = ModelArtifact.objects.create(manifest_digest='a'*64, provider='本地测试模型',
            version='fixture-v1', revision='frozen-revision', manifest=policy or {}, selection_state='selected')
        set_current_model(artifact.pk)
        photo, path = self.imported()
        run = RecognitionRun.objects.get(photo=photo)
        RecognitionRun.objects.filter(pk=run.pk).update(state='succeeded', finished_at=timezone.now())
        RecognitionState.objects.filter(photo=photo).update(active_run=run)
        species = []
        for index in range(1, count+1):
            item = Species.objects.create(scientific_name=f'Birdus species{index}',
                scientific_key=f'birdus species{index}', search_key=f'birdus species{index}',
                chinese_name='测试鸟' if index == 1 else '', source_snapshot=snapshot)
            species.append(item)
            RecognitionCandidate.objects.create(run=run, rank=index, species=item,
                mapping_status='mapped', mapping_evidence='fixture exact concept',
                raw_label=f'Birdus species{index}', score=0.9-index*0.1)
        return photo, path, run, species

    def test_active_candidates_render_actual_count_without_preselection(self):
        photo, path, run, species = self.candidates()
        before = path.read_bytes()
        response = self.client.get(f'/photos/{photo.pk}/')
        self.assertContains(response, 'data-recognition-candidate=', count=3,
            msg_prefix='必须展示活动快照的实际前三候选')
        self.assertContains(response, '本地测试模型')
        self.assertContains(response, 'fixture-v1')
        self.assertContains(response, 'AviList')
        self.assertContains(response, '中文名未知')
        self.assertNotContains(response, 'checked')
        self.assertFalse(ArchiveOperation.objects.exists())
        self.assertFalse(photo.confirmed_species.exists())
        self.assertEqual(path.read_bytes(), before)

    def test_zero_one_two_candidates_are_not_padded(self):
        from archive.models import RecognitionCandidate
        photo, _, run, _ = self.candidates()
        for count in (2, 1, 0):
            RecognitionCandidate.objects.filter(run=run, rank__gt=count).delete()
            response = self.client.get(f'/photos/{photo.pk}/')
            self.assertEqual(response.content.decode().count('data-recognition-candidate='), count)

    def test_unmapped_label_escaped_and_cannot_be_added_as_species(self):
        from archive.models import RecognitionCandidate
        photo, _, run, species = self.candidates(1)
        label = '<img src=x onerror=alert(1)>'
        RecognitionCandidate.objects.filter(run=run).update(raw_label=label,
            mapping_status='unmapped', species=None, mapping_evidence='')
        response = self.client.get(f'/photos/{photo.pk}/')
        self.assertContains(response, '&lt;img src=x onerror=alert(1)&gt;')
        self.assertNotContains(response, label)
        self.assertContains(response, '未映射')
        self.assertNotContains(response, 'value="candidate"')
        rejected = self.client.post(f'/photos/{photo.pk}/?candidate_rank=1&candidate_run={run.pk}',
            {'draft_action': 'candidate', 'metadata_version': 1, 'request_id': str(uuid.uuid4())})
        self.assertEqual(rejected.status_code, 400)
        self.assertFalse(ArchiveOperation.objects.exists())

    def test_candidate_click_adds_only_explicit_candidate_to_existing_draft(self):
        from archive.tests.test_confirmation import ConfirmationPage
        photo, path, run, species = self.candidates()
        page = ConfirmationPage(self.client.get(f'/photos/{photo.pk}/'))
        page.check(species[1].pk)  # Native form only includes visible existing selections.
        page.set_value('local_names', '漏识别鸟')
        response = page.submit(self.client, f'/photos/{photo.pk}/', '添加候选测试鸟')
        self.assertEqual(response.draft_redirect.status_code, 303)
        draft = response
        self.assertEqual(set(draft.context['selected_ids']), {str(species[0].pk)})
        self.assertEqual(draft.context['local_names'], ['漏识别鸟'])
        self.assertFalse(ArchiveOperation.objects.exists())
        # A second explicit candidate preserves prior selected IDs and the local-name draft.
        page = ConfirmationPage(draft)
        response = page.submit(self.client, f'/photos/{photo.pk}/', '添加候选Birdus species2')
        draft = response
        self.assertEqual(set(draft.context['selected_ids']), {str(species[0].pk), str(species[1].pk)})
        final = ConfirmationPage(draft).submit(self.client, f'/photos/{photo.pk}/', '确认并归档')
        self.assertEqual(final.status_code, 303)
        self.assertEqual(photo.confirmed_species.count(), 3)
        self.assertTrue(path.exists())

    def test_stale_candidate_button_is_rejected_and_draft_survives(self):
        photo, _, run, species = self.candidates()
        response = self.client.post(f'/photos/{photo.pk}/?candidate_rank=1&candidate_run={uuid.uuid4()}',
            {'draft_action': 'candidate', 'species_ids': [str(species[1].pk)],
             'local_names': ['草稿鸟'], 'metadata_version': 1, 'request_id': str(uuid.uuid4())})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['selected_ids'], [str(species[1].pk)])
        self.assertEqual(response.context['local_names'], ['草稿鸟'])
        self.assertFalse(ArchiveOperation.objects.exists())

    def test_unknown_and_close_policy_manual_first_scores_closed(self):
        photo, _, run, _ = self.candidates()
        response = self.client.get(f'/photos/{photo.pk}/')
        self.assertContains(response, '阈值未校验')
        self.assertTrue(response.context['candidates']['manual_first'])
        body = response.content.decode()
        self.assertLess(body.index('id="species-search"'), body.index('data-recognition-candidates'))
        self.assertNotIn('data-recognition-scores open', body)
        self.assertNotIn('data-recognition-candidates open', body)
        self.assertContains(response, '模型分数')

    def test_versioned_margin_uses_frozen_top1_top2_and_source_is_visible(self):
        photo, _, run, _ = self.candidates(policy={'score_kind': 'similarity', 'label_digest': 'b'*64,
            'threshold_version': 'fixture-policy-1', 'margin_threshold': 0.15,
            'calibration_evidence': 'synthetic calibration fixture, not a real benchmark'})
        response = self.client.get(f'/photos/{photo.pk}/')
        self.assertEqual(response.context['candidates']['ambiguity'], 'close')
        self.assertContains(response, 'fixture-policy-1')
        self.assertContains(response, 'Top1−Top2')
        self.assertContains(response, 'similarity')
        self.assertContains(response, 'b'*64)

    def test_candidate_view_uses_active_snapshot_while_rerun_pending(self):
        from archive.services.recognition import request_rerun
        photo, _, run, _ = self.candidates(1)
        request_rerun(photo.pk, 1, uuid.uuid4())
        response = self.client.get(f'/photos/{photo.pk}/')
        self.assertContains(response, 'data-recognition-candidate=', count=1)
        self.assertEqual(response.context['candidates']['run'].pk, run.pk)

    def test_separated_frozen_margin_opens_candidates_but_scores_stay_closed(self):
        photo, _, _, _ = self.candidates(policy={'score_kind': 'similarity', 'label_digest': 'b'*64,
            'threshold_version': 'fixture-policy-1', 'margin_threshold': 0.01,
            'calibration_evidence': 'synthetic fixture'})
        response = self.client.get(f'/photos/{photo.pk}/')
        self.assertEqual(response.context['candidates']['ambiguity'], 'separated')
        self.assertFalse(response.context['candidates']['manual_first'])
        self.assertNotContains(response, '<details data-recognition-scores open')
        self.assertNotContains(response, 'checked')

    def test_incomplete_policy_is_conservative_and_never_guesses_a_threshold(self):
        for index, threshold in enumerate((True, -1, '0.05', None)):
            # Reuse one frozen artifact by testing its read-only context with an isolated override.
            if index == 0:
                photo, _, run, _ = self.candidates()
            from unittest.mock import patch
            from archive.views import candidates_context
            run.artifact.manifest = {'margin_threshold': threshold, 'threshold_version': 'fixture'}
            with patch('archive.views.recognition_summary', return_value={'active_run': run}):
                self.assertEqual(candidates_context(photo)['ambiguity'], 'unknown')


class RecognitionStatusViewTests(RecognitionFixture):
    candidates = RecognitionCandidateViewTests.candidates

    def test_status_exposes_run_generation_and_review_count_without_writes(self):
        photo, _, run, _ = self.candidates(1)
        before = list(RecognitionRun.objects.values())
        response = self.client.get('/recognition/status/', {'photo_id': [str(photo.pk)]})
        row = response.json()['photos'][0]
        self.assertEqual(row.get('run_id'), str(run.pk), 'poll必须带run和代次以拒绝晚响应')
        self.assertEqual(row.get('generation'), 1)
        self.assertEqual(row.get('active_generation'), 1)
        self.assertEqual(row.get('active_run_id'), str(run.pk))
        self.assertEqual(response.json().get('review_count'), 1)
        self.assertEqual(response.json()['pending_count'], 0)
        self.assertFalse(row['retry_allowed'])
        self.assertEqual(list(RecognitionRun.objects.values()), before)
        self.assertIn('no-store', response['Cache-Control'])

    def test_status_during_rerun_distinguishes_current_and_displayed_snapshot(self):
        from archive.services.recognition import request_rerun
        photo, _, run, _ = self.candidates(1)
        new = request_rerun(photo.pk, 1, uuid.uuid4())
        response = self.client.get('/recognition/status/', {'photo_id': [str(photo.pk)]})
        row = response.json()['photos'][0]
        self.assertEqual((row.get('run_id'), row.get('generation')), (str(new.pk), 2))
        self.assertEqual((row.get('active_run_id'), row.get('active_generation')), (str(run.pk), 1))
        self.assertTrue(row['rerun_allowed'])

    def test_retry_controls_are_visible_only_for_temporary_failed_runs(self):
        photo, _, run, _ = self.candidates(1)
        for error, expected in [('temporarily_unavailable', True), ('database_busy', True),
                ('source_changed', False), ('model_memory_exceeded', False)]:
            RecognitionRun.objects.filter(pk=run.pk).update(state='failed', safe_error=error)
            response = self.client.get(f'/photos/{photo.pk}/')
            status = self.client.get('/recognition/status/', {'photo_id': [str(photo.pk)]}).json()['photos'][0]
            self.assertEqual(status.get('retry_allowed'), expected)
            from html.parser import HTMLParser
            class RetryButton(HTMLParser):
                visible = False
                def handle_starttag(self, tag, attrs):
                    attrs = dict(attrs)
                    if attrs.get('value') == 'recognition_retry': self.visible = 'hidden' not in attrs
            parsed = RetryButton(); parsed.feed(response.content.decode())
            self.assertEqual(parsed.visible, expected)
        self.assertContains(response, 'value="recognition_rerun"')
        self.assertFalse(ArchiveOperation.objects.exists())

    def test_native_recognition_action_preserves_manual_draft(self):
        from archive.tests.test_confirmation import ConfirmationPage
        photo, _, run, species = self.candidates(1)
        page = self.client.get(f'/photos/{photo.pk}/?species_ids={species[0].pk}')
        self.assertContains(page, 'form="confirmation-form"')
        redirect = self.client.post(f'/photos/{photo.pk}/', {'draft_action': 'recognition_rerun',
            'species_ids': [str(species[0].pk)], 'local_names': ['保留漏种'],
            'metadata_version': 1, 'request_id': str(uuid.uuid4())})
        self.assertEqual(redirect.status_code, 303)
        response = self.client.get(redirect['Location'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected_ids'], [str(species[0].pk)])
        self.assertEqual(response.context['local_names'], ['保留漏种'])
        self.assertEqual(RecognitionRun.objects.filter(photo=photo).count(), 2)
        self.assertFalse(ArchiveOperation.objects.exists())

    def test_status_regions_do_not_wrap_manual_form_and_workbench_remains_selectable(self):
        from html.parser import HTMLParser
        class Regions(HTMLParser):
            def __init__(self, content):
                super().__init__(); self.stack = []; self.bad = False; self.feed(content)
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'form' and any('data-recognition-status' in node for node in self.stack):
                    self.bad = True
                if tag not in ('input', 'img', 'br', 'meta', 'link', 'hr'):
                    self.stack.append(attrs)
            def handle_endtag(self, tag):
                if self.stack: self.stack.pop()
        photo, _, _, _ = self.candidates(1)
        response = self.client.get(f'/photos/{photo.pk}/')
        self.assertContains(response, 'data-recognition-poll=')
        self.assertFalse(Regions(response.content.decode()).bad)
        workbench = self.client.get('/review/')
        self.assertContains(workbench, 'data-recognition-count')
        self.assertContains(workbench, f'data-photo-id="{photo.pk}"')
        self.assertContains(workbench, 'name="photo_ids"')

    def test_native_retry_failure_preserves_inputs_and_no_archive_is_created(self):
        photo, _, run, species = self.candidates(1)
        response = self.client.post(f'/photos/{photo.pk}/', {'draft_action': 'recognition_retry',
            'species_ids': [str(species[0].pk)], 'local_names': ['草稿'], 'metadata_version': 1,
            'recognition_request_id': str(uuid.uuid4()), 'request_id': str(uuid.uuid4())})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['local_names'], ['草稿'])
        self.assertEqual(response.context['selected_ids'], [str(species[0].pk)])
        self.assertFalse(ArchiveOperation.objects.exists())


class RecognitionFallbackViewTests(RecognitionFixture):
    def test_rerun_validates_entire_bounded_explicit_set_before_writes(self):
        photo, _ = self.imported()
        one = {'photo_id': str(photo.pk), 'metadata_version': 1}
        payloads = [[], {}, {'request_id': 'bad', 'photos': [one]},
            {'request_id': str(uuid.uuid4()), 'photos': [one]*201},
            {'request_id': str(uuid.uuid4()), 'photos': [one, {'photo_id': 'bad', 'metadata_version': 1}]},
            {'request_id': str(uuid.uuid4()), 'photos': [one, one]},
            {'request_id': str(uuid.uuid4()), 'photos': [{**one, 'metadata_version': True}]},
            {'request_id': str(uuid.uuid4()), 'photos': []}]
        for payload in payloads:
            with self.subTest(payload=str(payload)[:90]):
                response = self.client.post('/recognition/rerun/', payload, content_type='application/json')
                self.assertEqual(response.status_code, 400)
                self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(RecognitionRun.objects.count(), 1)

    def test_rerun_partially_accepts_only_current_review_objects(self):
        from archive.services.confirmation import confirm_photo
        first, _ = self.imported()
        second, _ = self.imported('two.jpg', 'blue')
        confirm_photo(second.pk, 1, uuid.uuid4(), 'undetermined', [], [])
        response = self.client.post('/recognition/rerun/', {'request_id': str(uuid.uuid4()), 'photos': [
            {'photo_id': str(first.pk), 'metadata_version': 1},
            {'photo_id': str(second.pk), 'metadata_version': 1},
            {'photo_id': str(uuid.uuid4()), 'metadata_version': 1}]}, content_type='application/json')
        rows = response.json()['results']
        self.assertEqual([row['accepted'] for row in rows], [True, False, False])
        self.assertEqual([row.get('reason') for row in rows], [None, 'photo_changed', 'photo_unavailable'])
        self.assertEqual(RecognitionRun.objects.filter(photo=second).count(), 1)

    def test_recognition_methods_csrf_host_and_no_store(self):
        photo, _ = self.imported()
        retry_url = f'/photos/{photo.pk}/recognition/retry/'
        for url in ('/recognition/rerun/', retry_url):
            self.assertEqual(self.client.get(url).status_code, 405)
            response = Client(enforce_csrf_checks=True).post(url, '{}', content_type='application/json')
            self.assertEqual(response.status_code, 403)
            self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(self.client.post('/recognition/status/').status_code, 405)
        self.assertEqual(self.client.get('/recognition/status/', HTTP_HOST='foreign.invalid').status_code, 400)
        self.assertEqual(RecognitionRun.objects.count(), 1)

    def test_status_returns_fixed_messages_and_preserves_candidates(self):
        photo, _ = self.imported()
        RecognitionRun.objects.filter(photo=photo).update(safe_error='/private/secret <script>')
        before = list(RecognitionRun.objects.values())
        response = self.client.get('/recognition/status/', {'photo_id': [str(photo.pk)]})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('/private', response.content.decode())
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(list(RecognitionRun.objects.values()), before)
        self.assertEqual(self.client.get('/recognition/status/', {'photo_id': ['bad']}).status_code, 400)
        self.assertEqual(self.client.get('/recognition/status/', {'photo_id': [str(photo.pk)]*201}).status_code, 400)

    def test_retry_requires_temporary_failure_and_is_idempotent(self):
        from archive.models import ModelArtifact
        from archive.services.recognition import set_current_model
        artifact = ModelArtifact.objects.create(manifest_digest='b'*64, provider='test-only',
            version='1', revision='retry', manifest={}, selection_state='selected')
        set_current_model(artifact.pk)
        photo, _ = self.imported()
        url = f'/photos/{photo.pk}/recognition/retry/'
        body = {'metadata_version': 1, 'request_id': str(uuid.uuid4())}
        self.assertFalse(self.client.post(url, body, content_type='application/json').json()['accepted'])
        RecognitionRun.objects.filter(photo=photo).update(state='failed', safe_error='temporarily_unavailable', attempts=3, cycle_attempts=3)
        for _ in range(2):
            response = self.client.post(url, body, content_type='application/json')
            self.assertTrue(response.json()['accepted'])
        self.assertEqual(RecognitionRun.objects.filter(photo=photo).count(), 2)

    def test_explicit_rerun_is_idempotent_and_get_does_not_write(self):
        photo, _ = self.imported()
        before = RecognitionRun.objects.count()
        response = self.client.get('/recognition/status/')
        self.assertEqual(response.status_code, 200, '只读识别状态端点应存在')
        self.assertEqual(RecognitionRun.objects.count(), before)
        request_id = str(uuid.uuid4())
        payload = {'request_id': request_id, 'photos': [{'photo_id': str(photo.pk), 'metadata_version': 1}]}
        for _ in range(2):
            response = self.client.post('/recognition/rerun/', payload, content_type='application/json')
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()['results'][0]['accepted'])
        self.assertEqual(RecognitionRun.objects.filter(photo=photo).count(), 2)

    def test_unconfigured_detail_shows_reason_before_manual_form(self):
        photo, path = self.imported()
        before = path.read_bytes()
        response = self.client.get(f'/photos/{photo.pk}/')
        self.assertContains(response, '本地识别模型未配置')
        self.assertContains(response, 'href="#species-search"')
        body = response.content.decode()
        self.assertLess(body.index('本地识别模型未配置'), body.index('data-archive-confirm'))
        for text in ('搜索鸟种', '本地鸟名', '鸟种（可多选）', '确认并归档', '无法确定并归档'):
            self.assertContains(response, text)
        self.assertNotContains(response, 'checked')
        self.assertIn('no-store', response['Cache-Control'])
        photo.refresh_from_db()
        self.assertEqual((photo.state, photo.metadata_version), ('review_pending', 1))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(RecognitionRun.objects.count(), 1)
        self.assertFalse(ArchiveOperation.objects.exists())

    def test_history_without_run_does_not_enqueue_on_get(self):
        photo = Photo.objects.create(sha256='e'*64, byte_size=42, preview_key='')
        for _ in range(2):
            self.assertContains(self.client.get(f'/photos/{photo.pk}/'), '尚未识别')
        self.assertFalse(RecognitionState.objects.exists())
        self.assertFalse(RecognitionRun.objects.exists())
        Photo.objects.filter(pk=photo.pk).update(state='archived')
        response = self.client.get(f'/photos/{photo.pk}/')
        self.assertContains(response, '尚未识别')
        self.assertNotContains(response, 'href="#species-search"')

    def test_errors_use_safe_messages_and_never_echo_internal_values(self):
        photo, _ = self.imported()
        RecognitionRun.objects.filter(photo=photo).update(state='failed', safe_error='/private/model-secret <script>')
        response = self.client.get(f'/photos/{photo.pk}/')
        self.assertContains(response, '识别暂不可用')
        self.assertNotContains(response, '/private/model-secret')
        self.assertNotContains(response, '<script>')

    def test_csrf_and_host_are_still_enforced(self):
        photo, _ = self.imported()
        url = f'/photos/{photo.pk}/'
        client = Client(enforce_csrf_checks=True)
        rejected = client.post(url, {'classification': 'undetermined'})
        self.assertEqual(rejected.status_code, 403)
        self.assertIn('no-store', rejected['Cache-Control'])
        rejected = client.get(url, HTTP_HOST='foreign.invalid')
        self.assertEqual(rejected.status_code, 400)
        # Django rejects the Host before the photo view / private-response middleware.
        self.assertNotIn(b'one.jpg', rejected.content)
        self.assertFalse(ArchiveOperation.objects.exists())

    def submit(self, photo, classification='species', names=None, enhanced=False):
        client = Client(enforce_csrf_checks=True)
        url = f'/photos/{photo.pk}/'
        client.get(url)
        return client.post(url, {'metadata_version': 1, 'request_id': str(uuid.uuid4()),
            'classification': classification, 'local_names': names or [],
            'csrfmiddlewaretoken': client.cookies['csrftoken'].value},
            **({'HTTP_X_ARCHIVE_CONFIRM': 'true'} if enhanced else {}))

    def test_custom_multiple_species_can_confirm_with_no_model(self):
        photo, _ = self.imported()
        response = self.submit(photo, names=['树边鸟', '池边鸟'])
        self.assertEqual(response.status_code, 303)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(photo.confirmed_species.count(), 2)
        self.assertEqual(ArchiveOperation.objects.get(photo=photo).classification, 'species')

    def test_explicit_undetermined_remains_available_with_enhanced_post(self):
        photo, _ = self.imported()
        response = self.submit(photo, classification='undetermined', enhanced=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['accepted'])
        self.assertEqual(response.json()['next_url'], '/review/')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(ArchiveOperation.objects.get(photo=photo).classification, 'undetermined')
        self.assertFalse(photo.confirmed_species.exists())
