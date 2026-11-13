import uuid
from unittest import mock

from archive.models import Photo, Species, ManagementOperation, ArchiveOperation
from archive.services import management
from archive.tests.test_download import DownloadFixture


class SpeciesCorrectionTests(DownloadFixture):
    def test_set_transitions_formal_switch_same_directory_and_bytes(self):
        photo, target, managed = self.completed()
        photo.refresh_from_db()
        history = ArchiveOperation.objects.get(photo=photo).species_ids_snapshot
        original = target.read_bytes()
        extra = Species.objects.create(source_kind='local', local_name='翠鸟', local_key='翠鸟', search_key='翠鸟')
        third = Species.objects.create(source_kind='local', local_name='夜鹭', local_key='夜鹭', search_key='夜鹭')
        cases = [('add', [extra.pk], 2, '多鸟种'), ('add', [third.pk], 3, '多鸟种'),
            ('remove', [extra.pk], 2, '多鸟种'), ('replace', [third.pk], 1, '夜鹭')]
        last_path = None
        for mode, ids, count, directory in cases:
            with self.subTest(mode=mode, count=count):
                old_set = set(photo.confirmed_species.values_list('species_id', flat=True))
                operation = management.accept_management(photo.pk, photo.metadata_version, uuid.uuid4(), 'species',
                    {'mode':mode, 'classification':'identified', 'species_ids':[str(i) for i in ids]})
                def observe(stage):
                    if stage == 'registered':
                        self.assertEqual(set(photo.confirmed_species.values_list('species_id', flat=True)), old_set)
                        response = self.client.get(f'/photos/{photo.pk}/download/')
                        self.assertEqual(response.status_code, 200)
                        response.close()
                with mock.patch('archive.services.management.management_fault_hook', side_effect=observe):
                    self.run_worker()
                operation.refresh_from_db(); photo.refresh_from_db(); managed.refresh_from_db()
                self.assertEqual(operation.status, 'succeeded')
                self.assertEqual(photo.confirmed_species.count(), count)
                self.assertTrue(managed.relative_path.startswith(directory+'/'))
                if count == 3 or (mode == 'remove' and count == 2):
                    self.assertEqual(managed.relative_path, last_path)
                last_path = managed.relative_path
                self.assertEqual((self.archive/managed.relative_path).read_bytes(), original)
                self.assertEqual(len(list(self.archive.rglob('*.jpg'))), 1)
        self.assertEqual(ArchiveOperation.objects.get(photo=photo).species_ids_snapshot, history)

    def test_local_identity_reuse_explicit_unknown_and_empty_rejection(self):
        photo, target, managed = self.completed(); photo.refresh_from_db()
        payload = {'mode':'replace', 'local_names':['  Custom   Bird ', 'custom bird']}
        request = uuid.uuid4()
        operation = management.accept_management(photo.pk, photo.metadata_version, request, 'species', payload)
        self.assertEqual(management.accept_management(photo.pk, 1, request, 'species', payload).pk, operation.pk)
        self.run_worker(); photo.refresh_from_db()
        local = Species.objects.get(local_key='custom bird')
        self.assertEqual(photo.confirmed_species.get().species_id, local.pk)
        with self.assertRaisesRegex(ValueError, '最后一个'):
            management.accept_management(photo.pk, photo.metadata_version, uuid.uuid4(), 'species',
                {'mode':'remove','species_ids':[str(local.pk)]})
        operation = management.accept_management(photo.pk, photo.metadata_version, uuid.uuid4(), 'species',
            {'mode':'replace','local_names':['CUSTOM bird']})
        self.run_worker(); photo.refresh_from_db()
        self.assertEqual(Species.objects.filter(local_key='custom bird').count(), 1)
        unknown = {'mode':'replace','classification':'unidentified'}
        operation = management.accept_management(photo.pk, photo.metadata_version, uuid.uuid4(), 'species', unknown)
        self.run_worker(); photo.refresh_from_db(); managed.refresh_from_db()
        self.assertEqual(photo.current_classification, 'unidentified')
        self.assertEqual(photo.confirmed_species.count(), 0)
        self.assertTrue(managed.relative_path.startswith('无法确定/'))
        response = self.client.post('/management/preview/', {'photo_ids':[str(photo.pk)],
            'mode':'replace', 'classification':'unidentified', 'local_names':['']})
        self.assertEqual(response.status_code, 303)
        self.assertContains(self.client.get(response.url), '无法确定/')
        operation = management.accept_management(photo.pk, photo.metadata_version, uuid.uuid4(), 'species',
            {'mode':'replace','local_names':['白鹭']})
        self.run_worker(); photo.refresh_from_db(); managed.refresh_from_db()
        local_homonym = Species.objects.get(local_key='白鹭')
        self.assertNotEqual(local_homonym.pk, self.species.pk)
        self.assertEqual(photo.confirmed_species.get().species_id, local_homonym.pk)
        self.assertTrue(managed.relative_path.startswith('白鹭_'+local_homonym.pk.hex[:8]+'/'))

    def test_http_owned_fields_and_old_generation(self):
        photo, target, managed = self.completed(); photo.refresh_from_db()
        data = {'management_action':'species','metadata_version':photo.metadata_version,
            'request_id':str(uuid.uuid4()),'mode':'replace','classification':'identified','local_names':['新鸟名']}
        self.assertEqual(self.client.post(f'/photos/{photo.pk}/', data).status_code, 303)
        operation = ManagementOperation.objects.get(photo=photo)
        Photo.objects.filter(pk=photo.pk).update(user_location='后来地点', notes='后来备注', favorite=True)
        self.run_worker(); photo.refresh_from_db()
        self.assertEqual((photo.user_location,photo.notes,photo.favorite), ('后来地点','后来备注',True))
        self.assertEqual(self.client.post(f'/photos/{photo.pk}/', {**data,'request_id':str(uuid.uuid4())}).status_code, 409)
        stale = management.accept_management(photo.pk, photo.metadata_version, uuid.uuid4(), 'species',
            {'mode':'replace','local_names':['旧意图']})
        Photo.objects.filter(pk=photo.pk).update(management_generation=stale.generation+1)
        self.assertFalse(management.process_management(stale.pk))
        self.assertEqual(photo.confirmed_species.get().species.local_name, '新鸟名')
