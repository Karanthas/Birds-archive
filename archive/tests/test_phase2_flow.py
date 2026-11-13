"""Synthetic JPEG → anonymous CSRF HTTP → real worker → gallery → original bytes."""
import hashlib
import os
import sqlite3
import shutil
import uuid
from urllib.parse import unquote

from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TransactionTestCase

from archive.models import (ArchiveCompletion, ArchiveOperation, Job, ManagedOriginal,
                            Photo, SourceObservation, Species)
from archive.tests.fixtures import disk_hashes, jpeg
from archive.tests.test_archive_recovery import ArchiveFixture


class Phase2FlowTests(ArchiveFixture):
    def setUp(self):
        super().setUp()
        self.client = Client(enforce_csrf_checks=True)
        self.owned_input_directories = set()

    def tearDown(self):
        # The shared fixture root outlives this test class. Only remove nested
        # input directories created by this flow; older worker tests expect a flat
        # inbox and must not inherit our collision/duplicate input hierarchy.
        for directory in self.owned_input_directories:
            if directory.exists():
                shutil.rmtree(directory)
        super().tearDown()

    def imported(self, name='合成.jpg', color='red', capture=None):
        path = self.inbox/name
        if path.parent != self.inbox and not path.parent.exists():
            self.owned_input_directories.add(path.parent)
        digest = jpeg(path, color=color)
        self.subprocess_worker()
        photo = Photo.objects.get(sha256=digest)
        if capture:
            Photo.objects.filter(pk=photo.pk).update(manual_capture=capture)
            photo.refresh_from_db()
        return photo, path, digest

    def submit(self, photo, *, species=None, names=None, classification='species', payload=None):
        page = self.client.get(f'/photos/{photo.pk}/')
        token = self.client.cookies['csrftoken'].value
        data = payload or dict(metadata_version=photo.metadata_version, request_id=str(uuid.uuid4()),
            species_ids=[str(s.pk) for s in (species or [])], local_names=names or [], classification=classification)
        response = self.client.post(f'/photos/{photo.pk}/', data,
            HTTP_X_CSRFTOKEN=token, HTTP_X_ARCHIVE_CONFIRM='true')
        return response, data

    def assert_asset(self, photo, digest):
        operation = ArchiveOperation.objects.get(photo=photo)
        managed = ManagedOriginal.objects.get(photo=photo)
        self.assert_complete(photo, self.inbox/operation.source_observation.source.relative_path, operation)
        hashes = disk_hashes(self.archive)
        self.assertEqual([p for p, h in hashes.items() if h == digest], [managed.relative_path])
        self.assertEqual(disk_hashes(self.claims), {})
        self.assertContains(self.client.get('/'), f'data-photo-id="{photo.pk}"')
        response = self.client.get(f'/photos/{photo.pk}/download/')
        self.assertEqual(response.status_code, 200)
        try:
            content = b''.join(response.streaming_content)
        finally:
            response.close()
        self.assertEqual(hashlib.sha256(content).hexdigest(), digest)
        self.assertIn(managed.actual_basename, unquote(response['Content-Disposition']))
        self.assertIn('no-store', response['Cache-Control'])
        connection.close()
        with sqlite3.connect(settings.DATABASES['default']['NAME']) as db:
            self.assertEqual(db.execute('SELECT sha256 FROM archive_photo WHERE id=?', (photo.pk.hex,)).fetchone(), (digest,))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM archive_archivecompletion WHERE photo_id=?', (photo.pk.hex,)).fetchone(), (1,))
        return managed

    def test_import_confirm_archive_gallery_download(self):
        photo, path, digest = self.imported(capture={'local':'2026-10-15', 'precision':'date', 'offset':None})
        self.assertEqual(self.client.post(f'/photos/{photo.pk}/', {}).status_code, 403)
        response, _ = self.submit(photo, species=[self.species])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['accepted'])
        self.assertTrue(path.exists())
        self.assertFalse(ArchiveCompletion.objects.exists())
        self.assertNotContains(self.client.get('/'), f'data-photo-id="{photo.pk}"')
        self.subprocess_worker()
        managed = self.assert_asset(photo, digest)
        self.assertIn('/2026/', managed.relative_path)

    def test_local_multi_unknown_paths(self):
        for index, (species, names, classification, prefix) in enumerate((
            ([], ['本地观察鸟'], 'species', '本地观察鸟/未知年份/'),
            ([self.species], ['另一种本地鸟'], 'species', '多鸟种/未知年份/'),
            ([], [], 'undetermined', '无法确定/未知年份/'))):
            with self.subTest(classification=prefix):
                photo, _, digest = self.imported(f'{index}/同名.jpg', (index*70, 40, 90))
                self.assertEqual(self.submit(photo, species=species, names=names, classification=classification)[0].status_code, 200)
                self.subprocess_worker()
                self.assertTrue(self.assert_asset(photo, digest).relative_path.startswith(prefix))
                self.assertContains(self.client.get(f'/photos/{photo.pk}/'), names[0] if names else '无法确定')
        for local in Species.objects.filter(source_kind='local'):
            self.assertIsNone(local.scientific_name)

    def test_repeat_requests_no_duplicate_asset(self):
        photo, _, digest = self.imported('first/同名.jpg')
        response, payload = self.submit(photo, species=[self.species])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.submit(photo, payload=payload)[0].status_code, 200)
        self.subprocess_worker()
        first = self.assert_asset(photo, digest)
        duplicate = self.inbox/'duplicate.jpg'
        duplicate.write_bytes((self.archive/first.relative_path).read_bytes())
        self.subprocess_worker(); self.subprocess_worker()
        self.assertEqual(Photo.objects.count(), 1)
        self.assertEqual(SourceObservation.objects.filter(photo=photo, duplicate=True).count(), 1)
        self.assertEqual(hashlib.sha256(duplicate.read_bytes()).hexdigest(), digest)
        other, _, other_digest = self.imported('second/同名.jpg', 'blue')
        self.assertEqual(self.submit(other, species=[self.species])[0].status_code, 200)
        self.subprocess_worker()
        second = self.assert_asset(other, other_digest)
        self.assertNotEqual(first.actual_basename, second.actual_basename)
        self.assertEqual(hashlib.sha256((self.archive/first.relative_path).read_bytes()).hexdigest(), digest)
        self.assertEqual((ManagedOriginal.objects.count(), ArchiveOperation.objects.count()), (2, 2))

    def test_confirmed_metadata_frozen(self):
        photo, _, digest = self.imported()
        self.assertEqual(self.submit(photo, species=[self.species])[0].status_code, 200)
        frozen = ArchiveOperation.objects.get(photo=photo).capture_snapshot
        response, _ = self.submit(photo, names=['不能覆盖'])
        self.assertEqual(response.status_code, 409)
        self.subprocess_worker()
        self.assert_asset(photo, digest)
        self.assertEqual(ArchiveOperation.objects.get(photo=photo).capture_snapshot, frozen)
        self.assertEqual(list(photo.confirmed_species.values_list('species_id', flat=True)), [self.species.pk])

    def test_failure_during_next_review_keeps_contract(self):
        first, _, digest = self.imported()
        second, path, second_digest = self.imported('next.jpg', 'blue')
        response, _ = self.submit(first, species=[self.species])
        self.assertEqual(response.json()['next_url'], f'/photos/{second.pk}/')
        operation = ArchiveOperation.objects.get(photo=first)
        Job.objects.filter(pk=operation.job_id).update(state='failed', error_code='space_exhausted')
        fragment = self.client.get('/tasks/status/?scope=archive')
        self.assertContains(fragment, '归档失败 1')
        self.assertNotContains(fragment, '<form')
        self.assertNotContains(fragment, str(second.pk))
        self.assertContains(self.client.get(f'/photos/{second.pk}/'), 'data-archive-confirm')
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), second_digest)
        self.assertEqual(disk_hashes(self.archive), {})
        connection.close()
        with sqlite3.connect(settings.DATABASES['default']['NAME']) as db:
            self.assertEqual(db.execute('SELECT state FROM archive_photo WHERE id=?', (second.pk.hex,)).fetchone(), ('review_pending',))
            self.assertEqual(db.execute('SELECT sha256 FROM archive_photo WHERE id=?', (first.pk.hex,)).fetchone(), (digest,))

    def test_independent_hash_check_rejects_known_bad_control(self):
        photo, _, digest = self.imported()
        self.assertEqual(self.submit(photo, species=[self.species])[0].status_code, 200)
        self.subprocess_worker()
        if os.environ.get('BIRDS_PHASE2_KNOWN_BAD') == '1':
            self.assert_asset(photo, '0'*64)
        with self.assertRaises(AssertionError):
            self.assert_asset(photo, '0'*64)
        self.assert_asset(photo, digest)


class Phase2MigrationTests(TransactionTestCase):
    def test_0004_to_latest_preserves_existing_assets(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes('archive')
        # Resolve the historical node without guessing a generated filename.
        old = [node for node in executor.loader.graph.nodes if node[0] == 'archive' and node[1].startswith('0004_')]
        try:
            executor.migrate(old)
            apps = executor.loader.project_state(old).apps
            path = settings.BIRDS_INBOX_ROOT/'migration-original.jpg'
            digest = jpeg(path, 'yellow')
            values = {'Photo': dict(sha256=digest, byte_size=path.stat().st_size, preview_key='historical', original_available=True),
                      'SourceSnapshot': dict(source='migration', version='1', sha256='a'*64, acquired_at='2026-10-16',
                          download_page_url='https://example.org', license_url='https://example.org/license', attribution='test', row_count=1, species_count=1, manifest={})}
            photo = apps.get_model('archive','Photo').objects.create(**values['Photo'])
            snapshot = apps.get_model('archive','SourceSnapshot').objects.create(**values['SourceSnapshot'])
            species = apps.get_model('archive','Species').objects.create(scientific_name='Ardea alba', scientific_key='ardea alba', source_snapshot=snapshot)
            concept = apps.get_model('archive','SpeciesConcept').objects.create(snapshot=snapshot, species=species, external_id='old-1', evidence='retained')
            source = apps.get_model('archive','ImportSource').objects.create(relative_path=path.name, photo=photo)
            observation = apps.get_model('archive','SourceObservation').objects.create(source=source, version=1, photo=photo, sha256=digest, byte_size=photo.byte_size, available=True)
            source.current_observation=observation; source.save()
            job = apps.get_model('archive','Job').objects.create(source=source, source_version=1, state='succeeded', expected_sha256=digest)
            executor = MigrationExecutor(connection); executor.migrate(latest)
            connection.close(); connection.ensure_connection()
            with connection.cursor() as cursor:
                for pragma, expected in (('journal_mode','wal'), ('synchronous',2), ('foreign_keys',1)):
                    cursor.execute('PRAGMA '+pragma)
                    self.assertEqual(cursor.fetchone()[0], expected)
            with sqlite3.connect(settings.DATABASES['default']['NAME']) as db:
                self.assertEqual(db.execute('SELECT id,sha256 FROM archive_photo').fetchall(), [(photo.pk.hex,digest)])
                self.assertEqual(db.execute('SELECT photo_id,sha256 FROM archive_sourceobservation WHERE id=?',(observation.pk.hex,)).fetchone(), (photo.pk.hex,digest))
                self.assertEqual(db.execute('SELECT state,expected_sha256 FROM archive_job WHERE id=?',(job.pk.hex,)).fetchone(), ('succeeded',digest))
                self.assertEqual(db.execute('SELECT species_id,external_id,evidence FROM archive_speciesconcept WHERE id=?',(concept.pk,)).fetchone(), (species.pk.hex,'old-1','retained'))
                self.assertEqual(db.execute('SELECT source_snapshot_id,source_kind FROM archive_species WHERE id=?',(species.pk.hex,)).fetchone(), (snapshot.pk.hex,'catalog'))
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
                indexes = db.execute('PRAGMA index_list(archive_photo)').fetchall()
                self.assertTrue(any(row[2] for row in indexes))
                columns = [row[1] for row in db.execute('PRAGMA table_info(archive_photo)')]
                expressions = ['?' if name == 'id' else name for name in columns]
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute('INSERT INTO archive_photo SELECT '+','.join(expressions)+' FROM archive_photo', (uuid.uuid4().hex,))
                sql = db.execute("SELECT sql FROM sqlite_master WHERE name='archive_species'").fetchone()[0]
                self.assertIn('species_source_identity', sql)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM archive_archivecompletion').fetchone(), (0,))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        finally:
            MigrationExecutor(connection).migrate(latest)
