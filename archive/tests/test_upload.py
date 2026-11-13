"""受控投递：仅隔离合成 JPEG，不运行模型或触碰真实图库。"""
import hashlib
import io
import json
import os
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote

from PIL import Image
from django.core.management import call_command
from django.test import Client, TransactionTestCase, override_settings

from archive.models import ArchiveCompletion, ManagedOriginal, Photo, SourceObservation
from archive.services.intake import observe_file, scan_once
from archive.services.paths import fingerprint, open_root


class UploadFixture(TransactionTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='birds-upload-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ('inbox', 'data', 'archive', 'claim'):
            (self.root/name).mkdir()
        self.inbox, self.data = self.root/'inbox', self.root/'data'
        overrides = override_settings(**{
            **{'BIRDS_'+name.upper()+'_ROOT': str(self.root/name)
               for name in ('inbox', 'data', 'archive', 'claim')},
            'BIRDS_HANDOFF_MODE': 'published', 'BIRDS_PUBLISHED_CONTRACT': True,
            'BIRDS_RECOGNITION_ENABLED': False})
        overrides.enable()
        self.addCleanup(overrides.disable)
        self.client = Client(enforce_csrf_checks=True)
        stream = io.BytesIO()
        Image.new('RGB', (40, 30), 'red').save(stream, 'JPEG')
        self.payload = stream.getvalue()

    def headers(self, request_id=None, name='合成.jpg', payload=None):
        payload = self.payload if payload is None else payload
        return {'HTTP_X_BIRDS_REQUEST_ID': str(request_id or uuid.uuid4()),
                'HTTP_X_BIRDS_FILENAME': quote(name, safe=''),
                'HTTP_X_BIRDS_LENGTH': str(len(payload)),
                'HTTP_X_BIRDS_SHA256': hashlib.sha256(payload).hexdigest()}

    def send(self, headers=None, payload=None):
        response = self.client.get('/delivery/token/')
        self.assertEqual(response.status_code, 200)
        return self.client.post('/delivery/files/',
            self.payload if payload is None else payload, content_type='application/octet-stream',
            HTTP_X_CSRFTOKEN=response.json()['csrf_token'], HTTP_ORIGIN='http://testserver',
            **(headers or self.headers(payload=payload)))

    def published(self):
        return list(self.inbox.glob('*.jpg')) + list(self.inbox.glob('*.jpeg'))


class UploadTracerTests(UploadFixture):
    def test_receive_scan_confirm_download_same_bytes(self):
        headers = self.headers()
        response = self.send(headers)
        self.assertEqual(response.status_code, 201)
        receipt = response.json()
        self.assertEqual(receipt['status'], 'complete')
        self.assertEqual(len(self.published()), 1)
        self.assertFalse(ArchiveCompletion.objects.exists())
        call_command('runworker', '--once', verbosity=0)
        photo = Photo.objects.get()
        self.assertFalse(ArchiveCompletion.objects.exists())
        self.assertTrue(self.published()[0].exists())
        page = self.client.get(f'/photos/{photo.pk}/')
        self.assertEqual(page.status_code, 200)
        confirmation = self.client.post(f'/photos/{photo.pk}/', {
            'metadata_version': photo.metadata_version, 'request_id': str(uuid.uuid4()),
            'classification': 'undetermined'}, HTTP_X_CSRFTOKEN=self.client.cookies['csrftoken'].value,
            HTTP_X_ARCHIVE_CONFIRM='true')
        self.assertEqual(confirmation.status_code, 200)
        call_command('runworker', '--once', verbosity=0)
        self.assertEqual(ArchiveCompletion.objects.count(), 1)
        self.assertEqual(ManagedOriginal.objects.count(), 1)
        download = self.client.get(f'/photos/{photo.pk}/download/')
        self.assertEqual(download.status_code, 200)
        try:
            self.assertEqual(b''.join(download.streaming_content), self.payload)
        finally:
            download.close()
        retry = self.send(headers)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json(), receipt)
        self.assertEqual(self.client.get(receipt['receipt_url']).json(), receipt)
        self.assertIn('no-store', retry['Cache-Control'])


class UploadBoundaryTests(UploadFixture):
    def test_restored_terminal_receipt_is_replayed_only_for_its_bound_copy(self):
        headers = self.headers()
        first = self.send(headers)
        self.assertEqual(first.status_code, 201)
        receipt = first.json()
        name = headers['HTTP_X_BIRDS_REQUEST_ID'] + '.json'
        path = self.data / 'uploads' / name
        intent = json.loads(path.read_bytes())
        intent['roots']['data']['ino'] += 1  # Simulate the former deployment's recorded root.
        payload = json.dumps(intent).encode()
        path.write_bytes(payload)
        binding = SimpleNamespace(
            target_relative_path='uploads/' + name,
            target_fingerprint=fingerprint(path.stat()),
            sha256=hashlib.sha256(payload).hexdigest(),
            byte_size=len(payload),
        )
        with mock.patch('archive.services.restore.effective_path_binding', return_value=binding) as lookup:
            queried = self.client.get(receipt['receipt_url'])
            self.assertEqual((queried.status_code, queried.json()), (200, receipt))
            replay = self.send(headers)
            self.assertEqual((replay.status_code, replay.json()), (200, receipt))
            lookup.assert_called_with('data', 'uploads/' + name)
            self.assertEqual(len(self.published()), 1)
            self.assertEqual(path.read_bytes(), payload)
            replacement = path.with_name('outside-receipt')
            replacement.write_bytes(payload)
            os.replace(replacement, path)
            self.assertEqual(self.client.get(receipt['receipt_url']).status_code, 409)

    def test_consumed_publish_recovery_never_republishes_or_adds_observation(self):
        from archive.services import upload
        class Crash(BaseException):
            pass
        headers = self.headers()
        request_id = headers['HTTP_X_BIRDS_REQUEST_ID']
        with mock.patch.object(upload, 'fault_hook', side_effect=lambda stage:
                               (_ for _ in ()).throw(Crash()) if stage == 'published' else None):
            with self.assertRaises(Crash):
                self.send(headers)
        # 构造旧部署已登记来源的历史交错；新的scan_guard已禁止这条扫描路径。
        fd = open_root(str(self.inbox))
        try:
            observe_file(fd, self.published()[0].name)
        finally:
            os.close(fd)
        call_command('runworker', '--once', verbosity=0)
        photo = Photo.objects.get()
        self.client.get(f'/photos/{photo.pk}/')
        confirmed = self.client.post(f'/photos/{photo.pk}/', {
            'metadata_version': photo.metadata_version, 'request_id': str(uuid.uuid4()),
            'classification': 'undetermined'}, HTTP_X_CSRFTOKEN=self.client.cookies['csrftoken'].value,
            HTTP_X_ARCHIVE_CONFIRM='true')
        self.assertEqual(confirmed.status_code, 200)
        call_command('runworker', '--once', verbosity=0)
        self.assertEqual(self.published(), [])
        self.assertEqual(ArchiveCompletion.objects.count(), 1)
        count = SourceObservation.objects.count()
        recovered = self.send(headers)
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(recovered.json()['status'], 'complete')
        self.assertEqual(self.published(), [])
        scan_once()
        self.assertEqual(SourceObservation.objects.count(), count)
        self.assertEqual(list((self.inbox/'.delivery').glob('*.partial')), [])
        # 若在link前中断，无消费谱系的publishing也不能猜测并再次发布。
        unknown = self.headers()
        with mock.patch.object(upload, 'fault_hook', side_effect=lambda stage:
                               (_ for _ in ()).throw(Crash()) if stage == 'publishing' else None):
            with self.assertRaises(Crash):
                self.send(unknown)
        self.assertEqual(self.send(unknown).json()['code'], 'publishing_uncertain')
        self.assertEqual(self.send(unknown).json()['code'], 'publishing_uncertain')
        self.assertEqual(self.published(), [])
        self.assertEqual(len(list((self.inbox/'.delivery').glob('*.partial'))), 1)

    def test_receipt_query_after_response_loss_cleans_only_owned_anchor(self):
        from archive.services import upload
        class Crash(BaseException):
            pass
        headers = self.headers()
        request_id = headers['HTTP_X_BIRDS_REQUEST_ID']
        with mock.patch.object(upload, 'fault_hook', side_effect=lambda stage:
                               (_ for _ in ()).throw(Crash()) if stage == 'receipt_saved' else None):
            with self.assertRaises(Crash):
                self.send(headers)
        anchor = self.inbox/'.delivery'/f'{request_id}.partial'
        self.assertTrue(anchor.exists())
        unknown = self.inbox/'.delivery'/'unowned.partial'
        unknown.write_bytes(b'preserve')
        result = self.client.get(f'/delivery/receipts/{request_id}/')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['status'], 'complete')
        self.assertFalse(anchor.exists())
        self.assertEqual(unknown.read_bytes(), b'preserve')
        self.assertEqual(len(self.published()), 1)

    def test_scan_waits_for_complete_and_anchor_cleanup_under_request_lock(self):
        from archive.services import upload
        scanned = []
        def scan_interleaving(stage):
            if stage in ('published', 'receipt_saved'):
                self.assertEqual(scan_once(), 0)
                self.assertEqual(SourceObservation.objects.count(), 0)
                scanned.append(stage)
        with mock.patch.object(upload, 'fault_hook', side_effect=scan_interleaving):
            response = self.send()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(scanned, ['published', 'receipt_saved'])
        self.assertEqual(list((self.inbox/'.delivery').glob('*.partial')), [])
        self.assertEqual(scan_once(), 1)
        first = SourceObservation.objects.get()
        scan_once()
        self.assertEqual(SourceObservation.objects.count(), 1)
        self.assertEqual(SourceObservation.objects.get().fingerprint, first.fingerprint)
        call_command('runworker', '--once', verbosity=0)
        self.assertEqual(Photo.objects.count(), 1)
        self.assertEqual(SourceObservation.objects.count(), 1)
        class Crash(BaseException):
            pass
        headers = self.headers()
        with mock.patch.object(upload, 'fault_hook', side_effect=lambda stage:
                               (_ for _ in ()).throw(Crash()) if stage == 'receipt_saved' else None):
            with self.assertRaises(Crash):
                self.send(headers)
        # 请求锁已释放但complete anchor未清，扫描仍跳过，不能先登记后改ctime。
        self.assertEqual(scan_once(), 0)
        self.assertEqual(SourceObservation.objects.count(), 1)
        request_id = headers['HTTP_X_BIRDS_REQUEST_ID']
        self.assertEqual(self.client.get(f'/delivery/receipts/{request_id}/').status_code, 200)
        self.assertEqual(scan_once(), 1)
        self.assertEqual(SourceObservation.objects.count(), 2)
        # 无接收器命名空间的旧直接投递仍正常发现。
        (self.inbox/'direct.jpg').write_bytes(self.payload)
        self.assertEqual(scan_once(), 1)

    def test_failed_owned_temporary_cleanup_preserves_unknown_replacement(self):
        from archive.services import upload
        for payload, overrides in ((b'not jpeg', {}), (self.payload, {'MAX_IMAGE_PIXELS': 10})):
            with override_settings(**overrides):
                result = self.send(payload=payload)
            self.assertEqual(result.status_code, 422)
            self.assertEqual(result.json()['code'], 'image_invalid')
            self.assertEqual(list((self.inbox/'.delivery').glob('*.partial')), [])
        class Crash(BaseException):
            pass
        headers = self.headers(payload=b'bad jpeg')
        request_id = headers['HTTP_X_BIRDS_REQUEST_ID']
        with mock.patch.object(upload, 'fault_hook', side_effect=lambda stage:
                               (_ for _ in ()).throw(Crash()) if stage == 'failed_saved' else None):
            with self.assertRaises(Crash):
                self.send(headers, b'bad jpeg')
        anchor = self.inbox/'.delivery'/f'{request_id}.partial'
        anchor.rename(self.inbox/'.delivery'/'original-evidence.partial')
        anchor.write_bytes(b'foreign')
        result = self.client.get(f'/delivery/receipts/{request_id}/')
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()['code'], 'ownership_conflict')
        self.assertEqual(anchor.read_bytes(), b'foreign')
        self.assertEqual((self.inbox/'.delivery'/'original-evidence.partial').read_bytes(), b'bad jpeg')

    def test_token_endpoint_exists(self):
        response = self.client.get('/delivery/token/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('csrf_token', response.json())
        self.assertIn('csrftoken', response.cookies)
        self.assertIn('no-store', response['Cache-Control'])

    def test_csrf_host_origin_and_methods(self):
        self.assertEqual(self.client.post('/delivery/files/', self.payload,
            content_type='application/octet-stream', **self.headers()).status_code, 403)
        token = self.client.get('/delivery/token/').json()['csrf_token']
        self.assertEqual(self.client.post('/delivery/files/', self.payload,
            content_type='application/octet-stream', HTTP_X_CSRFTOKEN=token,
            HTTP_ORIGIN='https://foreign.invalid', **self.headers()).status_code, 403)
        self.assertEqual(self.client.get('/delivery/token/', HTTP_HOST='foreign.invalid').status_code, 400)
        self.assertEqual(self.client.get('/delivery/files/').status_code, 405)
        self.assertEqual(self.published(), [])

    def test_invalid_metadata_bytes_jpeg_and_pixel_limits(self):
        for field, value in (('HTTP_X_BIRDS_FILENAME', '../escape.jpg'),
                             ('HTTP_X_BIRDS_FILENAME', 'fake.png'),
                             ('HTTP_X_BIRDS_LENGTH', '-1'),
                             ('HTTP_X_BIRDS_SHA256', '0'*64)):
            with self.subTest(field=field, value=value):
                headers = self.headers(); headers[field] = value
                self.assertGreaterEqual(self.send(headers).status_code, 400)
        invalid_headers = self.headers(payload=b'not jpeg')
        invalid = self.send(invalid_headers, payload=b'not jpeg')
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()['code'], 'image_invalid')
        self.assertEqual(self.send(invalid_headers, payload=b'not jpeg').json(), invalid.json())
        with override_settings(MAX_FILE_BYTES=10):
            self.assertEqual(self.send().status_code, 413)
        with override_settings(MAX_IMAGE_PIXELS=10):
            self.assertEqual(self.send().status_code, 422)
        self.assertEqual(self.published(), [])
        scan_once()
        self.assertFalse(Photo.objects.exists())

    def test_same_request_conflict_and_same_name_keep_both(self):
        headers = self.headers()
        first = self.send(headers)
        self.assertEqual(first.status_code, 201)
        changed = {**headers, 'HTTP_X_BIRDS_SHA256': 'a'*64}
        self.assertEqual(self.send(changed).status_code, 409)
        stream = io.BytesIO()
        Image.new('RGB', (40, 30), 'blue').save(stream, 'JPEG')
        self.assertEqual(self.send(payload=stream.getvalue()).status_code, 201)
        self.assertEqual(len(self.published()), 2)
        self.assertEqual(len({path.read_bytes() for path in self.published()}), 2)
        self.assertEqual(self.send(headers).json(), first.json())
        tampered = bytearray(self.payload); tampered[-1] ^= 1
        self.assertEqual(self.send(headers, bytes(tampered)).json()['code'], 'digest_mismatch')

    def test_stream_interruption_and_chunk_limit_never_scan(self):
        from archive.services.upload import receive, UploadError
        class Broken:
            def read(self, size):
                raise OSError('disconnect /private/secret')
        metadata = dict(request_id=str(uuid.uuid4()), filename='break.jpg',
                        length=len(self.payload), sha256=hashlib.sha256(self.payload).hexdigest())
        with self.assertRaises(UploadError) as caught:
            receive(Broken(), **metadata)
        self.assertEqual(caught.exception.code, 'stream_interrupted')
        self.assertEqual(self.published(), [])
        with self.assertRaises(UploadError) as caught:
            receive(io.BytesIO(self.payload), **{**metadata, 'request_id': str(uuid.uuid4()), 'length': 10})
        self.assertEqual(caught.exception.code, 'length_mismatch')
        scan_once()
        self.assertFalse(Photo.objects.exists())

    def test_crashes_before_and_after_publish_reconcile_by_identity(self):
        from archive.services import upload
        class Crash(BaseException):
            pass
        for stage in ('prepared', 'published', 'receipt_saved'):
            headers = self.headers()
            def interrupt(current):
                if current == stage:
                    raise Crash()
            with mock.patch.object(upload, 'fault_hook', side_effect=interrupt):
                with self.assertRaises(Crash):
                    self.send(headers)
            self.assertEqual(len(self.published()), int(stage != 'prepared'))
            response = self.send(headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.send(headers).json(), response.json())
            self.assertEqual(len(self.published()), 1)
            self.assertEqual(list((self.inbox/'.delivery').glob('*.partial')), [])
            for path in self.published():
                path.unlink()

    def test_unprepared_crash_and_per_request_lock_preserve_evidence(self):
        from archive.services import upload
        class Crash(BaseException):
            pass
        headers = self.headers()
        request_id = headers['HTTP_X_BIRDS_REQUEST_ID']
        with mock.patch.object(upload, 'fault_hook', side_effect=lambda stage:
                               (_ for _ in ()).throw(Crash()) if stage == 'received' else None):
            with self.assertRaises(Crash):
                self.send(headers)
        partial = self.inbox/'.delivery'/f'{request_id}.partial'
        self.assertEqual(partial.read_bytes(), self.payload)
        self.assertEqual(self.send(headers).json()['code'], 'interrupted_unprepared')
        scan_once()
        self.assertFalse(Photo.objects.exists())
        with upload._storage() as roots, upload._lock(roots['records'], request_id):
            self.assertEqual(self.send(headers).json()['code'], 'request_busy')
        self.assertEqual(partial.read_bytes(), self.payload)

    def test_external_same_hash_is_not_adopted_and_symlinks_rejected(self):
        from archive.services import upload
        class Crash(BaseException):
            pass
        headers = self.headers()
        with mock.patch.object(upload, 'fault_hook', side_effect=lambda stage:
                               (_ for _ in ()).throw(Crash()) if stage == 'prepared' else None):
            with self.assertRaises(Crash):
                self.send(headers)
        request_id = headers['HTTP_X_BIRDS_REQUEST_ID']
        intent = json.loads((self.data/'uploads'/f'{request_id}.json').read_text())
        foreign = self.inbox/intent['target']
        foreign.write_bytes(self.payload)
        before = foreign.stat().st_ino
        response = self.send(headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'ownership_conflict')
        self.assertEqual(foreign.stat().st_ino, before)
        self.assertEqual(foreign.read_bytes(), self.payload)
        (self.inbox/'.delivery').rename(self.inbox/'.saved')
        (self.inbox/'.delivery').symlink_to(self.root/'archive', target_is_directory=True)
        self.assertEqual(self.send().status_code, 409)
        self.assertEqual(list((self.root/'archive').iterdir()), [])
