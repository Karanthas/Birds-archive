import hashlib
import uuid

from django.utils import timezone

from archive.models import Photo, MetadataReceipt
from archive.services.management import ManagementConflict, accept_management
from archive.services.management_metadata import set_personal_metadata
from archive.tests.test_download import DownloadFixture


class PersonalMetadataTests(DownloadFixture):
    def test_explicit_values_receipts_cas_and_validation(self):
        photo, _, _ = self.completed(); photo.refresh_from_db()
        version = photo.metadata_version
        request = uuid.uuid4()
        changes = {'favorite': True, 'rating': 5}
        result = set_personal_metadata(photo.pk, version, request, changes)
        self.assertEqual(set_personal_metadata(photo.pk, version, request, changes), result)
        photo.refresh_from_db()
        self.assertEqual((photo.favorite, photo.rating, photo.metadata_version), (True, 5, version+1))
        self.assertEqual(MetadataReceipt.objects.filter(photo=photo).count(), 1)
        with self.assertRaises(ManagementConflict):
            set_personal_metadata(photo.pk, version, request, {'favorite': False})
        with self.assertRaises(ManagementConflict):
            set_personal_metadata(photo.pk, version, uuid.uuid4(), {'favorite': False})
        for rating in (True, False, 0, 6, '3', 2.1):
            with self.subTest(rating=rating), self.assertRaises(ValueError):
                set_personal_metadata(photo.pk, photo.metadata_version, uuid.uuid4(), {'rating':rating})
        set_personal_metadata(photo.pk, photo.metadata_version, uuid.uuid4(), {'favorite':False,'rating':None})
        photo.refresh_from_db()
        self.assertFalse(photo.favorite); self.assertIsNone(photo.rating)

    def test_correction_keeps_personal_values_and_trash_rejects(self):
        photo, _, _ = self.completed(); photo.refresh_from_db()
        operation = accept_management(photo.pk, photo.metadata_version, uuid.uuid4(), 'capture',
            {'mode':'manual','capture':{'local':'2036-01-02','precision':'date','offset':None}})
        photo.refresh_from_db(); generation = photo.management_generation
        set_personal_metadata(photo.pk, photo.metadata_version, uuid.uuid4(), {'favorite':True,'rating':4})
        self.run_worker(); photo.refresh_from_db(); operation.refresh_from_db()
        self.assertEqual(operation.status, 'succeeded')
        self.assertEqual((photo.favorite, photo.rating, photo.management_generation), (True,4,generation))
        Photo.objects.filter(pk=photo.pk).update(trash_requested_at=timezone.now())
        with self.assertRaises(ManagementConflict):
            set_personal_metadata(photo.pk, photo.metadata_version, uuid.uuid4(), {'favorite':False})

    def test_quick_http_json_old_value_on_failure_and_pending(self):
        from django.test import Client
        photo, _, _ = self.completed(); photo.refresh_from_db()
        url = f'/photos/{photo.pk}/'
        data = {'management_action':'personal','personal_field':'favorite','favorite':'true',
            'metadata_version':photo.metadata_version,'request_id':str(uuid.uuid4())}
        strict = Client(enforce_csrf_checks=True)
        self.assertEqual(strict.post(url, data).status_code, 403)
        result = self.client.post(url, data, HTTP_ACCEPT='application/json')
        self.assertEqual(result.status_code, 200); self.assertTrue(result.json()['favorite'])
        self.assertIn('no-store', result['Cache-Control'])
        conflict = self.client.post(url, {**data, 'favorite':'false','request_id':str(uuid.uuid4())},
            HTTP_ACCEPT='application/json')
        self.assertEqual(conflict.status_code, 409); self.assertTrue(conflict.json()['favorite'])
        self.assertContains(self.client.get('/'), 'data-personal-quick')
        self.assertContains(self.client.get(url), '取消收藏')
        pending = Photo.objects.create(state='review_pending', byte_size=1, sha256='a'*64)
        set_personal_metadata(pending.pk, pending.metadata_version, uuid.uuid4(), {'rating':2})
        self.assertEqual(Photo.objects.get(pk=pending.pk).rating, 2)


class NoteLocationTests(DownloadFixture):
    def post_personal(self, photo, field, value, version=None):
        return self.client.post(f'/photos/{photo.pk}/', {'management_action':'personal',
            'personal_field':field, field:value, 'metadata_version':version or photo.metadata_version,
            'request_id':str(uuid.uuid4())})

    def test_notes_save_clear_escape_limit_and_conflict_draft(self):
        photo, target, _ = self.completed(); photo.refresh_from_db()
        sha = hashlib.sha256(target.read_bytes()).hexdigest()
        self.assertEqual(self.post_personal(photo,'notes','<script>alert(1)</script>').status_code,303)
        photo.refresh_from_db()
        page = self.client.get(f'/photos/{photo.pk}/')
        self.assertContains(page,'&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(page,'<script>alert(1)</script>')
        self.assertEqual(self.post_personal(photo,'notes','x'*10001).status_code,400)
        response = self.post_personal(photo,'notes','未保存草稿',version=photo.metadata_version-1)
        self.assertContains(response,'未保存草稿',status_code=409)
        self.assertContains(response,'&lt;script&gt;',status_code=409)
        photo.refresh_from_db(); self.assertEqual(photo.notes,'<script>alert(1)</script>')
        self.assertEqual(self.post_personal(photo,'notes','').status_code,303)
        photo.refresh_from_db(); self.assertEqual(photo.notes,'')
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),sha)

    def test_location_isolation_and_notes_survive_worker(self):
        photo, target, managed = self.completed(); photo.refresh_from_db()
        capture, raw, sha = photo.current_capture, photo.exif_raw, photo.sha256
        self.assertEqual(self.post_personal(photo,'user_location','  广东  ').status_code,303)
        photo.refresh_from_db(); self.assertEqual(photo.user_location,'广东')
        self.assertEqual((photo.current_capture,photo.exif_raw), (capture,raw))
        self.assertEqual(self.post_personal(photo,'user_location','x'*201).status_code,400)
        self.assertEqual(self.post_personal(photo,'user_location','  ').status_code,303)
        photo.refresh_from_db(); self.assertEqual(photo.user_location,'')
        accept_management(photo.pk,photo.metadata_version,uuid.uuid4(),'capture',
            {'mode':'manual','capture':{'local':'2037-01-01','precision':'date','offset':None}})
        photo.refresh_from_db()
        self.assertEqual(self.post_personal(photo,'notes','迁移时保存').status_code,303)
        self.run_worker(); photo.refresh_from_db(); managed.refresh_from_db()
        self.assertEqual(photo.notes,'迁移时保存')
        self.assertEqual(hashlib.sha256((self.archive/managed.relative_path).read_bytes()).hexdigest(),sha)
