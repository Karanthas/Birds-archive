import uuid
from datetime import timedelta

from django.utils import timezone
from archive.models import Photo, Species, ManagementOperation, ManagementPreview, ManagementResult
from archive.services.management import ManagementConflict
from archive.services.management_batch import (build_management_preview, management_token,
    revise_management_preview, submit_management_preview)
from archive.tests.test_download import DownloadFixture


class RestoreBatchTests(DownloadFixture):
    def pair(self):
        from archive.services.management import accept_management
        photos=[]
        for name,color in [('first.jpg','red'),('second.jpg','blue')]:
            photo,_,_=self.completed(name,color); photo.refresh_from_db()
            accept_management(photo.pk,photo.metadata_version,uuid.uuid4(),'trash',{})
            self.run_worker(); photo.refresh_from_db(); photos.append(photo)
        return photos

    def test_partial_failure_original_retry_replay_and_readonly(self):
        from unittest import mock
        from archive.services import management
        from archive.services.management_metadata import set_personal_metadata
        from archive.services.gallery import query_gallery
        first,second=self.pair()
        preview=build_management_preview('restore',[str(first.pk),str(second.pk)],{})
        token=management_token(preview); results=submit_management_preview(preview.pk,token)
        self.assertEqual([row['status'] for row in results],['accepted','accepted'])
        self.assertEqual(submit_management_preview(preview.pk,token),results)
        with self.assertRaises(ManagementConflict):
            set_personal_metadata(first.pk,first.metadata_version,uuid.uuid4(),{'notes':'not allowed'})
        publish=management._publish
        def fail_one(operation,root):
            if operation.photo_id==second.pk: raise OSError('isolated failure')
            return publish(operation,root)
        with mock.patch('archive.services.management._publish',side_effect=fail_one):
            self.run_worker(); self.run_worker()
        first.refresh_from_db(); second.refresh_from_db()
        self.assertIsNone(first.trashed_at); self.assertIsNotNone(second.trashed_at)
        self.assertEqual(query_gallery().remaining_count,1)
        page=self.client.get(f'/management/{preview.pk}/')
        self.assertContains(page,'已恢复'); self.assertContains(page,'恢复未完成')
        self.assertContains(page,f'data-restored-photo="{first.pk}"')
        self.assertNotContains(page,f'data-restored-photo="{second.pk}"')
        self.assertEqual(submit_management_preview(preview.pk,token),results)
        operation=ManagementOperation.objects.get(photo=second,kind='restore')
        management.retry_management(second.pk,operation.pk,uuid.uuid4()); self.run_worker()
        second.refresh_from_db(); self.assertIsNone(second.trashed_at)
        self.assertEqual(ManagementOperation.objects.filter(kind='restore').count(),2)

    def test_frozen_tombstone_version_tamper_scope(self):
        first,second=self.pair()
        preview=build_management_preview('restore',[str(first.pk),str(second.pk)],{})
        token=management_token(preview)
        Photo.objects.filter(pk=first.pk).update(metadata_version=99)
        self.assertEqual([row['status'] for row in submit_management_preview(preview.pk,token)],['skipped','accepted'])
        self.assertFalse(ManagementOperation.objects.filter(kind='restore',photo=first).exists())
        rows=preview.frozen_rows; rows[0]['photo_id']=str(uuid.uuid4())
        ManagementPreview.objects.filter(pk=preview.pk).update(frozen_rows=rows)
        with self.assertRaises(ManagementConflict): submit_management_preview(preview.pk,token)

    def test_native_confirmation_invalid_pending_and_separate_selection(self):
        first,second=self.pair()
        page=self.client.get('/?view=trash')
        self.assertContains(page,'data-management-scope="trash"')
        response=self.client.post('/management/preview/',{'photo_ids':[str(first.pk),str(second.pk)],'action':'restore','choose':'1'})
        self.assertEqual(response.status_code,303)
        page=self.client.get(response.url); self.assertContains(page,'明确选择 2 张')
        self.assertContains(page,'回收前位置'); self.assertContains(page,'确认恢复以上照片')
        preview=ManagementPreview.objects.get(); token=management_token(preview)
        self.assertEqual(self.client.post(f'/management/{preview.pk}/submit/',{'token':'bad'}).status_code,409)
        self.assertContains(self.client.post(f'/management/{preview.pk}/submit/',{'token':token}),'等待恢复')
        another=build_management_preview('restore',[str(first.pk)],{})
        self.assertTrue(another.frozen_rows[0]['reason'])
        with self.assertRaises(ValueError): build_management_preview('restore',[str(first.pk)],{'path':'foreign'})


class TrashBatchTests(DownloadFixture):
    def test_frozen_scope_partial_conflict_replay_and_normal_counts(self):
        from archive.services.gallery import query_gallery
        from archive.services.confirmation import confirmed_photos_for_species
        first, _, _ = self.completed()
        second, _, _ = self.completed('second.jpg','blue')
        ids = [str(first.pk),str(second.pk)]
        collection = query_gallery()
        preview=build_management_preview('trash',ids,{})
        token=management_token(preview)
        self.assertEqual(ManagementOperation.objects.count(),0)
        Photo.objects.filter(pk=first.pk).update(metadata_version=99)
        result=submit_management_preview(preview.pk,token)
        self.assertEqual([row['status'] for row in result],['skipped','accepted'])
        self.assertEqual(submit_management_preview(preview.pk,token),result)
        self.assertEqual(ManagementOperation.objects.count(),1)
        self.assertEqual(confirmed_photos_for_species(self.species.pk).count(),1)
        self.assertEqual(query_gallery(cursor=collection.cursor).remaining_count,1)
        self.run_worker()
        self.assertContains(self.client.get(f'/management/{preview.pk}/'),'已回收')
        self.assertEqual(submit_management_preview(preview.pk,token),result)
        self.assertEqual(ManagementOperation.objects.count(),1)

    def test_signed_revision_empty_invalid_and_no_path_input(self):
        photo, _, _ = self.completed()
        ids=[str(photo.pk),str(uuid.uuid4())]
        preview=build_management_preview('trash',ids,{})
        token=management_token(preview)
        with self.assertRaises(ManagementConflict): submit_management_preview(preview.pk,token+'bad')
        revised=revise_management_preview(preview.pk,token,action='trash',ids=ids[1:],payload={})
        with self.assertRaises(ManagementConflict): submit_management_preview(preview.pk,token)
        result=submit_management_preview(revised.pk,management_token(revised))
        self.assertEqual([row['status'] for row in result],['skipped'])
        self.assertEqual(ManagementOperation.objects.count(),0)
        for values,payload in [([],{}),([str(photo.pk)],{'path':'foreign'})]:
            with self.assertRaises(ValueError): build_management_preview('trash',values,payload)
        preview=build_management_preview('trash',[str(photo.pk)],{})
        token=management_token(preview)
        rows=preview.frozen_rows; rows[0]['metadata_version']=999
        ManagementPreview.objects.filter(pk=preview.pk).update(frozen_rows=rows)
        with self.assertRaises(ManagementConflict): submit_management_preview(preview.pk,token)

    def test_native_http_thumbnail_count_submit_and_all_invalid(self):
        photo, _, _ = self.completed()
        self.assertContains(self.client.get('/'),'核对批量移入回收站')
        response=self.client.post('/management/preview/',{'photo_ids':[str(photo.pk)],'choose':'1','action':'trash'})
        self.assertEqual(response.status_code,303)
        preview=ManagementPreview.objects.get()
        page=self.client.get(response.url)
        self.assertContains(page,'明确选择 1 张')
        self.assertContains(page,'可以恢复')
        self.assertContains(page,f'/media/{photo.pk}/thumb/')
        self.assertEqual(ManagementOperation.objects.count(),0)
        self.assertEqual(self.client.post(f'/management/{preview.pk}/submit/',{'token':'bad'}).status_code,409)
        response=self.client.post(f'/management/{preview.pk}/submit/',{'token':management_token(preview)})
        self.assertContains(response,'等待移入回收站')
        self.assertContains(self.client.get('/?view=trash'),'共 1 张')
        response=self.client.post('/management/preview/',{'action':'trash','photo_ids':[str(photo.pk)]})
        self.assertContains(self.client.get(response.url),'没有可受理项')
        self.assertEqual(ManagementOperation.objects.count(),1)


class PersonalBatchTests(DownloadFixture):
    def test_partial_conflict_replay_and_explicit_clear(self):
        first, _, _ = self.completed()
        second, _, _ = self.completed('second.jpg','blue')
        ids = [str(first.pk),str(second.pk)]
        preview = build_management_preview('set_favorite',ids,{'favorite':True})
        Photo.objects.filter(pk=first.pk).update(metadata_version=99)
        token = management_token(preview)
        results = submit_management_preview(preview.pk,token)
        self.assertEqual([row['status'] for row in results],['skipped','completed'])
        self.assertEqual(submit_management_preview(preview.pk,token),results)
        self.assertFalse(Photo.objects.get(pk=first.pk).favorite)
        self.assertTrue(Photo.objects.get(pk=second.pk).favorite)
        self.assertEqual(ManagementOperation.objects.count(),0)
        for action,payload in [('set_rating',{'rating':5}),('set_rating',{'rating':None}),
                ('set_location',{'user_location':'  江苏  '}),('set_location',{'user_location':''}),
                ('set_favorite',{'favorite':False})]:
            with self.subTest(action=action,payload=payload):
                preview = build_management_preview(action,ids,payload)
                results = submit_management_preview(preview.pk,management_token(preview))
                self.assertTrue(all(row['status']=='completed' for row in results))
                self.assertEqual(submit_management_preview(preview.pk,management_token(preview)),results)
        first.refresh_from_db(); second.refresh_from_db()
        self.assertEqual((first.favorite,first.rating,first.user_location),(False,None,''))
        self.assertEqual((second.favorite,second.rating,second.user_location),(False,None,''))

    def test_forbidden_fields_lifecycle_and_correction_allowed(self):
        from archive.services.management import accept_management
        photo, _, _ = self.completed(); photo.refresh_from_db()
        for action,payload in [('notes',{'notes':'x'}),('capture',{'capture':{}}),
                ('set_location',{'user_location':'x','notes':'x'}),('set_rating',{'rating':True}),
                ('set_favorite',{'favorite':'toggle'})]:
            with self.subTest(action=action),self.assertRaises(ValueError):
                build_management_preview(action,[str(photo.pk)],payload)
        accept_management(photo.pk,photo.metadata_version,uuid.uuid4(),'capture',
            {'mode':'manual','capture':{'local':'2038-01-01','precision':'date','offset':None}})
        preview=build_management_preview('set_rating',[str(photo.pk)],{'rating':3})
        self.assertEqual(submit_management_preview(preview.pk,management_token(preview))[0]['status'],'completed')
        preview=build_management_preview('set_favorite',[str(photo.pk)],{'favorite':True})
        Photo.objects.filter(pk=photo.pk).update(trash_requested_at=timezone.now())
        self.assertEqual(submit_management_preview(preview.pk,management_token(preview))[0]['status'],'skipped')

    def test_personal_http_preview_target_revise_and_rejection(self):
        photo, _, _ = self.completed()
        data={'photo_ids':[str(photo.pk)],'action':'set_location','user_location':'<广东>'}
        response=self.client.post('/management/preview/',data)
        self.assertEqual(response.status_code,303)
        preview=ManagementPreview.objects.get()
        page=self.client.get(response.url)
        self.assertContains(page,'明确目标：&lt;广东&gt;')
        self.assertContains(page,'value="set_location" selected')
        response=self.client.post(f'/management/{preview.pk}/revise/', {**data,
            'token':management_token(preview),'action':'set_rating','rating':''})
        self.assertEqual(response.status_code,303); preview.refresh_from_db()
        self.assertEqual(preview.action,'set_rating')
        response=self.client.post(f'/management/{preview.pk}/submit/',{'token':management_token(preview)})
        self.assertContains(response,'已完成')
        self.assertContains(response,'未评分（清除）')
        for action in ('notes','capture','set_rating'):
            response=self.client.post('/management/preview/',{**data,'action':action,'notes':'forbidden'})
            self.assertEqual(response.status_code,400)


class SpeciesBatchTests(DownloadFixture):
    def selection(self):
        first, _, _ = self.completed()
        second, _, _ = self.completed('two.jpg', 'blue')
        return [str(first.pk), str(second.pk)]

    def test_freeze_no_identity_or_file_side_effects_partial_cas_and_replay(self):
        ids = self.selection()
        before = sorted(str(path.relative_to(self.archive)) for path in self.archive.rglob('*'))
        preview = build_management_preview('species', ids, {'mode':'add','local_names':['  New   Bird ']})
        self.assertFalse(Species.objects.filter(local_key='new bird').exists())
        self.assertEqual(before, sorted(str(path.relative_to(self.archive)) for path in self.archive.rglob('*')))
        self.assertTrue(all(row['target_path'].startswith('多鸟种/') for row in preview.frozen_rows))
        Photo.objects.filter(pk=ids[0]).update(metadata_version=99)
        token = management_token(preview)
        results = submit_management_preview(preview.pk, token)
        self.assertEqual([row['status'] for row in results], ['skipped','accepted'])
        self.assertEqual(submit_management_preview(preview.pk, token), results)
        self.assertEqual(ManagementOperation.objects.count(), 1)
        self.assertEqual(ManagementResult.objects.count(), 2)
        self.assertEqual(Photo.objects.get(pk=ids[1]).confirmed_species.count(), 1)
        self.run_worker()
        photo = Photo.objects.get(pk=ids[1])
        self.assertEqual(photo.confirmed_species.count(), 2)
        self.assertContains(self.client.get(f'/management/{preview.pk}/'), '更正已完成')

    def test_signature_revision_expiry_scope_and_receipt_digest(self):
        ids = self.selection()
        preview = build_management_preview('species', ids, {'mode':'replace','species_ids':[str(self.species.pk)]})
        token = management_token(preview)
        with self.assertRaises(ManagementConflict):
            submit_management_preview(preview.pk, token+'tamper')
        revised = revise_management_preview(preview.pk, token, action='species', ids=ids[:1],
            payload={'mode':'replace','local_names':['独立本地名']})
        with self.assertRaises(ManagementConflict):
            submit_management_preview(preview.pk, token)
        results = submit_management_preview(preview.pk, management_token(revised))
        self.assertEqual(len(results), 1)
        self.assertFalse(ManagementOperation.objects.filter(photo_id=ids[1]).exists())
        receipt = ManagementResult.objects.get(preview=preview)
        receipt.payload_digest = '0'*64; receipt.save(update_fields=['payload_digest'])
        with self.assertRaises(ManagementConflict):
            submit_management_preview(preview.pk, management_token(revised))
        ManagementPreview.objects.filter(pk=preview.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        with self.assertRaises(ManagementConflict):
            submit_management_preview(preview.pk, management_token(revised))

    def test_modes_empty_selection_and_all_invalid_have_no_operations(self):
        photo, _, _ = self.completed(); photo.refresh_from_db()
        for mode in ('replace', 'add', 'remove'):
            with self.subTest(mode=mode):
                preview = build_management_preview('species', [str(photo.pk)],
                    {'mode':mode,'species_ids':[str(self.species.pk)]})
                result = submit_management_preview(preview.pk, management_token(preview))[0]
                if mode == 'remove':
                    self.assertEqual(result['status'], 'skipped')
                    self.assertIn('最后一个', result['reason'])
                else:
                    self.assertEqual(result['status'], 'accepted')
                    self.assertTrue(preview.frozen_rows[0]['target_path'].startswith('白鹭/'))
                    self.run_worker(); photo.refresh_from_db()
                    self.assertEqual(photo.confirmed_species.count(), 1)
        with self.assertRaises(ValueError):
            build_management_preview('species', [], {'mode':'replace','species_ids':[str(self.species.pk)]})
        preview = build_management_preview('species', [str(uuid.uuid4())], {'mode':'replace','classification':'unidentified'})
        self.assertEqual(submit_management_preview(preview.pk, management_token(preview))[0]['status'], 'skipped')

    def test_ordinary_http_csrf_no_store_draft_and_preview_tamper(self):
        from django.test import Client
        photo, _, _ = self.completed()
        data = {'photo_ids':[str(photo.pk)],'mode':'replace','classification':'identified','local_names':['<本地鸟>']}
        self.assertContains(self.client.get(f'/photos/{photo.pk}/'), '核对鸟种更正')
        chooser = self.client.post('/management/preview/', {**data, 'choose':'1'})
        self.assertContains(chooser, str(photo.pk))
        self.assertEqual(ManagementPreview.objects.count(), 0)
        search = self.client.post('/management/preview/', {**data, 'draft_search':'1', 'q':'白鹭'})
        self.assertContains(search, '&lt;本地鸟&gt;')
        self.assertContains(search, str(self.species.pk))
        strict = Client(enforce_csrf_checks=True)
        self.assertEqual(strict.post('/management/preview/', data).status_code, 403)
        response = self.client.post('/management/preview/', data)
        self.assertEqual(response.status_code, 303)
        preview = ManagementPreview.objects.get()
        page = self.client.get(response.url)
        self.assertContains(page, '&lt;本地鸟&gt;')
        self.assertIn('no-store', page['Cache-Control'])
        self.assertEqual(ManagementOperation.objects.count(), 0)
        bad = self.client.post(f'/management/{preview.pk}/submit/', {'token':'bad'})
        self.assertEqual(bad.status_code, 409)
        self.assertEqual(self.client.post(f'/management/{preview.pk}/submit/',
            {'token':management_token(preview)}).status_code, 200)
        self.assertEqual(ManagementOperation.objects.count(), 1)

    def test_tampered_stored_rows_and_directory_change_reject(self):
        photo, _, _ = self.completed()
        preview = build_management_preview('species', [str(photo.pk)], {'mode':'replace','local_names':['new']})
        token = management_token(preview)
        row = preview.frozen_rows[0]
        row['target']['directory'] = 'forged'
        ManagementPreview.objects.filter(pk=preview.pk).update(frozen_rows=[row])
        with self.assertRaises(ManagementConflict):
            submit_management_preview(preview.pk, token)
        self.assertEqual(ManagementOperation.objects.count(), 0)
