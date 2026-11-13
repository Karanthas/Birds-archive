"""阶段审查回归：真实临时磁盘、配置入口与可刷新操作。"""
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TransactionTestCase, override_settings

from archive.models import ImportSource, Job, Photo, SourceObservation
from archive.services.intake import scan_once
from archive.services.paths import UnsafePath
from archive.tests.fixtures import jpeg


class RootIsolationAuditTests(TransactionTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='birds-audit-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def layouts(self):
        for name in ('same', 'data_child', 'inbox_child', 'siblings'):
            base = self.root / name
            inbox = base / 'inbox'
            data = inbox if name == 'same' else (inbox / 'data' if name == 'data_child' else base / 'data')
            if name == 'inbox_child':
                inbox = data / 'inbox'
            inbox.mkdir(parents=True, exist_ok=True)
            data.mkdir(parents=True, exist_ok=True)
            yield name, inbox, data

    def overrides(self, inbox, data):
        return override_settings(BIRDS_INBOX_ROOT=str(inbox), BIRDS_DATA_ROOT=str(data),
                                 BIRDS_HANDOFF_MODE='published', BIRDS_PUBLISHED_CONTRACT=True)

    def test_direct_scan_rejects_same_and_nested_roots_without_side_effects(self):
        for name, inbox, data in self.layouts():
            digest = jpeg(inbox / 'original.jpg')
            before = set(data.rglob('*'))
            with self.subTest(layout=name), self.overrides(inbox, data):
                if name == 'siblings':
                    self.assertEqual(scan_once(), 1)
                    self.assertEqual(ImportSource.objects.count(), 1)
                    Job.objects.all().delete()
                    ImportSource.objects.update(current_observation=None)
                    SourceObservation.objects.all().delete()
                    ImportSource.objects.all().delete()
                else:
                    with self.assertRaises(UnsafePath):
                        scan_once()
                    self.assertEqual((ImportSource.objects.count(), Job.objects.count(), Photo.objects.count()), (0, 0, 0))
                    self.assertEqual(set(data.rglob('*')), before)
                self.assertEqual(hashlib.sha256((inbox / 'original.jpg').read_bytes()).hexdigest(), digest)

    def test_preflight_rejects_root_containment(self):
        from archive.management.commands.checkruntime import runtime_report
        for name, inbox, data in self.layouts():
            with self.subTest(layout=name), self.overrides(inbox, data):
                report = runtime_report()
                self.assertEqual('roots_not_isolated' in report['errors'], name != 'siblings')
                self.assertEqual(report['roots_isolated'], name == 'siblings')

    def test_actual_worker_rejects_before_lock_recovery_or_generated_files(self):
        for name, inbox, data in self.layouts():
            if name == 'siblings':
                continue
            digest = jpeg(inbox / 'original.jpg')
            before = set(data.rglob('*'))
            env = {**os.environ, 'BIRDS_INBOX_ROOT': str(inbox), 'BIRDS_DATA_ROOT': str(data),
                   'BIRDS_SECRET_KEY': 'isolated-root-audit-only', 'BIRDS_LOCAL_HTTP': '1'}
            result = subprocess.run([sys.executable, 'manage.py', 'runworker', '--once', '--settings=config.settings'],
                                    cwd=settings.BASE_DIR, env=env, capture_output=True, text=True, timeout=10)
            with self.subTest(layout=name):
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('roots_not_isolated', result.stderr)
                self.assertNotIn(str(self.root), result.stderr)
                self.assertEqual(set(data.rglob('*')), before)
                self.assertEqual((ImportSource.objects.count(), Job.objects.count(), Photo.objects.count()), (0, 0, 0))
                self.assertEqual(hashlib.sha256((inbox / 'original.jpg').read_bytes()).hexdigest(), digest)

    def test_scanner_checks_actual_child_fd_identity_before_any_source_write(self):
        inbox, data = self.root / 'inbox', self.root / 'data'
        inbox.mkdir(); data.mkdir(); (inbox / 'alias').mkdir()
        jpeg(inbox / 'a-original.jpg')
        jpeg(data / 'generated.jpg')
        original_open = os.open

        def alias_open(path, flags, *args, **kwargs):
            # FD returned by the OS points to real data; simulates a bind alias
            # without claiming a Linux mount was exercised on macOS.
            if path == 'alias':
                return original_open(data, flags)
            return original_open(path, flags, *args, **kwargs)

        with self.overrides(inbox, data), mock.patch('os.open', side_effect=alias_open):
            with self.assertRaises(UnsafePath):
                scan_once()
        self.assertEqual((ImportSource.objects.count(), Job.objects.count(), Photo.objects.count()), (0, 0, 0))
        # 调用者持有的data FD不对应配置路径时也必须硬停止，不能继续领取。
        from archive.services.paths import open_root
        other = self.root / 'other-data'
        other.mkdir()
        inbox_fd, wrong_data_fd = open_root(inbox), open_root(other)
        try:
            with self.overrides(inbox, data), self.assertRaises(UnsafePath):
                scan_once(inbox_fd, wrong_data_fd)
            self.assertEqual(list(other.iterdir()), [])
        finally:
            os.close(inbox_fd); os.close(wrong_data_fd)


class TaskNavigationAuditTests(TransactionTestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(settings.BIRDS_OWNER_USERNAME, password='audit-only')
        self.client.force_login(self.owner)
        source = ImportSource.objects.create(relative_path='failed.jpg', fingerprint={}, state='failed')
        observation = SourceObservation.objects.create(source=source, version=1, fingerprint={}, byte_size=1)
        source.current_observation = observation
        source.save()
        self.job = Job.objects.create(source=source, source_version=1, state='failed', error_code='source_permission')

    def test_retry_success_and_duplicate_are_prg_preserving_valid_state_page(self):
        url = f'/tasks/{self.job.pk}/retry/'
        payload = {'version': '1', 'state': 'failed', 'page': '2'}
        first = self.client.post(url, payload)
        self.assertEqual(first.status_code, 302)
        self.assertEqual(first['Location'], '/tasks/?state=failed&page=2')
        result = self.client.get(first['Location'])
        self.assertContains(result, '已加入重试队列')
        self.assertNotContains(self.client.get(first['Location']), '已加入重试队列')
        duplicate = self.client.post(url, payload)
        self.assertEqual(duplicate.status_code, 302)
        self.assertEqual(duplicate['Location'], first['Location'])
        self.assertEqual(Job.objects.count(), 1)

    def test_conflict_and_invalid_version_redirect_to_safe_get_with_message(self):
        url = f'/tasks/{self.job.pk}/retry/'
        for version in ('2', 'bad'):
            with self.subTest(version=version):
                result = self.client.post(url, {'version': version, 'state': 'failed', 'page': '1'})
                self.assertEqual(result.status_code, 302)
                self.assertEqual(result['Location'], '/tasks/?state=failed&page=1')
                self.assertContains(self.client.get(result['Location']), '刷新状态')
                self.job.refresh_from_db()
                self.assertEqual(self.job.state, 'failed')

    def test_retry_invalid_navigation_is_normalized_and_completed_has_no_disabled_action(self):
        result = self.client.post(f'/tasks/{self.job.pk}/retry/', {'version': '1', 'state': '//evil', 'page': '-2'})
        self.assertEqual(result['Location'], '/tasks/?state=&page=1')
        Job.objects.filter(pk=self.job.pk).update(state='succeeded')
        page = self.client.get('/tasks/')
        self.assertNotContains(page, '不可重试')
        self.assertContains(page, 'aria-current="page"')


class InterfaceAuditTests(TransactionTestCase):
    from archive.tests.test_metadata import MetadataTests
    setUp = MetadataTests.setUp
    photo = MetadataTests.photo
    preview = MetadataTests.preview
    submit = MetadataTests.submit

    def named_photo(self, number, name):
        photo = self.photo(number)
        source = ImportSource.objects.create(relative_path=name, fingerprint={}, photo=photo)
        observation = SourceObservation.objects.create(source=source, version=1, fingerprint={}, byte_size=1, photo=photo)
        source.current_observation = observation
        source.save()
        return photo

    def test_location_help_and_conflict_name_current_and_draft_are_visible(self):
        one = self.named_photo(1, '第一张.jpg')
        two = self.named_photo(2, '冲突照片.jpg')
        initial = self.client.get('/metadata/location/', {'photo_ids': [str(one.pk)], 'filter': 'all', 'page': '1'})
        self.assertContains(initial, 'aria-describedby="location-help"')
        preview = self.preview([one, two], location='待保存地点')
        self.assertContains(preview, '最多 200 字')
        self.assertContains(preview, 'aria-describedby="location-help"')
        Photo.objects.filter(pk=two.pk).update(user_location='当前地点', metadata_version=2)
        conflict = self.submit(preview)
        self.assertEqual(conflict.status_code, 409)
        entries = conflict.context['conflict_details']
        self.assertEqual(entries, [{'id': str(two.pk), 'name': '冲突照片.jpg',
                                  'available': True, 'current_location': '当前地点'}])
        self.assertContains(conflict, '待保存地点', status_code=409)
        one.refresh_from_db()
        self.assertEqual((one.user_location, one.metadata_version), ('', 1))

    def test_removed_conflict_is_explicitly_unavailable_and_batch_is_unchanged(self):
        one = self.named_photo(1, '保留.jpg')
        two = self.named_photo(2, '已移除.jpg')
        preview = self.preview([one, two])
        removed = str(two.pk)
        SourceObservation.objects.filter(photo=two).update(photo=None)
        ImportSource.objects.filter(photo=two).update(photo=None)
        two.delete()
        response = self.submit(preview)
        self.assertContains(response, '照片已不可用', status_code=409)
        self.assertEqual(response.context['conflict_details'][0]['id'], removed)
        one.refresh_from_db()
        self.assertEqual((one.user_location, one.metadata_version), ('', 1))

    def test_current_page_keeps_membership_snapshot_and_counts_but_renders_photos_first(self):
        ImportSource.objects.create(relative_path='较早失败.jpg', fingerprint={}, state='failed')
        for number in range(1, 26):
            self.named_photo(number, f'照片{number}.jpg')
        response = self.client.get('/review/')
        rows = list(response.context['page_obj'])
        self.assertEqual(len(rows), 24)
        self.assertEqual(rows[0]['kind'], 'source')
        self.assertEqual(response.context['page_obj'].paginator.count, 26)
        text = response.content.decode()
        self.assertLess(text.index('class="image-frame"'), text.index('较早失败.jpg'))
        from django.core import signing
        snapshot = signing.loads(response.context['page_snapshot'], salt='workbench-selection')
        expected = {str(row['photo'].pk) for row in rows if row['kind'] == 'photo'}
        self.assertEqual(set(snapshot), expected)
        self.assertEqual(len(expected), 23)
