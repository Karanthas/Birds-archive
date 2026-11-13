"""原图下载通过真实worker及HTTP流，独立检查磁盘字节和文件名。"""
import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import unquote
from unittest import mock

from django.core.management import call_command
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse

from archive.models import ArchiveCompletion, ManagedOriginal
from archive.tests.test_archive_recovery import ArchiveFixture


class DownloadFixture(ArchiveFixture):
    def current_registration(self):
        import uuid
        from archive.models import ManagementOperation
        from archive.services.paths import fingerprint, root_identity, open_root
        photo, old, managed = self.completed()
        target = self.archive/'新鸟种'/'2030'/'当前名.jpg'
        target.parent.mkdir(parents=True)
        os.link(old, target)
        root = open_root(self.archive)
        try:
            binding = {'path': str(self.archive), 'identity': root_identity(root)}
        finally:
            os.close(root)
        operation = ManagementOperation.objects.create(photo=photo, kind='capture',
            request_id=uuid.uuid4(), payload_digest='b'*64, generation=1,
            source_location_revision=0, stage='registered', status='running',
            root_identity=binding, target_relative_path=str(target.relative_to(self.archive)),
            target_fingerprint=fingerprint(target.stat()))
        managed.location_revision = 1
        managed.last_management_operation = operation
        managed.relative_path = operation.target_relative_path
        managed.actual_basename = target.name
        managed.fingerprint = operation.target_fingerprint
        managed.save()
        return photo, old, target, managed

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix='birds-download-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inbox, self.data = self.root/'inbox', self.root/'data'
        self.archive, self.claims = self.root/'archive', self.root/'claims'
        for directory in (self.inbox, self.data, self.archive, self.claims):
            directory.mkdir()
        isolated = override_settings(BIRDS_INBOX_ROOT=str(self.inbox), BIRDS_DATA_ROOT=str(self.data),
            BIRDS_ARCHIVE_ROOT=str(self.archive), BIRDS_CLAIM_ROOT=str(self.claims))
        isolated.enable(); self.addCleanup(isolated.disable)

    def completed(self, name='one.jpg', color='red'):
        photo, path, operation = self.confirmed(name, color)
        self.run_worker()
        operation.refresh_from_db()
        self.assertEqual(operation.stage, 'complete')
        managed = ManagedOriginal.objects.get(photo=photo)
        return photo, self.archive/managed.relative_path, managed

    def download(self, photo, method='get'):
        return getattr(self.client, method)(f'/photos/{photo.pk}/download/')

    def assert_download(self, photo, target, managed):
        response = self.download(photo)
        self.assertEqual(response.status_code, 200)
        try:
            content = b''.join(response.streaming_content)
        finally:
            response.close()
        self.assertEqual(content, target.read_bytes())
        self.assertEqual(hashlib.sha256(content).hexdigest(), hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual(response['Content-Type'], 'image/jpeg')
        self.assertIn(managed.actual_basename, unquote(response['Content-Disposition']))
        return response


class DownloadTests(DownloadFixture):
    def test_current_registration_download(self):
        photo, old, target, managed = self.current_registration()
        history = photo.archive_operation.capture_snapshot
        response = self.assert_download(photo, target, managed)
        self.assertIn('no-store', response['Cache-Control'])
        head = self.download(photo, 'head')
        self.assertEqual(head.status_code, 200); head.close()
        detail = self.client.get(f'/photos/{photo.pk}/')
        self.assertEqual(detail.status_code, 200)
        photo.refresh_from_db()
        self.assertEqual(photo.current_capture, history)
        from archive.services.intake import refresh_original_availability
        old.unlink()
        refresh_original_availability([photo.pk])
        photo.refresh_from_db()
        self.assertTrue(photo.original_available)

    def test_download_exact_hash_actual_name(self):
        photo, target, managed = self.completed()
        self.assert_download(photo, target, managed)
        response = self.client.get(f'/photos/{photo.pk}/')
        self.assertContains(response, f'/photos/{photo.pk}/download/')

    def test_suffix_filename(self):
        first, target, _ = self.completed()
        foreign = target.read_bytes()
        photo, new_target, managed = self.completed('second/one.jpg', color='blue')
        self.assertNotEqual(managed.actual_basename, 'one.jpg')
        self.assert_download(photo, new_target, managed)
        self.assertEqual(target.read_bytes(), foreign)

    def test_chinese_filename_header(self):
        photo, target, managed = self.completed('白鹭 飞行.jpg')
        response = self.assert_download(photo, target, managed)
        self.assertIn("filename*=utf-8''", response['Content-Disposition'])

    def test_undetermined_download(self):
        import uuid
        from archive.services.intake import scan_once
        from archive.services.jobs import process_source
        from archive.services.paths import open_root
        from archive.models import Job, Photo
        from archive.tests.fixtures import jpeg
        import os
        path = self.inbox/'unknown.jpg'; jpeg(path)
        scan_once()
        a, b = open_root(self.inbox), open_root(self.data)
        try:
            for job in Job.objects.filter(kind='import', state='pending'):
                self.assertTrue(process_source(job, a, b))
        finally:
            os.close(a); os.close(b)
        photo = Photo.objects.get(observations__source__relative_path='unknown.jpg')
        self.assertEqual(self.client.post(f'/photos/{photo.pk}/', dict(metadata_version=photo.metadata_version,
            request_id=str(uuid.uuid4()), classification='undetermined')).status_code, 303)
        call_command('runworker', '--once', verbosity=0)
        managed = ManagedOriginal.objects.get(photo=photo)
        self.assertTrue(managed.relative_path.startswith('无法确定/'))
        self.assert_download(photo, self.archive/managed.relative_path, managed)

    def test_get_head_no_store(self):
        photo, target, managed = self.completed()
        get = self.assert_download(photo, target, managed)
        head = self.download(photo, 'head')
        try:
            self.assertEqual(head.status_code, 200)
            self.assertEqual(b''.join(head.streaming_content), b'')
            for key in ('Content-Type', 'Content-Disposition', 'Content-Length', 'Cache-Control'):
                self.assertEqual(head[key], get[key])
            self.assertIn('no-store', head['Cache-Control'])
        finally:
            head.close()

    def test_incomplete_rejected(self):
        photo, path, _ = self.confirmed()
        before = path.read_bytes()
        self.assertEqual(self.download(photo).status_code, 404)
        self.assertNotContains(self.client.get(f'/photos/{photo.pk}/'), f'/photos/{photo.pk}/download/')
        self.run_worker()
        ArchiveCompletion.objects.filter(photo=photo).delete()
        self.assertEqual(self.download(photo).status_code, 404)
        managed = ManagedOriginal.objects.get(photo=photo)
        self.assertEqual((self.archive/managed.relative_path).read_bytes(), before)


class DownloadBoundaryTests(DownloadFixture):
    def test_current_registration_identity(self):
        from archive.tests.fixtures import jpeg
        photo, old, target, managed = self.current_registration()
        retained = target.with_suffix('.retained')
        target.rename(retained)
        target.symlink_to(retained)
        self.assertEqual(self.download(photo).status_code, 409)
        target.unlink(); jpeg(target, color='blue')
        foreign = target.read_bytes()
        self.assertEqual(self.download(photo).status_code, 409)
        self.assertEqual(target.read_bytes(), foreign)
        target.unlink(); os.link(retained, target)
        managed.last_management_operation.source_location_revision = 8
        managed.last_management_operation.save()
        self.assertEqual(self.download(photo).status_code, 409)
        self.assertEqual(old.read_bytes(), retained.read_bytes())

    def assert_rejected(self, photo):
        for method in ('get', 'head'):
            response = self.download(photo, method)
            self.assertEqual(response.status_code, 409)
            self.assertIn('no-store', response['Cache-Control'])
            if method == 'get':
                self.assertEqual(response.content.decode(), '原图当前不可用，请核对归档文件。')
            else:
                self.assertEqual(response.content, b'')
        photo.refresh_from_db()
        self.assertFalse(photo.original_available)
        photo.archive_operation.refresh_from_db()
        self.assertEqual(photo.archive_operation.last_error, 'original_unavailable')
        self.assertEqual(photo.archive_operation.stage, 'complete')
        self.assertTrue(ArchiveCompletion.objects.filter(photo=photo).exists())
        self.assertNotContains(self.client.get(f'/photos/{photo.pk}/'), reverse('download-original', args=[photo.pk]))

    def test_inbox_missing_archive_available(self):
        from archive.services.intake import refresh_original_availability
        photo, target, managed = self.completed()
        shutil.rmtree(self.inbox)
        refresh_original_availability([photo.pk])
        photo.refresh_from_db()
        self.assertTrue(photo.original_available)
        self.assertContains(self.client.get(f'/photos/{photo.pk}/'), reverse('download-original', args=[photo.pk]))
        self.assert_download(photo, target, managed)

    def test_root_replacement_rejected(self):
        from archive.services.paths import fingerprint
        photo, target, managed = self.completed()
        old = self.root/'old-archive'
        self.archive.rename(old)
        self.archive.mkdir()
        target.parent.mkdir(parents=True, exist_ok=True)
        # Same original inode and recorded stat cannot authorize a newly substituted root.
        os.link(old/managed.relative_path, target)
        managed.fingerprint = fingerprint(target.stat())
        managed.save(update_fields=['fingerprint'])
        before = target.read_bytes()
        self.assert_rejected(photo)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual((old/managed.relative_path).read_bytes(), before)

    def test_symlink_and_nonregular_rejected(self):
        photo, target, _ = self.completed()
        retained = target.with_suffix('.retained')
        target.rename(retained)
        before = retained.read_bytes()
        for kind in ('symlink', 'fifo', 'directory'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    target.symlink_to(retained)
                elif kind == 'fifo':
                    os.mkfifo(target)
                else:
                    target.mkdir()
                self.assert_rejected(photo)
                self.assertEqual(retained.read_bytes(), before)
                if kind == 'directory':
                    target.rmdir()
                else:
                    target.unlink()

    def test_directory_symlink_rejected(self):
        photo, target, _ = self.completed()
        directory = target.parent
        retained = directory.with_name('retained-directory')
        directory.rename(retained)
        directory.symlink_to(retained, target_is_directory=True)
        before = (retained/target.name).read_bytes()
        self.assert_rejected(photo)
        self.assertEqual((retained/target.name).read_bytes(), before)

    def test_same_size_corruption_rejected(self):
        from archive.services.paths import fingerprint
        photo, target, managed = self.completed()
        content = target.read_bytes()
        corrupted = content[:-1] + bytes([content[-1] ^ 1])
        target.write_bytes(corrupted)
        # Even a matching current stat does not replace full content verification.
        managed.fingerprint = fingerprint(target.stat())
        managed.save(update_fields=['fingerprint'])
        self.assert_rejected(photo)
        self.assertEqual(target.read_bytes(), corrupted)

    def test_filename_replacement_rejected(self):
        from archive.tests.fixtures import jpeg
        photo, target, _ = self.completed()
        target.rename(target.with_suffix('.retained'))
        jpeg(target, color='blue')
        foreign = target.read_bytes()
        self.assert_rejected(photo)
        self.assertEqual(target.read_bytes(), foreign)

    def test_replacement_during_hash_rejected_and_closed(self):
        from archive.services.images import content_hash
        from archive.tests.fixtures import jpeg
        photo, target, _ = self.completed()
        handles = []
        def replace(original):
            handles.append(original)
            result = content_hash(original)
            target.rename(target.with_suffix('.retained'))
            jpeg(target, color='blue')
            return result
        with mock.patch('archive.services.intake.content_hash', side_effect=replace):
            response = self.download(photo)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(handles)
        self.assertTrue(all(original.closed for original in handles))

    def test_root_replacement_during_hash_rejected(self):
        from archive.services.images import content_hash
        photo, target, _ = self.completed()
        before = target.read_bytes()
        def replace(original):
            result = content_hash(original)
            self.archive.rename(self.root/'old-archive')
            self.archive.mkdir()
            return result
        with mock.patch('archive.services.intake.content_hash', side_effect=replace):
            self.assertEqual(self.download(photo).status_code, 409)
        self.assertEqual((self.root/'old-archive'/target.relative_to(self.archive)).read_bytes(), before)

    def test_same_fd_stream_and_lifetime(self):
        from archive.services.images import content_hash
        from archive.views import download_original
        photo, target, _ = self.completed()
        opened = []
        def observe(original):
            opened.append(original)
            return content_hash(original)
        for consume in ('complete', 'partial', 'abort'):
            with self.subTest(consume=consume), mock.patch('archive.services.intake.content_hash', side_effect=observe):
                response = download_original(RequestFactory().get('/'), photo.pk)
                self.assertEqual(response.status_code, 200)
                self.assertIs(response.file_to_stream, opened[-1])
                self.assertFalse(opened[-1].closed)
                self.assertEqual(opened[-1].tell(), 0)
                if consume == 'complete':
                    self.assertEqual(b''.join(response.streaming_content), target.read_bytes())
                elif consume == 'partial':
                    self.assertTrue(next(iter(response.streaming_content)))
                response.close()  # includes cancellation before consuming any body
                self.assertTrue(opened[-1].closed)
        with mock.patch('archive.services.intake.content_hash', side_effect=observe):
            response = download_original(RequestFactory().head('/'), photo.pk)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(opened[-1].closed)
        response.close()

    def test_stream_keeps_verified_fd_after_path_replacement(self):
        from archive.tests.fixtures import jpeg
        photo, target, _ = self.completed()
        expected = target.read_bytes()
        response = self.download(photo)
        self.assertEqual(response.status_code, 200)
        target.rename(target.with_suffix('.retained'))
        jpeg(target, color='blue')
        foreign = target.read_bytes()
        try:
            self.assertEqual(b''.join(response.streaming_content), expected)
        finally:
            response.close()
        self.assertEqual(target.read_bytes(), foreign)

    def test_response_construction_failure_closes_fd(self):
        from archive.services.images import content_hash
        photo, _, _ = self.completed()
        handles = []
        def observe(original):
            handles.append(original)
            return content_hash(original)
        with mock.patch('archive.services.intake.content_hash', side_effect=observe), \
             mock.patch('archive.views.FileResponse', side_effect=RuntimeError('response failed')):
            with self.assertRaisesMessage(RuntimeError, 'response failed'):
                self.download(photo)
        self.assertTrue(handles[-1].closed)

    def test_host_rejected(self):
        photo, _, _ = self.completed()
        with mock.patch('archive.views.open_completed_original', side_effect=AssertionError('bad host must not open')):
            self.assertEqual(self.client.get(reverse('download-original', args=[photo.pk]), HTTP_HOST='evil.invalid').status_code, 400)

    def test_no_login_required(self):
        from django.contrib.auth.models import User
        photo, target, managed = self.completed()
        self.assertEqual(User.objects.count(), 0)
        self.assert_download(photo, target, managed)
        self.assertEqual(self.client.post(reverse('download-original', args=[photo.pk])).status_code, 405)
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post(f'/photos/{photo.pk}/', {}).status_code, 403)
        self.assertEqual(User.objects.count(), 0)
