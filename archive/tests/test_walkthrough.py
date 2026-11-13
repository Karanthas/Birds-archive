import hashlib
import io
import sqlite3
import fcntl
import os
import subprocess
import tempfile
import time
from unittest import mock
from pathlib import Path
from PIL import Image
from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection
from django.test import TransactionTestCase
from archive.models import Photo, ImportSource, Job
from archive.tests.fixtures import command, command_options, jpeg


class WorkbenchSourceTests(TransactionTestCase):
    def setUp(self):
        from django.test import override_settings
        self.temporary = tempfile.TemporaryDirectory(prefix="birds-workbench-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.inbox = self.root / "inbox"
        self.data = self.root / "data"
        self.inbox.mkdir()
        self.data.mkdir()
        self.overrides = override_settings(BIRDS_INBOX_ROOT=str(self.inbox), BIRDS_DATA_ROOT=str(self.data),
            BIRDS_HANDOFF_MODE="published", BIRDS_PUBLISHED_CONTRACT=True)
        self.overrides.enable()
        self.addCleanup(self.overrides.disable)

    def scan_process(self):
        from archive.services.intake import scan_once
        from archive.services.jobs import process_source
        from archive.services.paths import open_root
        scan_once()
        inbox_fd, data_fd = open_root(self.inbox), open_root(self.data)
        try:
            for job in Job.objects.filter(state="pending").select_related("source"):
                process_source(job, inbox_fd, data_fd)
        finally:
            os.close(inbox_fd)
            os.close(data_fd)

    def test_unique_photo_and_duplicate_counts_and_selection(self):
        import shutil
        jpeg(self.inbox / "first.jpg")
        shutil.copyfile(self.inbox / "first.jpg", self.inbox / "second.jpg")
        self.scan_process()
        response = self.client.get("/review/")
        self.assertContains(response, "重复来源 1 项")
        self.assertContains(response, "照片 1 张")
        self.assertContains(response, 'name="photo_ids"', count=1)
        duplicate_response = self.client.get("/review/?filter=duplicate")
        self.assertNotContains(duplicate_response, 'name="photo_ids"')
        self.assertContains(duplicate_response, f'/photos/{Photo.objects.get().id}/')
        self.assertContains(duplicate_response, "导入文件已保留")
        self.scan_process()
        self.assertContains(self.client.get("/review/"), "重复来源 1 项")

    def test_history_detail_and_current_version_do_not_mix_old_job(self):
        path = self.inbox / "<bird>.jpg"
        jpeg(path)
        self.scan_process()
        old = Photo.objects.get()
        jpeg(path, color=(80, 10, 40))
        self.scan_process()
        self.assertContains(self.client.get("/review/"), "照片 2 张")
        response = self.client.get(f"/photos/{old.pk}/")
        self.assertContains(response, "原图当前不可用")
        self.assertContains(response, "历史来源")
        self.assertContains(response, "&lt;bird&gt;.jpg")
        self.assertNotContains(response, str(self.root))
        new = Photo.objects.exclude(pk=old.pk).get()
        response = self.client.get(f"/photos/{new.pk}/")
        self.assertNotContains(response, "原图当前不可用")
        self.assertEqual(list(response.context["jobs"].values_list("source_version", flat=True)), [2])

    def test_waiting_and_root_diagnostic_are_visible_without_selection(self):
        from archive.services.intake import scan_once
        from django.test import override_settings
        jpeg(self.inbox / "waiting.jpg")
        with override_settings(BIRDS_HANDOFF_MODE="copy_compatible"):
            scan_once()
        response = self.client.get("/review/?filter=pending")
        self.assertContains(response, "等待写完")
        self.assertNotContains(response, 'name="photo_ids"')
        self.inbox.rename(self.root / "saved")
        scan_once()
        response = self.client.get("/review/")
        self.assertContains(response, "导入目录不可用")
        self.assertNotContains(response, str(self.root))

    def test_root_loss_marks_preview_as_original_unavailable_on_both_pages(self):
        from archive.services.intake import scan_once
        jpeg(self.inbox / "root-loss.jpg")
        self.scan_process()
        photo = Photo.objects.get()
        self.inbox.rename(self.root / "saved")
        scan_once()
        self.assertContains(self.client.get("/review/"), "原图当前不可用")
        self.assertContains(self.client.get(f"/photos/{photo.pk}/"), "原图当前不可用")
        self.assertEqual(Photo.objects.count(), 1)

    def test_stable_24_item_pagination_handles_invalid_and_overflow_pages(self):
        for number in range(25):
            Photo.objects.create(sha256=f"{number:064x}", byte_size=100, preview_key="")
        first = self.client.get("/review/")
        self.assertContains(first, 'name="photo_ids"', count=24)
        self.assertContains(first, "?filter=all&amp;page=2")
        second = self.client.get("/review/?filter=all&page=2")
        self.assertContains(second, 'name="photo_ids"', count=1)
        for page in ("not-a-number", "999", "-1"):
            self.assertEqual(self.client.get(f"/review/?page={page}").status_code, 200)
        self.assertContains(first, "暂无预览")

    def test_live_summary_and_page_version_guard_keep_selection_honest(self):
        jpeg(self.inbox / 'version.jpg')
        self.scan_process()
        photo = Photo.objects.get()
        response = self.client.get('/review/')
        self.assertContains(response, 'data-summary-poll=')
        token = response.context['page_snapshot']
        Photo.objects.filter(pk=photo.pk).update(metadata_version=2, user_location='另一标签页')
        draft = self.client.get('/metadata/location/', {'photo_ids':str(photo.pk),
            'location':'保留草稿', 'filter':'all', 'page':1, 'page_snapshot':token})
        self.assertEqual(draft.status_code, 409)
        self.assertContains(draft, '保留草稿', status_code=409)
        photo.refresh_from_db()
        self.assertEqual(photo.user_location, '另一标签页')
        before = list(Job.objects.values_list('pk', 'state', 'attempts'))
        summary = self.client.get('/tasks/status/?workbench_filter=all&workbench_page=1')
        self.assertContains(summary, 'data-workbench-summary')
        self.assertEqual(list(Job.objects.values_list('pk', 'state', 'attempts')), before)

    def test_status_filters_use_current_jobs_and_keep_old_urls(self):
        Photo.objects.create(sha256='a'*64,byte_size=100,preview_key='')
        sources={}
        for state in ('queued','running','failed','retry_wait'):
            source=ImportSource.objects.create(relative_path=state+'.jpg',state='pending')
            Job.objects.create(source=source,source_version=1,state=state)
            sources[state]=source.pk
        for name, expected in [('pending',{'queued','retry_wait'}),('running',{'running'}),('failed',{'failed'})]:
            response=self.client.get('/review/?filter='+name)
            ids={row['source'].pk for row in response.context['page_obj'] if row['kind']=='source'}
            self.assertEqual(ids,{sources[key] for key in expected})
            self.assertNotContains(response,'name="photo_ids"')
        review=self.client.get('/review/?filter=review')
        self.assertContains(review,'name="photo_ids"',count=1)
        self.assertEqual(self.client.get('/review/?filter=duplicate').context['filter_name'],'duplicate')
        self.assertEqual(self.client.get('/review/?filter=not-valid').context['filter_name'],'all')


class WalkingSkeletonTests(TransactionTestCase):
    def setUp(self):
        for item in Path(settings.BIRDS_INBOX_ROOT).iterdir():
            if item.is_file() or item.is_symlink():
                item.unlink()

    def test_visible_quiet_control_border_has_accessible_contrast(self):
        import re
        css=(settings.BASE_DIR/'archive/static/archive/app.css').read_text()
        tokens=dict(re.findall(r'--([\w-]+)\s*:\s*(#[0-9A-Fa-f]{6})',css))
        border=re.search(r'\.quiet\s*\{[^}]*border-color\s*:\s*var\(--([\w-]+)\)',css).group(1)
        def luminance(color):
            values=[int(color[i:i+2],16)/255 for i in (1,3,5)]
            values=[v/12.92 if v<=0.04045 else ((v+0.055)/1.055)**2.4 for v in values]
            return sum(v*w for v,w in zip(values,(0.2126,0.7152,0.0722)))
        foreground=luminance(tokens[border]);background=luminance(tokens['paper'])
        self.assertGreaterEqual((background+0.05)/(foreground+0.05),3,
            '描边按钮必须与纸白背景具有至少3:1可见边界对比')

    def test_worker_persists_photo_and_private_preview_without_changing_source(self):
        source = Path(settings.BIRDS_INBOX_ROOT) / "合成照片.jpg"
        digest = jpeg(source)
        result = command("runworker", "--once")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Photo.objects.count(), 1, "真实 worker 应持久化首张照片")
        photo = Photo.objects.get()
        imported = ImportSource.objects.get()
        job = Job.objects.get()
        self.assertEqual((photo.sha256, photo.byte_size), (digest, source.stat().st_size))
        self.assertEqual((imported.photo_id, imported.state, job.state), (photo.id, "ready", "succeeded"))
        self.assertEqual(photo.state, "review_pending")
        connection.close()
        with sqlite3.connect(settings.DATABASES["default"]["NAME"]) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM archive_photo").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM archive_importsource").fetchone()[0], 1)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
        media = f"/media/{photo.id}/preview/"
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(self.client.get(media).status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertContains(self.client.get(f"/photos/{photo.id}/"), "合成照片.jpg")
        response = self.client.get(media)
        self.assertEqual(response.status_code, 200)
        payload = b"".join(response.streaming_content)
        with Image.open(io.BytesIO(payload)) as image:
            self.assertEqual(image.format, "JPEG")
            image.load()
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(self.client.post("/logout/").status_code, 404)
        self.assertEqual(self.client.get(media).status_code, 200)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)

    def test_runtime_database_is_disk_wal_full_with_foreign_keys(self):
        self.assertNotIn(":memory:", str(settings.DATABASES["default"]["NAME"]))
        with connection.cursor() as cursor:
            self.assertEqual(cursor.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(cursor.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(cursor.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_empty_test_suite_is_rejected(self):
        from unittest import mock, TestSuite
        from archive.tests.runner import NonEmptyDiscoverRunner
        with mock.patch("django.test.runner.DiscoverRunner.build_suite", return_value=TestSuite()):
            with self.assertRaisesRegex(RuntimeError, "测试集为空"):
                NonEmptyDiscoverRunner(verbosity=0).build_suite()

    def test_oversized_bytes_rejected_before_decoder(self):
        from archive.services.images import decode_jpeg, MAX_BYTES, ImageRejected
        with tempfile.TemporaryFile() as source:
            source.truncate(MAX_BYTES + 1)
            with mock.patch("archive.services.images.Image.open", side_effect=AssertionError("不应进入解码")):
                with self.assertRaises(ImageRejected) as error:
                    decode_jpeg(source)
            self.assertEqual(error.exception.code, "image_bytes_exceeded")

    def test_oversized_pixels_rejected_before_full_load(self):
        from archive.services.images import decode_jpeg, ImageRejected
        source = Path(settings.BIRDS_INBOX_ROOT) / "大图.jpg"
        jpeg(source)
        payload = bytearray(source.read_bytes())
        marker = payload.index(b"\xff\xc0")
        payload[marker + 5:marker + 9] = (6500).to_bytes(2, "big") * 2
        source.write_bytes(payload)
        with source.open("rb") as stream, mock.patch.object(Image.Image, "load", side_effect=AssertionError("不应加载像素")):
            with self.assertRaises(ImageRejected) as error:
                decode_jpeg(stream)
        self.assertEqual(error.exception.code, "image_pixels_exceeded")

    def test_non_jpeg_and_truncated_input_fail_safely_without_source_changes(self):
        for kind in ("png", "truncated"):
            with self.subTest(kind=kind):
                source = Path(settings.BIRDS_INBOX_ROOT) / f"{kind}.jpg"
                if kind == "png":
                    Image.new("RGB", (30, 20)).save(source, "PNG")
                else:
                    jpeg(source)
                    source.write_bytes(source.read_bytes()[:-20])
                before = source.read_bytes()
                result = command("runworker", "--once")
                self.assertEqual(result.returncode, 0, result.stderr)
                job = Job.objects.get(source__relative_path=source.name)
                self.assertEqual(job.state, "failed")
                self.assertIn(job.error_code, ("image_invalid", "image_not_jpeg"))
                self.assertEqual(Photo.objects.count(), 0)
                self.assertEqual(source.read_bytes(), before)
                self.assertNotIn(str(settings.BIRDS_INBOX_ROOT), job.safe_message)

    def test_safe_fd_rejects_traversal_symlinks_and_fifo(self):
        from archive.services.paths import open_root, open_regular_beneath, UnsafePath
        inbox = Path(settings.BIRDS_INBOX_ROOT)
        outside = Path(settings.BIRDS_TEST_ROOT) / "outside.jpg"
        jpeg(outside)
        (inbox / "linked.jpg").symlink_to(outside)
        (inbox / "linked-dir").symlink_to(outside.parent, target_is_directory=True)
        os.mkfifo(inbox / "fifo.jpg")
        fd = open_root(inbox)
        try:
            for path in ("../outside.jpg", str(outside), "linked.jpg", "linked-dir/outside.jpg", "fifo.jpg", "x//y.jpg"):
                with self.subTest(path=path), self.assertRaises((UnsafePath, OSError)):
                    open_regular_beneath(fd, path)
        finally:
            os.close(fd)
            (inbox / "fifo.jpg").unlink()
            (inbox / "linked-dir").unlink()

    def test_open_root_fd_stays_bound_if_directory_name_is_replaced(self):
        from archive.services.paths import open_root, open_regular_beneath
        with tempfile.TemporaryDirectory(dir=settings.BIRDS_TEST_ROOT) as test:
            parent = Path(test)
            original = parent / "root"
            original.mkdir()
            jpeg(original / "photo.jpg", color=(10, 20, 30))
            wanted = (original / "photo.jpg").read_bytes()
            fd = open_root(original)
            try:
                original.rename(parent / "renamed")
                original.mkdir()
                jpeg(original / "photo.jpg", color=(80, 90, 70))
                with open_regular_beneath(fd, "photo.jpg") as stream:
                    self.assertEqual(stream.read(), wanted)
            finally:
                os.close(fd)

    def test_worker_lock_covers_lifetime_and_releases_on_exit(self):
        options = command_options("runworker")
        first = subprocess.Popen(**options, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        lock = None
        try:
            deadline = time.monotonic() + 5
            held = False
            while time.monotonic() < deadline:
                path = Path(settings.BIRDS_DATA_ROOT) / "worker.lock"
                if path.exists():
                    lock = path.open("rb")
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(lock, fcntl.LOCK_UN)
                    except BlockingIOError:
                        held = True
                        break
                    lock.close()
                    lock = None
                time.sleep(.05)
            self.assertTrue(held, "首 worker 须持生命周期锁")
            second = command("runworker", "--once")
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("worker_busy", second.stderr)
        finally:
            first.terminate()
            first.communicate(timeout=10)
            if lock:
                lock.close()
        self.assertEqual(first.returncode, 0)
        jpeg(Path(settings.BIRDS_INBOX_ROOT) / "exit.jpg")
        self.assertEqual(command("runworker", "--once").returncode, 0)
        self.assertEqual(Photo.objects.count(), 1)

    def test_photo_identity_and_logical_keys_remain_stable(self):
        from django.db import IntegrityError, transaction
        photo = Photo.objects.create(sha256="a" * 64, byte_size=5, preview_key="previews/example.jpg")
        photo.sha256 = "b" * 64
        with self.assertRaisesRegex(ValueError, "不可改写"):
            photo.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Photo.objects.create(sha256="a" * 64, byte_size=5, preview_key="previews/other.jpg")

    def test_test_settings_ignore_real_roots_and_reject_unsafe_shared_root(self):
        from archive.tests.fixtures import command_options
        options = command_options("shell", "-c", "from django.conf import settings; print(settings.BIRDS_INBOX_ROOT); print(settings.BIRDS_DATA_ROOT)")
        options["env"]["BIRDS_INBOX_ROOT"] = "/unconfigured-private-nas/inbox"
        options["env"]["BIRDS_DATA_ROOT"] = "/unconfigured-private-nas/data"
        result = subprocess.run(**options, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(settings.BIRDS_TEST_ROOT), result.stdout)
        self.assertNotIn("unconfigured-private-nas", result.stdout)
        options["env"]["BIRDS_TEST_ROOT"] = str(settings.BASE_DIR)
        result = subprocess.run(**options, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("拒绝未经验证的测试根目录", result.stderr)
