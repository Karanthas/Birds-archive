"""离线适配器控制合同；假张量不是模型效果或 NAS 实测。"""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from django.test import SimpleTestCase, TestCase
from django.core.management import call_command
from django.core.management.base import CommandError
from archive.services import model_packages as packages, local_model
from archive.tests.fixtures import jpeg
from archive.tests.test_recognition_state import RecognitionFixture


class AdapterRedGateTests(SimpleTestCase):
    def test_evaluation_and_production_share_local_cpu_adapter(self):
        from archive.services import local_model
        self.assertTrue(callable(getattr(local_model, 'run_cpu_evaluation', None)),
                        '评测必须复用实际本地 CPU 角色，而不是缺失适配器或假报告')


class PackageFixture:
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix='birds-model-control-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.bundle = self.root/'bundle'
        self.bundle.mkdir()
        self.raw_config = {'model_cfg':{'embed_dim':768,
            'vision_cfg':{'image_size':224,'layers':24,'width':1024,'patch_size':14},
            'text_cfg':{'context_length':77,'vocab_size':49408,'width':768,'heads':12,'layers':12}},
            'preprocess_cfg':{'mean':[0.48145466,0.4578275,0.40821073],
            'std':[0.26862954,0.26130258,0.27577711],'interpolation':'bicubic','resize_mode':'shortest'}}
        self.labels = [{'identity_kind':'catalog','external_id':str(i),'scientific_name':name,
                        'species_id':str(uuid.uuid4()) if i != 1 else None,
                        'mapping_evidence':'fixed snapshot concept' if i != 1 else ''}
                       for i,name in enumerate(['Anas platyrhynchos','Passer montanus','Turdus merula'])]
        self.runner = {'version':'openclip-cpu-eager-v1','device':'cpu','dtype':'float32',
                       'batch_size':1,'intra_threads':2,'inter_threads':1,'embedding_chunk_size':32}
        documents = {'weights':b'FAKE CONTROL WEIGHTS', 'tokenizer':b'FAKE CONTROL BPE',
            'text_features':b'FAKE CONTROL EMBEDDINGS','config':self.raw_config,'runner':self.runner,
            'license':b'MIT control fixture license', 'dependency_lock':b'control==1.0 --hash=sha256:fake',
            'preprocess':{**self.raw_config['preprocess_cfg'],'size':224,'mode':'RGB','exif_transpose':True},
            'labels':self.labels,'snapshot':{'source':'AviList','version':'2025b','species_count':3,
                                            'sha256':'a'*64,'concepts':self.labels},
            'prompts':{'version':'avilist-scientific-v1','template':packages.PROMPT,
                       'items':[packages.PROMPT.format(scientific_name=r['scientific_name']) for r in self.labels]}}
        self.manifest = {'schema_version':1,'provider':packages.PROVIDER,'repository':packages.REPOSITORY,
            'revision':packages.REVISION,'local_root':str(self.bundle),'license':'MIT','attribution':'Control only',
            'score_kind':'cosine_similarity','threshold_version':'uncalibrated-v1','margin_threshold':None,
            'calibration_evidence':None,'files':[]}
        for role,value in documents.items():
            name = {'weights':'open_clip_model.safetensors','config':'open_clip_config.json',
                    'text_features':'text-features.safetensors'}.get(role,role+'.json')
            raw = value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode()
            (self.bundle/name).write_bytes(raw)
            self.manifest['files'].append({'role':role,'path':name,'sha256':hashlib.sha256(raw).hexdigest(),'size_bytes':len(raw)})
        for role,key in [('dependency_lock','dependency_lock_digest'),('config','config_digest'),
                         ('tokenizer','tokenizer_digest'),('labels','label_digest'),('snapshot','snapshot_digest'),
                         ('preprocess','preprocess_digest'),('prompts','prompt_digest'),('runner','runner_digest'),
                         ('text_features','text_features_digest')]:
            self.manifest[key] = next(r['sha256'] for r in self.manifest['files'] if r['role']==role)
        for name,value in [('GLOBAL_SPECIES_COUNT',3),('WEIGHTS_SHA256',self.manifest['files'][0]['sha256']),
                           ('CONFIG_SHA256',self.manifest['config_digest']),('BPE_SHA256',self.manifest['tokenizer_digest'])]:
            mock = patch.object(packages,name,value)
            mock.start()
            self.addCleanup(mock.stop)
        self.path = self.root/'manifest.json'
        self.path.write_text(json.dumps(self.manifest))

    def change_json(self, role, mutate):
        row = next(r for r in self.manifest['files'] if r['role']==role)
        file = self.bundle/row['path']
        obj = json.loads(file.read_text())
        mutate(obj)
        raw = json.dumps(obj).encode()
        file.write_bytes(raw)
        row.update(sha256=hashlib.sha256(raw).hexdigest(),size_bytes=len(raw))
        key = {'labels':'label_digest','snapshot':'snapshot_digest','prompts':'prompt_digest',
               'runner':'runner_digest','preprocess':'preprocess_digest'}[role]
        self.manifest[key] = row['sha256']


class ModelPackageTests(PackageFixture, SimpleTestCase):
    def test_complete_bundle_verifies_and_freezes_identity(self):
        checked = packages.verify_manifest(self.manifest)
        self.assertEqual(checked['labels'],self.labels)
        self.assertEqual(checked['manifest_digest'],packages.digest(self.manifest))

    def test_hash_revision_repository_license_and_missing_roles_fail(self):
        for key,value in [('revision','main'),('repository','evil/model'),('license',''),('attribution','')]:
            with self.subTest(key=key), self.assertRaises(packages.ModelPackageError):
                packages.verify_manifest({**self.manifest,key:value})
        (self.bundle/'open_clip_model.safetensors').write_bytes(b'changed')
        with self.assertRaisesRegex(packages.ModelPackageError,'hash_mismatch'):
            packages.verify_manifest(self.manifest)

    def test_relative_external_path_symlink_and_pickle_refused(self):
        for path in ('../escape','/etc/passwd','https://example.org/model','sub/file'):
            broken = copy.deepcopy(self.manifest)
            broken['files'][0]['path'] = path
            with self.subTest(path=path), self.assertRaises(packages.ModelPackageError):
                packages.verify_manifest(broken)
        original = self.bundle/'open_clip_model.safetensors'
        original.rename(self.root/'weights')
        original.symlink_to(self.root/'weights')
        with self.assertRaises(packages.ModelPackageError):
            packages.verify_manifest(self.manifest)

    def test_extra_checkpoint_or_python_code_cannot_enter_factory_scan(self):
        for name in ('model.safetensors','other.bin','custom.py'):
            file = self.bundle/name
            file.write_bytes(b'bad')
            with self.subTest(name=name), self.assertRaises(packages.ModelPackageError):
                packages.verify_manifest(self.manifest)
            file.unlink()

    def test_global_complete_labels_and_no_custom_or_ambiguous_mapping(self):
        for mutate in (lambda labels: labels.pop(), lambda labels: labels[0].update(identity_kind='local'),
                       lambda labels: labels[0].update(species_id='guessed'),
                       lambda labels: labels[0].update(mapping_evidence=''),
                       lambda labels: labels.reverse(),
                       lambda labels: labels[1].update(scientific_name=labels[0]['scientific_name'])):
            labels = copy.deepcopy(self.labels)
            mutate(labels)
            snap = {'source':'AviList','version':'2025b','sha256':'a'*64,'species_count':3,'concepts':labels}
            with self.subTest(mutate=mutate), self.assertRaises(packages.ModelPackageError):
                packages.validate_labels(labels,snap)

    def test_modified_preprocess_prompt_and_uncalibrated_numeric_threshold_fail(self):
        self.change_json('preprocess',lambda x:x.update(size=256))
        with self.assertRaisesRegex(packages.ModelPackageError,'preprocess_invalid'):
            packages.verify_manifest(self.manifest)
        with self.assertRaises(packages.ModelPackageError):
            packages.verify_manifest({**self.manifest,'margin_threshold':.1})

    def test_json_duplicates_and_nonfinite_rejected(self):
        for raw in ('{"x":1,"x":2}','{"x":NaN}'):
            self.path.write_text(raw)
            with self.assertRaises(packages.ModelPackageError):
                packages.load_json(self.path)

    def test_manifest_declared_upstream_hash_cannot_replace_pinned_identity(self):
        with patch.object(packages,'WEIGHTS_SHA256','b'*64), self.assertRaisesRegex(
                packages.ModelPackageError,'upstream_hash_mismatch'):
            packages.verify_manifest(self.manifest)


class Scalar:
    def all(self): return True
    def item(self): return .9


class FakeFeatures:
    shape = (3,768)
    dtype = 'fp32'
    T = None
    def norm(self,dim): return self


class FakeScores:
    def squeeze(self,axis): return self
    def __getitem__(self,index): return SimpleNamespace(item=lambda: [.9,.8,.7][index])


class FakePixels:
    def unsqueeze(self,value): return self
    def to(self,*args,**kwargs): return self
    def __matmul__(self,other): return FakeScores()


class AdapterControlTests(PackageFixture, SimpleTestCase):
    def fake_adapter(self):
        from contextlib import nullcontext
        self.calls = []
        def record(name):
            return lambda *a,**kw:self.calls.append((name,a,kw))
        torch = SimpleNamespace(float32='fp32',set_num_threads=record('intra'),set_num_interop_threads=record('inter'),
            inference_mode=nullcontext,isfinite=lambda x:Scalar(),allclose=lambda *a,**k:True,ones_like=lambda x:x,
            argsort=lambda *a,**k:SimpleNamespace(__getitem__=None))
        class Indices:
            def __getitem__(self,key): return self
            def tolist(self): return [0,1,2]
        torch.argsort = lambda *a,**k: Indices()
        model = SimpleNamespace(eval=record('eval'),encode_image=lambda *a,**kw:FakePixels())
        def create(*args,**kwargs):
            self.calls.append(('create',args,kwargs))
            return model
        def transform(*a,**kw):
            self.calls.append(('transform',a,kw))
            def apply(image):
                self.calls.append(('pixels',image.size,image.mode))
                return FakePixels()
            return apply
        clip = SimpleNamespace(create_model=create,image_transform=transform)
        return local_model.OfflineCPUAdapter(packages.verify_manifest(self.manifest),
            _modules=(torch,clip,lambda **kw:None,lambda *a,**kw:{'text_features':FakeFeatures()},None))

    def test_production_readonly_flow_uses_cpu_eval_exif_copy_and_raw_unmapped_label(self):
        adapter = self.fake_adapter()
        path = self.root/'input.jpg'
        from PIL import Image
        with Image.new('RGB',(20,10)) as image:
            exif = Image.Exif()
            exif[274] = 6
            image.save(path,exif=exif)
        raw = path.read_bytes()
        fd = os.open(path,os.O_RDONLY)
        self.addCleanup(os.close,fd)
        with patch.object(local_model,'_get_adapter',return_value=adapter), patch('socket.create_connection',side_effect=AssertionError('outbound')):
            result = local_model.infer_readonly(fd,input_sha256=hashlib.sha256(raw).hexdigest(),input_size=len(raw),
                artifact={'provider':packages.PROVIDER,'manifest':self.manifest})
        self.assertEqual([x['raw_label'] for x in result['candidates']],[r['scientific_name'] for r in self.labels])
        self.assertIsNone(result['candidates'][1]['species_id'])
        self.assertEqual(result['candidates'][1]['mapping_status'],'unmapped')
        self.assertEqual(path.read_bytes(),raw)
        self.assertIn(('pixels',(10,20),'RGB'),self.calls)
        names = [x[0] for x in self.calls]
        self.assertLess(names.index('intra'),names.index('create'))
        self.assertLess(names.index('inter'),names.index('create'))
        create = next(x for x in self.calls if x[0]=='create')
        self.assertEqual(create[2]['device'],'cpu')
        self.assertFalse(create[2]['jit'])
        self.assertFalse(create[2]['pretrained_text'])
        self.assertIn('eval',names)

    def test_invalid_fd_hash_and_size_do_not_construct_model(self):
        path = self.root/'input.jpg'
        sha = jpeg(path)
        fd = os.open(path,os.O_RDONLY)
        self.addCleanup(os.close,fd)
        with patch.object(local_model,'_get_adapter') as create:
            result = local_model.infer_readonly(fd,input_sha256='0'*64,input_size=path.stat().st_size,artifact={})
            self.assertEqual(result,{'error_code':'input_invalid'})
            create.assert_not_called()

    def test_missing_dependency_returns_fixed_unavailable_and_no_photo_outbound(self):
        path = self.root/'input.jpg'
        sha = jpeg(path)
        fd = os.open(path,os.O_RDONLY)
        self.addCleanup(os.close,fd)
        with patch.object(local_model,'OfflineCPUAdapter',side_effect=ImportError('torch')):
            result = local_model.infer_readonly(fd,input_sha256=sha,input_size=path.stat().st_size,
                artifact={'provider':packages.PROVIDER,'manifest':self.manifest})
        self.assertEqual(result,{'error_code':'model_package_missing'})

    def test_web_safe_import_does_not_import_optional_libraries(self):
        result = subprocess.run([sys.executable,'-c',
            'import archive.services.local_model; import sys; assert not set(("torch","open_clip","safetensors","PIL","django")) & set(sys.modules)'],
            capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_role_network_guard_blocks_before_dns_or_connect(self):
        result = subprocess.run([sys.executable,'-c',
            'from archive.services.local_model import _deny_network; _deny_network(); import socket; '
            '\ntry: socket.create_connection(("example.org",443))'
            '\nexcept OSError as e: assert str(e)=="model_network_forbidden"'
            '\nelse: raise AssertionError("network permitted")'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_cpu_evaluation_uses_same_fd_adapter_channel_and_results_envelope(self):
        from archive.tests.test_recognition_evaluation import EvaluationFixture
        from archive.services.recognition_evaluation import validate_results, run_cpu
        fixture = EvaluationFixture(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.manifest['evidence_type'] = 'public'
        stable_id = 'avilist:2025b:0'
        fixture.manifest['images'][0]['truth_set'] = [stable_id]
        fixture.manifest['images'][0]['taxonomy'] = [dict(id=stable_id,snapshot='AviList-2025b',external_id='0',
            scientific_name=self.labels[0]['scientific_name'],evidence='Control snapshot identity')]
        fixture.manifest['images'][3]['pixel_review']['visible_birds'] = [
            {'position':'control '+s,'species_id':s,'identification':'synthetic contract only'}
            for s in fixture.manifest['images'][3]['truth_set']]
        fixture.protocol['execution'] = {'python':sys.executable,'model_lock':str(self.root/'model.lock'),
                                         'memory_mb':6144,'timeout_seconds':30}
        adapter = self.fake_adapter()
        calls = []
        class Channel:
            def __init__(inner,**kwargs):
                calls.append(kwargs)
                inner.process = SimpleNamespace(pid=os.getpid())
                inner.ready = {'resources':{'memory_control':'enforced','soft_bytes':6144*1024*1024,
                                           'hard_bytes':6144*1024*1024},'injected_control':True}
            def start(inner): return inner
            def wait_ready(inner): return inner.ready
            def submit(inner,**kwargs):
                calls.append(kwargs)
                inner.fd = os.dup(kwargs['input_fd'])
                inner.payload = kwargs
            def poll(inner):
                try:
                    return local_model.infer_readonly(inner.fd,input_sha256=inner.payload['input_sha256'],
                        input_size=inner.payload['input_size'],artifact=inner.payload['artifact'])
                finally:
                    os.close(inner.fd)
            def close(inner): calls.append('closed')
        with patch.object(local_model,'_get_adapter',return_value=adapter), \
             patch('archive.services.recognition_process.InferenceChannel',Channel), \
             patch.object(sys,'platform','linux'), \
             patch.object(local_model,'_linux_sample',return_value={'VmRSS':10,'VmHWM':20,'VmPeak':30}):
            results = local_model.run_cpu_evaluation(model_manifest=str(self.path),manifest=fixture.manifest,
                protocol=fixture.protocol,data_root=fixture.data,smoke=True)
        results['smoke'] = True
        validate_results(results,fixture.manifest,fixture.protocol)
        self.assertEqual(len(results['images']),1)
        self.assertEqual(results['images'][0]['ranked_candidates'][0]['species_id'],stable_id,
            '评测命中必须按snapshot+external_id连接公开稳定ID，不能拿本库随机UUID与truth_set直接比较')
        from archive.services.recognition_evaluation import build_report
        self.assertEqual(build_report(fixture.manifest,fixture.protocol,results)['metrics']['single']['top1_hits'],1)
        self.assertEqual(results['images'][0]['threshold_outcome'],'skipped')
        self.assertEqual(results['environment']['target_kind'],'unattested_linux')
        self.assertEqual(calls[1]['artifact']['provider'],packages.PROVIDER)
        self.assertEqual(calls[0]['limits'].memory_mb,6144)
        self.assertEqual(calls[-1],'closed')
        # These injected counters/tensors are control evidence, never written as a real report.
        self.assertFalse((self.root/'report.json').exists())

    def test_evaluation_rejects_unfrozen_resource_budget_before_channel_start(self):
        from archive.tests.test_recognition_evaluation import EvaluationFixture
        from archive.services.recognition_evaluation import EvaluationError
        fixture = EvaluationFixture(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with patch.object(sys,'platform','linux'), patch('archive.services.recognition_process.InferenceChannel') as start:
            with self.assertRaisesRegex(EvaluationError,'frozen_execution_budget_required'):
                local_model.run_cpu_evaluation(model_manifest=str(self.path),manifest=fixture.manifest,
                    protocol=fixture.protocol,data_root=fixture.data)
            start.assert_not_called()

    def test_evaluation_mapping_rejects_wrong_snapshot_missing_or_ambiguous_concepts(self):
        checked=packages.verify_manifest(self.manifest)
        label=self.labels[0]
        candidate=dict(rank=1,raw_label=label['scientific_name'],species_id=label['species_id'],
            score=.8,mapping_status='mapped',mapping_evidence=label['mapping_evidence'])
        concept=dict(id='avilist:2025b:0',snapshot='AviList-2025b',external_id='0',
            scientific_name=label['scientific_name'],evidence='Control')
        corpus={'images':[{'included':True,'taxonomy':[concept]}]}
        original=copy.deepcopy(candidate)
        result=local_model.evaluation_concept_candidates(checked,corpus,[candidate])[0]
        self.assertEqual(result['species_id'],'avilist:2025b:0')
        self.assertEqual((result['score'],result['rank']),(.8,1))
        self.assertEqual(candidate,original)
        for field,value in [('snapshot','AviList-2025a'),('external_id','other'),('scientific_name','Guessed name')]:
            bad={'images':[{'included':True,'taxonomy':[{**concept,field:value}]}]}
            self.assertIsNone(local_model.evaluation_concept_candidates(checked,bad,[candidate])[0]['species_id'])
        for field,value in [('species_id',str(uuid.uuid4())),('mapping_evidence','guessed')]:
            self.assertIsNone(local_model.evaluation_concept_candidates(checked,corpus,[{**candidate,field:value}])[0]['species_id'])
        corpus['images'][0]['taxonomy'].append({**concept,'id':'ambiguous'})
        self.assertIsNone(local_model.evaluation_concept_candidates(checked,corpus,[candidate])[0]['species_id'])
        self.assertIsNone(local_model.evaluation_concept_candidates(checked,{'images':[]},[candidate])[0]['species_id'])

    def test_nas_evidence_cannot_be_selected_by_a_device_string(self):
        self.assertEqual(local_model._nas_identity({'target_kind':'nas','target_id':'NAS'}),
                         {'evidence_type':'model_cpu','target_kind':'unattested_linux'})
        record = self.root/'nas.json'
        record.write_text(json.dumps({'approved_target':True,'target_id':'NAS'}))
        with self.assertRaises(packages.ModelPackageError):
            local_model._nas_identity({'nas_attestation':{'path':str(record),'sha256':'a'*64}})

    def test_cpu_identity_ignores_dynamic_mhz_bogomips_order_and_spacing(self):
        cpu = 'processor : 0\nvendor_id : GenuineIntel\ncpu family : 6\nmodel : 63\nmodel name : Intel Xeon E5-2676 v3\nstepping : 2\nflags : avx sse2 fpu\ncpu MHz : 1200.500\nbogomips : 4600.00\n'
        changed = cpu.replace('1200.500','2300.999').replace('4600.00','4599.99').replace('avx sse2 fpu','fpu avx   sse2')
        self.assertEqual(packages.digest(local_model._cpu_identity(cpu)),
                         packages.digest(local_model._cpu_identity(changed)))
        self.assertNotIn('1200.500',json.dumps(local_model._cpu_identity(cpu)))

    def test_cpu_identity_stable_field_change_invalidates_approved_record(self):
        cpu = 'vendor_id : GenuineIntel\ncpu family : 6\nmodel : 63\nmodel name : Intel Xeon E5-2676 v3\nstepping : 2\nflags : avx sse2 fpu\n'
        approved = {'schema_version':1,'approved_target':True,'target_id':'CONTROL ONLY',
            'approval_evidence_ref':'synthetic identity contract, not actual NAS evidence',
            'host':{'machine':'x86_64','kernel_release':'control-kernel',
                'cpu_identity_sha256':packages.digest(local_model._cpu_identity(cpu)),
                'memory_total_bytes':1024*1024,'network_interfaces':['lo']}}
        record = self.root/'control-nas.json'
        raw = json.dumps(approved).encode()
        record.write_bytes(raw)
        declaration = {'nas_attestation':{'path':str(record),'sha256':hashlib.sha256(raw).hexdigest()}}
        observed = {'cpu':cpu}
        def proc(path,*args,**kwargs):
            return {'/proc/cpuinfo':observed['cpu'],'/proc/meminfo':'MemTotal: 1024 kB\n',
                    '/proc/net/dev':' lo: 0 0 0 0\n'}[str(path)]
        with patch.object(Path,'read_text',proc), patch.object(os,'uname',return_value=SimpleNamespace(
                machine='x86_64',release='control-kernel')):
            self.assertEqual(local_model._nas_identity(declaration)['target_id'],'CONTROL ONLY')
            for old,new in [('GenuineIntel','OtherVendor'),('model : 63','model : 64'),
                            ('stepping : 2','stepping : 3'),('avx sse2 fpu','sse2 fpu')]:
                observed['cpu'] = cpu.replace(old,new)
                with self.subTest(field=old), self.assertRaisesRegex(packages.ModelPackageError,'target_or_offline'):
                    local_model._nas_identity(declaration)
        with self.assertRaisesRegex(packages.ModelPackageError,'identity_incomplete'):
            local_model._cpu_identity('cpu MHz : 2300.500\n')


class AdapterPublicationTests(PackageFixture, RecognitionFixture):
    def test_actual_adapter_output_crosses_real_channel_and_publishes_active_run(self):
        """Controlled tensors, real adapter/FD/process/dispatcher/publication; no model claim."""
        import time
        import inspect
        import textwrap
        from archive.models import (ModelArtifact, RecognitionRun, RecognitionState,
            RecognitionCandidate, SourceSnapshot, Species, SpeciesConcept, PhotoSpecies, ArchiveOperation)
        from archive.services.recognition import RecognitionDispatcher, set_current_model
        from archive.services.recognition_process import InferenceChannel, InferenceLimits
        from archive.services.paths import open_root
        snapshot = SourceSnapshot.objects.create(source='AviList', version='2025b', sha256='a'*64,
            acquired_at='2026-10-17', download_page_url='https://example.org/control',
            license_url='https://example.org/license', attribution='Control only',
            row_count=3, species_count=3, manifest={})
        for row in self.labels:
            if row['species_id']:
                species = Species.objects.create(id=row['species_id'], scientific_name=row['scientific_name'],
                    scientific_key=row['scientific_name'].lower(), source_snapshot=snapshot)
                SpeciesConcept.objects.create(snapshot=snapshot, species=species,
                    external_id=row['external_id'], evidence='synthetic exact catalog concept')
        artifact = ModelArtifact.objects.create(provider=packages.PROVIDER, version='1',
            revision=packages.REVISION, manifest=self.manifest,
            manifest_digest=packages.digest(self.manifest), selection_state='selected')
        set_current_model(artifact.pk)
        photo, path = self.imported()
        before = path.read_bytes()
        # Only the test's subprocess caller replaces heavy modules. No artifact/setting
        # or production test_provider selects this adapter or supplies candidates.
        controls = '\n'.join(inspect.getsource(cls) for cls in (Scalar, FakeFeatures, FakeScores, FakePixels))
        controls += '\n'+textwrap.dedent(inspect.getsource(AdapterControlTests.fake_adapter))
        script = '''
import json, sys
from types import SimpleNamespace
from archive.services import local_model, model_packages as packages
from archive.services.recognition_process import _child, InferenceLimits
'''+controls+'''
manifest = json.loads(sys.argv[7])
packages.GLOBAL_SPECIES_COUNT = 3
packages.WEIGHTS_SHA256 = manifest['files'][0]['sha256']
packages.CONFIG_SHA256 = manifest['config_digest']
packages.BPE_SHA256 = manifest['tokenizer_digest']
adapter = fake_adapter(SimpleNamespace(manifest=manifest))
local_model._get_adapter = lambda manifest: adapter
assert sys.argv[6] == 'model'
_child(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4],
       InferenceLimits(**json.loads(sys.argv[5])), False)
'''
        real_popen = subprocess.Popen
        def controlled_caller(args, **kwargs):
            self.assertEqual(args[1:3], ['-m', 'archive.services.recognition_process'])
            self.assertEqual(args[-1], 'model')
            return real_popen([args[0], '-c', script, *args[3:], json.dumps(self.manifest)], **kwargs)
        channel = InferenceChannel(python=sys.executable, lock_path=self.data/'recognition.lock',
            limits=InferenceLimits(512, 5))
        self.addCleanup(channel.close)
        self.assertIsNone(channel.test_provider)
        with patch('archive.services.recognition_process.subprocess.Popen', side_effect=controlled_caller):
            channel.start().wait_ready()
        inbox_fd, data_fd = open_root(self.inbox), open_root(self.data)
        self.addCleanup(os.close, inbox_fd)
        self.addCleanup(os.close, data_fd)
        dispatcher = RecognitionDispatcher(channel, inbox_fd, data_fd)
        self.addCleanup(dispatcher.close_input)
        dispatcher.tick()
        run = RecognitionRun.objects.get(photo=photo)
        deadline = time.monotonic()+6
        while run.state == 'running' and time.monotonic() < deadline:
            dispatcher.tick()
            run.refresh_from_db()
            time.sleep(.01)
        self.assertEqual((run.state, run.safe_error), ('succeeded', ''),
            '实际适配器输出必须满足rank合同，不能发布为invalid_result')
        values = list(RecognitionCandidate.objects.filter(run=run).order_by('rank').values(
            'rank', 'raw_label', 'score', 'species_id', 'mapping_status', 'mapping_evidence'))
        self.assertEqual([r['rank'] for r in values], [1, 2, 3])
        self.assertEqual([r['score'] for r in values], [.9, .8, .7])
        self.assertEqual([r['raw_label'] for r in values], [r['scientific_name'] for r in self.labels])
        self.assertEqual([str(r['species_id']) if r['species_id'] else None for r in values],
            [r['species_id'] for r in self.labels])
        self.assertEqual(values[1]['mapping_status'], 'unmapped')
        self.assertEqual(values[0]['mapping_evidence'],
            'Frozen catalog '+snapshot.sha256+' concept '+self.labels[0]['external_id'])
        self.assertEqual(RecognitionState.objects.get(photo=photo).active_run_id, run.pk)
        self.assertFalse(PhotoSpecies.objects.exists())
        self.assertFalse(ArchiveOperation.objects.exists())
        self.assertEqual(path.read_bytes(), before)
        self.assertIsNone(dispatcher.input)
        self.assertFalse(list(self.data.glob('.recognition-*')))


class ModelMaintenanceTests(PackageFixture,TestCase):
    def test_export_is_complete_deterministic_catalog_only_and_never_overwrites(self):
        from archive.models import SourceSnapshot,Species,SpeciesConcept
        snapshot = SourceSnapshot.objects.create(source='AviList',version='2025b',sha256='a'*64,
            acquired_at='2026-10-17',download_page_url='https://www.avilist.org/',
            license_url='https://creativecommons.org/licenses/by/4.0/',attribution='Control only',
            row_count=3,species_count=3,manifest={})
        for row in reversed(self.labels):
            species = Species.objects.create(scientific_name=row['scientific_name'],
                scientific_key=row['scientific_name'].lower(),source_snapshot=snapshot)
            SpeciesConcept.objects.create(species=species,snapshot=snapshot,
                external_id=row['external_id'],evidence='Control concept evidence')
        Species.objects.create(source_kind='local',local_name='私人鸟名',local_key='私人鸟名')
        output = self.root/'export'
        call_command('preparemodel',export_labels=True,snapshot=str(snapshot.pk),output=str(output),stdout=io.StringIO())
        labels = json.loads((output/'labels.json').read_text())
        self.assertEqual([r['scientific_name'] for r in labels],[r['scientific_name'] for r in self.labels])
        self.assertTrue(all(r['identity_kind']=='catalog' for r in labels))
        self.assertNotIn('私人鸟名',(output/'prompts.json').read_text())
        with self.assertRaises(CommandError):
            call_command('preparemodel',export_labels=True,snapshot=str(snapshot.pk),output=str(output))

    def test_register_is_candidate_only_and_idempotent(self):
        from archive.models import ModelArtifact
        call_command('preparemodel',register=True,manifest=str(self.path),stdout=io.StringIO())
        call_command('preparemodel',register=True,manifest=str(self.path),stdout=io.StringIO())
        item = ModelArtifact.objects.get()
        self.assertEqual(item.selection_state,'candidate')
        self.assertFalse(item.is_current)
        self.assertEqual(item.manifest,self.manifest)

    def test_activate_requires_full_selection_report_and_keeps_candidate(self):
        from archive.models import ModelArtifact
        call_command('preparemodel',register=True,manifest=str(self.path),stdout=io.StringIO())
        item = ModelArtifact.objects.get()
        selection = self.root/'selection.json'
        selection.write_text(json.dumps({'state':'selected','checks':{'license':True}}))
        with self.assertRaises(CommandError):
            call_command('preparemodel',activate=True,artifact=str(item.pk),selection=str(selection))
        item.refresh_from_db()
        self.assertEqual(item.selection_state,'candidate')
        self.assertFalse(item.is_current)


class InstallationReviewTests(SimpleTestCase):
    def setUp(self):
        self.inventory = packages.load_json('docs/recognition/installation-inventory.json')

    def test_complete_linux_inventory_includes_base_and_full_model_closure(self):
        result = packages.verify_inventory(self.inventory)
        self.assertEqual(result['candidate_packages'],36)
        self.assertEqual(result['model_files'],5)
        self.assertFalse(result['installation_authorized'])
        for name,expected in [('.venv-evaluation',6),('.venv-recognition',31)]:
            self.assertEqual(len(self.inventory['environments'][name]['packages']),expected)

    def test_missing_hash_wrong_version_nonlinux_source_or_unresolved_blocks_review(self):
        for mutate in (lambda d:d['packages'][0].update(sha256=''),
                       lambda d:d['packages'][0].update(version='latest'),
                       lambda d:d['packages'][0].update(filename='x-1-cp312-cp312-macosx_11_0_arm64.whl'),
                       lambda d:d['packages'][0].update(url='https://example.org/x.whl'),
                       lambda d:d.update(unresolved=['missing license']),
                       lambda d:d['packages'][0].update(license='UNKNOWN')):
            value = copy.deepcopy(self.inventory)
            mutate(value)
            with self.subTest(mutate=mutate),self.assertRaises(packages.ModelPackageError):
                packages.verify_inventory(value)

    def test_dependency_row_or_metadata_forgery_does_not_pass_closure(self):
        for mutate in (lambda d:d['packages'][0].update(required_dependencies=[]),
                       lambda d:d['packages'][0].update(metadata_sha256='a'*64),
                       lambda d:d['packages'].pop(),
                       lambda d:d['environments']['.venv-evaluation']['packages'].pop(),
                       lambda d:d['environments']['.venv-recognition'].update(lock_sha256='0'*64)):
            value = copy.deepcopy(self.inventory)
            mutate(value)
            with self.subTest(mutate=mutate),self.assertRaises(packages.ModelPackageError):
                packages.verify_inventory(value)

    def test_cpu_pair_and_platform_marker_target_are_fixed(self):
        for mutate in (lambda d:next(p for p in d['packages'] if p['name']=='torch').update(version='2.13.0'),
                       lambda d:d['dependency_closure']['target_marker_environment'].update(sys_platform='darwin'),
                       lambda d:d['packages'][0].update(filename='anyio-4.15.1-cp313-cp313-manylinux_2_28_x86_64.whl')):
            value = copy.deepcopy(self.inventory)
            mutate(value)
            with self.subTest(mutate=mutate),self.assertRaises(packages.ModelPackageError):
                packages.verify_inventory(value)

    def test_marker_parser_cannot_evaluate_code_and_version_constraints_are_not_ignored(self):
        with self.assertRaises(packages.ModelPackageError):
            packages._marker('__import__("os").system("x")',{})
        self.assertTrue(packages._version_satisfies('2.13.0+cpu','==2.13.0'))
        self.assertFalse(packages._version_satisfies('8.3.5','!=8.3.*,>=5.3.0'))
        self.assertFalse(packages._version_satisfies('3.0','<2.0,>=1.5'))

    def test_check_review_command_has_no_database_or_install_side_effects(self):
        out = io.StringIO()
        with patch('subprocess.run',side_effect=AssertionError('unexpected process')):
            call_command('preparemodel',check_review=True,inventory='docs/recognition/installation-inventory.json',stdout=out)
        self.assertEqual(json.loads(out.getvalue())['review'],'complete_pending_03_09_authorization')
