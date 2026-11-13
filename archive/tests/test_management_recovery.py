"""Real request → shared-lock worker → current media and formal completion."""
import hashlib
import errno
import os
import uuid
from unittest import mock

from archive.models import Photo, ManagedOriginal, ManagementOperation, ArchiveOperation
from archive.services import management
from archive.tests.test_download import DownloadFixture
from archive.tests.test_archive_recovery import ArchiveFixture


class CaptureTracerTests(DownloadFixture):
    def test_copy_fallback_and_collision_preserve_foreign_file(self):
        photo, old, managed = self.completed()
        photo.refresh_from_db()
        original = old.read_bytes()
        occupied = self.archive/'白鹭'/'2032'/'one.jpg'
        occupied.parent.mkdir(parents=True)
        occupied.write_bytes(b'foreign original must survive')
        operation = management.accept_management(photo.pk, photo.metadata_version, uuid.uuid4(), 'capture',
            {'capture': {'local':'2032-01-01','precision':'date','offset':None}})
        link = os.link
        def cross_device(source, target, **kwargs):
            if source == 'one.jpg':
                raise OSError(errno.EXDEV, 'synthetic cross-device')
            return link(source, target, **kwargs)
        with mock.patch('archive.services.management.os.link', side_effect=cross_device):
            self.run_worker()
        operation.refresh_from_db(); managed.refresh_from_db()
        self.assertEqual(operation.status, 'succeeded')
        self.assertEqual(occupied.read_bytes(), b'foreign original must survive')
        self.assertNotEqual(managed.relative_path, str(occupied.relative_to(self.archive)))
        self.assertEqual((self.archive/managed.relative_path).read_bytes(), original)
        self.assertFalse(old.exists())
        self.assertFalse(list(self.archive.rglob('*.tmp')))
        self.assert_download(photo, self.archive/managed.relative_path, managed)

    def test_capture_request_worker_current_projection_and_download(self):
        photo, target, managed = self.completed()
        original = target.read_bytes()
        photo.refresh_from_db()
        old_capture, old_history = photo.current_capture, ArchiveOperation.objects.get(photo=photo).capture_snapshot
        request_id = uuid.uuid4()
        data = {'management_action':'capture', 'metadata_version':photo.metadata_version,
            'request_id':str(request_id), 'date':'2030-02-03', 'time':'', 'offset':''}
        response = self.client.post(f'/photos/{photo.pk}/', data)
        self.assertEqual(response.status_code, 303)
        operation = ManagementOperation.objects.get(request_id=request_id)
        photo.refresh_from_db()
        self.assertEqual(photo.current_capture, old_capture)
        self.assertIsNone(photo.manual_capture)
        self.assertTrue(target.exists())
        self.assertContains(self.client.get(f'/photos/{photo.pk}/'), '已受理')
        self.assertEqual(self.client.post(f'/photos/{photo.pk}/', data).status_code, 303)
        self.assertEqual(ManagementOperation.objects.count(), 1)
        observed = []
        def observe(stage):
            if stage == 'registered':
                current = ManagedOriginal.objects.get(photo=photo)
                current_photo = Photo.objects.get(pk=photo.pk)
                self.assertEqual(current_photo.current_capture, old_capture)
                self.assertTrue(target.exists())
                management._registered_readback(ManagementOperation.objects.get(pk=operation.pk))
                self.assert_download(current_photo, self.archive/current.relative_path, current)
                observed.append(stage)
        # The unique worker dispatches the management journal; import root is absent.
        self.inbox.rmdir()
        with mock.patch('archive.services.management.management_fault_hook', side_effect=observe):
            self.run_worker()
        operation.refresh_from_db(); photo.refresh_from_db(); managed.refresh_from_db()
        self.assertEqual(observed, ['registered'])
        self.assertEqual((operation.stage, operation.status), ('completed', 'succeeded'))
        self.assertEqual(managed.relative_path, '白鹭/2030/one.jpg')
        self.assertEqual(managed.location_revision, 1)
        self.assertFalse(target.exists())
        current = self.archive/managed.relative_path
        self.assertEqual(current.read_bytes(), original)
        self.assertEqual(hashlib.sha256(current.read_bytes()).hexdigest(), photo.sha256)
        self.assertEqual(len(list(self.archive.rglob('*.jpg'))), 1)
        self.assertEqual(photo.current_capture['local'], '2030-02-03')
        self.assertEqual(photo.manual_capture['precision'], 'date')
        self.assertEqual(ArchiveOperation.objects.get(photo=photo).capture_snapshot, old_history)
        self.assertContains(self.client.get('/'), '2030 年 2 月')
        self.assertContains(self.client.get(f'/photos/{photo.pk}/'), '2030-02-03')
        self.assert_download(photo, current, managed)
        self.assertTrue(management.process_management(operation.pk))
        self.assertEqual(ManagedOriginal.objects.get(photo=photo).location_revision, 1)

    def test_same_year_does_not_copy_and_conflict_keeps_draft(self):
        photo, target, managed = self.completed()
        photo.refresh_from_db()
        # First unknown → known year, followed by same-directory known-time change.
        operation = management.accept_management(photo.pk, photo.metadata_version, uuid.uuid4(), 'capture',
            {'capture': {'local':'2030-01-01','precision':'date','offset':None}})
        self.run_worker(); photo.refresh_from_db(); managed.refresh_from_db()
        target = self.archive/managed.relative_path
        inode = target.stat().st_ino
        next_operation = management.accept_management(photo.pk, photo.metadata_version, uuid.uuid4(), 'capture',
            {'capture': {'local':'2030-06-01','precision':'date','offset':None}})
        self.run_worker(); photo.refresh_from_db(); managed.refresh_from_db()
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(len(list(self.archive.rglob('*.jpg'))), 1)
        self.assertEqual(photo.current_capture['local'], '2030-06-01')
        self.assertEqual(managed.location_revision, 2)
        response = self.client.post(f'/photos/{photo.pk}/', {'management_action':'capture',
            'metadata_version':1, 'request_id':str(uuid.uuid4()), 'date':'2031-01-01'})
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, '2031-01-01', status_code=409)


class CaptureModesTests(DownloadFixture):
    def test_unknown_fill_same_year_precision_and_explicit_inherit(self):
        photo, old, managed = self.completed()
        raw = {'DateTimeOriginal': '2020:03:04 12:13:14'}
        Photo.objects.filter(pk=photo.pk).update(captured_local='2020-03-04 12:13:14',
            capture_precision='second', capture_quality='reliable', capture_offset=480, exif_raw=raw)
        original = old.read_bytes()
        fraction = '12345678901234567890123456789012'
        cases = [
            ('manual', '2030-02-03', '', '2030'),
            ('unknown', '', '', '未知年份'),
            ('manual', '2031-04-05', '12:34:56.'+fraction, '2031'),
            ('manual', '2031-06-07', '12:34:56.'+fraction, '2031'),
            ('unknown', '', '', '未知年份'),
            ('inherit', '', '', '2020'),
        ]
        previous = None
        for mode, day, clock, year in cases:
            photo.refresh_from_db(); managed.refresh_from_db()
            before = self.archive/managed.relative_path
            inode = before.stat().st_ino
            response = self.client.post(f'/photos/{photo.pk}/', {'management_action': 'capture',
                'request_id': str(uuid.uuid4()), 'metadata_version': photo.metadata_version,
                'mode': mode, 'date': day, 'time': clock, 'offset': ''})
            self.assertEqual(response.status_code, 303)
            self.run_worker()
            photo.refresh_from_db(); managed.refresh_from_db()
            current = self.archive/managed.relative_path
            self.assertEqual(photo.capture_override_mode, mode)
            self.assertEqual(current.parent.name, year)
            self.assertEqual(current.read_bytes(), original)
            self.assertEqual(photo.exif_raw, raw)
            self.assertEqual(len(list(self.archive.rglob('*.jpg'))), 1)
            self.assertEqual(photo.current_capture, photo.effective_capture())
            if mode == 'unknown':
                self.assertTrue(all(value is None for value in photo.effective_capture().values()))
                self.assertEqual(photo.capture_display, '未知')
                self.assertEqual(photo.capture_source, '未知')
                self.assertEqual(photo.capture_precision_display, '未知')
                self.assertEqual(photo.capture_offset_display, '时区未知')
                self.assertContains(self.client.get('/'), '时间未知')
            if clock:
                self.assertEqual(photo.current_capture['local'], day+' '+clock)
                self.assertEqual(photo.current_capture['sort_key'][-32:], fraction)
                self.assertIsNone(photo.current_capture['offset'])
            if previous == year:
                self.assertEqual(current, before)
                self.assertEqual(current.stat().st_ino, inode)
                operation = photo.management_operations.latest('created_at')
                self.assertEqual(operation.collision_counter, 0)
            previous = year
        self.assertEqual(photo.capture_source, '原始 EXIF')
        self.assertEqual(photo.effective_capture()['offset'], 480)

    def test_invalid_calendar_and_precision_never_queue(self):
        photo, _, _ = self.completed(); photo.refresh_from_db()
        for day, clock in [('2030-02-30', ''), ('2030-01-01', '12:00:00.'+'1'*33)]:
            response = self.client.post(f'/photos/{photo.pk}/', {'management_action': 'capture',
                'request_id': str(uuid.uuid4()), 'metadata_version': photo.metadata_version,
                'mode': 'manual', 'date': day, 'time': clock})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(ManagementOperation.objects.count(), 0)


class ManagementRecoveryTests(ArchiveFixture):
    completed = DownloadFixture.completed
    download = DownloadFixture.download
    assert_download = DownloadFixture.assert_download

    def intent(self):
        photo, source, managed = self.completed()
        photo.refresh_from_db()
        operation = management.accept_management(photo.pk, photo.metadata_version, uuid.uuid4(), 'capture',
            {'capture': {'local': '2035-03-04', 'precision': 'date', 'offset': None}})
        return photo, source, managed, operation

    def recover_at(self, stage):
        photo, source, managed, operation = self.intent()
        content = source.read_bytes()
        old_capture = Photo.objects.get(pk=photo.pk).current_capture
        process = self.start_paused('management_'+stage)
        self.assertIsNone(process.poll())
        self.stop_process(process)
        self.assertIsNotNone(process.returncode)
        operation.refresh_from_db(); managed.refresh_from_db()
        self.assertEqual(operation.stage, stage)
        target = self.archive/operation.target_relative_path
        self.assertEqual(target.read_bytes(), content)
        self.assertEqual(Photo.objects.get(pk=photo.pk).current_capture, old_capture)
        readable = self.archive/managed.relative_path
        self.assertTrue(readable.exists())
        self.assert_download(photo, readable, managed)
        fixed = operation.target_relative_path
        Photo.objects.filter(pk=photo.pk).update(user_location='后来地点', notes='后来备注',
            favorite=True, rating=5, metadata_version=99)
        self.subprocess_worker()
        photo.refresh_from_db(); managed.refresh_from_db(); operation.refresh_from_db()
        self.assertEqual((operation.stage, operation.status), ('completed', 'succeeded'))
        self.assertEqual(operation.target_relative_path, fixed)
        self.assertEqual((photo.notes, photo.user_location, photo.favorite, photo.rating, photo.metadata_version),
            ('后来备注', '后来地点', True, 5, 99))
        self.assertEqual(target.read_bytes(), content)
        self.assertFalse(source.exists())
        self.assertEqual(len(list(self.archive.rglob('*.jpg'))), 1)
        self.assertEqual(photo.current_capture['year'], '2035')
        from archive.services.archiving import process_archive
        Photo.objects.filter(pk=photo.pk).update(state='newer-state')
        self.assertTrue(process_archive(ArchiveOperation.objects.get(photo=photo).job))
        photo.refresh_from_db()
        self.assertEqual(photo.state, 'newer-state')
        self.assertEqual(photo.current_capture['year'], '2035')
        self.assertTrue(management.process_management(operation.pk))
        self.assert_download(photo, target, managed)

    def test_restart_after_publish(self):
        self.recover_at('published')

    def test_restart_after_registration_readback(self):
        self.recover_at('registered')

    def test_restart_after_source_cleanup(self):
        self.recover_at('cleaned')

    def test_foreign_same_hash_collision_and_copy_sync_retry(self):
        photo, source, managed, operation = self.intent()
        occupied = self.archive/'白鹭/2035/one.jpg'
        occupied.parent.mkdir(parents=True)
        occupied.write_bytes(source.read_bytes())
        foreign_inode = occupied.stat().st_ino
        link, sync = os.link, management._sync
        def exdev(source_name, target, **kwargs):
            if source_name == 'one.jpg':
                raise OSError(errno.EXDEV, 'synthetic cross-device')
            return link(source_name, target, **kwargs)
        def fail_file_sync(descriptor):
            import stat
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError(errno.EIO, 'synthetic fsync failure')
            return sync(descriptor)
        with mock.patch('archive.services.management.os.link', side_effect=exdev), \
                mock.patch('archive.services.management._sync', side_effect=fail_file_sync):
            self.assertFalse(management.process_management(operation.pk))
        operation.refresh_from_db()
        self.assertEqual(operation.status, 'blocked')
        self.assertTrue(source.exists())
        self.assertEqual(source.read_bytes(), occupied.read_bytes())
        temporary = operation.retry_context['temporary_relative_path']
        with mock.patch('archive.services.management.os.link', side_effect=exdev):
            self.assertTrue(management.process_management(operation.pk))
        managed.refresh_from_db(); operation.refresh_from_db()
        self.assertEqual(occupied.stat().st_ino, foreign_inode)
        self.assertNotEqual(managed.relative_path, str(occupied.relative_to(self.archive)))
        self.assertEqual((self.archive/managed.relative_path).read_bytes(), occupied.read_bytes())
        self.assertFalse((self.archive/temporary).exists())
        fixed = operation.target_relative_path
        self.assertTrue(management.process_management(operation.pk))
        operation.refresh_from_db()
        self.assertEqual(operation.target_relative_path, fixed)

    def test_changed_source_and_root_block_without_adoption(self):
        photo, source, managed, operation = self.intent()
        content = source.read_bytes()
        saved = source.with_suffix('.saved')
        source.rename(saved); source.write_bytes(content)
        self.assertFalse(management.process_management(operation.pk))
        operation.refresh_from_db()
        self.assertEqual(operation.error_code, 'ownership_changed')
        self.assertEqual(source.read_bytes(), content)
        source.unlink(); saved.rename(source)
        moved_root = self.archive.with_name('original-root')
        self.archive.rename(moved_root); self.archive.mkdir()
        try:
            self.assertFalse(management.process_management(operation.pk))
            operation.refresh_from_db()
            self.assertEqual(operation.error_code, 'root_changed')
            self.assertTrue((moved_root/managed.relative_path).exists())
        finally:
            self.archive.rmdir(); moved_root.rename(self.archive)

    def test_obsolete_generation_preserves_published_evidence(self):
        photo, source, managed, operation = self.intent()
        def newer_decision(stage):
            if stage == 'published':
                Photo.objects.filter(pk=photo.pk).update(management_generation=operation.generation+1,
                    current_capture={'local': 'newer-value'})
        with mock.patch('archive.services.management.management_fault_hook', side_effect=newer_decision):
            self.assertFalse(management.process_management(operation.pk))
        operation.refresh_from_db(); photo.refresh_from_db()
        self.assertEqual(operation.status, 'stale')
        self.assertEqual(operation.stage, 'published')
        self.assertEqual(photo.current_capture, {'local': 'newer-value'})
        self.assertTrue(source.exists())
        self.assertEqual((self.archive/operation.target_relative_path).read_bytes(), source.read_bytes())
        self.assertFalse(management.process_management(operation.pk))


class ManagementStatusTests(DownloadFixture):
    def blocked_intent(self):
        photo, source, managed = self.completed(); photo.refresh_from_db()
        operation = management.accept_management(photo.pk, photo.metadata_version, uuid.uuid4(), 'capture',
            {'capture': {'local': '2036-01-01', 'precision': 'date', 'offset': None}})
        with mock.patch('archive.services.management._sync', side_effect=PermissionError('fixture')):
            self.assertFalse(management.process_management(operation.pk))
        operation.refresh_from_db()
        return photo, source, managed, operation

    def test_status_read_only_and_pending_draft_download(self):
        photo, source, managed, operation = self.blocked_intent()
        count = ManagementOperation.objects.count()
        for url in ('/tasks/', '/tasks/status/', f'/photos/{photo.pk}/'):
            response = self.client.get(url)
            self.assertContains(response, '更正未完成')
            self.assertContains(response, '存储权限不足')
            self.assertContains(response, '当前')
            self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(ManagementOperation.objects.count(), count)
        self.assert_download(photo, source, managed)
        response = self.client.post(f'/photos/{photo.pk}/', {'management_action': 'capture',
            'mode': 'manual', 'date': '2037-02-03', 'time': '12:34:56.123456789',
            'request_id': str(uuid.uuid4()), 'metadata_version': Photo.objects.get(pk=photo.pk).metadata_version})
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, '2037-02-03', status_code=409)
        self.assertContains(response, '12:34:56.123456789', status_code=409)
        self.assertEqual(ManagementOperation.objects.count(), count)

    def test_retry_same_receipt_same_target_and_reject_superseded(self):
        photo, source, managed, operation = self.blocked_intent()
        stage, target, original_request = operation.stage, operation.target_relative_path, operation.request_id
        data = {'management_action': 'retry', 'operation_id': str(operation.pk), 'request_id': str(uuid.uuid4())}
        for _ in range(2):
            self.assertEqual(self.client.post(f'/photos/{photo.pk}/', data).status_code, 303)
        operation.refresh_from_db()
        self.assertEqual((operation.stage, operation.target_relative_path, operation.request_id),
            (stage, target, original_request))
        self.assertEqual(operation.status, 'pending')
        self.run_worker()
        operation.refresh_from_db(); managed.refresh_from_db()
        self.assertEqual(operation.status, 'succeeded')
        self.assertEqual(self.client.post(f'/photos/{photo.pk}/', data).status_code, 303)
        self.assertEqual(ManagementOperation.objects.count(), 1)
        self.assertEqual(len(list(self.archive.rglob('*.jpg'))), 1)
        self.assert_download(photo, self.archive/managed.relative_path, managed)
        self.assertContains(self.client.get(f'/photos/{photo.pk}/'), '更正已完成')
        Photo.objects.filter(pk=photo.pk).update(management_generation=operation.generation+1)
        self.assertEqual(self.client.post(f'/photos/{photo.pk}/', data).status_code, 409)

    def test_csrf_unknown_action_and_cross_photo_rejected(self):
        from django.test import Client
        photo, _, _, operation = self.blocked_intent()
        data = {'management_action': 'retry', 'operation_id': str(operation.pk), 'request_id': str(uuid.uuid4())}
        secure = Client(enforce_csrf_checks=True)
        self.assertEqual(secure.post(f'/photos/{photo.pk}/', data).status_code, 403)
        secure.get(f'/photos/{photo.pk}/')
        token = secure.cookies['csrftoken'].value
        self.assertEqual(secure.post(f'/photos/{photo.pk}/', {**data, 'csrfmiddlewaretoken': token}).status_code, 303)
        operation.refresh_from_db(); self.assertEqual(operation.status, 'pending')
        self.assertEqual(self.client.post(f'/photos/{photo.pk}/', {**data, 'management_action': 'unknown'}).status_code, 400)
        other, _, _ = self.confirmed('other.jpg', 'blue')
        self.assertEqual(self.client.post(f'/photos/{other.pk}/', data).status_code, 409)
        self.assertEqual(ManagementOperation.objects.count(), 1)
