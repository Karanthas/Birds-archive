"""预览等待的渐进增强；VM状态回归不代替真实浏览器解码证据。"""
import subprocess

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, SimpleTestCase

from archive.models import Photo


class DetailMediaMarkupTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(settings.BIRDS_OWNER_USERNAME, password='synthetic-only')
        self.client.force_login(self.owner)

    def test_preview_has_hidden_waiting_status_and_get_recovery(self):
        photo = Photo.objects.create(sha256='a' * 64, byte_size=1, preview_key='preview.jpg')
        response = self.client.get(f'/photos/{photo.pk}/?filter=review&page=2')
        self.assertContains(response, 'data-media-loading role="status" hidden>加载中…')
        self.assertContains(response, f'src="/media/{photo.pk}/preview/"')
        self.assertContains(response, f'href="/photos/{photo.pk}/?filter=review&amp;page=2">刷新</a>')
        self.assertContains(response, '暂无预览')
        self.assertIn('no-store', response['Cache-Control'])

    def test_missing_preview_has_no_image_or_waiting_and_metadata_actions_remain(self):
        photo = Photo.objects.create(sha256='b' * 64, byte_size=1)
        response = self.client.get(f'/photos/{photo.pk}/')
        self.assertNotContains(response, '<img')
        self.assertNotContains(response, 'data-media-loading')
        for text in ('暂无预览', '未知', '待确认', '填写地点', '补充时间'):
            self.assertContains(response, text)


class MediaFeedbackStateTests(SimpleTestCase):
    def test_pending_decode_success_failure_and_cached_results(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
async function fixture(complete=false,width=0) {
  const loading={hidden:true},error={hidden:true},listeners={};
  let resolve,reject,decodes=0,focus=0,fetches=0;
  const decoded=new Promise((yes,no)=>{resolve=yes;reject=no});
  const image={dataset:{},hidden:false,complete,naturalWidth:width,
    parentElement:{querySelector:s=>s==='[data-media-loading]'?loading:s==='.media-error'?error:null},
    addEventListener:(name,handler)=>{(listeners[name]??=[]).push(handler)},
    decode:()=>{decodes++;return decoded}};
  const document={body:{dataset:{}},querySelectorAll:s=>s==='img'?[image]:[],
    querySelector:()=>null,addEventListener:()=>{},activeElement:{focus:()=>focus++}};
  const context={document,URL,AbortSignal,setInterval:()=>{},window:{addEventListener:()=>{}},
    fetch:()=>{fetches++;throw Error('media binding must not fetch or replay')}};
  vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
  return {image,loading,error,listeners,resolve,reject,context,
    facts:()=>({decodes,focus,fetches}),flush:async()=>{await Promise.resolve();await Promise.resolve();}};
}
(async()=>{
  const pending=await fixture();
  assert.equal(pending.loading.hidden,false,'pending image must show waiting');
  pending.image.complete=true;pending.image.naturalWidth=96;
  pending.listeners.load[0]();
  assert.equal(pending.loading.hidden,false,'wait until decode settles');
  pending.resolve();await pending.flush();
  assert.equal(pending.loading.hidden,true);assert.equal(pending.error.hidden,true);assert.equal(pending.image.hidden,false);
  vm.runInContext('bindPage()',pending.context);assert.equal(pending.listeners.load.length,1);
  assert.deepEqual(pending.facts(),{decodes:1,focus:0,fetches:0});
  const broken=await fixture();broken.listeners.error[0]();
  assert.equal(broken.loading.hidden,true);assert.equal(broken.error.hidden,false);assert.equal(broken.image.hidden,true);
  const invalid=await fixture();invalid.image.naturalWidth=96;invalid.listeners.load[0]();invalid.reject(Error('bad JPEG'));await invalid.flush();
  assert.equal(invalid.loading.hidden,true);assert.equal(invalid.error.hidden,false);
  const cachedBroken=await fixture(true,0);
  assert.equal(cachedBroken.loading.hidden,true);assert.equal(cachedBroken.error.hidden,false);assert.equal(cachedBroken.image.hidden,true);
  const cachedGood=await fixture(true,96);cachedGood.resolve();await cachedGood.flush();
  assert.equal(cachedGood.loading.hidden,true);assert.equal(cachedGood.error.hidden,true);assert.equal(cachedGood.image.hidden,false);
  // error wins over a late decode completion; no permanent or renewed waiting.
  const raced=await fixture();raced.image.naturalWidth=96;raced.listeners.load[0]();raced.listeners.error[0]();raced.resolve();await raced.flush();
  assert.equal(raced.loading.hidden,true);assert.equal(raced.error.hidden,false);assert.equal(raced.image.hidden,true);
  process.stdout.write('MEDIA_FEEDBACK_STATE_PASS');
})().catch(error=>{process.stderr.write(error.stack);process.exit(1)});
'''
        result = subprocess.run(['node', '-e', script, str(settings.BASE_DIR / 'archive/static/archive/app.js')],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('MEDIA_FEEDBACK_STATE_PASS', result.stdout)
