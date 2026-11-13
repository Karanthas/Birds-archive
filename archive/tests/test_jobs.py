import errno
import os
import tempfile
from pathlib import Path
from unittest import mock
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from archive.models import Job, Photo, ImportSource
from archive.services.intake import scan_once
from archive.services.jobs import process_source
from archive.services.paths import open_root
from archive.tests.fixtures import jpeg
from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client


class JobTests(TransactionTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='birds-jobs-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inbox, self.data = self.root/'inbox', self.root/'data'
        self.inbox.mkdir(); self.data.mkdir()
        self.override = override_settings(BIRDS_INBOX_ROOT=str(self.inbox), BIRDS_DATA_ROOT=str(self.data),
            BIRDS_HANDOFF_MODE='published', BIRDS_PUBLISHED_CONTRACT=True)
        self.override.enable(); self.addCleanup(self.override.disable)
        self.digest = jpeg(self.inbox/'测试.jpg')
        scan_once()
        self.job = Job.objects.get()

    def process(self, job=None):
        inbox_fd, data_fd = open_root(self.inbox), open_root(self.data)
        try:
            return process_source(job or self.job, inbox_fd, data_fd)
        finally:
            os.close(inbox_fd); os.close(data_fd)

    def test_transient_io_waits_then_succeeds_same_job(self):
        with mock.patch('archive.services.jobs.inspect_open_jpeg', side_effect=OSError(errno.EIO, 'secret /private')):
            self.assertFalse(self.process())
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, 'retry_wait')
        self.assertEqual(self.job.attempts, 1)
        self.assertGreater(self.job.next_attempt_at, timezone.now())
        Job.objects.filter(pk=self.job.pk).update(next_attempt_at=timezone.now())
        self.assertTrue(self.process())
        self.job.refresh_from_db()
        self.assertEqual((self.job.state, self.job.attempts), ('succeeded', 2))
        self.assertEqual((Job.objects.count(), Photo.objects.count()), (1, 1))

    def test_permission_error_is_permanent_and_safe(self):
        with mock.patch('archive.services.jobs.inspect_open_jpeg', side_effect=PermissionError('TOKEN\n<script>')):
            self.assertFalse(self.process())
        self.job.refresh_from_db()
        self.assertEqual((self.job.state, self.job.error_code), ('failed', 'source_permission'))
        self.assertNotIn('TOKEN', self.job.safe_message)

    def test_bounded_three_attempts_with_one_two_four_second_schedule(self):
        from datetime import timedelta
        for attempt, delay in ((1,1),(2,2),(3,4)):
            now=timezone.now()
            Job.objects.filter(pk=self.job.pk).update(next_attempt_at=now)
            with (mock.patch('archive.services.jobs.inspect_open_jpeg',side_effect=OSError(errno.EIO,'io')),
                  mock.patch('archive.services.jobs.timezone.now',return_value=now)):
                self.process()
            self.job.refresh_from_db()
            self.assertEqual(self.job.attempts,attempt)
            self.assertEqual(self.job.next_attempt_at,now+timedelta(seconds=delay))
            self.assertEqual(self.job.state,'failed' if attempt==3 else 'retry_wait')
        self.assertFalse(self.process())
        self.job.refresh_from_db(); self.assertEqual(self.job.attempts,3)

    def test_due_claim_order_and_condition_prevent_double_claim(self):
        from archive.services.jobs import claim_next,claim_job
        from datetime import timedelta
        jpeg(self.inbox/'另图.jpg',color=(1,2,3)); scan_once()
        second=Job.objects.exclude(pk=self.job.pk).get()
        now=timezone.now()
        Job.objects.filter(pk=self.job.pk).update(next_attempt_at=now+timedelta(seconds=1))
        Job.objects.filter(pk=second.pk).update(next_attempt_at=now)
        self.assertEqual(claim_next(now).pk,second.pk)
        self.assertIsNone(claim_job(second.pk,now))
        self.assertIsNone(claim_next(now))

    def test_busy_database_retry_is_bounded_and_other_error_not_retried(self):
        from django.db import OperationalError
        from archive.services.jobs import database_retry
        operation=mock.Mock(side_effect=[OperationalError('database is locked'),OperationalError('database is busy'),42])
        with mock.patch('archive.services.jobs.time.sleep') as sleep:
            self.assertEqual(database_retry(operation),42)
            self.assertEqual(sleep.call_args_list,[mock.call(1),mock.call(2)])
        operation=mock.Mock(side_effect=OperationalError('bad schema'))
        with self.assertRaises(OperationalError):
            database_retry(operation)
        self.assertEqual(operation.call_count,1)


class TaskViewTests(TransactionTestCase):
    process = JobTests.process

    def setUp(self):
        JobTests.setUp(self)

    def test_every_state_has_true_chinese_stage_and_times(self):
        for state,label in (('queued','待处理'),('running','处理中'),('retry_wait','等待重试'),
                            ('succeeded','已完成'),('failed','失败'),('stale','来源已变化')):
            Job.objects.filter(pk=self.job.pk).update(state=state,stage='reading',error_code='source_permission')
            self.assertContains(self.client.get('/tasks/'),label)
        self.assertContains(self.client.get('/tasks/status/'),'更新时间')

    def test_retry_is_versioned_post_idempotent_and_never_decodes(self):
        Job.objects.filter(pk=self.job.pk).update(state='failed',error_code='source_permission',attempts=3)
        payload={'version':str(self.job.source_version)}
        with mock.patch('archive.services.jobs.inspect_open_jpeg',side_effect=AssertionError('Web不能解码')):
            response=self.client.post(f'/tasks/{self.job.pk}/retry/',payload,follow=True)
        self.assertEqual(response.redirect_chain, [('/tasks/?state=&page=1', 302)])
        self.assertContains(response,'已加入重试队列')
        self.job.refresh_from_db()
        self.assertEqual((self.job.state,self.job.attempts),('queued',3))
        second=self.client.post(f'/tasks/{self.job.pk}/retry/',payload,follow=True)
        self.assertEqual(second.status_code,200)
        self.assertNotContains(second,'已加入重试队列')
        self.assertEqual(Job.objects.count(),1)
        self.assertEqual(self.client.get(f'/tasks/{self.job.pk}/retry/').status_code,405)
        conflict=self.client.post(f'/tasks/{self.job.pk}/retry/',{'version':'0'},follow=True)
        self.assertEqual(conflict.redirect_chain, [('/tasks/?state=&page=1', 302)])
        self.assertContains(conflict,'来源已变化')

    def test_retry_current_failed_only_and_csrf_protection(self):
        endpoint=f'/tasks/{self.job.pk}/retry/'
        response=self.client.post(endpoint,{'version':'1'},follow=True)
        self.assertNotContains(response,'已加入重试队列')
        secure=Client(enforce_csrf_checks=True)
        self.assertEqual(secure.post(endpoint,{'version':'1'}).status_code,403)
        for path in ('/tasks/','/tasks/status/'):
            self.assertEqual(Client().get(path).status_code,200)
        self.assertEqual(Client().get(endpoint).status_code,405)

    def test_safe_error_fallback_filename_escape_and_read_only_status(self):
        source=self.job.source
        source.relative_path='长中文<script>\n控制字符.jpg';source.save()
        Job.objects.filter(pk=self.job.pk).update(state='failed',error_code='unknown_SECRET',safe_message='TOKEN\n<script>/private')
        for endpoint in ('/tasks/','/tasks/status/'):
            response=self.client.get(endpoint)
            self.assertContains(response,'processing_failed')
            self.assertContains(response,'&lt;script&gt;')
            self.assertNotContains(response,'TOKEN')
            self.assertNotContains(response,'unknown_SECRET')
            self.assertNotContains(response,'/private')
            self.assertNotContains(response,'<script>')
        self.assertEqual(Job.objects.count(),1)
        self.job.refresh_from_db();self.assertEqual(self.job.state,'failed')

    def test_anonymous_real_csrf_retries_same_job_and_rejects_unsafe_requests(self):
        Job.objects.filter(pk=self.job.pk).update(state='failed',error_code='source_permission')
        client=Client(enforce_csrf_checks=True)
        self.assertEqual(client.get('/tasks/').status_code,200)
        token=client.cookies[settings.CSRF_COOKIE_NAME].value
        endpoint=f'/tasks/{self.job.pk}/retry/'
        before=list(Job.objects.values())
        for data,headers in (({},{}),({'csrfmiddlewaretoken':'z'*64},{}),
                             ({'csrfmiddlewaretoken':token},{'HTTP_ORIGIN':'https://evil.invalid'})):
            self.assertEqual(client.post(endpoint,{'version':'1',**data},**headers).status_code,403)
            self.assertEqual(list(Job.objects.values()),before)
        response=client.post(endpoint,{'version':'1','csrfmiddlewaretoken':token},HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code,302)
        self.job.refresh_from_db();self.assertEqual(self.job.state,'queued')
        self.assertEqual(Job.objects.count(),1)
        self.assertEqual(User.objects.count(),0)
        import hashlib
        self.assertEqual(hashlib.sha256((self.inbox/'测试.jpg').read_bytes()).hexdigest(),self.digest)

    def test_log_failure_does_not_undo_success_and_never_emits_raw_exception(self):
        with mock.patch('archive.services.jobs.logging.getLogger') as logger:
            logger.return_value.info.side_effect=OSError('COOKIE_SECRET')
            self.assertTrue(self.process())
        self.job.refresh_from_db();self.assertEqual(self.job.state,'succeeded')
        self.assertEqual(Photo.objects.count(),1)
        for args,kwargs in logger.return_value.info.call_args_list:
            self.assertNotIn('COOKIE_SECRET',str((args,kwargs)))

    def test_raw_exception_secret_never_enters_structured_log_or_response(self):
        import json
        secret='COOKIE_SECRET TOKEN\n<script>/private/path'
        with mock.patch('archive.services.jobs.inspect_open_jpeg',side_effect=RuntimeError(secret)):
            with self.assertLogs('archive.jobs',level='INFO') as output:
                self.assertFalse(self.process())
        for record in output.records:
            self.assertNotIn(secret,record.getMessage())
            event=json.loads(record.getMessage())
            self.assertEqual(set(event),{'job_id','source_id','stage','attempt','error_code'})
        self.assertContains(self.client.get('/tasks/'),'processing_failed')
        self.assertNotContains(self.client.get('/tasks/'),'COOKIE_SECRET')

    def test_old_heartbeat_is_observation_only_and_empty_filter_clearable(self):
        from datetime import timedelta
        from archive.services.paths import open_root,write_worker_heartbeat
        fd=open_root(self.data)
        try:
            with mock.patch('django.utils.timezone.now',return_value=timezone.now()-timedelta(days=1)):
                write_worker_heartbeat(fd)
        finally:
            os.close(fd)
        Job.objects.filter(pk=self.job.pk).update(state='running')
        self.assertContains(self.client.get('/tasks/'),'后台更新时间较早')
        self.assertContains(self.client.get('/tasks/?state=failed'),'清除筛选')
        self.job.refresh_from_db();self.assertEqual(self.job.state,'running')
        Job.objects.all().delete()
        self.assertContains(self.client.get('/tasks/'),'暂无任务')

    def test_two_concurrent_retry_requests_queue_once_on_disk_database(self):
        from concurrent.futures import ThreadPoolExecutor
        from django.db import connections
        Job.objects.filter(pk=self.job.pk).update(state='failed',error_code='source_permission')
        def post():
            try:
                client=Client()
                response=client.post(f'/tasks/{self.job.pk}/retry/',{'version':'1'},follow=True)
                return response.status_code, '已加入重试队列'.encode() in response.content
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:post(),range(2)))
        self.assertEqual([code for code,_ in results],[200,200])
        self.assertEqual(sum(queued for _,queued in results),1)
        self.assertEqual(Job.objects.count(),1)

    def test_task_javascript_get_only_pause_focus_failure_and_submit_guard(self):
        import subprocess
        script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
(async()=>{
  for(const mode of ['normal','failed','rejected','focus-during-fetch']){
    let timer,requests=[],replaced=0,cleared=0,destination='',submit;
    const message={textContent:''},button={disabled:false,textContent:'重试',dataset:{}};
    const form={dataset:{},method:'post',action:'http://localhost/retry/',elements:[button],reportValidity:()=>true,
      hasAttribute:s=>s==='data-task-retry',setAttribute:()=>{},removeAttribute:()=>{},querySelector:()=>button,
      addEventListener:(_,fn)=>submit=fn};
    const count={textContent:'任务 1 项'};
    const region={dataset:{taskPoll:'/tasks/status/'},contains:()=>false,hasAttribute:()=>false,
      querySelectorAll:()=>[count],querySelector:s=>s==='.page-heading span'?count:
        s.includes('data-task-refresh-message')?message:null,
      replaceWith:()=>replaced++};
    const doc={visibilityState:'visible',activeElement:null,
      querySelector:s=>s==='[data-summary-poll], [data-task-poll]'?region:null,
      querySelectorAll:s=>s==='form'?[form]:[],
      getElementById:()=>({replaceChildren:()=>cleared++}),addEventListener:()=>{}};
    const win={location:{pathname:'/tasks/',search:'?state=failed',origin:'http://localhost',
      replace:v=>destination=v},addEventListener:()=>{}};
    const context={document:doc,window:win,URL,URLSearchParams,AbortSignal,FormData:class{},DOMParser:class{
      parseFromString(){return {querySelector:()=>region};}},setInterval:fn=>timer=fn,
      fetch:async(url,options)=>{requests.push({url,options});
        if(options.method==='POST')return new Promise(()=>{});
        if(mode==='focus-during-fetch')region.contains=()=>true;
        return {url:'http://localhost/tasks/',status:mode==='failed'?503:mode==='rejected'?403:200,
          ok:mode==='normal'||mode==='focus-during-fetch',text:async()=>'<section></section>'};}};
    vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
    assert.equal(requests.length,0);
    doc.visibilityState='hidden';await timer();assert.equal(requests.length,0);
    doc.visibilityState='visible';region.contains=()=>true;await timer();assert.equal(requests.length,0);
    region.contains=()=>false;await timer();assert.equal(requests.length,1);
    assert.equal(String(requests[0].url),'http://localhost/tasks/?state=failed'.replace('/tasks/?','/tasks/status/?'));
    assert.equal(requests[0].options.method,undefined);
    assert.equal(requests[0].options.redirect,'manual');
    if(mode==='normal'){
      assert.equal(replaced,1);let prevented=0;
      submit({preventDefault:()=>prevented++});assert.equal(button.disabled,true);
      assert.equal(button.textContent,'正在提交…');
      submit({preventDefault:()=>prevented++});assert.equal(prevented,2);
      assert.equal(requests.filter(r=>r.options.method==='POST').length,1);
    }else if(mode==='failed'||mode==='rejected'){
      assert.equal(replaced,0);assert(message.textContent.includes('状态暂未更新'));
      await timer();assert.equal(requests.length,1);
    }else{assert.equal(replaced,0);}
    assert.equal(cleared,0);assert.equal(destination,'');
  }
  console.log('TASK_JS_PASS');
})().catch(error=>{console.error(error);process.exit(1)});
'''
        result=subprocess.run(['node','-e',script,str(settings.BASE_DIR/'archive/static/archive/app.js')],
                              capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('TASK_JS_PASS',result.stdout)



