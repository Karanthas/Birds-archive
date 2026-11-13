import hashlib
import io
import uuid
from pathlib import Path
from PIL import Image
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.test import Client, TransactionTestCase
from archive.models import Photo, ImportSource
from archive.tests.fixtures import command, jpeg


def body(response):
    return b"".join(response.streaming_content) if response.streaming else response.content


class DirectAccessTests(TransactionTestCase):
    def setUp(self):
        self.source = Path(settings.BIRDS_INBOX_ROOT) / "私有照片_中文.jpg"
        for item in self.source.parent.iterdir():
            if item.is_file() or item.is_symlink():
                item.unlink()
        self.digest = jpeg(self.source)
        result = command("runworker", "--once")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.photo = Photo.objects.get()
        self.media = f"/media/{self.photo.pk}/preview/"
        self.paths = ["/", "/review/", f"/photos/{self.photo.pk}/", "/tasks/", "/tasks/status/", "/species/",
                      self.media, f"/media/{self.photo.pk}/thumb/"]

    def test_new_client_reads_real_pages_and_decodable_media_without_accounts(self):
        client = Client(enforce_csrf_checks=True)
        for path in self.paths:
            for headers in ({}, {"HTTP_RANGE": "bytes=0-30"}, {"HTTP_HX_REQUEST": "true"}):
                with self.subTest(path=path, headers=headers):
                    response = client.get(path, **headers)
                    self.assertEqual(response.status_code, 200)
                    for directive in ("private", "no-store", "no-cache", "must-revalidate"):
                        self.assertIn(directive, response["Cache-Control"])
                    self.assertEqual(response["Pragma"], "no-cache")
                    self.assertEqual(response["Expires"], "0")
                    payload = body(response)
                    if path.startswith("/media/"):
                        with Image.open(io.BytesIO(payload)) as image:
                            image.load()
                            self.assertEqual(image.format, "JPEG")
        self.assertEqual(User.objects.count(), 0)
        self.assertNotIn("_auth_user_id", client.session)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.digest)

    def test_head_follows_existing_methods_and_host_is_still_checked(self):
        for path in self.paths:
            response = self.client.head(path)
            self.assertEqual(response.status_code, 200 if path.startswith("/media/") else 405)
            self.assertEqual(body(response), b"")
            self.assertEqual(self.client.get(path, HTTP_HOST="other.invalid").status_code, 400)

    def test_removed_account_routes_cannot_create_identity_even_with_valid_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.get(f"/photos/{self.photo.pk}/capture-time/", {"date": "2025-01-01"})
        token = client.cookies[settings.CSRF_COOKIE_NAME].value
        for path in ("/login/", "/logout/"):
            self.assertEqual(client.get(path).status_code, 404)
            response = client.post(path, {"csrfmiddlewaretoken": token, "username": "sentinel", "password": "secret"})
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("_auth_user_id", client.session)
        self.assertEqual(User.objects.count(), 0)

    def test_missing_wrong_token_and_foreign_origin_have_safe_403(self):
        client = Client(enforce_csrf_checks=True)
        client.get(f"/photos/{self.photo.pk}/capture-time/", {"date": "2025-01-01"})
        token = client.cookies[settings.CSRF_COOKIE_NAME].value
        paths = ["/metadata/location/", f"/photos/{self.photo.pk}/capture-time/",
                 f"/tasks/{uuid.uuid4()}/retry/"]
        for path in paths:
            for data, headers in (({}, {}), ({"csrfmiddlewaretoken": "x" * 64}, {}),
                                  ({"csrfmiddlewaretoken": token}, {"HTTP_ORIGIN": "https://evil.invalid"})):
                with self.subTest(path=path, headers=headers):
                    response = client.post(path, {"location": "private-sentinel", **data}, HTTP_HX_REQUEST="true", **headers)
                    self.assertContains(response, "页面已过期，请刷新后重试。", status_code=403)
                    self.assertNotContains(response, "private-sentinel", status_code=403)
                    self.assertNotContains(response, "evil.invalid", status_code=403)
                    self.assertNotIn("HX-Redirect", response)
                    self.assertIn("no-store", response["Cache-Control"])
        self.photo.refresh_from_db()
        self.assertEqual((self.photo.user_location, self.photo.metadata_version), ("", 1))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.digest)

    def test_stale_or_deleted_session_has_no_access_semantics(self):
        for cookie in ("not-a-session", "a" * 32):
            client = Client()
            client.cookies[settings.SESSION_COOKIE_NAME] = cookie
            Session.objects.filter(session_key=cookie).delete()
            for path in self.paths:
                self.assertEqual(client.get(path).status_code, 200)
            self.assertNotIn("_auth_user_id", client.session)

    def test_old_session_probe_header_is_an_ordinary_get(self):
        response = self.client.get("/review/", HTTP_X_BIRDS_SESSION_CHECK="1", HTTP_HX_REQUEST="true")
        self.assertContains(response, "私有照片_中文.jpg")
        self.assertIn("no-store", response["Cache-Control"])

    def test_source_cache_and_absent_routes_have_no_static_bypass(self):
        for path in (f"/media/{self.photo.pk}/original/", f"/media/{uuid.uuid4()}/preview/",
                     "/original/", "/trash/", "/register/"):
            self.assertEqual(self.client.get(path).status_code, 404)
        response = self.client.get("/static/" + self.photo.preview_key)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"\xff\xd8", body(response))
        self.assertEqual(self.client.get("/static/archive/app.css").status_code, 200)

    def test_media_path_cannot_escape_controlled_root(self):
        for key in ("../outside.jpg", "/tmp/outside.jpg"):
            Photo.objects.filter(pk=self.photo.pk).update(preview_key=key)
            self.assertEqual(self.client.get(self.media).status_code, 404)

    def test_long_chinese_filename_remains_readable_and_escaped(self):
        name = "鸟" * 65 + "<script>.jpg"
        jpeg(Path(settings.BIRDS_INBOX_ROOT) / name, color=(10, 30, 120))
        result = command("runworker", "--once")
        self.assertEqual(result.returncode, 0, result.stderr)
        photo = ImportSource.objects.get(relative_path=name).photo
        for path in (f"/photos/{photo.pk}/", "/review/"):
            response = self.client.get(path)
            self.assertContains(response, "鸟" * 65 + "&lt;script&gt;.jpg")
            self.assertNotContains(response, "<script>.jpg")
