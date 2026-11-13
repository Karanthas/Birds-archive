import hashlib
import os
import shutil
import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from archive.models import ImportSource, Job, Photo
from archive.services.intake import scan_once
from archive.services.jobs import process_source
from archive.services.paths import open_root, read_scan_status
from archive.tests.fixtures import command_options, jpeg


class IntakeTests(TransactionTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="birds-intake-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.inbox = self.root / "inbox"
        self.data = self.root / "data"
        self.inbox.mkdir()
        self.data.mkdir()
        self.overrides = override_settings(BIRDS_INBOX_ROOT=str(self.inbox), BIRDS_DATA_ROOT=str(self.data),
            BIRDS_HANDOFF_MODE="copy_compatible", BIRDS_STABILITY_SECONDS=30,
            BIRDS_PUBLISHED_CONTRACT=False)
        self.overrides.enable()
        self.addCleanup(self.overrides.disable)

    def scan_at(self, seconds):
        with mock.patch("django.utils.timezone.now", return_value=self.start + timedelta(seconds=seconds)):
            return scan_once()

    def test_copy_waits_for_stable_observations_and_preserves_input(self):
        self.start = timezone.now()
        digest = jpeg(self.inbox / "普通复制.JPG")
        self.scan_at(0)
        source = ImportSource.objects.get(relative_path="普通复制.JPG")
        self.assertEqual(source.state, "waiting", "普通复制首扫必须等待写入稳定")
        self.assertFalse(Job.objects.filter(state="pending").exists())
        self.scan_at(20)
        self.assertFalse(Job.objects.filter(state="pending").exists())
        self.scan_at(31)
        self.assertEqual(Job.objects.filter(state="pending").count(), 1)
        self.assertEqual(hashlib.sha256((self.inbox / "普通复制.JPG").read_bytes()).hexdigest(), digest)

    def test_partial_ignored_recursive_sorted_discovery_and_empty_root(self):
        self.assertEqual(scan_once(), 0)
        jpeg(self.inbox / "ignored.jpg.partial")
        jpeg(self.inbox / "z/photo.JPEG")
        jpeg(self.inbox / "a/photo.jpg")
        scan_once()
        self.assertEqual(list(ImportSource.objects.order_by("created_at", "id").values_list("relative_path", flat=True)),
                         ["a/photo.jpg", "z/photo.JPEG"])

    def test_missing_permission_and_replaced_root_pause_without_clearing_records(self):
        jpeg(self.inbox / "keep.jpg")
        scan_once()
        self.inbox.rename(self.root / "saved")
        self.assertEqual(scan_once(), 0)
        self.assertEqual(ImportSource.objects.count(), 1)
        self.assertEqual(Photo.objects.count(), 0)
        self.inbox.mkdir()
        jpeg(self.inbox / "foreign.jpg")
        self.assertEqual(scan_once(), 0)
        self.assertFalse(ImportSource.objects.filter(relative_path="foreign.jpg").exists())
        self.assertIn("扫描已暂停", read_scan_status())

    def test_permission_error_is_safe_diagnostic_and_keeps_database(self):
        jpeg(self.inbox / "keep.jpg")
        scan_once()
        with mock.patch("archive.services.intake.jpeg_paths", side_effect=PermissionError("secret-token /private/nas")):
            self.assertEqual(scan_once(), 0)
        self.assertEqual(ImportSource.objects.count(), 1)
        self.assertIn("权限", read_scan_status())
        self.assertNotIn("secret-token", read_scan_status())

    def test_paused_copy_resets_window_when_writing_resumes(self):
        self.start = timezone.now()
        path = self.inbox / "resume.jpg"
        jpeg(path)
        self.scan_at(0)
        self.scan_at(20)
        with path.open("ab") as stream:
            stream.write(b"continued")
        self.scan_at(25)
        self.scan_at(31)
        self.assertFalse(Job.objects.filter(state="pending").exists())
        self.scan_at(56)
        self.assertEqual(Job.objects.filter(state="pending").count(), 1)

    def test_replacement_while_building_never_publishes_old_result(self):
        path = self.inbox / "changing.jpg"
        jpeg(path)
        with override_settings(BIRDS_HANDOFF_MODE="published", BIRDS_PUBLISHED_CONTRACT=True):
            scan_once()
        inbox_fd, data_fd = open_root(self.inbox), open_root(self.data)
        try:
            from archive.services.images import write_derivatives
            def replaced(*args):
                result = write_derivatives(*args)
                jpeg(self.inbox / "replacement.partial", color=(10, 60, 30))
                os.replace(self.inbox / "replacement.partial", path)
                return result
            with mock.patch("archive.services.jobs.write_derivatives", side_effect=replaced):
                self.assertFalse(process_source(Job.objects.get(), inbox_fd, data_fd))
            self.assertEqual(Photo.objects.count(), 0)
            self.assertEqual(Job.objects.get().error_code, "source_changed")
        finally:
            os.close(inbox_fd)
            os.close(data_fd)

    def test_published_contract_required_and_closed_input_imports(self):
        jpeg(self.inbox / "published.jpg")
        with override_settings(BIRDS_HANDOFF_MODE="published", BIRDS_PUBLISHED_CONTRACT=False):
            self.assertEqual(scan_once(), 0)
            self.assertEqual(ImportSource.objects.count(), 0)
        with override_settings(BIRDS_HANDOFF_MODE="published", BIRDS_PUBLISHED_CONTRACT=True):
            self.assertEqual(scan_once(), 1)
            self.assertEqual(Job.objects.filter(state="pending").count(), 1)

    def test_real_worker_copy_window_then_private_workbench(self):
        import subprocess
        source_path = Path(settings.BIRDS_TEST_ROOT) / "inbox/copy-closed.jpg"
        # 子进程settings_test的根固定为已验证临时根，不继承本测试override根。
        for item in source_path.parent.iterdir():
            if item.is_file() or item.is_symlink():
                item.unlink()
        digest = jpeg(source_path)
        options = command_options("runworker", "--once")
        options["env"].update(BIRDS_HANDOFF_MODE="copy_compatible", BIRDS_STABILITY_SECONDS="0")
        for _ in range(2):
            result = subprocess.run(**options, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Photo.objects.count(), 1)
        owner = User.objects.create_superuser(settings.BIRDS_OWNER_USERNAME, password="copy-pass")
        self.client.force_login(owner)
        self.assertContains(self.client.get("/review/"), "copy-closed.jpg")
        self.assertEqual(hashlib.sha256(source_path.read_bytes()).hexdigest(), digest)

    def process_all(self):
        inbox_fd, data_fd = open_root(self.inbox), open_root(self.data)
        try:
            for job in Job.objects.filter(state="pending").select_related("source"):
                process_source(job, inbox_fd, data_fd)
        finally:
            os.close(inbox_fd)
            os.close(data_fd)

    def published_scan(self):
        with override_settings(BIRDS_HANDOFF_MODE="published", BIRDS_PUBLISHED_CONTRACT=True):
            return scan_once()

    def test_duplicate_history_persists_and_x_to_y_keeps_old_photo(self):
        x = self.inbox / "a/same.jpg"
        y = self.inbox / "b/same.jpg"
        digest = jpeg(x)
        y.parent.mkdir()
        shutil.copyfile(x, y)
        self.published_scan()
        self.process_all()
        self.assertEqual(Photo.objects.count(), 1)
        old = Photo.objects.get()
        self.assertEqual(ImportSource.objects.filter(state="duplicate").count(), 1)
        self.assertTrue(hasattr(ImportSource.objects.get(relative_path="a/same.jpg"), "current_observation"),
                        "每个来源必须指向持久来源版本，而不是仅改当前字段")
        from archive.models import SourceObservation
        self.assertEqual(SourceObservation.objects.count(), 2)
        duplicate = SourceObservation.objects.get(duplicate=True)
        first_duplicate_at = duplicate.duplicate_first_seen
        old.state = "human_kept"
        old.save(update_fields=["state"])
        self.published_scan()
        self.process_all()
        connection.close()
        with sqlite3.connect(settings.DATABASES["default"]["NAME"]) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM archive_sourceobservation").fetchone()[0], 2)
        duplicate.refresh_from_db()
        self.assertEqual(duplicate.duplicate_first_seen, first_duplicate_at)
        jpeg(x, color=(30, 70, 100))
        self.published_scan()
        self.process_all()
        self.assertEqual(Photo.objects.count(), 2)
        self.assertEqual(SourceObservation.objects.count(), 3)
        old.refresh_from_db()
        self.assertEqual((old.sha256, old.state, old.original_available), (digest, "human_kept", True))
        self.assertEqual(ImportSource.objects.get(relative_path="b/same.jpg").photo_id, old.id)
        self.assertNotEqual(ImportSource.objects.get(relative_path="a/same.jpg").photo_id, old.id)

    def test_equal_length_retained_mtime_overwrite_is_rehashed(self):
        path = self.inbox / "overwritten.jpg"
        jpeg(path)
        with path.open("ab") as stream:
            stream.write(b"AAAA")
        self.published_scan()
        self.process_all()
        old = Photo.objects.get()
        info = path.stat()
        payload = bytearray(path.read_bytes())
        # JPEG结束后的等长字节变化保留可解码像素，同时改变内容身份。
        payload[-1] ^= 1
        path.write_bytes(payload)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        original_fingerprint = ImportSource.objects.get().fingerprint
        from archive.services.paths import fingerprint as real_fingerprint
        # 即使挂载返回完全相同stat也须重哈希，不能靠ctime的偶然变化通过。
        with mock.patch("archive.services.intake.fingerprint", return_value=original_fingerprint):
            self.published_scan()
        current = ImportSource.objects.get()
        self.assertEqual(current.observed_version, 2)
        # 恢复真实指纹供worker进行同FD复核。
        observed = real_fingerprint(path.stat())
        ImportSource.objects.filter(pk=current.pk).update(fingerprint=observed)
        current.current_observation.__class__.objects.filter(pk=current.current_observation_id).update(fingerprint=observed)
        self.process_all()
        self.assertEqual(Photo.objects.count(), 2)
        old.refresh_from_db()
        self.assertFalse(old.original_available)

    def test_stale_worker_cannot_commit_after_new_observation(self):
        path = self.inbox / "stale.jpg"
        jpeg(path)
        self.published_scan()
        old_job = Job.objects.get()
        jpeg(path, color=(90, 30, 10))
        self.published_scan()
        inbox_fd, data_fd = open_root(self.inbox), open_root(self.data)
        try:
            self.assertFalse(process_source(old_job, inbox_fd, data_fd))
        finally:
            os.close(inbox_fd)
            os.close(data_fd)
        old_job.refresh_from_db()
        self.assertEqual(old_job.state, "stale")
        self.assertEqual(Photo.objects.count(), 0)
        self.process_all()
        self.assertEqual(Photo.objects.count(), 1)

    def test_running_worker_cannot_publish_after_rescan_replaces_current_version(self):
        from archive.services.images import write_derivatives
        path = self.inbox / "running.jpg"
        jpeg(path)
        self.published_scan()
        job = Job.objects.get()
        def changed(*args):
            result = write_derivatives(*args)
            replacement = self.inbox / "new.partial"
            jpeg(replacement, color=(40, 90, 20))
            os.replace(replacement, path)
            self.published_scan()
            return result
        inbox_fd, data_fd = open_root(self.inbox), open_root(self.data)
        try:
            with mock.patch("archive.services.jobs.write_derivatives", side_effect=changed):
                self.assertFalse(process_source(job, inbox_fd, data_fd))
        finally:
            os.close(inbox_fd)
            os.close(data_fd)
        source = ImportSource.objects.get()
        self.assertEqual((source.observed_version, source.state), (2, "pending"))
        self.assertEqual(Photo.objects.count(), 0)
        self.process_all()
        self.assertEqual(Photo.objects.count(), 1)

    def test_duplicate_reconnect_rescan_preserves_jobs_and_source_bytes(self):
        from archive.models import SourceObservation
        jpeg(self.inbox / "one.jpg")
        shutil.copyfile(self.inbox / "one.jpg", self.inbox / "two.jpg")
        originals = {p.name: p.read_bytes() for p in self.inbox.iterdir()}
        self.published_scan()
        self.process_all()
        identifiers = set(Job.objects.values_list("id", flat=True))
        connection.close()
        self.published_scan()
        self.process_all()
        self.assertEqual(set(Job.objects.values_list("id", flat=True)), identifiers)
        self.assertEqual((Photo.objects.count(), SourceObservation.objects.count()), (1, 2))
        self.assertEqual({p.name: p.read_bytes() for p in self.inbox.iterdir()}, originals)

    def test_unreadable_subdirectory_keeps_siblings_and_does_not_clear_library(self):
        jpeg(self.inbox / "a/private.jpg")
        jpeg(self.inbox / "b/accessible.jpg", color=(70, 20, 10))
        self.published_scan()
        self.process_all()
        kept = ImportSource.objects.get(relative_path="b/accessible.jpg").photo
        real_open = os.open
        def deny_a(path, flags, *args, **kwargs):
            if path == "a":
                raise PermissionError("private-path credential-sentinel")
            return real_open(path, flags, *args, **kwargs)
        with mock.patch("os.open", side_effect=deny_a):
            self.published_scan()
        self.assertEqual(Photo.objects.count(), 2)
        kept.refresh_from_db()
        self.assertTrue(kept.original_available)
        inaccessible = ImportSource.objects.get(relative_path="a/private.jpg").photo
        inaccessible.refresh_from_db()
        self.assertFalse(inaccessible.original_available)
        self.assertIn("部分目录无法读取", read_scan_status())
        self.assertNotIn("sentinel", read_scan_status())

    def test_all_sources_disappeared_retains_photo_but_original_unavailable(self):
        path = self.inbox / "gone.jpg"
        digest = jpeg(path)
        self.published_scan()
        self.process_all()
        photo = Photo.objects.get()
        path.unlink()
        self.published_scan()
        photo.refresh_from_db()
        self.assertFalse(photo.original_available)
        self.assertEqual(photo.sha256, digest)
        self.assertTrue(photo.preview_key)

    def test_migration_backfills_version_one_on_real_disk_and_reconnect(self):
        import subprocess
        with tempfile.TemporaryDirectory(prefix="birds-tests-") as temporary:
            root = Path(temporary)
            (root / ".birds-test-root").write_text("birds isolated v1")
            def run(*args):
                options = command_options(*args)
                options["env"]["BIRDS_TEST_ROOT"] = str(root)
                result = subprocess.run(**options, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
            run("migrate", "archive", "0001", "--noinput")
            run("shell", "-c", "from django.db import connection; from django.db.migrations.executor import MigrationExecutor; "
                "apps=MigrationExecutor(connection).loader.project_state([('archive','0001_initial')]).apps; "
                "p=apps.get_model('archive','Photo').objects.create(sha256='a'*64,byte_size=10,preview_key='cache.jpg'); "
                "s=apps.get_model('archive','ImportSource').objects.create(relative_path='legacy.jpg',sha256='a'*64,"
                "fingerprint={'size':10},photo=p,state='duplicate'); "
                "apps.get_model('archive','Job').objects.create(source=s,source_version=1,state='succeeded')")
            run("migrate", "--noinput")
            with sqlite3.connect(root / "data/test.sqlite3") as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM archive_sourceobservation").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT version,duplicate,available FROM archive_sourceobservation").fetchone(), (1, 1, 1))
                self.assertEqual(db.execute("SELECT original_available FROM archive_photo").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT expected_sha256 FROM archive_job").fetchone()[0], "a" * 64)
            with sqlite3.connect(root / "data/test.sqlite3") as db:
                self.assertEqual(db.execute("SELECT o.id=s.current_observation_id FROM archive_sourceobservation o JOIN archive_importsource s ON s.id=o.source_id").fetchone()[0], 1)
