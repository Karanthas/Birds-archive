import hashlib
import os
import tempfile
import uuid
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.db import connection, IntegrityError, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings, Client

from archive.models import Photo, Job, Species, SourceSnapshot
from archive.services.intake import scan_once
from archive.services.jobs import process_source
from archive.services.paths import open_root
from archive.tests.fixtures import jpeg


class ConfirmationPage(HTMLParser):
    """Exercise native successful-control submission without relying on JavaScript."""
    def __init__(self, response):
        super().__init__()
        self.forms, self.inputs, self.buttons = [], [], []
        self.current_form = self.current_button = None
        self.feed(response.content.decode())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self.current_form = attrs
            self.forms.append(attrs)
        elif tag in ('input', 'button') and self.current_form is not None:
            attrs['form'] = self.current_form
            if tag == 'input':
                self.inputs.append(attrs)
            else:
                attrs['text'] = ''
                self.buttons.append(attrs)
                self.current_button = attrs

    def handle_data(self, data):
        if self.current_button is not None:
            self.current_button['text'] += data

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current_form = None
        elif tag == 'button':
            self.current_button = None

    def set_value(self, name, value):
        matches = [field for field in self.inputs if field.get('name') == name]
        matches[-1]['value'] = value

    def check(self, ident, checked=True):
        for field in self.inputs:
            if field.get('name') == 'species_ids' and field.get('value') == str(ident):
                if checked:
                    field['checked'] = ''
                else:
                    field.pop('checked', None)

    def submit(self, client, url, label, **headers):
        matches = [button for button in self.buttons
            if button.get('aria-label', button['text'].strip()) == label]
        assert matches, f'缺少可提交当前草稿的操作：{label}'
        button = matches[0]
        form = button['form']
        values = {}
        for field in self.inputs:
            name = field.get('name')
            if field['form'] is not form or not name or 'disabled' in field:
                continue
            if field.get('type') == 'checkbox' and 'checked' not in field:
                continue
            values.setdefault(name, []).append(field.get('value', ''))
        if button.get('name'):
            values.setdefault(button['name'], []).append(button.get('value', ''))
        method = button.get('formmethod', form.get('method', 'get'))
        action = button.get('formaction', form.get('action', url))
        self.last_method, self.last_action = method, action
        response = getattr(client, method)(action, values, **headers)
        if button.get('name') == 'draft_action' and response.status_code == 303:
            draft_redirect = response
            response = client.get(response.url)
            response.draft_redirect = draft_redirect
        return response


class ConfirmationFixture(TransactionTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='birds-confirmation-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inbox, self.data = self.root/'inbox', self.root/'data'
        self.inbox.mkdir(); self.data.mkdir()
        overrides = override_settings(BIRDS_INBOX_ROOT=str(self.inbox), BIRDS_DATA_ROOT=str(self.data),
            BIRDS_HANDOFF_MODE='published', BIRDS_PUBLISHED_CONTRACT=True)
        overrides.enable(); self.addCleanup(overrides.disable)
        self.snapshot = SourceSnapshot.objects.create(source='synthetic', version='1', sha256='a'*64,
            acquired_at='2026-10-16', download_page_url='https://example.org/catalog',
            license_url='https://example.org/license', attribution='synthetic', row_count=2,
            species_count=2, manifest={})
        self.species = Species.objects.create(scientific_name='Ardea alba', scientific_key='ardea alba',
            chinese_name='白鹭', search_key='白鹭 ardea alba', source_snapshot=self.snapshot)

    def imported(self, name='one.jpg', color='red'):
        path = self.inbox/name
        jpeg(path, color=color)
        scan_once()
        inbox_fd, data_fd = open_root(self.inbox), open_root(self.data)
        try:
            for job in Job.objects.filter(kind='import', state='pending'):
                self.assertTrue(process_source(job, inbox_fd, data_fd))
        finally:
            os.close(inbox_fd); os.close(data_fd)
        return Photo.objects.get(observations__source__relative_path=name), path

    def payload(self, photo, **extra):
        return dict(metadata_version=photo.metadata_version, request_id=str(uuid.uuid4()),
            classification='species', species_ids=[str(self.species.pk)], **extra)

    def post(self, photo, data=None, **headers):
        return self.client.post(f'/photos/{photo.pk}/', data or self.payload(photo), **headers)


class ConfirmationFlowTests(ConfirmationFixture):
    def test_all_draft_actions_post_with_csrf_and_redirect_only_allowed_fields(self):
        from archive.models import ArchiveOperation, PhotoSpecies
        from archive.services.confirmation import ConfirmationValidation
        photo, path = self.imported()
        url = f'/photos/{photo.pk}/'
        secured = Client(enforce_csrf_checks=True)
        page = ConfirmationPage(secured.get(url))
        token = next(field['value'] for field in page.inputs if field.get('name') == 'csrfmiddlewaretoken')
        request_id = str(uuid.uuid4())
        allowed = {'q', 'species_page', 'species_ids', 'local_names', 'request_id', 'metadata_version'}
        for action in ('search', 'add', 'remove', 'page', 'append'):
            with self.subTest(action=action):
                data = {'draft_action': action, 'q': '白鹭', 'species_page': '1',
                    'species_ids': [str(self.species.pk)], 'local_names': ['当前本地鸟', ''],
                    'metadata_version': '1', 'request_id': request_id, 'csrfmiddlewaretoken': token,
                    'untrusted_field': 'discard-me'}
                target = url
                if action in ('add', 'remove'):
                    target += f'?draft_species={self.species.pk}'
                elif action == 'page':
                    target += '?draft_page=2'
                with mock.patch('archive.views.confirm_photo',
                        side_effect=ConfirmationValidation('草稿不得调用确认')) as confirm:
                    response = secured.post(target, data, HTTP_X_ARCHIVE_CONFIRM='true')
                self.assertEqual(response.status_code, 303)
                confirm.assert_not_called()
                query = parse_qs(urlsplit(response.url).query)
                self.assertLessEqual(set(query), allowed)
                self.assertNotIn('csrfmiddlewaretoken', response.url)
                self.assertNotIn(token, response.url)
                self.assertEqual(query['request_id'], [request_id])
                self.assertEqual(query['metadata_version'], ['1'])
                self.assertEqual(query['local_names'], ['当前本地鸟'])
                self.assertEqual(query['species_page'], ['2' if action == 'page' else '1'])
                self.assertEqual(query.get('species_ids', []), [] if action == 'remove' else [str(self.species.pk)])
                self.assertEqual(secured.get(response.url).status_code, 200)
                data.pop('csrfmiddlewaretoken')
                self.assertEqual(secured.post(target, data).status_code, 403)
        legacy = self.client.get(url, {'add_species': str(self.species.pk), 'q': '白鹭',
            'local_names': ['旧深链接本地鸟'], 'request_id': request_id, 'metadata_version': '1'})
        self.assertEqual(legacy.status_code, 200)
        self.assertIn(str(self.species.pk), legacy.context['selected_ids'])
        self.assertIn('旧深链接本地鸟', legacy.context['local_names'])
        legacy = self.client.get(url, {'species_ids': str(self.species.pk),
            'remove_species': str(self.species.pk)})
        self.assertEqual(legacy.context['selected_ids'], [])
        self.assertFalse(ArchiveOperation.objects.exists())
        self.assertFalse(PhotoSpecies.objects.exists())
        self.assertFalse(Job.objects.filter(kind='archive').exists())
        photo.refresh_from_db()
        self.assertEqual((photo.state, photo.metadata_version), ('review_pending', 1))
        self.assertTrue(path.exists())

    def test_unknown_or_ambiguous_draft_action_never_falls_through_to_confirmation(self):
        from archive.models import ArchiveOperation
        from archive.services.confirmation import ConfirmationValidation
        photo, _ = self.imported()
        for changes in ({'draft_action': 'unknown'}, {'draft_action': ''},
                {'draft_action': ['search', 'append']}, {'draft_action': 'search'}):
            with self.subTest(changes=changes):
                data = self.payload(photo)
                data.update(changes)
                with mock.patch('archive.views.confirm_photo',
                        side_effect=ConfirmationValidation('草稿不得调用确认')) as confirm:
                    response = self.post(photo, data)
                self.assertEqual(response.status_code, 400)
                confirm.assert_not_called()
        for target in ('?draft_species=invalid', '?draft_species=one&draft_species=two', ''):
            data = self.payload(photo)
            data.pop('classification')
            data['draft_action'] = 'add'
            self.assertEqual(self.client.post(f'/photos/{photo.pk}/'+target, data).status_code, 400)
        data['draft_action'] = 'page'
        for target in ('', '?draft_page=0', '?draft_page=bad', '?draft_page=1&draft_page=2'):
            self.assertEqual(self.client.post(f'/photos/{photo.pk}/'+target, data).status_code, 400)
        self.assertFalse(ArchiveOperation.objects.exists())
        self.assertFalse(Job.objects.filter(kind='archive').exists())

    def test_rendered_draft_buttons_keep_token_out_of_request_urls(self):
        for number in range(30):
            Species.objects.create(scientific_name=f'Draft {number:02d}', scientific_key=f'draft {number:02d}',
                chinese_name=f'查询鸟{number:02d}', search_key='查询鸟', source_snapshot=self.snapshot)
        photo, _ = self.imported()
        url = f'/photos/{photo.pk}/'
        secured = Client(enforce_csrf_checks=True)
        initial = secured.get(url, {'q': '查询鸟', 'species_ids': str(self.species.pk)})
        selected = initial.context['species_search_page'][0]
        for label in ('搜索', f'添加{selected.display_name}', f'移除{self.species.display_name}',
                '下一页鸟种', '继续添加'):
            with self.subTest(label=label):
                page = ConfirmationPage(initial)
                page.set_value('local_names', '刚输入的鸟')
                response = page.submit(secured, url, label)
                self.assertEqual(page.last_method, 'post')
                self.assertNotIn('csrfmiddlewaretoken', page.last_action)
                self.assertTrue(hasattr(response, 'draft_redirect'))
                self.assertNotIn('csrfmiddlewaretoken', response.draft_redirect.url)
                self.assertIn('刚输入的鸟', response.context['local_names'])

    def test_current_checks_and_local_input_survive_native_search(self):
        photo, path = self.imported()
        url = f'/photos/{photo.pk}/'
        page = ConfirmationPage(self.client.get(url, {'q': '白鹭'}))
        page.check(self.species.pk)
        page.set_value('q', '另外一种')
        page.set_value('local_names', '窗边的小鸟')
        response = page.submit(self.client, url, '搜索')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected_ids'], [str(self.species.pk)])
        self.assertIn('窗边的小鸟', response.context['local_names'])
        photo.refresh_from_db()
        self.assertEqual((photo.state, photo.metadata_version), ('review_pending', 1))
        self.assertTrue(path.exists())
        self.assertFalse(Job.objects.filter(kind='archive').exists())

    def test_unchecked_species_stays_removed_on_native_search(self):
        photo, _ = self.imported()
        url = f'/photos/{photo.pk}/'
        page = ConfirmationPage(self.client.get(url, {'species_ids': str(self.species.pk)}))
        page.check(self.species.pk, False)
        page.set_value('q', '另外一种')
        page.set_value('local_names', '未提交名称')
        response = page.submit(self.client, url, '搜索')
        self.assertEqual(response.context['selected_ids'], [])
        self.assertIn('未提交名称', response.context['local_names'])

    def test_current_draft_survives_native_add_remove_and_pagination(self):
        for number in range(30):
            Species.objects.create(scientific_name=f'Synthetic {number:02d}',
                scientific_key=f'synthetic {number:02d}', chinese_name=f'查询鸟{number:02d}',
                search_key='查询鸟', source_snapshot=self.snapshot)
        photo, _ = self.imported()
        url = f'/photos/{photo.pk}/'
        response = self.client.get(url, {'q': '查询鸟', 'species_ids': str(self.species.pk)})
        first, second = response.context['species_search_page'][:2]
        page = ConfirmationPage(response)
        page.check(self.species.pk, False)
        page.check(second.pk)
        page.set_value('local_names', '刚输入的本地鸟')
        response = page.submit(self.client, url, f'添加{first.display_name}')
        self.assertIn(str(first.pk), response.context['selected_ids'])
        self.assertIn(str(second.pk), response.context['selected_ids'])
        self.assertNotIn(str(self.species.pk), response.context['selected_ids'])
        self.assertIn('刚输入的本地鸟', response.context['local_names'])
        page = ConfirmationPage(response)
        page.check(second.pk, False)
        page.set_value('local_names', '翻页前的鸟')
        response = page.submit(self.client, url, '下一页鸟种')
        self.assertEqual(response.context['species_search_page'].number, 2)
        self.assertIn(str(first.pk), response.context['selected_ids'])
        self.assertNotIn(str(second.pk), response.context['selected_ids'])
        self.assertIn('翻页前的鸟', response.context['local_names'])
        page = ConfirmationPage(response)
        page.set_value('local_names', '移除前的鸟')
        response = page.submit(self.client, url, f'移除{first.display_name}')
        self.assertNotIn(str(first.pk), response.context['selected_ids'])
        self.assertIn('移除前的鸟', response.context['local_names'])
        page = ConfirmationPage(response)
        response = page.submit(self.client, url, '上一页鸟种')
        self.assertEqual(response.context['species_search_page'].number, 1)
        self.assertIn('移除前的鸟', response.context['local_names'])
        self.assertFalse(Job.objects.filter(kind='archive').exists())

    def test_native_final_confirm_after_search_retains_csrf_and_species(self):
        from archive.models import ArchiveOperation
        photo, _ = self.imported()
        url = f'/photos/{photo.pk}/'
        secured = Client(enforce_csrf_checks=True)
        page = ConfirmationPage(secured.get(url, {'q': '白鹭'}))
        page.check(self.species.pk)
        page.set_value('local_names', '另一个本地鸟')
        response = page.submit(secured, url, '搜索')
        self.assertIn(str(self.species.pk), response.context['selected_ids'])
        page = ConfirmationPage(response)
        response = page.submit(secured, url, '确认并归档')
        self.assertEqual(response.status_code, 303)
        self.assertEqual(len(ArchiveOperation.objects.get(photo=photo).species_ids_snapshot), 2)

    def test_enhanced_final_confirm_uses_same_current_draft_and_csrf(self):
        from archive.models import ArchiveOperation
        photo, _ = self.imported()
        url = f'/photos/{photo.pk}/'
        secured = Client(enforce_csrf_checks=True)
        page = ConfirmationPage(secured.get(url, {'q': '白鹭'}))
        page.check(self.species.pk)
        page.set_value('local_names', '增强路径本地鸟')
        response = page.submit(secured, url, '搜索')
        response = ConfirmationPage(response).submit(secured, url, '确认并归档',
            HTTP_X_ARCHIVE_CONFIRM='true', HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['accepted'])
        self.assertEqual(response.json()['next_url'], '/review/')
        self.assertEqual(len(ArchiveOperation.objects.get(photo=photo).species_ids_snapshot), 2)

    def test_native_add_local_keeps_names_without_deciding_classification(self):
        photo, _ = self.imported()
        url = f'/photos/{photo.pk}/'
        response = self.client.get(url)
        for name in ('第一只本地鸟', '第二只本地鸟'):
            page = ConfirmationPage(response)
            page.set_value('local_names', name)
            response = page.submit(self.client, url, '继续添加')
        self.assertEqual(response.context['local_names'], ['第一只本地鸟', '第二只本地鸟'])
        self.assertEqual(response.context['classification'], 'species')
        self.assertEqual(response.context['selected_ids'], [])
        self.assertFalse(Job.objects.filter(kind='archive').exists())
        photo.refresh_from_db()
        self.assertEqual((photo.metadata_version, photo.state), (1, 'review_pending'))

    def test_draft_navigation_keeps_old_cas_version(self):
        photo, _ = self.imported()
        url = f'/photos/{photo.pk}/'
        page = ConfirmationPage(self.client.get(url, {'species_ids': str(self.species.pk)}))
        Photo.objects.filter(pk=photo.pk).update(metadata_version=2, user_location='其他标签保存')
        response = page.submit(self.client, url, '搜索')
        response = ConfirmationPage(response).submit(self.client, url, '确认并归档')
        self.assertEqual(response.status_code, 409)
        self.assertFalse(Job.objects.filter(kind='archive').exists())

    def test_confirm_advances_without_file_io(self):
        photo, path = self.imported()
        next_photo, _ = self.imported('two.jpg', 'blue')
        before = path.read_bytes()
        with mock.patch('archive.views.open_root', side_effect=AssertionError('HTTP must not open files')), \
             mock.patch('archive.services.images.content_hash', side_effect=AssertionError('HTTP must not read files')):
            response = self.post(photo)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.url, f'/photos/{next_photo.pk}/')
        from archive.models import ArchiveOperation, ManagedOriginal, ArchiveCompletion, PhotoSpecies
        operation = ArchiveOperation.objects.get(photo=photo)
        self.assertEqual(operation.job.kind, 'archive')
        self.assertEqual(operation.job.state, 'awaiting_executor')
        self.assertEqual(operation.roots_snapshot, {'binding_state': 'unbound'})
        self.assertEqual(operation.expected_sha256, hashlib.sha256(before).hexdigest())
        self.assertEqual(operation.source_observation.photo_id, photo.pk)
        self.assertEqual(operation.capture_snapshot['local'], None)
        self.assertEqual(PhotoSpecies.objects.filter(photo=photo).count(), 1)
        self.assertFalse(ManagedOriginal.objects.exists())
        self.assertFalse(ArchiveCompletion.objects.exists())
        photo.refresh_from_db()
        self.assertEqual((photo.state, photo.metadata_version), ('confirmed', 2))
        self.assertEqual(path.read_bytes(), before)
        self.assertContains(self.client.get(f'/photos/{photo.pk}/'), '等待归档')

    def test_queue_end(self):
        photo, _ = self.imported()
        response = self.post(photo)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.url, '/review/')
        self.assertContains(self.client.get(response.url), '审核结束')

    def test_empty_rejected(self):
        photo, path = self.imported()
        data = self.payload(photo); data['species_ids'] = []
        self.assertEqual(self.post(photo, data).status_code, 400)
        photo.refresh_from_db(); self.assertEqual(photo.state, 'review_pending')
        self.assertTrue(path.exists())
        self.assertFalse(Job.objects.filter(kind='archive').exists())

    def test_unsubmitted_untouched(self):
        photo, path = self.imported()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(self.client.get(f'/photos/{photo.pk}/').status_code, 200)
        photo.refresh_from_db(); self.assertEqual(photo.metadata_version, 1)
        self.assertEqual(photo.state, 'review_pending')
        self.assertFalse(Job.objects.filter(kind='archive').exists())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_anonymous_csrf_and_enhanced_next_url(self):
        photo, _ = self.imported()
        secured = Client(enforce_csrf_checks=True)
        self.assertEqual(secured.post(f'/photos/{photo.pk}/', self.payload(photo)).status_code, 403)
        response = self.post(photo, HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response['HX-Redirect'].startswith('/'))
        self.assertEqual(response['X-Next-URL'], '/review/')
        self.assertIn('no-store', response['Cache-Control'])

    def test_effective_capture_preserves_precision_and_rejects_invalid(self):
        photo, _ = self.imported()
        self.assertTrue(hasattr(photo, 'effective_capture'))
        photo.manual_capture = {'local': '2025-12-31', 'precision': 'date', 'offset': None}
        capture = photo.effective_capture()
        self.assertEqual(capture['local'], '2025-12-31')
        self.assertEqual(capture['sort_key'], '2025-12-31')
        self.assertEqual((capture['year'], capture['month']), ('2025', '2025-12'))
        photo.manual_capture = {'local': '2025-02-30', 'precision': 'date', 'offset': None}
        self.assertIsNone(photo.effective_capture()['local'])

    def test_confirmation_freezes_full_supported_exif_subseconds(self):
        for index, fraction in enumerate(('1234568', '12345678901234567890123456789012')):
            with self.subTest(fraction=fraction):
                photo, _ = self.imported(f'precision{index}.jpg', color=(index*60, 10, 20))
                local = '2026-10-01 12:00:00.'+fraction
                Photo.objects.filter(pk=photo.pk).update(captured_local=local,
                    capture_quality='reliable', capture_precision='subsecond')
                self.assertEqual(self.post(photo).status_code, 303)
                snapshot = photo.archive_operation.capture_snapshot
                self.assertEqual(snapshot['local'], local)
                self.assertEqual(snapshot['sort_key'], '2026-10-01 12:00:00.'+fraction.ljust(32, '0'))
                self.assertEqual((snapshot['precision'], snapshot['source']), ('subsecond', 'exif'))
                self.assertContains(self.client.get(f'/photos/{photo.pk}/'), local)

    def test_effective_capture_normalizes_equal_fraction_values_without_loss(self):
        photo, _ = self.imported()
        values = []
        for fraction in ('12', '1200000', '12000000000000000000000000000000'):
            local = '2026-10-01 12:00:00.'+fraction
            photo.manual_capture = {'local': local, 'precision': 'subsecond', 'offset': None}
            snapshot = photo.effective_capture()
            self.assertEqual(snapshot['local'], local)
            values.append(snapshot['sort_key'])
        self.assertEqual(len(set(values)), 1)
        self.assertEqual(len(values[0]), 52)

    def test_effective_capture_preserves_legacy_iso_separator(self):
        photo, _ = self.imported()
        for precision, fraction in (('second', ''), ('subsecond', '.12345678901234567890123456789012')):
            with self.subTest(precision=precision):
                photo.manual_capture = {'local': '2025-01-01T00:30:00'+fraction,
                                        'precision': precision, 'offset': 480}
                capture = photo.effective_capture()
                self.assertEqual(capture['local'], '2025-01-01 00:30:00'+fraction)
                self.assertEqual(capture['year'], '2025')
                self.assertEqual(capture['offset'], 480)

    def test_effective_capture_rejects_malformed_fraction(self):
        photo, _ = self.imported()
        for fraction in ('', '1'*33, '１２３', '123Z', '12+00:00', '1e2'):
            with self.subTest(fraction=fraction):
                photo.manual_capture = {'local': '2026-10-01 12:00:00.'+fraction,
                                        'precision': 'subsecond', 'offset': None}
                self.assertIsNone(photo.effective_capture()['sort_key'])


class ConfirmationFeedbackTests(ConfirmationFixture):
    def test_draft_post_javascript_keeps_native_controls_and_no_save_state(self):
        """Draft POST remains native and must not enter final-confirm JSON handling."""
        import subprocess
        from django.conf import settings
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
(async()=>{
 for(const action of ['search','add','remove','page','append']){
  let submit,prevented=false,requests=0;
  const input={disabled:false,value:'未提交的本地鸟'};
  const button={disabled:false,textContent:action,dataset:{},name:'draft_action',value:action,
   getAttribute:()=>null};
  const form={dataset:{pendingText:'正在确认…'},method:'post',elements:[input,button],
   action:'http://localhost/photos/current/',reportValidity:()=>true,
   hasAttribute:key=>['data-pending-text','data-archive-confirm'].includes(key),
   querySelector:()=>button,setAttribute:()=>{},removeAttribute:()=>{},
   addEventListener:(_,fn)=>submit=fn,append:()=>{}};
  const context={document:{querySelector:()=>null,querySelectorAll:key=>key==='form'?[form]:[],
   addEventListener:()=>{},createElement:()=>({setAttribute:()=>{},append:()=>{},focus:()=>{}})},
   window:{location:{href:'http://localhost/photos/current/'}},URL,URLSearchParams,AbortSignal,setInterval:()=>{},
   FormData:class{append(){}},fetch:()=>{requests++;throw Error('draft must stay native POST')}};
  vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
  await submit({submitter:button,preventDefault:()=>prevented=true});
  assert.equal(button.disabled,false,'draft submitter must remain a successful native control');
  assert.equal(input.disabled,false);assert.equal(input.value,'未提交的本地鸟');
  assert.equal(button.textContent,action,'draft POST must not promise a save');
  assert.notEqual(form.dataset.submitting,'true');assert.notEqual(form.dataset.resultUnknown,'true');
  assert.equal(prevented,false);assert.equal(requests,0);
 }
 console.log('DRAFT_POST_PASS');
})().catch(error=>{console.error(error);process.exit(1)});
'''
        result = subprocess.run(['node', '-e', script, str(settings.BASE_DIR/'archive/static/archive/app.js')],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('DRAFT_POST_PASS', result.stdout)

    def test_accepted_returns_same_origin_next(self):
        photo, _ = self.imported()
        next_photo, _ = self.imported('two.jpg', 'blue')
        response = self.post(photo, HTTP_X_ARCHIVE_CONFIRM='true')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['accepted'], True)
        self.assertEqual(response.json()['next_url'], f'/photos/{next_photo.pk}/')
        self.assertEqual(response.json()['operation_id'], str(photo.archive_operation.pk))
        self.assertIn('no-store', response['Cache-Control'])

    def test_failed_other_photo_status_only(self):
        other, _ = self.imported()
        self.post(other)
        current, _ = self.imported('two.jpg', 'blue')
        operation = other.archive_operation
        Job.objects.filter(pk=operation.job_id).update(state='failed', error_code='space_exhausted',
            safe_message='/private/SECRET')
        response = self.client.get('/tasks/status/?scope=archive')
        self.assertContains(response, 'data-archive-status')
        self.assertContains(response, '归档失败 1')
        self.assertContains(response, f'data-failure-key="{operation.pk}:space_exhausted:')
        self.assertNotContains(response, '<form')
        self.assertNotContains(response, '<img')
        self.assertNotContains(response, '/private/SECRET')
        self.assertNotContains(response, str(current.pk))
        page = self.client.get(f'/photos/{current.pk}/')
        self.assertContains(page, 'data-archive-status')
        self.assertContains(page, 'name="local_names"')

    def test_success_silent_fragment(self):
        photo, _ = self.imported()
        self.post(photo)
        operation = photo.archive_operation
        Job.objects.filter(pk=operation.job_id).update(state='succeeded', stage='complete')
        response = self.client.get('/tasks/status/?scope=archive')
        self.assertContains(response, '已归档 1')
        self.assertContains(response, 'data-archive-counts')
        self.assertNotContains(response, 'data-failure-key=')
        self.assertNotContains(response, '归档成功')
        self.assertNotContains(response, 'role="alert"')

    def test_unknown_result_has_verify_link(self):
        from django.urls import reverse
        photo, _ = self.imported()
        response = self.client.get(f'/photos/{photo.pk}/')
        self.assertContains(response, f'data-verify-url="{reverse("workbench")}"')
        self.assertContains(response, 'data-archive-confirm')
        response = self.post(photo, HTTP_X_ARCHIVE_CONFIRM='true')
        self.assertEqual(response.json()['next_url'], reverse('workbench'))
        self.assertContains(self.client.get(reverse('workbench')), '审核结束')

    def test_confirmation_javascript_acceptance_and_unknown_protocol(self):
        """VM protocol regression; this deliberately does not claim browser DOM evidence."""
        import subprocess
        from django.conf import settings
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
(async()=>{
 for(const mode of ['accepted','foreign','missing','timeout']){
  let submit,requests=0,destination='',focus=0,links=[];
  const input={disabled:false,value:'未提交的鸟'};
  const button={disabled:false,textContent:'确认',dataset:{},name:'classification',value:'undetermined',getAttribute:()=>null};
  const form={dataset:{verifyUrl:'/workbench/'},method:'post',action:'http://localhost/photos/current/',
   elements:[input,button],reportValidity:()=>true,
   hasAttribute:key=>['data-pending-text','data-archive-confirm'].includes(key),
   querySelector:()=>button,setAttribute:()=>{},removeAttribute:()=>{},
   addEventListener:(_,fn)=>submit=fn,append:()=>{}};
  const doc={activeElement:input,visibilityState:'visible',
   querySelector:()=>null,querySelectorAll:key=>key==='form'?[form]:[],
   getElementById:()=>{throw Error('must not replace main')},addEventListener:()=>{},
   createElement:tag=>{const node={setAttribute:()=>{},append:()=>{},focus:()=>focus++};
    if(tag==='a')links.push(node);return node;}};
  const context={document:doc,window:{location:{origin:'http://localhost',href:'http://localhost/photos/current/',
   assign:url=>destination=url}},URL,URLSearchParams,AbortSignal,setInterval:()=>{},
   FormData:class{constructor(){this.values=[['local_names',input.value]];}append(...args){this.values.push(args);}},
   fetch:async(_,options)=>{requests++;assert.equal(options.headers['X-Archive-Confirm'],'true');
    assert.equal(options.redirect,'manual');
    assert(options.body.values.some(([key,value])=>key==='classification'&&value==='undetermined'));
    assert(options.body.values.some(([key,value])=>key==='local_names'&&value==='未提交的鸟'));
    if(mode==='timeout')throw Error('timeout');
    return {status:200,redirected:false,json:async()=>({accepted:mode!=='missing',
      next_url:mode==='foreign'?'https://other.test/photos/next/':'/photos/next/'})};}};
  vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
  await submit({submitter:button,preventDefault:()=>{}});
  assert.equal(requests,1);assert.equal(input.value,'未提交的鸟');
  if(mode==='accepted')assert.equal(destination,'http://localhost/photos/next/');
  else{assert.equal(destination,'');assert.equal(form.dataset.resultUnknown,'true');
    assert.equal(input.disabled,false);assert.equal(links[0].href,'/workbench/');
    await submit({submitter:button,preventDefault:()=>{}});assert.equal(requests,1);assert.equal(focus,0);}
 }
 console.log('CONFIRM_PROTOCOL_PASS');
})().catch(error=>{console.error(error);process.exit(1)});
'''
        result = subprocess.run(['node', '-e', script, str(settings.BASE_DIR/'archive/static/archive/app.js')],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('CONFIRM_PROTOCOL_PASS', result.stdout)

    def test_archive_poll_javascript_updates_only_summary(self):
        """Synthetic nodes prove update boundaries; real focus UAT belongs to plan 08."""
        import subprocess
        from django.conf import settings
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
(async()=>{
 let timer,requests=0,appended=0;
 const active={value:'我的草稿'},counts={textContent:'等待归档 1'},message={textContent:''};
 const item={dataset:{failureKey:'operation:space_exhausted:v1'}};
 const list={querySelectorAll:()=>[],append:()=>appended++};
 const region={dataset:{archivePoll:'/tasks/status/?scope=archive'},
  querySelector:key=>key==='[data-archive-counts]'?counts:key==='[data-archive-failures]'?list:message};
 const next={querySelector:()=>({textContent:'归档失败 1'}),querySelectorAll:()=>[item]};
 const doc={visibilityState:'visible',activeElement:active,querySelectorAll:()=>[],
  querySelector:key=>key==='[data-archive-status]'?region:null,addEventListener:()=>{},
  getElementById:()=>{throw Error('must not replace main')},createElement:()=>{throw Error('must not toast')}};
 const context={document:doc,window:{location:{origin:'http://localhost'}},URL,URLSearchParams,AbortSignal,
  setInterval:fn=>timer=fn,DOMParser:class{parseFromString(){return {querySelector:()=>next};}},
  fetch:async(_,options)=>{requests++;assert.equal(options.method,undefined);
   return {status:200,redirected:false,text:async()=>''};}};
 vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
 await timer();await timer();assert.equal(requests,2);assert.equal(appended,1);
 assert.equal(counts.textContent,'归档失败 1');assert.equal(doc.activeElement,active);assert.equal(active.value,'我的草稿');
 doc.visibilityState='hidden';await timer();assert.equal(requests,2);
 console.log('ARCHIVE_POLL_PROTOCOL_PASS');
})().catch(error=>{console.error(error);process.exit(1)});
'''
        result = subprocess.run(['node', '-e', script, str(settings.BASE_DIR/'archive/static/archive/app.js')],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ARCHIVE_POLL_PROTOCOL_PASS', result.stdout)


class ConfirmationMigrationTests(TransactionTestCase):
    def test_upgrade_preserves_identity(self):
        self.assertIn('archive_archiveoperation', connection.introspection.table_names())
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(latest))
        executor.migrate([('archive', '0004_catalog')])
        old = executor.loader.project_state([('archive', '0004_catalog')]).apps
        ident = uuid.uuid4()
        old.get_model('archive', 'Photo').objects.create(id=ident, sha256='b'*64,
            byte_size=42, preview_key='old-key')
        executor = MigrationExecutor(connection)
        executor.migrate([('archive', '0005_confirmation_archive')])
        current = executor.loader.project_state([('archive', '0005_confirmation_archive')]).apps
        photo = current.get_model('archive', 'Photo').objects.get(pk=ident)
        self.assertEqual((photo.sha256, photo.byte_size, photo.preview_key), ('b'*64, 42, 'old-key'))
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, 'archive_archiveoperation')
        for column in ('photo_id', 'job_id', 'request_id'):
            self.assertTrue(any(c['unique'] and c['columns'] == [column] for c in constraints.values()), column)
        with connection.cursor() as cursor:
            pair_constraints = connection.introspection.get_constraints(cursor, 'archive_photospecies')
        self.assertTrue(any(c['unique'] and c['columns'] == ['photo_id', 'species_id']
                            for c in pair_constraints.values()))
        self.assertTrue(any(c['foreign_key'] == ('archive_sourceobservation', 'id')
                            for c in constraints.values()))


class ConfirmationConflictTests(ConfirmationFixture):
    def test_same_request_replays_result(self):
        photo, _ = self.imported()
        next_photo, _ = self.imported('two.jpg', 'blue')
        data = self.payload(photo)
        first = self.post(photo, data)
        self.assertEqual(first.status_code, 303)
        self.post(next_photo)
        replay = self.post(photo, data)
        self.assertEqual(replay.status_code, 303)
        self.assertEqual(replay.url, first.url)
        from archive.models import ArchiveOperation
        self.assertEqual(ArchiveOperation.objects.filter(photo=photo).count(), 1)

    def test_key_payload_conflict(self):
        photo, _ = self.imported()
        other = Species.objects.create(scientific_name='Ardea cinerea', scientific_key='ardea cinerea',
            chinese_name='<苍鹭>', search_key='苍鹭 ardea cinerea', source_snapshot=self.snapshot)
        data = self.payload(photo)
        self.assertEqual(self.post(photo, data).status_code, 303)
        data['species_ids'] = [str(other.pk)]
        response = self.post(photo, data)
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, '&lt;苍鹭&gt;', status_code=409)

    def test_two_tabs_one_wins(self):
        from concurrent.futures import ThreadPoolExecutor
        from django.db import close_old_connections
        photo, _ = self.imported()
        payloads = [self.payload(photo), self.payload(photo)]
        def submit(data):
            close_old_connections()
            try:
                return Client().post(f'/photos/{photo.pk}/', data).status_code
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(submit, payloads)), [303, 409])
        from archive.models import ArchiveOperation, PhotoSpecies
        self.assertEqual(ArchiveOperation.objects.count(), 1)
        self.assertEqual(PhotoSpecies.objects.count(), 1)

    def test_multiple_species_unique(self):
        photo, _ = self.imported()
        other = Species.objects.create(scientific_name='Ardea cinerea', scientific_key='ardea cinerea',
            chinese_name='苍鹭', search_key='苍鹭 ardea cinerea', source_snapshot=self.snapshot)
        data = self.payload(photo)
        data['species_ids'] = [str(self.species.pk), str(other.pk), str(self.species.pk)]
        self.assertEqual(self.post(photo, data).status_code, 303)
        from archive.models import PhotoSpecies, ArchiveOperation
        self.assertEqual(PhotoSpecies.objects.filter(photo=photo).count(), 2)
        self.assertEqual(ArchiveOperation.objects.get().species_ids_snapshot,
                         sorted([str(self.species.pk), str(other.pk)]))

    def test_metadata_after_confirmation_rejected(self):
        photo, path = self.imported()
        capture = self.client.get(f'/photos/{photo.pk}/capture-time/', {'date': '2025-12-31'})
        draft = capture.context['form'].data.dict()
        draft.update(snapshot=capture.context['snapshot'], version=photo.metadata_version)
        self.assertEqual(self.post(photo).status_code, 303)
        for state in ('confirmed', 'archiving', 'archive_failed', 'archived'):
            Photo.objects.filter(pk=photo.pk).update(state=state)
            self.assertEqual(self.client.post(f'/photos/{photo.pk}/capture-time/', draft).status_code, 409)
            self.assertEqual(self.client.post('/metadata/location/', {
                'photo_ids': [str(photo.pk)], 'location': '江苏', 'filter': 'all', 'page': 1}).status_code, 409)
        photo.refresh_from_db()
        self.assertIsNone(photo.manual_capture)
        self.assertEqual((photo.user_location, photo.metadata_version), ('', 2))
        self.assertTrue(path.exists())

    def test_candidate_fixture_requires_human_submit(self):
        photo, _ = self.imported()
        candidate_fixture = {'species_id': str(self.species.pk), 'source': 'fixed-test-only'}
        response = self.client.get(f'/photos/{photo.pk}/', {'q': '白鹭'})
        self.assertContains(response, '白鹭')
        self.assertFalse(Job.objects.filter(kind='archive').exists())
        data = self.payload(photo); data['species_ids'] = [candidate_fixture['species_id']]
        self.assertEqual(self.post(photo, data).status_code, 303)

    def test_invalid_payload_never_leaves_partial_decision(self):
        photo, _ = self.imported()
        for changes in ({'species_ids': ['../../escape']}, {'species_ids': [str(uuid.uuid4())]},
                        {'request_id': 'bad'}, {'classification': 'undetermined'},
                        {'local_names': ['x' * 301]}):
            data = self.payload(photo); data.update(changes)
            self.assertEqual(self.post(photo, data).status_code, 400)
        self.assertFalse(Job.objects.filter(kind='archive').exists())
        photo.refresh_from_db(); self.assertEqual(photo.metadata_version, 1)

    def test_search_keeps_selected_species_and_allows_removal(self):
        photo, _ = self.imported()
        response = self.client.get(f'/photos/{photo.pk}/', {'q': 'nonexistent', 'species_ids': str(self.species.pk)})
        self.assertContains(response, '白鹭')
        self.assertContains(response, 'checked')
        self.assertContains(response, '取消勾选可移除')
        response = self.client.get(f'/photos/{photo.pk}/', {'q': '白鹭'})
        self.assertNotContains(response, 'checked')

    def test_old_tab_draft_keeps_original_version(self):
        photo, _ = self.imported()
        data = self.payload(photo)
        Photo.objects.filter(pk=photo.pk).update(metadata_version=2, user_location='新地点')
        response = self.post(photo, data)
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, 'name="metadata_version" value="1"', status_code=409)
        self.assertContains(response, 'checked', status_code=409)
        self.assertContains(response, '新地点', status_code=409)


class LocalClassificationTests(ConfirmationFixture):
    def setUp(self):
        super().setUp()
        self.archive, self.claims = self.root/'archive', self.root/'claims'
        self.archive.mkdir(); self.claims.mkdir()
        overrides = override_settings(BIRDS_ARCHIVE_ROOT=str(self.archive), BIRDS_CLAIM_ROOT=str(self.claims))
        overrides.enable(); self.addCleanup(overrides.disable)

    def local_payload(self, photo, names=None, **changes):
        data = self.payload(photo)
        data.update(species_ids=[], local_names=names or ['池边小鸟'])
        data.update(changes)
        return data

    def complete(self, photo, path):
        from django.core.management import call_command
        from archive.models import ArchiveCompletion, ManagedOriginal
        original = path.read_bytes()
        call_command('runworker', '--once', verbosity=0)
        photo.refresh_from_db()
        self.assertEqual(photo.state, 'archived')
        managed = ManagedOriginal.objects.get(photo=photo)
        self.assertEqual((self.archive/managed.relative_path).read_bytes(), original)
        self.assertFalse(path.exists())
        self.assertTrue(ArchiveCompletion.objects.filter(photo=photo).exists())
        return managed

    def test_local_name_round_trip(self):
        self.species.delete(); self.snapshot.delete()
        photo, path = self.imported()
        response = self.post(photo, self.local_payload(photo))
        self.assertEqual(response.status_code, 303)
        species = photo.confirmed_species.get().species
        self.assertEqual(species.local_name, '池边小鸟')
        self.assertEqual(species.source_kind, 'local')
        managed = self.complete(photo, path)
        self.assertEqual(managed.relative_path, '池边小鸟/未知年份/one.jpg')
        self.assertFalse(SourceSnapshot.objects.exists())

    def test_concurrent_normalized_names_share_one_identity(self):
        from concurrent.futures import ThreadPoolExecutor
        from django.db import close_old_connections
        photos = [self.imported('one.jpg', 'red')[0], self.imported('two.jpg', 'blue')[0]]
        payloads = [self.local_payload(photo, [name]) for photo, name in zip(photos, ('Café Bird', 'Cafe\u0301 BIRD'))]
        def submit(index):
            close_old_connections()
            try:
                return Client().post(f'/photos/{photos[index].pk}/', payloads[index]).status_code
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(submit, range(2)))
        self.assertEqual(statuses, [303, 303])
        local = Species.objects.get(source_kind='local')
        self.assertEqual(local.confirmed_photos.count(), 2)

    def test_same_local_name_reused(self):
        ids = []
        for i, name in enumerate((' Café  BIRD ', 'Cafe\u0301 bird')):
            photo, _ = self.imported(f'{i}.jpg', ('red', 'blue')[i])
            self.assertEqual(self.post(photo, self.local_payload(photo, [name, name])).status_code, 303)
            ids.append(photo.confirmed_species.get().species_id)
        self.assertEqual(ids[0], ids[1])
        species = Species.objects.get(pk=ids[0])
        self.assertEqual(species.local_key, 'café bird')
        self.assertEqual(species.local_name, ' Café  BIRD ')

    def test_no_fake_scientific_or_snapshot(self):
        photo, _ = self.imported()
        self.assertEqual(self.post(photo, self.local_payload(photo)).status_code, 303)
        species = photo.confirmed_species.get().species
        self.assertIsNone(species.scientific_name)
        self.assertIsNone(species.scientific_key)
        self.assertIsNone(species.source_snapshot_id)
        self.assertFalse(species.concepts.exists())
        self.assertEqual(SourceSnapshot.objects.count(), 1)

    def test_undetermined_explicit_archive(self):
        photo, path = self.imported()
        self.assertEqual(self.post(photo, self.local_payload(photo, [''], classification='undetermined')).status_code, 303)
        self.assertFalse(photo.confirmed_species.exists())
        self.assertEqual(Species.objects.count(), 1)
        self.assertEqual(self.complete(photo, path).relative_path, '无法确定/未知年份/one.jpg')

    def test_empty_not_undetermined(self):
        from archive.models import ArchiveOperation
        photo, path = self.imported()
        for changes in ({'local_names': [' ', '\t']}, {'classification': ''},
                        {'classification': 'undetermined', 'local_names': ['小鸟']},
                        {'local_names': ['bad\x00name']}, {'local_names': ['x'*301]}):
            data = self.local_payload(photo); data.update(changes)
            self.assertEqual(self.post(photo, data).status_code, 400)
        self.assertTrue(path.exists())
        self.assertFalse(ArchiveOperation.objects.exists())
        self.assertEqual(Species.objects.count(), 1)

    def test_mixed_multiple_species(self):
        from archive.models import ArchiveOperation, ManagedOriginal
        photo, path = self.imported()
        data = self.local_payload(photo, ['林中鸟', '林中鸟'], species_ids=[str(self.species.pk)]*2)
        self.assertEqual(self.post(photo, data).status_code, 303)
        self.assertEqual(photo.confirmed_species.count(), 2)
        self.assertEqual(ArchiveOperation.objects.get(photo=photo).classification, 'species')
        self.assertEqual(self.complete(photo, path).relative_path, '多鸟种/未知年份/one.jpg')
        self.assertEqual(ManagedOriginal.objects.count(), 1)

    def test_local_draft_errors_escape_and_preserve_input(self):
        photo, _ = self.imported()
        data = self.local_payload(photo, ['<小鸟>'])
        data['metadata_version'] = 9
        response = self.post(photo, data)
        self.assertContains(response, '&lt;小鸟&gt;', status_code=409)
        self.assertContains(response, 'name="local_names"', status_code=409)
        self.assertEqual(Species.objects.count(), 1)

    def test_search_includes_local_without_snapshot(self):
        from archive.services import confirmation
        photo, _ = self.imported()
        self.assertEqual(self.post(photo, self.local_payload(photo)).status_code, 303)
        species = photo.confirmed_species.get().species
        available = getattr(confirmation, 'search_confirmable_species', lambda query: [])
        self.assertEqual([item.pk for item in available('池边')], [species.pk])
        response = self.client.get(f'/photos/{photo.pk}/', {'q': '池边'})
        self.assertEqual([item.pk for item in response.context['species_results']], [species.pk])
        next_photo, _ = self.imported('two.jpg', 'blue')
        response = self.client.get(f'/photos/{next_photo.pk}/', {'q': '池边'})
        self.assertContains(response, '本地名，资料待补')
        self.assertNotContains(response, 'None')
        data = self.payload(next_photo); data['species_ids'] = [str(species.pk)]
        self.assertEqual(self.post(next_photo, data).status_code, 303)
        self.assertEqual(next_photo.confirmed_species.get().species_id, species.pk)

    def test_catalog_import_preserves_local(self):
        from archive.services.catalog import import_catalog
        from archive.tests.test_catalog import CatalogTests
        from archive.models import SpeciesConcept
        photo, path = self.imported()
        self.assertEqual(self.post(photo, self.local_payload(photo, ['白鹭'])).status_code, 303)
        self.complete(photo, path)
        local = photo.confirmed_species.get().species
        before = dict(Species.objects.filter(pk=local.pk).values().get())
        fixture = CatalogTests(); fixture.root = self.root
        files = fixture.fixture()
        import_catalog(*files)
        catalog = Species.objects.get(source_kind='catalog', scientific_name='Ardea alba',
            source_snapshot__source='synthetic-test-only')
        catalog.archive_directory, catalog.directory_key = '旧目录', '旧目录'
        catalog.save(update_fields=['archive_directory', 'directory_key'])
        other = Species.objects.get(scientific_name='Cardinalis cardinalis')
        mapping = {key: {'species_id': str(item.pk), 'evidence': 'same synthetic concept'}
            for key, item in [('concept-a', catalog), ('concept-b', other)]}
        files = fixture.fixture([('concept-a', 'Ardea renamed', '新名'),
            ('concept-b', 'Cardinalis cardinalis', '')], version='two', concept_mapping=mapping)
        import_catalog(*files)
        self.assertEqual(Species.objects.filter(pk=local.pk).values().get(), before)
        self.assertFalse(SpeciesConcept.objects.filter(species=local).exists())
        catalog.refresh_from_db()
        self.assertEqual((catalog.scientific_name, catalog.archive_directory), ('Ardea renamed', '旧目录'))

    def test_homonym_not_auto_bound(self):
        from archive.services.confirmation import search_confirmable_species
        photo, _ = self.imported()
        self.assertEqual(self.post(photo, self.local_payload(photo, ['白鹭'])).status_code, 303)
        local = photo.confirmed_species.get().species
        self.assertNotEqual(local.pk, self.species.pk)
        self.assertEqual({item.pk for item in search_confirmable_species('白鹭')}, {local.pk, self.species.pk})
        self.assertEqual([item.pk for item in search_confirmable_species('ardea')], [self.species.pk])
        self.assertFalse(local.concepts.exists())
        catalog_response = self.client.get('/species/', {'q': '白鹭'})
        self.assertNotContains(catalog_response, 'None')
        self.assertEqual([item.pk for item in catalog_response.context['page_obj']], [self.species.pk])

    def test_each_species_has_same_photo(self):
        from archive.services.confirmation import confirmed_photos_for_species
        photo, path = self.imported()
        data = self.local_payload(photo, species_ids=[str(self.species.pk)])
        self.assertEqual(self.post(photo, data).status_code, 303)
        species = list(Species.objects.filter(confirmed_photos__photo=photo))
        self.assertEqual(len(species), 2)
        for item in species:
            self.assertFalse(confirmed_photos_for_species(item.pk).exists())
        self.complete(photo, path)
        for item in species:
            self.assertEqual(list(confirmed_photos_for_species(item.pk).values_list('pk', flat=True)), [photo.pk])
            response = self.client.get(f'/photos/{photo.pk}/', {'species': str(item.pk)})
            self.assertEqual(response.context['collection_species'].pk, item.pk)
            self.assertContains(response, f'href="/photos/{photo.pk}/"')
            self.assertContains(response, item.display_name)
        self.assertEqual(self.client.get(f'/photos/{photo.pk}/', {'species': '../../etc'}).status_code, 400)
        self.assertEqual(self.client.get(f'/photos/{photo.pk}/', {'species': str(uuid.uuid4())}).status_code, 404)

    def test_candidate_selection_still_explicit(self):
        from archive.models import ArchiveOperation
        photo, path = self.imported()
        response = self.client.get(f'/photos/{photo.pk}/', {'q': '白鹭'})
        self.assertContains(response, '白鹭')
        self.assertNotContains(response, 'checked')
        self.assertFalse(ArchiveOperation.objects.exists())
        data = self.payload(photo); data['species_ids'] = []
        self.assertEqual(self.post(photo, data).status_code, 400)
        self.assertTrue(path.exists())

    def test_unified_search_pagination_preserves_selection_and_local_draft(self):
        for i in range(30):
            Species.objects.create(scientific_name=f'Synthetic {i:02d}', scientific_key=f'synthetic {i:02d}',
                chinese_name='查询鸟', search_key='查询鸟', source_snapshot=self.snapshot)
        photo, _ = self.imported()
        response = self.client.get(f'/photos/{photo.pk}/', {'q': '查询鸟',
            'species_ids': str(self.species.pk), 'local_names': ['未提交的鸟']})
        self.assertTrue(response.context.get('species_search_next'), '必须可到达第24项以后的全球结果')
        first = {item.pk for item in response.context['species_search_page']}
        second = ConfirmationPage(response).submit(self.client, f'/photos/{photo.pk}/', '下一页鸟种')
        remaining = {item.pk for item in second.context['species_search_page']}
        self.assertFalse(first & remaining)
        self.assertEqual(len(first | remaining), 30)
        self.assertContains(second, '未提交的鸟')
        self.assertContains(second, f'value="{self.species.pk}" checked')


class LocalSpeciesMigrationTests(TransactionTestCase):
    def test_existing_catalog_unchanged(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(latest))
        executor.migrate([('archive', '0005_confirmation_archive')])
        old = executor.loader.project_state([('archive', '0005_confirmation_archive')]).apps
        snapshot = old.get_model('archive', 'SourceSnapshot').objects.create(source='migration', version='1',
            sha256='a'*64, acquired_at='2026-10-16', download_page_url='https://example.org',
            license_url='https://example.org/license', attribution='synthetic', row_count=1, species_count=1, manifest={})
        species = old.get_model('archive', 'Species').objects.create(scientific_name='Ardea alba',
            scientific_key='ardea alba', chinese_name='白鹭', search_key='白鹭', source_snapshot=snapshot)
        concept = old.get_model('archive', 'SpeciesConcept').objects.create(species=species,
            snapshot=snapshot, external_id='test-1', evidence='synthetic')
        photo = old.get_model('archive', 'Photo').objects.create(sha256='b'*64, byte_size=42)
        executor = MigrationExecutor(connection)
        executor.migrate([('archive', '0006_local_species')])
        current = executor.loader.project_state([('archive', '0006_local_species')]).apps
        SpeciesModel = current.get_model('archive', 'Species')
        saved = SpeciesModel.objects.get(pk=species.pk)
        self.assertEqual((saved.source_kind, saved.scientific_name, saved.source_snapshot_id),
            ('catalog', 'Ardea alba', snapshot.pk))
        self.assertEqual(current.get_model('archive', 'SpeciesConcept').objects.get(pk=concept.pk).species_id, species.pk)
        self.assertEqual(current.get_model('archive', 'Photo').objects.get(pk=photo.pk).sha256, 'b'*64)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SpeciesModel.objects.create(source_kind='catalog', scientific_name=None,
                scientific_key=None, source_snapshot=None, search_key='')
        local = dict(source_kind='local', local_name='小鸟', local_key='小鸟', scientific_name=None,
            scientific_key=None, source_snapshot=None, search_key='小鸟')
        SpeciesModel.objects.create(**local)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SpeciesModel.objects.create(**local)
        for changes in ({'local_name': ''}, {'local_key': None}, {'scientific_name': 'Fake name'},
                        {'source_snapshot_id': snapshot.pk}):
            invalid = {**local, 'local_key': 'different', **changes}
            with self.assertRaises(IntegrityError), transaction.atomic():
                SpeciesModel.objects.create(**invalid)
