"""Archive invariants checked through real HTTP, workers and independent file/DB reads."""
import hashlib
import os
import shutil
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from archive.models import ArchiveOperation, ArchiveCompletion, ManagedOriginal, Photo, Job, Species, SourceSnapshot
from archive.services.intake import scan_once
from archive.services.jobs import process_source
from archive.services.paths import open_root
from archive.tests.fixtures import jpeg, command, command_options, disk_hashes


class ArchiveFixture(TransactionTestCase):
    def setUp(self):
        self.root = Path(settings.BIRDS_TEST_ROOT)
        self.inbox, self.data = Path(settings.BIRDS_INBOX_ROOT), Path(settings.BIRDS_DATA_ROOT)
        self.archive, self.claims = self.root/'archive', self.root/'claims'
        for directory in (self.inbox, self.archive, self.claims):
            directory.mkdir(exist_ok=True)
            for child in directory.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        for name in ('fault-ready', 'fault-release'):
            (self.root/name).unlink(missing_ok=True)
        self.overrides = override_settings(BIRDS_ARCHIVE_ROOT=str(self.archive), BIRDS_CLAIM_ROOT=str(self.claims),
            BIRDS_HANDOFF_MODE='published', BIRDS_PUBLISHED_CONTRACT=True)
        self.overrides.enable(); self.addCleanup(self.overrides.disable)
        snapshot = SourceSnapshot.objects.create(source='synthetic', version='1', sha256='a'*64,
            acquired_at='2026-10-16', download_page_url='https://example.org/catalog',
            license_url='https://example.org/license', attribution='synthetic', row_count=2,
            species_count=2, manifest={})
        self.species = Species.objects.create(scientific_name='Ardea alba', scientific_key='ardea alba',
            chinese_name='白鹭', search_key='白鹭', source_snapshot=snapshot)

    def confirmed(self, name='one.jpg', color='red', species=None, capture=None):
        path = self.inbox/name; jpeg(path, color=color)
        scan_once()
        a, b = open_root(self.inbox), open_root(self.data)
        try:
            for job in Job.objects.filter(kind='import', state='pending'):
                self.assertTrue(process_source(job, a, b))
        finally:
            os.close(a); os.close(b)
        photo = Photo.objects.get(observations__source__relative_path=name)
        if capture:
            photo.manual_capture = capture
            photo.save(update_fields=['manual_capture'])
        response = self.client.post(f'/photos/{photo.pk}/', {'metadata_version': photo.metadata_version,
            'request_id': str(uuid.uuid4()), 'classification': 'species',
            'species_ids': [str(item.pk) for item in (species or [self.species])]})
        self.assertEqual(response.status_code, 303)
        return photo, path, ArchiveOperation.objects.get(photo=photo)

    def run_worker(self):
        call_command('runworker', '--once', verbosity=0)

    def start_paused(self, stage):
        for name in ('fault-ready', 'fault-release'):
            (self.root/name).unlink(missing_ok=True)
        options = command_options('runworker', '--once')
        options['env']['BIRDS_TEST_FAULT_STAGE'] = stage
        options['env'].update(BIRDS_ARCHIVE_ROOT=str(self.archive), BIRDS_CLAIM_ROOT=str(self.claims))
        process = subprocess.Popen(**options, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_process, process)
        deadline = time.monotonic()+10
        while not (self.root/'fault-ready').exists():
            if process.poll() is not None:
                self.fail('worker未到故障点：'+process.communicate(timeout=2)[1])
            if time.monotonic() >= deadline:
                self.fail('worker故障点超时：'+stage)
            time.sleep(.02)
        self.assertEqual((self.root/'fault-ready').read_text(), stage.split(',')[0])
        return process

    def subprocess_worker(self):
        options = command_options('runworker', '--once')
        options['env'].update(BIRDS_ARCHIVE_ROOT=str(self.archive), BIRDS_CLAIM_ROOT=str(self.claims))
        result = subprocess.run(**options, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=3)

    def assert_complete(self, photo, path, operation):
        photo.refresh_from_db(); operation.refresh_from_db(); operation.job.refresh_from_db()
        self.assertEqual((photo.state, operation.stage, operation.job.state), ('archived', 'complete', 'succeeded'))
        managed = ManagedOriginal.objects.get(photo=photo)
        target = self.archive/managed.relative_path
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), photo.sha256)
        self.assertEqual(target.stat().st_size, photo.byte_size)
        self.assertFalse(path.exists())
        self.assertFalse((self.claims/operation.claim_relative_path).exists())
        self.assertFalse((self.archive/operation.temporary_relative_path).exists())
        self.assertEqual(ArchiveCompletion.objects.filter(photo=photo).count(), 1)
        connection.close()
        with sqlite3.connect(settings.DATABASES['default']['NAME']) as db:
            self.assertEqual(db.execute('SELECT state FROM archive_photo WHERE id=?', (photo.pk.hex,)).fetchone()[0], 'archived')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM archive_managedoriginal WHERE photo_id=?', (photo.pk.hex,)).fetchone()[0], 1)


class ArchiveLifecycleTests(ArchiveFixture):
    def test_multi_species_one_primary_and_year(self):
        other = Species.objects.create(scientific_name='Ardea cinerea', scientific_key='ardea cinerea',
            chinese_name='苍鹭', search_key='苍鹭', source_snapshot=self.species.source_snapshot)
        photo, path, operation = self.confirmed(species=[self.species, other],
            capture={'local': '2025-12-31', 'precision': 'date', 'offset': None})
        self.run_worker(); self.assert_complete(photo, path, operation)
        self.assertEqual(ManagedOriginal.objects.get(photo=photo).relative_path, '多鸟种/2025/one.jpg')
        self.assertEqual(len(list(self.archive.rglob('*.jpg'))), 1)
        self.assertEqual(photo.confirmed_species.count(), 2)

    def test_confirm_worker_archive_hash(self):
        photo, path, operation = self.confirmed()
        self.run_worker()
        self.assert_complete(photo, path, operation)

    def test_copy_compatible_keeps_source(self):
        photo, path, operation = self.confirmed()
        before = path.read_bytes()
        with override_settings(BIRDS_HANDOFF_MODE='copy_compatible'):
            self.run_worker()
        operation.job.refresh_from_db()
        self.assertEqual(operation.job.state, 'blocked')
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(ArchiveCompletion.objects.exists())

    def test_single_managed_original(self):
        photo, path, operation = self.confirmed()
        for _ in range(3):
            self.run_worker()
        self.assert_complete(photo, path, operation)
        self.assertEqual((Photo.objects.count(), ManagedOriginal.objects.count()), (1, 1))


class ArchiveCrashTests(ArchiveFixture):
    def test_kill_after_exclusive_create_before_identity_keeps_unknown_and_next_job_runs(self):
        photo, path, operation = self.confirmed()
        original = path.read_bytes()
        options = command_options('runworker', '--once')
        options['args'] = [options['args'][0], '-c', '''
import os, signal, django
django.setup()
from archive.services import archiving
from django.core.management import call_command
save_context = archiving._context
def kill_before_identity(operation, **values):
    if 'temporary_identity' in values:
        os.kill(os.getpid(), signal.SIGKILL)
    return save_context(operation, **values)
archiving._context = kill_before_identity
call_command('runworker', '--once', verbosity=0)
''']
        killed = subprocess.run(**options, capture_output=True, text=True, timeout=10)
        self.assertEqual(killed.returncode, -9, killed.stderr)
        operation.refresh_from_db()
        self.assertEqual(operation.stage, 'protecting')
        self.assertNotIn('temporary_identity', operation.retry_context)
        unknown = self.archive/operation.temporary_relative_path
        self.assertTrue(unknown.is_file())
        unknown_stat, unknown_bytes = unknown.stat(), unknown.read_bytes()
        self.assertEqual(unknown_bytes, b'')
        healthy, healthy_path, healthy_op = self.confirmed('healthy.jpg', color='blue')
        # This worker is a new, unpatched process, including its startup recovery.
        self.subprocess_worker()
        self.subprocess_worker()
        operation.refresh_from_db(); operation.job.refresh_from_db(); photo.refresh_from_db()
        self.assertEqual((photo.state, operation.job.state, operation.job.error_code),
                         ('archive_failed', 'blocked', 'target_conflict'))
        self.assertEqual(operation.stage, 'protecting')
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(unknown.read_bytes(), unknown_bytes)
        self.assertEqual((unknown.stat().st_dev, unknown.stat().st_ino),
                         (unknown_stat.st_dev, unknown_stat.st_ino))
        self.assertFalse(ArchiveCompletion.objects.filter(photo=photo).exists())
        self.assertFalse(ManagedOriginal.objects.filter(photo=photo).exists())
        self.assert_complete(healthy, healthy_path, healthy_op)

    def test_each_side_effect_boundary(self):
        stages = ('before_protecting', 'protecting', 'partial_copy', 'after_hash', 'before_file_fsync',
                  'after_file_fsync', 'protected', 'before_claim', 'after_claim_rename', 'claimed',
                  'after_link', 'registered', 'after_temp_unlink', 'after_claim_unlink')
        for index, stage in enumerate(stages):
            with self.subTest(stage=stage):
                photo, path, operation = self.confirmed(stage+'.jpg', color=(index*15, 40, 90))
                foreign = self.archive/('foreign-'+stage+'.jpg')
                foreign.write_bytes(b'unowned-'+stage.encode())
                before = foreign.read_bytes()
                worker = self.start_paused('archive_'+stage)
                operation.refresh_from_db()
                self.assertNotEqual(operation.stage, 'complete')
                self.assertFalse(ArchiveCompletion.objects.filter(photo=photo).exists())
                # The durable protected copy must predate every destructive source change.
                if stage in ('before_claim', 'after_claim_rename', 'claimed', 'after_link', 'registered'):
                    protected = self.archive/operation.temporary_relative_path
                    self.assertEqual(hashlib.sha256(protected.read_bytes()).hexdigest(), photo.sha256)
                worker.kill(); worker.communicate(timeout=3)
                self.assertIsNotNone(worker.returncode)
                self.subprocess_worker(); self.subprocess_worker()
                self.assert_complete(photo, path, operation)
                self.assertEqual(foreign.read_bytes(), before)
                actual = disk_hashes(self.archive)
                managed = ManagedOriginal.objects.get(photo=photo)
                self.assertEqual([name for name, digest in actual.items() if digest == photo.sha256],
                                 [managed.relative_path])
                self.assertFalse(any(name.endswith('.partial') or '/.archive-' in name for name in actual))
                self.assertEqual(disk_hashes(self.claims), {})
                with sqlite3.connect(settings.DATABASES['default']['NAME']) as db:
                    self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM archive_archivecompletion WHERE photo_id=?',
                                                (photo.pk.hex,)).fetchone(), (1,))
                self.assertEqual(Photo.objects.count(), index+1)


class ArchiveInterleavingTests(ArchiveFixture):
    def test_recovery_with_inbox_unavailable(self):
        photo, path, operation = self.confirmed('offline.jpg')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        worker = self.start_paused('archive_claimed')
        worker.kill(); worker.communicate(timeout=3)
        retained = self.root/'offline-inbox-process'
        self.inbox.rename(retained)
        try:
            # settings_test creates missing fixture directories at import. Remove
            # only that empty test directory before worker startup to model absence,
            # rather than a different mounted root (which must correctly block).
            options = command_options('runworker', '--once')
            options['args'] = [options['args'][0], '-c',
                'import django; django.setup(); from django.conf import settings; '
                'from pathlib import Path; p=Path(settings.BIRDS_INBOX_ROOT); '
                'p.rmdir() if p.exists() else None; '
                'from django.core.management import call_command; '
                'call_command("runworker", "--once", verbosity=0)']
            for _ in range(2):
                result = subprocess.run(**options, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_complete(photo, path, operation)
            self.assertEqual(disk_hashes(retained), {})
            self.assertEqual(list(disk_hashes(self.archive).values()), [digest])
        finally:
            if self.inbox.exists():
                self.inbox.rmdir()
            retained.rename(self.inbox)

    def test_claim_recovery_does_not_require_available_inbox(self):
        photo, path, operation = self.confirmed()
        worker = self.start_paused('archive_claimed')
        worker.kill(); worker.communicate(timeout=3)
        retained = self.root/'inbox-offline'
        if retained.exists():
            shutil.rmtree(retained)
        self.inbox.rename(retained)
        # settings_test recreates its absent inbox root; use in-process settings so the
        # unmounted root is truly unavailable, without substituting a fresh identity.
        try:
            self.run_worker()
            self.assert_complete(photo, path, operation)
        finally:
            retained.rename(self.inbox)

    def test_source_replaced_between_open_and_claim(self):
        photo, path, operation = self.confirmed()
        replacement = self.inbox/'replacement.partial'; new_hash = jpeg(replacement, color='blue')
        reached = []
        def replace(stage):
            reached.append(stage)
            if stage == 'archive_before_claim':
                os.replace(replacement, path)
        with override_settings(BIRDS_TEST_FAULT_CALLBACK=replace):
            self.run_worker()
        operation.refresh_from_db(); operation.job.refresh_from_db()
        self.assertIn('archive_before_claim', reached)
        self.assertEqual(operation.job.state, 'blocked')
        self.assertEqual(hashlib.sha256((self.archive/operation.temporary_relative_path).read_bytes()).hexdigest(), photo.sha256)
        self.assertEqual(hashlib.sha256((self.claims/operation.claim_relative_path).read_bytes()).hexdigest(), new_hash)
        self.assertFalse(ArchiveCompletion.objects.exists())

    def test_kill_after_wrong_claim_keeps_original_copy(self):
        photo, path, operation = self.confirmed()
        worker = self.start_paused('archive_before_claim,archive_after_claim_rename')
        replacement = self.inbox/'new.partial'; new_hash = jpeg(replacement, color='blue')
        os.replace(replacement, path)
        (self.root/'fault-ready').unlink()
        (self.root/'fault-release').write_text('continue')
        deadline = time.monotonic()+5
        while not (self.root/'fault-ready').exists():
            self.assertIsNone(worker.poll())
            if time.monotonic()>deadline:
                self.fail('wrong claim故障点超时')
            time.sleep(.02)
        self.assertEqual((self.root/'fault-ready').read_text(), 'archive_after_claim_rename')
        worker.kill(); worker.communicate(timeout=3)
        self.subprocess_worker(); self.subprocess_worker()
        operation.refresh_from_db(); operation.job.refresh_from_db()
        self.assertEqual(operation.job.state, 'blocked')
        self.assertEqual(hashlib.sha256((self.archive/operation.temporary_relative_path).read_bytes()).hexdigest(), photo.sha256)
        self.assertEqual(hashlib.sha256((self.claims/operation.claim_relative_path).read_bytes()).hexdigest(), new_hash)
        self.assertFalse(ArchiveCompletion.objects.exists())

    def test_bind_empty_configuration_once(self):
        photo, path, operation = self.confirmed()
        with override_settings(BIRDS_ARCHIVE_ROOT='', BIRDS_CLAIM_ROOT=''):
            self.run_worker()
        operation.refresh_from_db()
        self.assertEqual(operation.roots_snapshot, {'binding_state': 'unbound'})
        self.assertTrue(path.exists())
        self.run_worker()
        self.assert_complete(photo, path, operation)
        self.assertEqual(operation.roots_snapshot['binding_state'], 'bound')

    def test_bound_roots_never_rebind(self):
        _, path, operation = self.confirmed()
        worker = self.start_paused('archive_before_claim')
        worker.kill(); worker.communicate(timeout=3)
        operation.refresh_from_db(); snapshot = operation.roots_snapshot
        other = self.root/'other-archive'; other.mkdir(exist_ok=True)
        with override_settings(BIRDS_ARCHIVE_ROOT=str(other)):
            self.run_worker()
        operation.refresh_from_db(); operation.job.refresh_from_db()
        self.assertEqual(operation.roots_snapshot, snapshot)
        self.assertEqual(operation.job.error_code, 'root_changed')
        self.assertTrue(path.exists())
        self.run_worker()
        self.assert_complete(operation.photo, path, operation)


class ArchiveFaultTests(ArchiveFixture):
    def test_root_replacement(self):
        photo, path, operation = self.confirmed()
        worker = self.start_paused('archive_claimed')
        worker.kill(); worker.communicate(timeout=3)
        operation.refresh_from_db(); snapshot = operation.roots_snapshot
        old = self.root/'retained-archive'
        if old.exists():
            shutil.rmtree(old)
        self.archive.rename(old); self.archive.mkdir()
        try:
            self.subprocess_worker()
            operation.refresh_from_db(); operation.job.refresh_from_db()
            self.assertEqual(operation.job.error_code, 'root_changed')
            self.assertEqual(operation.roots_snapshot, snapshot)
            self.assertEqual(hashlib.sha256((self.claims/operation.claim_relative_path).read_bytes()).hexdigest(), photo.sha256)
            self.assertFalse(ArchiveCompletion.objects.exists())
        finally:
            self.archive.rmdir(); old.rename(self.archive)
        self.subprocess_worker(); self.assert_complete(photo, path, operation)

    def test_space_permission_fsync_preserve_claim(self):
        import errno
        for index, number in enumerate((errno.ENOSPC, errno.EDQUOT, errno.EACCES, errno.EIO)):
            with self.subTest(errno=number):
                photo, path, operation = self.confirmed(str(number)+'.jpg', color=(index*50, 30, 70))
                worker = self.start_paused('archive_claimed')
                worker.kill(); worker.communicate(timeout=3)
                if number == errno.EIO:
                    original = os.fsync
                    archive_directories = {self.archive.stat().st_ino, *[
                        item.stat().st_ino for item in self.archive.rglob('*') if item.is_dir()]}
                    def fsync(fd):
                        if stat_is_directory(fd) and os.fstat(fd).st_ino in archive_directories:
                            raise OSError(number, 'injected directory fsync failure')
                        return original(fd)
                    patcher = mock.patch('archive.services.archiving.os.fsync', side_effect=fsync)
                else:
                    patcher = mock.patch('archive.services.archiving.os.link', side_effect=OSError(number, 'injected'))
                with patcher:
                    self.run_worker()
                operation.refresh_from_db(); operation.job.refresh_from_db()
                self.assertEqual(operation.job.state, 'failed')
                self.assertEqual(hashlib.sha256((self.claims/operation.claim_relative_path).read_bytes()).hexdigest(), photo.sha256)
                self.assertFalse(ArchiveCompletion.objects.filter(photo=photo).exists())
                self.assertNotIn(str(self.root), operation.job.safe_message)
                Job.objects.filter(pk=operation.job_id).update(state='queued')
                self.subprocess_worker(); self.assert_complete(photo, path, operation)

    def test_two_worker_lock(self):
        photo, path, operation = self.confirmed()
        first = self.start_paused('archive_claimed')
        inode = (self.data/'worker.lock').stat().st_ino
        options = command_options('runworker', '--once')
        options['env'].update(BIRDS_ARCHIVE_ROOT=str(self.archive), BIRDS_CLAIM_ROOT=str(self.claims))
        second = subprocess.run(**options, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('worker_busy', second.stderr)
        self.assertEqual((self.data/'worker.lock').stat().st_ino, inode)
        self.assertFalse(ArchiveCompletion.objects.exists())
        first.kill(); first.communicate(timeout=3)
        self.subprocess_worker(); self.assert_complete(photo, path, operation)

    def test_replaced_published_target_is_not_adopted_or_deleted(self):
        photo, _, operation = self.confirmed()
        worker = self.start_paused('archive_registered')
        worker.kill(); worker.communicate(timeout=3)
        operation.refresh_from_db()
        target = self.archive/operation.target_relative_path
        foreign = target.with_name('new.partial'); jpeg(foreign, color='blue')
        before = foreign.read_bytes(); os.replace(foreign, target)
        self.subprocess_worker(); self.subprocess_worker()
        operation.refresh_from_db(); operation.job.refresh_from_db()
        self.assertEqual(operation.job.state, 'blocked')
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(hashlib.sha256((self.claims/operation.claim_relative_path).read_bytes()).hexdigest(), photo.sha256)
        self.assertFalse(ArchiveCompletion.objects.exists())


def stat_is_directory(fd):
    import stat
    return stat.S_ISDIR(os.fstat(fd).st_mode)


class ArchiveIdentityTests(SimpleTestCase):
    def test_inode_identity_requires_two_valid_structures(self):
        from archive.services.archiving import _same_inode
        valid = {'dev': 1, 'ino': 2}
        self.assertTrue(_same_inode(valid, {**valid, 'size': 3}))
        invalid = (None, {}, [], 'unknown', {'dev': 1}, {'ino': 2},
                   {'dev': None, 'ino': None}, {'dev': True, 'ino': 2},
                   {'dev': 1.0, 'ino': 2}, {'dev': 1, 'ino': -1})
        for value in invalid:
            for first, second in ((valid, value), (value, valid), (value, value)):
                with self.subTest(first=first, second=second):
                    self.assertFalse(_same_inode(first, second))
