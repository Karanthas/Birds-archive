"""Synthetic originals only: acceptance, current-FD reads and lifecycle guards."""
import os
import uuid
from unittest import mock

from django.utils import timezone
from archive.models import Photo, ManagementOperation, PhotoSpecies, RecognitionRun, Job
from archive.services.management import (accept_management, ManagementConflict,
    trash_confirmation, accept_confirmed_trash)
from archive.tests.test_download import DownloadFixture


class RestoreTests(DownloadFixture):
    def trashed(self):
        photo, original, managed = self.completed()
        Photo.objects.filter(pk=photo.pk).update(favorite=True, rating=4, notes='恢复保留', user_location='江苏')
        photo.refresh_from_db()
        accept_confirmed_trash(photo.pk, trash_confirmation(photo)); self.run_worker()
        photo.refresh_from_db(); managed.refresh_from_db()
        return photo, original, managed

    def test_collision_same_hash_foreign_replay_and_metadata_download(self):
        from archive.services.management import restore_confirmation, accept_confirmed_restore
        photo, old, managed = self.trashed()
        raw = (self.archive/managed.relative_path).read_bytes()
        old.write_bytes(raw)  # Even identical bytes in an unrelated inode are foreign.
        foreign_inode = old.stat().st_ino
        page = self.client.get(f'/photos/{photo.pk}/?management_action=restore')
        self.assertContains(page, '本次明确恢复 1 张')
        token = page.context['restore_token']
        self.assertEqual(self.client.post(f'/photos/{photo.pk}/', {'management_action':'restore', 'restore_token':token}).status_code,303)
        operation = ManagementOperation.objects.get(kind='restore')
        self.assertEqual(self.client.post(f'/photos/{photo.pk}/', {'management_action':'restore', 'restore_token':token}).status_code,303)
        self.assertEqual(self.client.post(f'/photos/{photo.pk}/', {'management_action':'personal',
            'personal_field':'notes','notes':'bad','metadata_version':photo.metadata_version,'request_id':uuid.uuid4()}).status_code,409)
        self.run_worker(); photo.refresh_from_db(); managed.refresh_from_db(); operation.refresh_from_db()
        actual = self.archive/managed.relative_path
        self.assertNotEqual(actual,old); self.assertIn(operation.pk.hex[:8], actual.name)
        self.assertEqual((old.read_bytes(),old.stat().st_ino),(raw,foreign_inode))
        self.assertEqual(actual.read_bytes(),raw); self.assertIsNone(photo.trashed_at); self.assertIsNone(photo.trash_requested_at)
        self.assertEqual((photo.favorite,photo.rating,photo.notes,photo.user_location),(True,4,'恢复保留','江苏'))
        self.assertEqual(PhotoSpecies.objects.filter(photo=photo).count(),1)
        self.assert_download(photo,actual,managed)
        self.assertEqual(accept_confirmed_restore(photo.pk,token).pk,operation.pk)
        self.run_worker(); self.assertEqual(len(list(old.parent.glob('*.jpg'))),2)
        self.assertNotContains(self.client.get('/?view=trash'),str(photo.pk))

    def test_registered_failure_keeps_tombstone_target_and_resumes_original(self):
        from archive.services.management import restore_confirmation, accept_confirmed_restore, retry_management
        from archive.services.gallery import query_gallery
        photo, old, managed = self.trashed(); trash_path=self.archive/managed.relative_path
        old.write_bytes(b'foreign occupant')
        operation=accept_confirmed_restore(photo.pk,restore_confirmation(photo))
        with mock.patch('archive.services.management.management_fault_hook', side_effect=lambda stage:
                (_ for _ in ()).throw(OSError('synthetic registered stop')) if stage=='registered' else None):
            self.run_worker()
        operation.refresh_from_db(); photo.refresh_from_db(); managed.refresh_from_db()
        fixed=operation.target_relative_path
        self.assertEqual((operation.stage,operation.status),('registered','blocked'))
        self.assertIsNotNone(photo.trashed_at); self.assertTrue(trash_path.exists()); self.assertEqual(query_gallery().remaining_count,0)
        self.assert_download(photo,self.archive/managed.relative_path,managed)
        retry_management(photo.pk,operation.pk,uuid.uuid4()); self.run_worker()
        operation.refresh_from_db(); photo.refresh_from_db()
        self.assertEqual(operation.target_relative_path,fixed); self.assertEqual(operation.status,'succeeded')
        self.assertFalse(trash_path.exists()); self.assertIsNone(photo.trashed_at)
        self.assertEqual(query_gallery().remaining_count,1); self.assertEqual(old.read_bytes(),b'foreign occupant')

    def test_restore_requires_completed_trash_and_signature_version_generation(self):
        from archive.services.management import restore_confirmation, accept_confirmed_restore
        photo, _, _ = self.completed(); photo.refresh_from_db()
        with self.assertRaises(ManagementConflict): restore_confirmation(photo)
        accept_confirmed_trash(photo.pk,trash_confirmation(photo)); photo.refresh_from_db()
        with self.assertRaises(ManagementConflict): restore_confirmation(photo)
        self.run_worker(); photo.refresh_from_db(); token=restore_confirmation(photo)
        with self.assertRaises(ManagementConflict): accept_confirmed_restore(photo.pk,token+'bad')
        Photo.objects.filter(pk=photo.pk).update(metadata_version=99)
        with self.assertRaises(ManagementConflict): accept_confirmed_restore(photo.pk,token)

    def test_fixed_suffix_retry_blocks_new_foreign_occupant_without_reallocating(self):
        from archive.services.management import restore_confirmation, accept_confirmed_restore, retry_management
        photo,old,managed=self.trashed()
        old.write_bytes(b'first foreign occupant')
        operation=accept_confirmed_restore(photo.pk,restore_confirmation(photo))
        link=os.link
        def stop_suffix(source,target,**kwargs):
            if target!=old.name: raise OSError('isolated stop before suffix publish')
            return link(source,target,**kwargs)
        with mock.patch('archive.services.management.os.link',side_effect=stop_suffix): self.run_worker()
        operation.refresh_from_db(); fixed=operation.target_relative_path
        self.assertEqual((operation.stage,operation.status,operation.collision_counter),('prepared','blocked',1))
        occupied=self.archive/fixed; occupied.write_bytes(b'second foreign occupant')
        retry_management(photo.pk,operation.pk,uuid.uuid4()); self.run_worker()
        operation.refresh_from_db(); photo.refresh_from_db()
        self.assertEqual((operation.target_relative_path,operation.collision_counter,operation.error_code),(fixed,1,'target_conflict'))
        self.assertIsNotNone(photo.trashed_at); self.assertEqual(occupied.read_bytes(),b'second foreign occupant')
        occupied.unlink()  # Only this test's synthetic occupant is removed.
        retry_management(photo.pk,operation.pk,uuid.uuid4()); self.run_worker()
        operation.refresh_from_db()
        self.assertEqual((operation.status,operation.target_relative_path),('succeeded',fixed))
        self.assertEqual(old.read_bytes(),b'first foreign occupant')

    def test_collision_allocation_checkpoint_interrupt_retries_same_fixed_suffix(self):
        from archive.services import management
        photo,old,managed=self.trashed()
        trash_path=self.archive/managed.relative_path
        raw=trash_path.read_bytes()
        fields=('favorite','rating','notes','user_location','current_capture','manual_capture','exif_raw')
        metadata={field:getattr(photo,field) for field in fields}
        species=list(photo.confirmed_species.values_list('species_id',flat=True))
        old.write_bytes(b'foreign original path occupant')
        foreign_inode=old.stat().st_ino
        operation=management.accept_confirmed_restore(photo.pk,management.restore_confirmation(photo))
        request_id=operation.request_id
        save=management._save
        checkpoints=[]
        def interrupt_allocation(current,**values):
            save(current,**values)
            if current.pk==operation.pk and values.get('collision_counter'):
                persisted=ManagementOperation.objects.get(pk=current.pk)
                # The first durable collision write must include the derived path
                # and drop the old target's publication identity in the same row.
                self.assertEqual(persisted.collision_counter,1)
                self.assertNotEqual(persisted.target_relative_path,str(old.relative_to(self.archive)))
                self.assertIn(current.pk.hex[:8],persisted.target_relative_path)
                self.assertNotIn('publish_identity',persisted.retry_context)
                checkpoints.append(persisted.target_relative_path)
                raise OSError('isolated interruption immediately after allocation commit')
        with mock.patch('archive.services.management._save',side_effect=interrupt_allocation):
            self.run_worker()
        operation.refresh_from_db(); photo.refresh_from_db()
        self.assertEqual(len(checkpoints),1)
        fixed=checkpoints[0]
        self.assertEqual((operation.stage,operation.status),('prepared','blocked'))
        self.assertIsNotNone(photo.trashed_at); self.assertTrue(trash_path.exists())
        self.assertEqual(trash_path.read_bytes(),raw)
        # The same case also retains the existing fixed-target/new-occupant guard.
        occupied=self.archive/fixed
        occupied.write_bytes(b'foreign fixed suffix occupant')
        management.retry_management(photo.pk,operation.pk,uuid.uuid4()); self.run_worker()
        operation.refresh_from_db()
        self.assertEqual((operation.target_relative_path,operation.collision_counter,operation.error_code),
            (fixed,1,'target_conflict'))
        self.assertEqual(occupied.read_bytes(),b'foreign fixed suffix occupant')
        occupied.unlink()  # Only the synthetic occupant created by this case.
        management.retry_management(photo.pk,operation.pk,uuid.uuid4()); self.run_worker()
        operation.refresh_from_db(); photo.refresh_from_db(); managed.refresh_from_db()
        self.assertEqual((operation.status,operation.target_relative_path,operation.request_id),('succeeded',fixed,request_id))
        self.assertEqual(ManagementOperation.objects.filter(photo=photo,kind='restore').count(),1)
        self.assertIsNone(photo.trashed_at); self.assertIsNone(photo.trash_requested_at)
        self.assertFalse(trash_path.exists()); self.assertEqual(managed.relative_path,fixed)
        self.assertEqual({field:getattr(photo,field) for field in fields},metadata)
        self.assertEqual(list(photo.confirmed_species.values_list('species_id',flat=True)),species)
        self.assertEqual((old.read_bytes(),old.stat().st_ino),(b'foreign original path occupant',foreign_inode))
        self.assertEqual(set(self.archive.rglob('*.jpg')),{old,self.archive/fixed})
        self.assertFalse(list(self.archive.rglob('*.tmp')))
        self.assert_download(photo,self.archive/fixed,managed)


class TrashLifecycleTests(DownloadFixture):
    def test_confirmation_acceptance_current_collection_and_completed_preservation(self):
        from archive.services.gallery import query_gallery
        from archive.services.confirmation import confirmed_photos_for_species
        photo, original, managed = self.completed(); photo.refresh_from_db()
        Photo.objects.filter(pk=photo.pk).update(favorite=True, rating=5, notes='<保留>', user_location='广东')
        photo.refresh_from_db()
        history = photo.archive_operation.capture_snapshot
        raw = original.read_bytes()
        collection = query_gallery()
        page = self.client.get(f'/photos/{photo.pk}/?management_action=trash')
        self.assertContains(page, '本次明确移入 1 张')
        self.assertContains(page, '可以恢复')
        self.assertEqual(ManagementOperation.objects.count(), 0)
        token = page.context['trash_token']
        response = self.client.post(f'/photos/{photo.pk}/', {'management_action':'trash','trash_token':token})
        self.assertEqual(response.status_code, 303)
        photo.refresh_from_db(); self.assertIsNotNone(photo.trash_requested_at); self.assertIsNone(photo.trashed_at)
        self.assertTrue(original.exists())
        self.assertEqual(query_gallery(cursor=collection.cursor).remaining_count, 0)
        self.assertEqual(confirmed_photos_for_species(self.species.pk).count(), 0)
        self.assertEqual(len(query_gallery().items), 0)
        self.assert_download(photo, original, managed)
        self.assertContains(self.client.get('/?view=trash'), '等待移入回收站')
        self.assertEqual(self.client.post(f'/photos/{photo.pk}/', {'management_action':'trash','trash_token':token}).status_code,303)
        self.assertEqual(ManagementOperation.objects.count(),1)
        self.run_worker()
        photo.refresh_from_db(); managed.refresh_from_db()
        target = self.archive/managed.relative_path
        self.assertTrue(managed.relative_path.startswith(f'.archive-trash/{photo.pk}/'))
        self.assertFalse(original.exists()); self.assertEqual(target.read_bytes(),raw)
        self.assertIsNotNone(photo.trashed_at)
        self.assertEqual((photo.favorite,photo.rating,photo.notes,photo.user_location),(True,5,'<保留>','广东'))
        self.assertEqual(photo.current_capture,history)
        self.assertEqual(PhotoSpecies.objects.filter(photo=photo).count(),1)
        self.assert_download(photo,target,managed)
        self.assertContains(self.client.get('/?view=trash'),'已回收')
        detail = self.client.get(f'/photos/{photo.pk}/?return=trash')
        self.assertContains(detail,'&lt;保留&gt;'); self.assertNotContains(detail,'name="personal_field"')

    def test_failed_move_keeps_readable_current_file_and_retry(self):
        photo, original, managed = self.completed(); photo.refresh_from_db()
        operation = accept_confirmed_trash(photo.pk,trash_confirmation(photo))
        with mock.patch('archive.services.management._publish',side_effect=OSError('synthetic')):
            self.run_worker()
        operation.refresh_from_db(); photo.refresh_from_db()
        self.assertEqual(operation.status,'blocked'); self.assertIsNone(photo.trashed_at)
        self.assert_download(photo,original,managed)
        self.assertContains(self.client.get('/?view=trash'),'移入回收站未完成')
        response=self.client.post(f'/photos/{photo.pk}/',{'management_action':'retry',
            'operation_id':str(operation.pk),'request_id':str(uuid.uuid4())})
        self.assertEqual(response.status_code,303)
        self.run_worker(); operation.refresh_from_db(); managed.refresh_from_db()
        self.assertEqual(operation.status,'succeeded')
        self.assert_download(photo,self.archive/managed.relative_path,managed)

    def test_signature_cas_generation_csrf_and_pending_correction(self):
        from django.test import Client
        photo, original, _ = self.completed(); photo.refresh_from_db()
        token=trash_confirmation(photo)
        for bad in ('',token+'bad'):
            self.assertEqual(self.client.post(f'/photos/{photo.pk}/',{'management_action':'trash','trash_token':bad}).status_code,409)
        strict=Client(enforce_csrf_checks=True)
        self.assertEqual(strict.post(f'/photos/{photo.pk}/',{'management_action':'trash','trash_token':token}).status_code,403)
        Photo.objects.filter(pk=photo.pk).update(metadata_version=photo.metadata_version+1)
        with self.assertRaises(ManagementConflict): accept_confirmed_trash(photo.pk,token)
        photo.refresh_from_db(); token=trash_confirmation(photo)
        Photo.objects.filter(pk=photo.pk).update(management_generation=1)
        with self.assertRaises(ManagementConflict): accept_confirmed_trash(photo.pk,token)
        photo.refresh_from_db()
        accept_management(photo.pk,photo.metadata_version,uuid.uuid4(),'capture',{'mode':'unknown','capture':None})
        self.assertEqual(self.client.get(f'/photos/{photo.pk}/?management_action=trash').status_code,409)
        with self.assertRaises(ManagementConflict): accept_confirmed_trash(photo.pk,trash_confirmation(photo))
        self.assertTrue(original.exists()); photo.refresh_from_db(); self.assertIsNone(photo.trash_requested_at)


class TrashGuardTests(DownloadFixture):
    def assert_read_only(self, photo):
        from archive.services.management_metadata import set_personal_metadata
        from archive.services.confirmation import confirm_photo, ConfirmationConflict
        from archive.services.management_batch import build_management_preview, submit_management_preview, management_token
        photo.refresh_from_db()
        for changes in ({'notes':'overwrite'},{'user_location':'overwrite'},{'favorite':True},{'rating':4}):
            with self.subTest(changes=changes), self.assertRaises(ManagementConflict):
                set_personal_metadata(photo.pk,photo.metadata_version,uuid.uuid4(),changes)
        for kind,payload in [('capture',{'mode':'unknown','capture':None}),
                ('species',{'mode':'replace','classification':'unidentified'}),('trash',{})]:
            with self.subTest(kind=kind),self.assertRaises(ManagementConflict):
                accept_management(photo.pk,photo.metadata_version,uuid.uuid4(),kind,payload)
        with self.assertRaises(ConfirmationConflict):
            confirm_photo(photo.pk,photo.metadata_version,uuid.uuid4(),'species',[str(self.species.pk)],[])
        for action,payload in [('set_favorite',{'favorite':True}),('set_rating',{'rating':5}),
                ('set_location',{'user_location':'x'}),('species',{'mode':'replace','classification':'unidentified'}),('trash',{})]:
            preview=build_management_preview(action,[str(photo.pk)],payload)
            self.assertEqual(submit_management_preview(preview.pk,management_token(preview))[0]['status'],'skipped')
        for field,value in [('notes','bad'),('user_location','bad'),('favorite','true'),('rating','5')]:
            response=self.client.post(f'/photos/{photo.pk}/',{'management_action':'personal','personal_field':field,
                field:value,'metadata_version':photo.metadata_version,'request_id':str(uuid.uuid4())},HTTP_ACCEPT='application/json')
            self.assertEqual(response.status_code,409)
            self.assertIn('error',response.json())
        for data in [{'management_action':'capture','mode':'unknown'},
                {'management_action':'species','mode':'replace','classification':'unidentified'},
                {'classification':'species','species_ids':[str(self.species.pk)]},
                {'draft_action':'recognition_retry'},{'draft_action':'recognition_rerun'}]:
            response=self.client.post(f'/photos/{photo.pk}/',{**data,'metadata_version':photo.metadata_version,
                'request_id':str(uuid.uuid4())})
            self.assertEqual(response.status_code,409)
        self.assertEqual(self.client.post(f'/photos/{photo.pk}/capture-time/',{'date':'2040-01-01'}).status_code,409)
        self.assertEqual(self.client.post('/metadata/location/',{'photo_ids':[str(photo.pk)],'filter':'all',
            'page':'1','location':'bad'}).status_code,409)

    def test_pending_completed_and_restoring_are_read_only_in_service_and_http(self):
        photo, _, _ = self.completed(); photo.refresh_from_db()
        accept_confirmed_trash(photo.pk,trash_confirmation(photo))
        self.assert_read_only(photo)
        self.run_worker(); photo.refresh_from_db()
        self.assert_read_only(photo)
        # Restore's retained tombstone is this plan's guard boundary; file restore is 04-07.
        Photo.objects.filter(pk=photo.pk).update(management_generation=photo.management_generation+1)
        ManagementOperation.objects.create(photo=photo,kind='restore',request_id=uuid.uuid4(),
            payload_digest='c'*64,generation=photo.management_generation+1,source_location_revision=1)
        self.assert_read_only(photo)
        photo.refresh_from_db(); self.assertEqual(photo.state,'archived')
        self.assertEqual((photo.favorite,photo.rating,photo.notes,photo.user_location),(False,None,'',''))

    def test_old_personal_confirmation_management_and_first_archive_cannot_revive(self):
        from archive.services.management_metadata import set_personal_metadata
        from archive.services.management import process_management, retry_management
        from archive.services.confirmation import confirm_photo, ConfirmationConflict
        from archive.services.archiving import process_archive
        photo, _, managed=self.completed(); photo.refresh_from_db()
        request_id=uuid.uuid4()
        set_personal_metadata(photo.pk,photo.metadata_version,request_id,{'notes':'preserved'})
        photo.refresh_from_db(); old_version=photo.metadata_version
        accept_confirmed_trash(photo.pk,trash_confirmation(photo)); self.run_worker()
        photo.refresh_from_db(); managed.refresh_from_db(); path=managed.relative_path
        with self.assertRaises(ManagementConflict):
            set_personal_metadata(photo.pk,old_version-1,request_id,{'notes':'preserved'})
        first=photo.archive_operation
        with self.assertRaises(ConfirmationConflict):
            confirm_photo(photo.pk,1,first.request_id,'species',[str(self.species.pk)],[])
        self.assertTrue(process_archive(first.job))
        old=ManagementOperation.objects.create(photo=photo,kind='capture',request_id=uuid.uuid4(),
            payload_digest='d'*64,generation=photo.management_generation,source_location_revision=0)
        with self.assertRaises(ManagementConflict): retry_management(photo.pk,old.pk,uuid.uuid4())
        self.assertFalse(process_management(old.pk)); old.refresh_from_db()
        self.assertEqual(old.status,'stale')
        photo.refresh_from_db(); managed.refresh_from_db()
        self.assertIsNotNone(photo.trashed_at); self.assertEqual(photo.notes,'preserved')
        self.assertEqual(managed.relative_path,path)

    def test_duplicate_import_and_old_recognition_publication_do_not_revive(self):
        from archive.services.intake import scan_once
        from archive.services.jobs import process_source
        from archive.services.paths import open_root
        from archive.services.recognition import request_rerun, retry_run, publish_result
        from archive.services.batch_review import build_preview, submit_preview, preview_token
        photo, _, managed=self.completed(); photo.refresh_from_db()
        run=RecognitionRun.objects.get(photo=photo)
        accept_confirmed_trash(photo.pk,trash_confirmation(photo)); self.run_worker()
        photo.refresh_from_db(); managed.refresh_from_db()
        original=self.archive/managed.relative_path
        duplicate=self.inbox/'duplicate.jpg'; duplicate.write_bytes(original.read_bytes())
        before=RecognitionRun.objects.count()
        scan_once()
        a,b=open_root(self.inbox),open_root(self.data)
        try:
            with mock.patch('archive.services.recognition.enqueue_import') as enqueue:
                for job in Job.objects.filter(kind='import',state='pending'):
                    self.assertTrue(process_source(job,a,b))
                enqueue.assert_not_called()
        finally:
            os.close(a); os.close(b)
        self.assertEqual(Photo.objects.count(),1); self.assertEqual(RecognitionRun.objects.count(),before)
        self.assertTrue(duplicate.exists())
        for service in (request_rerun,retry_run):
            with self.assertRaises(ValueError): service(photo.pk,photo.metadata_version,uuid.uuid4())
        self.assertFalse(publish_result(run.pk,[],artifact_id=uuid.uuid4()))
        preview=build_preview([str(photo.pk)],mode='uniform_species',species_ids=[str(self.species.pk)])
        result=submit_preview(preview.pk,preview_token(preview))
        self.assertEqual(result[0]['status'],'skipped')
        photo.refresh_from_db(); self.assertIsNotNone(photo.trash_requested_at); self.assertIsNotNone(photo.trashed_at)
        self.assertEqual(photo.state,'archived'); self.assert_download(photo,original,managed)
