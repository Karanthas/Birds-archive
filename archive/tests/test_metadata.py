import hashlib
import tempfile
import uuid
from pathlib import Path
from django.conf import settings
from django.contrib.auth.models import User
from django.test import TransactionTestCase, override_settings, Client
from archive.models import Photo, Job


class MetadataTests(TransactionTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='birds-metadata-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.inbox, self.data = self.root/'inbox', self.root/'data'
        self.inbox.mkdir(); self.data.mkdir()
        overrides = override_settings(BIRDS_INBOX_ROOT=str(self.inbox),BIRDS_DATA_ROOT=str(self.data),
                                      BIRDS_HANDOFF_MODE='published',BIRDS_PUBLISHED_CONTRACT=True)
        overrides.enable(); self.addCleanup(overrides.disable)

    def photo(self, number=1, **kwargs):
        return Photo.objects.create(sha256=f'{number:064x}',byte_size=100,preview_key='',**kwargs)

    def preview(self, photos, location='广东  深圳', **kwargs):
        return self.client.get('/metadata/location/', {'photo_ids':[str(p.pk) for p in photos],
                                                       'location':location,'filter':'all','page':'1',**kwargs})

    def submit(self, response, **kwargs):
        form = response.context['form']
        data = {key:values if key=='photo_ids' else values[-1] for key,values in form.data.lists()}
        data['snapshot'] = response.context['snapshot']
        for photo in response.context['photos']:
            data[f'version_{photo.pk}'] = str(photo.metadata_version)
        data.update(kwargs)
        return self.client.post('/metadata/location/',data)

    def test_preview_and_save_only_unique_selected_photos(self):
        one,two,other = self.photo(),self.photo(2),self.photo(3)
        response = self.preview([two,one,one])
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'将更新 2 张')
        self.assertContains(response,'未知')
        self.assertEqual([p.pk for p in response.context['photos']],[one.pk,two.pk])
        self.assertEqual(self.submit(response).status_code,302)
        for photo in (one,two):
            photo.refresh_from_db(); self.assertEqual(photo.user_location,'广东  深圳')
            self.assertEqual(photo.metadata_version,2)
        other.refresh_from_db(); self.assertEqual(other.user_location,'')
        self.assertEqual(Job.objects.count(),0)

    def test_zero_invalid_outside_page_and_noneditable_rejected(self):
        photo = self.photo()
        for ids in ([],['malicious']):
            response = self.client.get('/metadata/location/',{'photo_ids':ids,'location':'广东'})
            self.assertEqual(response.status_code,400)
        self.assertEqual(self.client.get('/metadata/location/',{'photo_ids':[str(uuid.uuid4())],'location':'广东'}).status_code,409)
        for number in range(2,27):
            outside = self.photo(number)
        self.assertEqual(self.preview([outside]).status_code,409)
        photo.state='archived';photo.save(update_fields=['state'])
        self.assertEqual(self.preview([photo]).status_code,409)

    def test_unicode_length_whitespace_and_escaping(self):
        photo = self.photo()
        self.assertEqual(self.preview([photo], '  '+ '鸟'*200+'  ').status_code,200)
        for value in ('',' \t\n ','鸟'*201):
            invalid=self.preview([photo],value)
            self.assertEqual(invalid.status_code,400)
            self.assertContains(invalid,f'name="photo_ids" value="{photo.pk}"',status_code=400)
        response=self.preview([photo],'<script>深圳</script>')
        self.assertContains(response,'&lt;script&gt;深圳&lt;/script&gt;')
        self.assertNotContains(response,'<script>深圳</script>')

    def test_conflict_rolls_back_entire_batch_retains_draft(self):
        one,two=self.photo(),self.photo(2)
        response=self.preview([one,two])
        Photo.objects.filter(pk=two.pk).update(user_location='江苏',metadata_version=2)
        conflict=self.submit(response)
        self.assertEqual(conflict.status_code,409)
        self.assertContains(conflict,'广东  深圳',status_code=409)
        self.assertContains(conflict,'江苏',status_code=409)
        one.refresh_from_db(); self.assertEqual(one.user_location,'')
        self.assertEqual(one.metadata_version,1)

    def test_frozen_value_csrf_and_duplicate_post(self):
        photo=self.photo()
        response=self.preview([photo])
        self.assertEqual(self.submit(response,location='篡改').status_code,409)
        self.assertEqual(self.submit(response).status_code,302)
        self.assertEqual(self.submit(response).status_code,409)
        self.assertEqual(Job.objects.count(),0)
        secured=Client(enforce_csrf_checks=True)
        self.assertEqual(secured.post('/metadata/location/',{}).status_code,403)
        anon=Client()
        self.assertEqual(anon.get('/metadata/location/').status_code,400)
        self.assertEqual(anon.post('/metadata/location/',{}).status_code,400)

    def test_same_location_does_not_write_twice(self):
        photo=self.photo(user_location='广东  深圳')
        response=self.preview([photo])
        self.assertEqual(self.submit(response).status_code,302)
        self.assertEqual(self.submit(response).status_code,302)
        photo.refresh_from_db();self.assertEqual(photo.metadata_version,1)

    def test_anonymous_real_csrf_full_page_and_hx_metadata_saves(self):
        secured=Client(enforce_csrf_checks=True)
        photo=self.photo()
        for hx in (False,True):
            location='江苏' if hx else '广东'
            response=secured.get('/metadata/location/',{'photo_ids':[str(photo.pk)],'location':location})
            self.assertEqual(response.status_code,200)
            data=response.context['form'].data.dict()
            data.update(snapshot=response.context['snapshot'],**{f'version_{photo.pk}':photo.metadata_version})
            token=secured.cookies[settings.CSRF_COOKIE_NAME].value
            before=list(Photo.objects.values())
            for fields,headers in (({},{}),({'csrfmiddlewaretoken':'z'*64},{}),
                                   ({'csrfmiddlewaretoken':token},{'HTTP_ORIGIN':'https://evil.invalid'})):
                rejected=secured.post('/metadata/location/',{**data,**fields},**headers)
                self.assertEqual(rejected.status_code,403)
                self.assertEqual(list(Photo.objects.values()),before)
            saved=secured.post('/metadata/location/',{**data,'csrfmiddlewaretoken':token},
                               **({'HTTP_HX_REQUEST':'true'} if hx else {}))
            self.assertEqual(saved.status_code,302)
            photo.refresh_from_db();self.assertEqual(photo.user_location,location)
        preview=secured.get(f'/photos/{photo.pk}/capture-time/',{'date':'2025-12-31'})
        data=preview.context['form'].data.dict()
        data.update(snapshot=preview.context['snapshot'],version=photo.metadata_version,
                    csrfmiddlewaretoken=secured.cookies[settings.CSRF_COOKIE_NAME].value)
        self.assertEqual(secured.post(f'/photos/{photo.pk}/capture-time/',data,HTTP_HX_REQUEST='true').status_code,302)
        photo.refresh_from_db();self.assertEqual(photo.manual_capture['local'],'2025-12-31')
        self.assertEqual(User.objects.count(),0)
        self.assertNotIn('_auth_user_id',secured.session)

    def capture_preview(self, photo, **kwargs):
        return self.client.get(f'/photos/{photo.pk}/capture-time/',{'date':'2025-12-31','filter':'all','page':'2',**kwargs})

    def capture_submit(self, photo, response, **kwargs):
        data = response.context['form'].data.dict()
        data['snapshot'] = response.context['snapshot']
        data['version'] = str(response.context['photo'].metadata_version)
        data.update(kwargs)
        return self.client.post(f'/photos/{photo.pk}/capture-time/',data)

    def test_missing_capture_date_only_stays_date_and_retains_return_page(self):
        photo=self.photo()
        response=self.capture_preview(photo)
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'2025-12-31')
        saved=self.capture_submit(photo,response)
        self.assertEqual(saved.status_code,302)
        photo.refresh_from_db()
        self.assertEqual(photo.manual_capture,{'local':'2025-12-31','precision':'date','offset':None})
        detail=self.client.get(saved.url)
        self.assertContains(detail,'人工补充')
        self.assertContains(detail,'日期')
        self.assertContains(detail,'时区未知')
        self.assertContains(detail,'filter=all&amp;page=2')
        self.assertNotContains(detail,'00:00')

    def test_capture_time_offset_boundaries_and_invalid_drafts(self):
        photo=self.photo(capture_quality='invalid',exif_raw={'DateTimeOriginal':'bad'})
        for value in ('-14:00','+14:00','+00:00','-00:30',''):
            response=self.capture_preview(photo,time='23:59:59',offset=value)
            self.assertEqual(response.status_code,200)
        for fields in ({'date':'','time':'12:00:00'},{'date':'2025-02-29'},
                       {'time':'25:00:00'},{'offset':'+14:01'},{'offset':'-14:59'},
                       {'offset':'+00:60'},{'offset':'UTC'},{'time':'12:00:00.123'}):
            self.assertEqual(self.capture_preview(photo,**fields).status_code,400)
        response=self.capture_preview(photo,time='23:59:59',offset='-14:00')
        self.assertEqual(self.capture_submit(photo,response).status_code,302)
        photo.refresh_from_db()
        self.assertEqual(photo.manual_capture,{'local':'2025-12-31 23:59:59','precision':'second','offset':-840})
        self.assertEqual(photo.exif_raw,{'DateTimeOriginal':'bad'})
        self.assertIsNone(photo.captured_local)

    def test_reliable_existing_manual_and_capture_conflicts_reject(self):
        reliable=self.photo(capture_quality='reliable',captured_local='2024-01-01 01:00:00')
        self.assertEqual(self.capture_preview(reliable).status_code,409)
        photo=self.photo(2)
        response=self.capture_preview(photo)
        Photo.objects.filter(pk=photo.pk).update(user_location='新地点',metadata_version=2)
        conflict=self.capture_submit(photo,response)
        self.assertEqual(conflict.status_code,409)
        self.assertContains(conflict,'2025-12-31',status_code=409)
        photo.refresh_from_db();self.assertIsNone(photo.manual_capture)
        response=self.capture_preview(photo)
        self.assertEqual(self.capture_submit(photo,response).status_code,302)
        self.assertEqual(self.capture_submit(photo,response).status_code,409)
        self.assertEqual(self.capture_preview(photo).status_code,409)

    def test_real_import_metadata_saves_rescans_and_duplicates_preserve_jpeg(self):
        from archive.tests.test_images import exif_jpeg
        from archive.services.intake import scan_once
        from archive.services.jobs import process_source
        from archive.services.paths import open_root
        import os, shutil
        path=self.inbox/'长中文文件名<鸟>.jpg'
        digest=exif_jpeg(path)
        scan_once()
        inbox_fd,data_fd=open_root(self.inbox),open_root(self.data)
        try:
            for job in Job.objects.filter(state='pending'):
                self.assertTrue(process_source(job,inbox_fd,data_fd))
            photo=Photo.objects.get()
            self.assertEqual(self.submit(self.preview([photo])).status_code,302)
            photo.refresh_from_db()
            self.assertEqual(self.capture_submit(photo,self.capture_preview(photo)).status_code,302)
            photo.refresh_from_db(); manual=photo.manual_capture
            shutil.copyfile(path,self.inbox/'duplicate.jpg')
            scan_once()
            for job in Job.objects.filter(state='pending'):
                process_source(job,inbox_fd,data_fd)
            scan_once()
        finally:
            os.close(inbox_fd);os.close(data_fd)
        photo.refresh_from_db()
        self.assertEqual(photo.manual_capture,manual)
        self.assertEqual(photo.user_location,'广东  深圳')
        self.assertEqual(Photo.objects.count(),1)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),digest)
        self.assertContains(self.client.get(f'/photos/{photo.pk}/'),'&lt;鸟&gt;.jpg')

    def test_capture_csrf_frozen_fields_and_no_initial_error(self):
        photo=self.photo()
        self.assertEqual(self.client.get(f'/photos/{photo.pk}/capture-time/').status_code,200)
        response=self.capture_preview(photo)
        self.assertEqual(self.capture_submit(photo,response,date='2026-01-01').status_code,409)
        secured=Client(enforce_csrf_checks=True)
        self.assertEqual(secured.post(f'/photos/{photo.pk}/capture-time/',{}).status_code,403)

    def test_browser_forms_keep_drafts_single_post_and_safe_get_recovery(self):
        import subprocess
        script = r'''
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
async function run(status,origin='https://private.test',throws=false) {
  let replaced=0,history=0,methods=[],submit,warnings=[],listeners=[];
  const button={disabled:false,textContent:'保存',dataset:{}};
  const form={dataset:{},method:'post',action:'https://private.test/metadata/location/',elements:[button],
    reportValidity:()=>true,hasAttribute:s=>s==='data-pending-text',setAttribute:()=>{},removeAttribute:()=>{},
    querySelector:()=>button,addEventListener:(_,fn)=>submit=fn,append:v=>warnings.push(v)};
  const next={draft:'保留地点草稿'};
  const document={visibilityState:'visible',querySelector:()=>null,
    querySelectorAll:s=>s==='form'?[form]:s.startsWith('#main')?[button]:[],
    getElementById:()=>({replaceWith:v=>{assert.equal(v,next);replaced++;}}),
    createElement:()=>({append(...values){this.children=values;},focus:()=>{},setAttribute:()=>{}}),
    addEventListener:(name,fn)=>listeners.push(name)};
  const context={URL,AbortSignal,FormData:class {constructor(){assert.equal(button.disabled,false);}},
    DOMParser:class {parseFromString(){return {querySelector:s=>{assert.equal(s,'main#main');return next;}};}},
    setInterval:()=>{},document,window:{location:{href:'https://private.test/metadata/location/?draft=1',
      origin:'https://private.test'},history:{replaceState:()=>history++},addEventListener:(name)=>listeners.push(name)},
    fetch:async(url,options)=>{methods.push(options.method);if(throws)throw Error('offline');
      return {status,redirected:status===200,url:origin+'/',text:async()=>'<main id="main"></main>'};}};
  vm.createContext(context);vm.runInContext(source,context);
  await submit({preventDefault:()=>{}});
  if(form.dataset.resultUnknown==='true')await submit({preventDefault:()=>{}});
  assert.deepEqual(methods,['POST']);assert(!listeners.includes('pagehide'));assert(!listeners.includes('pageshow'));
  return {replaced,history,form,button,warnings};
}
(async()=>{
  for(const status of [200,400,409]) {
    const result=await run(status);assert.equal(result.replaced,1);assert.equal(result.history,status===200?1:0);
  }
  for(const status of [403,404,500]) {
    const result=await run(status);assert.equal(result.replaced,0);assert.equal(result.form.dataset.resultUnknown,'true');
    assert.equal(result.button.disabled,true);assert.equal(result.warnings.length,1);
    const link=result.warnings[0].children[1];assert(link.href.startsWith('https://private.test')||link.href==='/');
    if(status===403){assert.equal(result.warnings[0].textContent,'页面已过期，请刷新后重试。');assert.equal(result.button.textContent,'保存');}
  }
  assert.equal((await run(200,'https://external.test')).replaced,0);
  assert.equal((await run(200,'https://private.test',true)).replaced,0);
  process.stdout.write('FORM_DRAFT_PASS');
})().catch(error=>{process.stderr.write(error.stack);process.exit(1)});
'''
        result=subprocess.run(['node','-e',script,str(settings.BASE_DIR/'archive/static/archive/app.js')],
                              capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('FORM_DRAFT_PASS',result.stdout)

    def test_cas_failure_after_first_update_rolls_back_all_rows(self):
        from unittest import mock
        from django.db.models.query import QuerySet
        one,two=self.photo(),self.photo(2)
        response=self.preview([one,two])
        original_update=QuerySet.update
        attempts=[]
        def update(query, **fields):
            if query.model is Photo and 'user_location' in fields:
                attempts.append(fields)
                if len(attempts)==2:
                    return 0
            return original_update(query,**fields)
        with mock.patch.object(QuerySet,'update',update):
            self.assertEqual(self.submit(response).status_code,409)
        self.assertEqual(len(attempts),2)
        self.assertEqual(list(Photo.objects.order_by('created_at').values_list('user_location','metadata_version')),
                         [('',1),('',1)])

    def test_no_js_initial_selection_preview_frozen_post_and_prg(self):
        photo=self.photo()
        selected=self.client.get('/metadata/location/',{'photo_ids':[str(photo.pk)],'filter':'all','page':1})
        self.assertEqual(selected.status_code,200)
        self.assertNotContains(selected,'请填写地点')
        self.assertContains(selected,f'name="photo_ids" value="{photo.pk}"')
        preview=self.preview([photo],'江苏')
        saved=self.submit(preview)
        self.assertContains(self.client.get(saved.url),'已保存 1 张地点。')
        self.assertNotContains(self.client.get(saved.url),'已保存 1 张地点。')
        ids=[str(uuid.uuid4()) for _ in range(25)]
        self.assertEqual(self.client.get('/metadata/location/',{'photo_ids':ids,'location':'江苏'}).status_code,400)

    def test_old_probe_header_does_not_replace_metadata_business_response(self):
        photo=self.photo()
        for url in ('/metadata/location/',f'/photos/{photo.pk}/capture-time/'):
            response=self.client.get(url,HTTP_X_BIRDS_SESSION_CHECK='1',HTTP_HX_REQUEST='true')
            self.assertEqual(response.status_code,400 if url=='/metadata/location/' else 200)
            self.assertIn(b'<main',response.content)

    def test_removed_or_now_ineligible_selection_never_partially_saves(self):
        one,two=self.photo(),self.photo(2)
        response=self.preview([one,two])
        two.delete()
        missing=self.submit(response)
        self.assertEqual(missing.status_code,409)
        self.assertContains(missing,str(two.pk) if two.pk else response.context['photos'][1].pk,status_code=409)
        one.refresh_from_db();self.assertEqual(one.user_location,'')
        preview=self.preview([one])
        Photo.objects.filter(pk=one.pk).update(state='archived')
        self.assertEqual(self.submit(preview).status_code,409)
        one.refresh_from_db();self.assertEqual(one.user_location,'')
