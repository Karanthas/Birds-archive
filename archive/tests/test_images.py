import hashlib
import io
from pathlib import Path
from PIL import Image
from django.conf import settings
from django.contrib.auth.models import User
from django.test import TransactionTestCase
from archive.models import Photo, Job
from archive.tests.fixtures import command


def exif_jpeg(path, orientation=1, date=None, subsecond=None, offset=None):
    exif = Image.Exif()
    exif[274] = orientation
    fields = {key: value for key, value in ((36867, date), (37521, subsecond),
                                           (36881, offset)) if value is not None}
    if fields:
        exif[34665] = fields
    # Four distinct quadrants let tests detect mirrors as well as rotations.
    with Image.new('RGB', (80, 40)) as image:
        for box, color in (((0, 0, 40, 20), (240, 10, 10)),
                           ((40, 0, 80, 20), (10, 240, 10)),
                           ((0, 20, 40, 40), (10, 10, 240)),
                           ((40, 20, 80, 40), (240, 240, 10))):
            image.paste(color, box)
        image.save(path, 'JPEG', exif=exif, quality=98, subsampling=0)
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ImageMetadataTests(TransactionTestCase):
    def setUp(self):
        self.inbox = Path(settings.BIRDS_INBOX_ROOT)
        for item in self.inbox.iterdir():
            if item.is_file() or item.is_symlink():
                item.unlink()
        self.client.force_login(User.objects.create_superuser(settings.BIRDS_OWNER_USERNAME,
                                                              password='image-test'))

    def import_image(self, **kwargs):
        path = self.inbox / f'image-{len(list(self.inbox.iterdir()))}.jpg'
        digest = exif_jpeg(path, **kwargs)
        result = command('runworker', '--once')
        self.assertEqual(result.returncode, 0, result.stderr)
        photo = Photo.objects.get(sha256=digest)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        return photo, path

    def test_worker_persists_sub_ifd_time_and_detail_local_year(self):
        photo, path = self.import_image(date='2025:12:31 23:59:58', subsecond='001234567', offset='-14:00')
        response = self.client.get(f'/photos/{photo.pk}/')
        self.assertContains(response, '2025-12-31 23:59:58.001234567')
        self.assertEqual(photo.capture_offset, -840)
        self.assertEqual(photo.capture_precision, 'subsecond')
        self.assertEqual(photo.capture_quality, 'reliable')
        self.assertEqual(photo.exif_raw['SubsecTimeOriginal'], '001234567')

    def test_all_eight_orientations_pixels_sizes_and_source_hash(self):
        corners = {1:'RGBY', 2:'GRYB', 3:'YBGR', 4:'BYRG',
                   5:'RBGY', 6:'BRYG', 7:'YGBR', 8:'GYRB'}
        colors = {'R': (240,10,10), 'G': (10,240,10), 'B': (10,10,240), 'Y': (240,240,10)}
        for orientation in range(1, 9):
            with self.subTest(orientation=orientation):
                photo, path = self.import_image(orientation=orientation)
                for variant in ('thumb', 'preview'):
                    response = self.client.get(f'/media/{photo.pk}/{variant}/')
                    self.assertEqual(response.status_code, 200)
                    with Image.open(io.BytesIO(b''.join(response.streaming_content))) as image:
                        self.assertEqual(image.size, (40,80) if orientation >= 5 else (80,40))
                        points = [(image.width//4,image.height//4), (image.width*3//4,image.height//4),
                                  (image.width//4,image.height*3//4), (image.width*3//4,image.height*3//4)]
                        actual = ''.join(min(colors, key=lambda c: sum(abs(a-b) for a,b in zip(image.getpixel(p),colors[c]))) for p in points)
                        self.assertEqual(actual, corners[orientation])
                self.assertContains(self.client.get(f'/photos/{photo.pk}/'), '未知')

    def test_missing_invalid_and_offset_boundaries_do_not_use_mtime(self):
        for date, subsecond, offset, quality in (
            (None,None,None,'missing'), ('',None,None,'missing'),
            ('2025:02:29 12:00:00',None,None,'invalid'),
            ('2024:02:29 12:00:00',None,'+14:01','invalid'),
            ('2024:02:29 12:00:00',None,'+14:00','reliable'),
            ('2025:12:31 23:59:59','123',None,'reliable'),
            ('2024:02:29 12:00:00','x',None,'invalid')):
            with self.subTest(date=date, offset=offset):
                photo, _ = self.import_image(date=date, subsecond=subsecond, offset=offset)
                self.assertEqual(photo.capture_quality, quality)
                if quality != 'reliable':
                    self.assertIsNone(photo.captured_local)
                    self.assertEqual(photo.capture_precision, 'unknown')
                else:
                    self.assertEqual(photo.captured_local[:4], date[:4])

    def test_invalid_orientation_falls_back_and_keeps_raw(self):
        for orientation in (0, 9):
            photo, _ = self.import_image(orientation=orientation)
            response = self.client.get(f'/media/{photo.pk}/preview/')
            with Image.open(io.BytesIO(b''.join(response.streaming_content))) as image:
                self.assertEqual(image.size, (80,40))
            self.assertEqual(photo.exif_raw['Orientation'], orientation)

    def test_non_jpeg_truncated_and_pixel_limits(self):
        from archive.services.images import decode_jpeg, ImageRejected, MAX_BYTES
        from unittest import mock
        import tempfile
        with tempfile.TemporaryFile() as source:
            source.truncate(MAX_BYTES+1)
            with mock.patch('archive.services.images.Image.open', side_effect=AssertionError('header must not open')):
                with self.assertRaises(ImageRejected):
                    decode_jpeg(source)
        for kind in ('png','truncated','pixels'):
            path = self.inbox / f'{kind}.jpg'
            exif_jpeg(path)
            if kind == 'png':
                Image.new('RGB',(5,5)).save(path,'PNG')
            elif kind == 'truncated':
                path.write_bytes(path.read_bytes()[:-30])
            else:
                payload = bytearray(path.read_bytes())
                marker = payload.index(b'\xff\xc0')
                payload[marker+5:marker+9] = (6500).to_bytes(2,'big')*2
                path.write_bytes(payload)
            before = path.read_bytes()
            result = command('runworker','--once')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(Job.objects.get(source__relative_path=path.name).state,'failed')
            self.assertEqual(path.read_bytes(),before)

    def test_metadata_migration_and_reconnect_preserve_old_photo(self):
        import os
        import sqlite3
        import subprocess
        import tempfile
        from archive.tests.fixtures import command_options
        with tempfile.TemporaryDirectory(prefix='birds-tests-') as directory:
            root = Path(directory)
            (root / '.birds-test-root').write_text('birds isolated v1')
            options = command_options('migrate','archive','0002','--noinput')
            options['env']['BIRDS_TEST_ROOT'] = str(root)
            result = subprocess.run(**options,capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            database = root / 'data/test.sqlite3'
            import uuid
            ident = uuid.uuid4().hex
            with sqlite3.connect(database) as db:
                db.execute('INSERT INTO archive_photo(id,sha256,byte_size,state,preview_key,original_available,created_at) VALUES(?,?,?,?,?,?,?)',
                           (ident,'a'*64,42,'review_pending','',0,'2025-01-01'))
            options['args'] = options['args'][:2]+['migrate','--noinput','--settings=config.settings_test']
            result = subprocess.run(**options,capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            with sqlite3.connect(database) as db:
                row = db.execute('SELECT capture_quality,capture_precision,captured_local,manual_capture,metadata_version,user_location FROM archive_photo WHERE id=?',(ident,)).fetchone()
                self.assertEqual(row,('missing','unknown',None,None,1,''))
                db.execute('UPDATE archive_photo SET user_location=?,metadata_version=2 WHERE id=?',('广东',ident))
            with sqlite3.connect(database) as db:
                self.assertEqual(db.execute('SELECT user_location,metadata_version FROM archive_photo').fetchone(),('广东',2))

    def test_raw_illegal_bytes_and_top_level_datetime_are_not_capture_facts(self):
        from archive.services.images import capture_metadata
        from unittest import mock
        exif = mock.Mock()
        exif.get.return_value = 'broken orientation'
        exif.get_ifd.return_value = {36867:b'\xffbad'}
        image = mock.Mock()
        image.getexif.return_value = exif
        result = capture_metadata(image)
        self.assertEqual(result['capture_quality'],'invalid')
        self.assertEqual(result['exif_raw']['DateTimeOriginal'],{'bytes_hex':'ff626164'})
        exif.get_ifd.return_value = {}
        result = capture_metadata(image)
        self.assertEqual(result['capture_quality'],'missing')
        self.assertIsNone(result['captured_local'])
