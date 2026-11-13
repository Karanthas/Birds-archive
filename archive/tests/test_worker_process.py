import hashlib
import os
import signal
import sqlite3
import subprocess
import time
from pathlib import Path
from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection
from django.test import TransactionTestCase
from archive.models import Photo, Job
from archive.tests.fixtures import command, command_options, jpeg


class WorkerProcessTests(TransactionTestCase):
    def setUp(self):
        self.inbox, self.data = Path(settings.BIRDS_INBOX_ROOT), Path(settings.BIRDS_DATA_ROOT)
        for path in self.inbox.iterdir():
            if path.is_file() or path.is_symlink():
                path.unlink()
        self.owner = User.objects.create_superuser(settings.BIRDS_OWNER_USERNAME, password='process-pass')
        self.client.force_login(self.owner)
        for name in ('fault-ready', 'fault-release'):
            (Path(settings.BIRDS_TEST_ROOT)/name).unlink(missing_ok=True)

    def start_paused(self, stage):
        options = command_options('runworker','--once')
        options['env']['BIRDS_TEST_FAULT_STAGE'] = stage
        process = subprocess.Popen(**options, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_process, process)
        marker = Path(settings.BIRDS_TEST_ROOT)/'fault-ready'
        deadline = time.monotonic()+8
        while not marker.exists():
            if process.poll() is not None:
                self.fail('worker未到注入点：'+process.communicate(timeout=2)[1])
            if time.monotonic()>deadline:
                self.fail('worker注入点超时')
            time.sleep(.025)
        self.assertEqual(marker.read_text(),stage)
        return process

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=3)

    def test_four_kill_boundaries_recover_one_current_preview(self):
        import json
        from archive.models import (ImportSource, SourceObservation, RecognitionState,
            RecognitionCandidate, RecognitionRun)
        stages = ('claimed','temporary_complete','published','before_finish')
        evidence=[]
        for stage in stages:
            with self.subTest(stage=stage):
                # DB与输入按测试切片清空；从不删除共享锁inode。
                RecognitionState.objects.all().delete()
                RecognitionCandidate.objects.all().delete()
                RecognitionRun.objects.all().delete()
                Job.objects.all().delete()
                ImportSource.objects.update(current_observation=None,photo=None)
                SourceObservation.objects.all().delete(); ImportSource.objects.all().delete(); Photo.objects.all().delete()
                for file in self.inbox.iterdir():
                    file.unlink()
                (Path(settings.BIRDS_TEST_ROOT)/'fault-ready').unlink(missing_ok=True)
                source = self.inbox/f'{stage}.jpg'
                digest = jpeg(source)
                worker = self.start_paused(stage)
                inode = (self.data/'worker.lock').stat().st_ino
                self.assertEqual(Job.objects.get().state,'running')
                worker.kill(); worker.communicate(timeout=3)
                recovered = command('runworker','--once')
                self.assertEqual(recovered.returncode,0,recovered.stderr)
                self.assertEqual((Photo.objects.count(),Job.objects.count()),(1,1))
                self.assertEqual((Job.objects.get().state,Photo.objects.get().state),('succeeded','review_pending'))
                self.assertEqual((self.data/'worker.lock').stat().st_ino,inode)
                connection.close()
                with sqlite3.connect(settings.DATABASES['default']['NAME']) as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM archive_photo").fetchone()[0],1)
                self.assertContains(self.client.get('/review/'),'待确认')
                photo=Photo.objects.get()
                response=self.client.get(f'/media/{photo.pk}/preview/')
                self.assertEqual(response.status_code,200)
                self.assertTrue(b''.join(response.streaming_content))
                cache=self.data/'previews'/'jobs'/str(Job.objects.get().pk)
                self.assertEqual(len(list(cache.glob('*.jpg'))),2)
                self.assertEqual(list(cache.glob('.build-*')),[])
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),digest)
                evidence.append({'stage':stage,'job_id':str(Job.objects.get().pk),'state':'succeeded',
                    'photos':Photo.objects.count(),'preview_files':len(list(cache.glob('*.jpg'))),
                    'sha256_unchanged':True,'lock_inode_unchanged':True,'reconnected_db':True,'private_get':200})
        Path('/tmp/birds-01-04-recovery-evidence.json').write_text(json.dumps(evidence,indent=2))

    def test_stale_heartbeat_never_steals_lock_from_paused_process(self):
        from datetime import timedelta
        from django.utils import timezone
        jpeg(self.inbox/'暂停.jpg')
        worker=self.start_paused('claimed')
        Job.objects.update(updated_at=timezone.now()-timedelta(days=1))
        inode=(self.data/'worker.lock').stat().st_ino
        second=command('runworker','--once',timeout=4)
        self.assertNotEqual(second.returncode,0)
        self.assertIn('worker_busy',second.stderr)
        self.assertEqual(Photo.objects.count(),0)
        self.assertEqual((self.data/'worker.lock').stat().st_ino,inode)
        self.stop_process(worker)

    def test_old_worker_result_rejected_after_source_version_changes(self):
        from archive.services.intake import scan_once
        jpeg(self.inbox/'换图.jpg')
        worker=self.start_paused('before_finish')
        old=Job.objects.get()
        digest=jpeg(self.inbox/'换图.jpg',color=(2,3,4))
        scan_once()
        (Path(settings.BIRDS_TEST_ROOT)/'fault-release').write_text('release')
        worker.communicate(timeout=4)
        self.assertEqual(worker.returncode,0)
        old.refresh_from_db(); self.assertEqual(old.state,'stale')
        self.assertEqual(Photo.objects.count(),0)
        recovered=command('runworker','--once')
        self.assertEqual(recovered.returncode,0,recovered.stderr)
        self.assertEqual(Photo.objects.get().sha256,digest)

    def test_sigterm_stops_before_next_job_and_releases_lock(self):
        from archive.services.intake import scan_once
        jpeg(self.inbox/'终止.jpg')
        worker=self.start_paused('claimed')
        worker.send_signal(signal.SIGTERM)
        (Path(settings.BIRDS_TEST_ROOT)/'fault-release').write_text('release')
        worker.communicate(timeout=4)
        self.assertEqual(worker.returncode,0)
        self.assertEqual(Job.objects.get().state,'succeeded')
        result=command('runworker','--once')
        self.assertEqual(result.returncode,0,result.stderr)

    def test_invalid_resource_environment_rejected_before_worker(self):
        for key,value in (('WORKER_CONCURRENCY','2'),('MAX_FILE_BYTES','0'),('MAX_IMAGE_PIXELS','-1'),
                          ('SCAN_INTERVAL_MS','0.1'),('STABLE_WINDOW_MS','abc'),('WORKER_MEMORY_MB','0'),
                          ('RETRY_LIMIT','0'),('RETRY_BASE_MS','-4'),('WORKER_JOB_SLEEP_MS','NaN')):
            with self.subTest(key=key):
                options=command_options('check')
                options['env'][key]=value
                result=subprocess.run(**options,capture_output=True,text=True,timeout=4)
                self.assertNotEqual(result.returncode,0, f'{key}应拒绝非法值')
                self.assertIn(key,result.stderr)

    def test_configured_byte_pixel_limits_reject_before_decode(self):
        from unittest import mock
        from django.test import override_settings
        from archive.services.images import inspect_open_jpeg,ImageRejected
        path=self.inbox/'限额.jpg'; jpeg(path,size=(10,10))
        with path.open('rb') as source, override_settings(MAX_FILE_BYTES=path.stat().st_size-1):
            with mock.patch('archive.services.images.Image.open',side_effect=AssertionError('不应解码')):
                with self.assertRaises(ImageRejected) as caught:
                    inspect_open_jpeg(source)
                self.assertEqual(caught.exception.code,'image_bytes_exceeded')
        with path.open('rb') as source, override_settings(MAX_IMAGE_PIXELS=99):
            with mock.patch('archive.services.images.Image.Image.load',side_effect=AssertionError('不应load')):
                with self.assertRaises(ImageRejected) as caught:
                    inspect_open_jpeg(source)
                self.assertEqual(caught.exception.code,'image_pixels_exceeded')
        with path.open('rb') as source, override_settings(MAX_FILE_BYTES=path.stat().st_size,MAX_IMAGE_PIXELS=100):
            image,_=inspect_open_jpeg(source); image.close()

    def test_web_queries_never_hash_or_decode(self):
        from unittest import mock
        for target in ('archive.services.images.content_hash','archive.services.jobs.content_hash',
                       'archive.services.images.inspect_open_jpeg','archive.services.jobs.inspect_open_jpeg'):
            with mock.patch(target,side_effect=AssertionError('Web不能处理图像')):
                self.assertEqual(self.client.get('/').status_code,200)

    def test_runtime_report_real_database_units_filesystem_and_safe_output(self):
        import json
        result=command('checkruntime')
        self.assertEqual(result.returncode,0,result.stderr)
        report=json.loads(result.stdout)
        self.assertEqual(report['sqlite']['wal_fix'],'verified_source_id')
        self.assertEqual((report['sqlite']['journal_mode'],report['sqlite']['synchronous'],report['sqlite']['foreign_keys']),('wal',2,1))
        self.assertTrue(report['roots_distinct'])
        self.assertEqual(report['data_filesystem']['status'],'local_observed')
        self.assertEqual(report['resources']['memory_requested_bytes'],1024*1024*1024)
        self.assertFalse(report['recognition_resources']['enabled'])
        self.assertIsNone(report['recognition_resources']['memory_mb'])
        self.assertEqual(report['recognition_resources']['child_effective'], 'not_observed')
        if os.uname().sysname=='Darwin':
            self.assertEqual(report['resources']['memory_control'],'unsupported_unverified')
        self.assertNotIn(str(settings.BIRDS_TEST_ROOT),result.stdout)
        self.assertNotIn(settings.SECRET_KEY,result.stdout)
        self.assertEqual(json.loads(command('checkruntime').stdout)['parameters'],report['parameters'])

    def test_runtime_missing_and_identical_roots_fail_without_paths(self):
        import io,json
        from django.core.management import call_command
        from django.core.management.base import CommandError
        from django.test import override_settings
        for root in (str(self.data),str(self.data/'missing')):
            stream=io.StringIO()
            with override_settings(BIRDS_INBOX_ROOT=root),self.assertRaises(CommandError):
                call_command('checkruntime',stdout=stream)
            self.assertIn('roots_not_distinct',json.loads(stream.getvalue())['errors'])
            self.assertNotIn(root,stream.getvalue())

    def test_millisecond_values_preserve_small_positive_units(self):
        import json
        options=command_options('checkruntime')
        options['env'].update(SCAN_INTERVAL_MS='1',STABLE_WINDOW_MS='1',WORKER_JOB_SLEEP_MS='1',RETRY_BASE_MS='1')
        result=subprocess.run(**options,capture_output=True,text=True,timeout=4)
        self.assertEqual(result.returncode,0,result.stderr)
        report=json.loads(result.stdout)
        self.assertEqual(report['parameters']['SCAN_INTERVAL_MS'],1)
        self.assertEqual(report['parameters']['STABLE_WINDOW_MS'],1)


    def test_recover_running_task_after_previous_worker_exit(self):
        from archive.services.intake import scan_once
        source = self.inbox/'恢复.jpg'
        digest = jpeg(source)
        scan_once()
        Job.objects.update(state='running', stage='reading', attempts=1)
        result = command('runworker', '--once')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Photo.objects.count(), 1, '遗留running任务必须在获锁后恢复')
        self.assertEqual(Job.objects.get().state, 'succeeded')
        connection.close()
        with sqlite3.connect(settings.DATABASES['default']['NAME']) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM archive_photo').fetchone()[0], 1)
        self.assertContains(self.client.get('/review/'), '待确认')
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)

