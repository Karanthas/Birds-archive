"""真实确认与归档验证目录身份、原名、安全预算和单主文件。"""
import hashlib
import os
import unicodedata
import uuid
from pathlib import Path
from unittest import mock

from archive.models import ArchiveOperation, ManagedOriginal, Job, Species
from archive.services.intake import scan_once
from archive.services.jobs import process_source
from archive.services.paths import open_root
from archive.tests.fixtures import jpeg
from archive.tests.test_archive_recovery import ArchiveFixture


class ArchivePathTests(ArchiveFixture):
    def test_directory_collision_escalates_stable_id(self):
        for name in ('白鹭', '白鹭_'+self.species.pk.hex[:8]):
            Species.objects.create(scientific_name=name, scientific_key=name,
                chinese_name='other', search_key='other', source_snapshot=self.species.source_snapshot,
                archive_directory=name, directory_key=name.casefold())
        photo, path, operation = self.confirmed()
        self.run_worker(); self.assert_complete(photo, path, operation)
        self.species.refresh_from_db()
        self.assertEqual(self.species.archive_directory, '白鹭_'+self.species.pk.hex[:12])

    def test_directory_mapping_stable(self):
        self.species.chinese_name = 'CON.txt'
        self.species.save(update_fields=['chinese_name'])
        photo, path, operation = self.confirmed()
        self.run_worker()
        self.assert_complete(photo, path, operation)
        self.species.refresh_from_db()
        allocated = self.species.archive_directory
        # A suffix after the dot still leaves the Windows CON device stem.
        self.assertEqual(allocated, '_CON.txt_'+self.species.pk.hex[:8])
        self.species.chinese_name = '新名字'
        self.species.save(update_fields=['chinese_name'])
        second, source, second_op = self.confirmed('two.jpg', 'blue')
        self.run_worker()
        self.assert_complete(second, source, second_op)
        self.assertTrue(ManagedOriginal.objects.get(photo=second).relative_path.startswith(allocated+'/'))
        collision = Species.objects.create(scientific_name='Test bird', scientific_key='test bird',
            chinese_name='新名字', search_key='new', source_snapshot=self.species.source_snapshot)
        third, source, third_op = self.confirmed('three.jpg', 'green', [collision])
        self.run_worker(); self.assert_complete(third, source, third_op)
        collision.refresh_from_db()
        self.assertNotEqual(collision.directory_key, self.species.directory_key)

    def test_filename_collision_replay(self):
        photo, source, operation = self.confirmed()
        directory = self.archive/'白鹭'/'未知年份'; directory.mkdir(parents=True)
        foreign = directory/'one.jpg'; foreign.write_bytes(b'foreign different bytes')
        worker = self.start_paused('archive_after_link')
        operation.refresh_from_db(); allocated = operation.target_relative_path
        self.assertEqual(Path(allocated).name, 'one_'+operation.pk.hex[:8]+'.jpg')
        worker.kill(); worker.communicate(timeout=3)
        self.subprocess_worker(); self.subprocess_worker()
        self.assert_complete(photo, source, operation)
        operation.refresh_from_db(); self.assertEqual(operation.target_relative_path, allocated)
        self.assertEqual(foreign.read_bytes(), b'foreign different bytes')

    def test_foreign_same_hash_unowned(self):
        photo, source, operation = self.confirmed()
        directory = self.archive/'白鹭'/'未知年份'; directory.mkdir(parents=True)
        foreign = directory/'one.jpg'; foreign.write_bytes(source.read_bytes())
        before = foreign.stat()
        self.run_worker(); self.assert_complete(photo, source, operation)
        managed = ManagedOriginal.objects.get(photo=photo)
        self.assertNotEqual(managed.relative_path, '白鹭/未知年份/one.jpg')
        self.assertEqual((foreign.stat().st_ino, foreign.stat().st_mtime_ns), (before.st_ino, before.st_mtime_ns))
        self.assertEqual(hashlib.sha256(foreign.read_bytes()).hexdigest(), photo.sha256)

    def local_confirmed(self, names, *, name='one.jpg', color='red', classification='species', ids=None, capture=None):
        from archive.models import Photo
        path = self.inbox/name
        jpeg(path, color=color)
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
            'request_id': str(uuid.uuid4()), 'classification': classification,
            'local_names': names, 'species_ids': ids or []})
        self.assertEqual(response.status_code, 303)
        return photo, path, ArchiveOperation.objects.get(photo=photo)

    def finish(self, photo, path, operation):
        original = path.read_bytes()
        self.run_worker(); self.assert_complete(photo, path, operation)
        managed = ManagedOriginal.objects.get(photo=photo)
        self.assertEqual((self.archive/managed.relative_path).read_bytes(), original)
        self.assertEqual(operation.original_basename, path.name)
        target = managed.relative_path
        self.run_worker()
        managed.refresh_from_db()
        self.assertEqual(managed.relative_path, target)
        return managed

    def test_local_and_reserved_directory_names(self):
        names = ('多鸟种', '无法确定', 'CON.txt', 'nul', 'LPT9', '../危险/鸟\\名',
                 '尾点. ', '点名', '鸟'*200)
        allocated = []
        for index, name in enumerate(names):
            with self.subTest(name=name):
                photo, path, operation = self.local_confirmed([name], name=f'{index}.jpg', color=(index*20, 4, 60))
                managed = self.finish(photo, path, operation)
                species = photo.confirmed_species.get().species
                directory = managed.relative_path.split('/')[0]
                self.assertEqual(species.local_name, name)
                self.assertEqual(directory, species.archive_directory)
                self.assertEqual(species.directory_key, unicodedata.normalize('NFC', directory).casefold())
                self.assertNotIn(directory, ('多鸟种', '无法确定', '.', '..', '.archive-ops'))
                self.assertFalse(any(unicodedata.category(c).startswith('C') for c in directory))
                self.assertFalse(set(directory) & set('/\\:*?"<>|'))
                self.assertFalse(directory.endswith(('.', ' ')))
                self.assertNotIn(directory.casefold().split('.')[0], ('con', 'nul', 'lpt9'))
                self.assertLessEqual(len(directory.encode()), 180)
                if len(name.encode()) > 180:
                    self.assertIn(species.pk.hex[:8], directory)
                allocated.append(directory)
        self.assertEqual(len(set(allocated)), len(names))

    def test_unknown_year_and_undetermined(self):
        photo, path, operation = self.local_confirmed([], classification='undetermined')
        self.assertTrue(all(value is None for value in operation.capture_snapshot.values()))
        self.assertEqual(self.finish(photo, path, operation).relative_path, '无法确定/未知年份/one.jpg')
        self.assertFalse(photo.confirmed_species.exists())
        photo, path, operation = self.local_confirmed(['其他鸟'], name='two.jpg', color='blue',
            capture={'local': '2024-12-31', 'precision': 'date', 'offset': None})
        self.assertEqual((operation.capture_snapshot['year'], operation.capture_snapshot['month']), ('2024', '2024-12'))
        self.assertEqual(self.finish(photo, path, operation).relative_path, '其他鸟/2024/two.jpg')

    def test_unicode_casefold_collisions(self):
        targets = []
        for i, name in enumerate(('ÉGRET', 'e\u0301gret', 'Straße', 'STRASSE')):
            species = Species.objects.create(scientific_name=f'Synthetic {i}', scientific_key=f'synthetic {i}',
                chinese_name=name, search_key=name.casefold(), source_snapshot=self.species.source_snapshot)
            photo, path, operation = self.confirmed(name=f'{i}.jpg', color=(i*50, 10, 80), species=[species])
            target = self.finish(photo, path, operation).relative_path.split('/')[0]
            species.refresh_from_db()
            self.assertEqual(species.directory_key, unicodedata.normalize('NFC', target).casefold())
            targets.append(target)
        keys = [unicodedata.normalize('NFC', value).casefold() for value in targets]
        self.assertEqual(len(set(keys)), 4)
        for value in (targets[1], targets[3]):
            self.assertRegex(value, r'_[0-9a-f]{8}$')

    def test_long_utf8_basename(self):
        name = '鸟'*75+'.JPEG'
        photo, path, operation = self.local_confirmed(['长文件鸟'], name=name)
        managed = self.finish(photo, path, operation)
        self.assertTrue(managed.actual_basename.endswith('.JPEG'))
        self.assertLessEqual(len(managed.actual_basename.encode()), 180)
        self.assertIn(operation.pk.hex[:8], managed.actual_basename)

    def test_multispecies_one_original(self):
        photo, path, operation = self.local_confirmed(['第二鸟', '第二鸟'], ids=[str(self.species.pk)],
            capture={'local': '2025-01-01T00:30:00', 'precision': 'second', 'offset': 480})
        self.assertEqual(self.finish(photo, path, operation).relative_path, '多鸟种/2025/one.jpg')
        self.assertEqual(photo.confirmed_species.count(), 2)
        self.assertEqual(ManagedOriginal.objects.count(), 1)
        self.assertEqual(len(list(self.archive.rglob('*.jpg'))), 1)

    def test_catalog_rename_keeps_directory(self):
        photo, path, operation = self.confirmed()
        first = self.finish(photo, path, operation)
        self.species.refresh_from_db()
        self.species.chinese_name = '改后名称'
        self.species.save(update_fields=['chinese_name'])
        photo, path, operation = self.confirmed(name='two.jpg', color='blue')
        second = self.finish(photo, path, operation)
        self.assertEqual(first.relative_path.split('/')[0], second.relative_path.split('/')[0])
        self.assertEqual(second.relative_path, '白鹭/未知年份/two.jpg')

    def test_smaller_filesystem_name_limit_and_collision(self):
        photo, path, operation = self.local_confirmed(['鸟'*120], name='长'*70+'.jpg')
        real_fpathconf = os.fpathconf
        def small_limit(fd, key):
            return 80 if key == 'PC_NAME_MAX' else real_fpathconf(fd, key)
        with mock.patch('archive.services.archiving.os.fpathconf', side_effect=small_limit):
            # Occupy the preallocated safe path with identical foreign bytes.
            from archive.services.archiving import bind_archive_roots, ensure_archive_directory
            bind_archive_roots(operation)
            fd = open_root(self.archive)
            try:
                directory = ensure_archive_directory(operation, root_fd=fd)
            finally:
                os.close(fd)
            parent = self.archive/directory/'未知年份'; parent.mkdir(parents=True)
            foreign = parent/operation.retry_context['allocated_basename']
            foreign.write_bytes(path.read_bytes()); before = foreign.read_bytes()
            managed = self.finish(photo, path, operation)
        self.assertEqual(foreign.read_bytes(), before)
        self.assertNotEqual(managed.relative_path, str(foreign.relative_to(self.archive)))
        for part in managed.relative_path.split('/'):
            self.assertLessEqual(len(part.encode()), 80)
        self.assertIn('next_photo_id', operation.retry_context)
        self.assertGreater(operation.retry_context['collision_counter'], 0)

    def test_safe_original_basename_is_not_renamed(self):
        photo, path, operation = self.local_confirmed(['小鸟'], name='原来的照片 01.JPG')
        self.assertEqual(self.finish(photo, path, operation).actual_basename, '原来的照片 01.JPG')

    def test_catalog_controls_and_unsafe_filename_are_sanitized(self):
        self.species.chinese_name = '点\u200b名/鸟'
        self.species.save(update_fields=['chinese_name'])
        photo, path, operation = self.confirmed(name='CON.bad?.jpg')
        managed = self.finish(photo, path, operation)
        self.assertEqual(managed.relative_path.split('/')[0], '点_名_鸟')
        self.assertTrue(managed.actual_basename.endswith('.jpg'))
        self.assertNotIn('?', managed.actual_basename)
        self.assertNotEqual(managed.actual_basename.casefold().split('.')[0], 'con')
