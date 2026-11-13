"""Generated public-style control JPEGs only; never a real model or NAS claim."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

from django.core.management import get_commands
from django.test import SimpleTestCase
from archive.tests.test_local_model import PackageFixture
from archive.tests.test_recognition_evaluation import protocol
from archive.tests.fixtures import jpeg


# Only this test caller injects a control provider. There is no CLI/env selector.
DRIVER = r'''
import io, json, os, signal, sys, time
from pathlib import Path
from unittest.mock import patch
os.environ['DJANGO_SETTINGS_MODULE']='config.settings_test'
import django
django.setup()
from django.core.management import call_command
from archive.services import model_packages as packages
from archive.services.evaluation_workspace import bootstrap_cohort, enqueue_candidate_runs, export_published_runs
from archive.models import ModelArtifact, Photo, PhotoSpecies, RecognitionRun, RecognitionState, Species
from archive.services.recognition_process import Supervisor, InferenceChannel, InferenceLimits
from django.test import Client
args=json.loads(sys.argv[1])
manifest=packages.load_json(args['model_manifest'])
packages.GLOBAL_SPECIES_COUNT=3
packages.WEIGHTS_SHA256=manifest['files'][0]['sha256']
packages.CONFIG_SHA256=manifest['config_digest']
packages.BPE_SHA256=manifest['tokenizer_digest']
out=io.StringIO()
call_command('reviewcandidate', bootstrap=True, settings='config.settings_test', stdout=out, **args)
session=json.loads(out.getvalue())
root=Path(os.environ['BIRDS_TEST_ROOT'])
labels=packages.load_json(Path(manifest['local_root'])/'labels.json')
output=[dict(rank=1, raw_label=labels[0]['scientific_name'], score=.8,
    species_id=labels[0]['species_id'], mapping_status='mapped', mapping_evidence=labels[0]['mapping_evidence'])]
channel=InferenceChannel(python=sys.executable,lock_path=root/'data'/'recognition.lock',
    limits=InferenceLimits(512,5),test_provider={'candidates':output})
worker_code = """import os,sys,json
os.environ['DJANGO_SETTINGS_MODULE']='config.settings_test'
import django; django.setup()
from archive.services import model_packages as p
m=json.loads(sys.argv[1])
p.GLOBAL_SPECIES_COUNT=3
p.WEIGHTS_SHA256=m['files'][0]['sha256']
p.CONFIG_SHA256=m['config_digest']
p.BPE_SHA256=m['tokenizer_digest']
from django.core.management import execute_from_command_line
execute_from_command_line(['manage.py','runworker','--settings=config.settings_test',*sys.argv[2:]])
"""
supervisor=Supervisor(worker_args=[sys.executable,'-c',worker_code,json.dumps(manifest)],
    worker_env=os.environ.copy(),worker_memory_mb=1024,inference=channel)
def until(predicate):
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        supervisor.tick()
        if predicate(): return
        time.sleep(.02)
    raise AssertionError('pipeline timeout')
try:
    supervisor.start()
    until(lambda: Photo.objects.count()==1)
    first=enqueue_candidate_runs(session['artifact_id'])
    until(lambda: RecognitionRun.objects.filter(reason='evaluation',state='succeeded').exists())
    published=export_published_runs()
    run=RecognitionRun.objects.get(reason='evaluation')
    response=Client().get('/photos/'+str(run.photo_id)+'/')
    assert response.status_code==200
    assert labels[0]['scientific_name'] in response.content.decode()
    assert 'no-store' in response['Cache-Control']
    assert run.candidates.get().species_id==Species.objects.get(scientific_name=labels[0]['scientific_name']).pk
    assert str(run.candidates.get().species_id)!=labels[0]['species_id']
    assert not ModelArtifact.objects.filter(is_current=True).exists()
    assert not PhotoSpecies.objects.exists()
    assert Photo.objects.get().state=='review_pending'
    assert enqueue_candidate_runs(session['artifact_id'])==first
    assert RecognitionRun.objects.filter(reason='evaluation').count()==1
    assert bootstrap_cohort(**args)['session_id']==session['session_id']
    print('WORKSPACE_RESULT='+json.dumps(published))
finally:
    supervisor.close()
'''


class WorkspaceFixture(PackageFixture, SimpleTestCase):
    def setUp(self):
        super().setUp()
        workspace=tempfile.TemporaryDirectory(prefix='birds-tests-evaluation-')
        self.addCleanup(workspace.cleanup)
        self.workspace=Path(workspace.name).resolve()
        (self.workspace/'.birds-test-root').write_text('birds isolated v1')
        self.public=self.root/'public'
        self.public.mkdir()
        sha=jpeg(self.public/'one.jpg')
        self.catalog=self.root/'catalog.csv'
        self.catalog.write_text('id,name\n'+''.join(f'{i},{r["scientific_name"]}\n' for i,r in enumerate(self.labels)))
        catalog_sha=hashlib.sha256(self.catalog.read_bytes()).hexdigest()
        meta=dict(source='AviList',version='2025b',sha256=catalog_sha,row_count=3,species_count=3,
            acquired_at='2026-10-17',download_page_url='https://example.org/catalog',
            license_url='https://example.org/license',attribution='Control',concept_evidence='Control concepts',
            columns={'external_id':'id','scientific_name':'name'})
        self.change_json('snapshot',lambda x:x.update(sha256=catalog_sha))
        self.path.write_text(json.dumps(self.manifest))
        image=dict(photo_id='public-one',path='one.jpg',sha256=sha,size_bytes=(self.public/'one.jpg').stat().st_size,
            split='evaluation',near_duplicate_group='one',training_overlap='unknown',truth_set=['bird'],
            complete_truth_verified=True,included=True,scenarios=['common','confusable'],
            source_url='https://example.org/public',source_revision='1',author='Control',license='CC0-1.0',
            license_url='https://creativecommons.org/publicdomain/zero/1.0/',attribution='Generated control JPEG',
            acquired_at='2026-10-17',label_evidence='Control only',pixel_review={'reviewer':'test','method':'control'},
            taxonomy=[dict(id='bird',snapshot='AviList-2025b',external_id='0',
                scientific_name=self.labels[0]['scientific_name'],evidence='Control')])
        self.corpus=dict(schema_version=1,dataset_id='control',evidence_type='control',images=[image])
        self.args=dict(cohort='single',manifest=str(self.write('corpus.json',self.corpus)),
            protocol=str(self.write('protocol.json',protocol())),data_root=str(self.public),model_manifest=str(self.path),
            catalog_file=str(self.catalog),catalog_manifest=str(self.write('catalog-manifest.json',meta)))

    def write(self,name,value):
        path=self.root/name
        path.write_text(json.dumps(value))
        return path

    def driver(self, code=DRIVER, args=None, root=None):
        env={**os.environ,'BIRDS_TEST_ROOT':str(root or self.workspace),'DJANGO_SETTINGS_MODULE':'config.settings_test',
            'SCAN_INTERVAL_MS':'25','WORKER_JOB_SLEEP_MS':'1'}
        return subprocess.run([sys.executable,'-c',code,json.dumps(args or self.args)],env=env,
            capture_output=True,text=True,timeout=25)


class WorkspaceTracerTests(WorkspaceFixture):
    def test_late_evaluation_publication_rejects_changed_identity_in_real_process(self):
        for mutation in (
            "Photo.objects.filter(pk=run.photo_id).update(metadata_version=99)",
            "RecognitionState.objects.filter(photo_id=run.photo_id).update(requested_generation=99)",
            "type(run.source_observation.source).objects.filter(pk=run.source_observation.source_id).update(fingerprint={'changed':True})",
            "ModelArtifact.objects.filter(pk=run.artifact_id).update(manifest={**manifest,'threshold_version':'changed'})"):
            temp=tempfile.TemporaryDirectory(prefix='birds-tests-late-')
            self.addCleanup(temp.cleanup)
            root=Path(temp.name).resolve()
            (root/'.birds-test-root').write_text('birds isolated v1')
            code=DRIVER.replace("test_provider={'candidates':output}","test_provider={'candidates':output,'delay':.15}")
            boundary="    until(lambda: RecognitionRun.objects.filter(reason='evaluation',state='succeeded').exists())"
            code=code.split(boundary,1)[0]+r'''
    until(lambda: RecognitionRun.objects.filter(reason='evaluation',state='running').exists())
    run=RecognitionRun.objects.select_related('source_observation__source','artifact').get(reason='evaluation')
'''+"    "+mutation+"\n"+r'''
    until(lambda: RecognitionRun.objects.filter(pk=run.pk,state='stale').exists())
    assert not run.candidates.exists()
    assert RecognitionState.objects.get(photo_id=run.photo_id).active_run_id is None
    assert not PhotoSpecies.objects.exists()
finally:
    supervisor.close()
'''
            completed=self.driver(code,root=root)
            self.assertEqual(completed.returncode,0,mutation+completed.stdout+completed.stderr)

    def test_cli_bootstrap_worker_cas_and_existing_detail_without_current(self):
        self.assertIn('reviewcandidate',get_commands(), '隔离candidate必须经CLI、真实worker和发布CAS进入既有审核页')
        completed=self.driver()
        self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)
        result=json.loads(completed.stdout.split('WORKSPACE_RESULT=')[1].splitlines()[0])
        self.assertEqual(result['evidence_type'],'control')
        self.assertEqual(result['cohort'],'single')
        self.assertEqual(result['images'][0]['image_id'],'public-one')
        self.assertNotIn(str(self.workspace),json.dumps(result))
        self.assertEqual((self.public/'one.jpg').read_bytes(),next((self.workspace/'inbox').glob('*.jpg')).read_bytes())

    def test_current_other_reference_and_source_metadata_generation_artifact_conflicts(self):
        checks = r'''
    from archive.services.evaluation_workspace import map_evaluation_candidates
    from archive.services.recognition import publish_result
    from archive.services.recognition_evaluation import EvaluationError
    candidate=output[0]
    mapped=map_evaluation_candidates(run,output)
    for field,value in [('species_id',str(__import__('uuid').uuid4())),('raw_label','Unknown species'),
                        ('mapping_evidence','guessed name')]:
        try:
            map_evaluation_candidates(run,[{**candidate,field:value}])
        except EvaluationError: pass
        else: raise AssertionError('foreign or invented label accepted')
    photo=run.photo
    for field,value in [('metadata_version',photo.metadata_version+1)]:
        Photo.objects.filter(pk=photo.pk).update(**{field:value})
        assert publish_result(run.pk,mapped,artifact_id=run.artifact_id) is False
        try: export_published_runs()
        except EvaluationError: pass
        else: raise AssertionError('stale metadata export')
        Photo.objects.filter(pk=photo.pk).update(**{field:photo.metadata_version})
    state=RecognitionState.objects.get(photo=photo)
    RecognitionState.objects.filter(pk=state.pk).update(requested_generation=run.generation+1)
    assert publish_result(run.pk,mapped,artifact_id=run.artifact_id) is False
    try: export_published_runs()
    except EvaluationError: pass
    else: raise AssertionError('stale generation export')
    RecognitionState.objects.filter(pk=state.pk).update(requested_generation=run.generation)
    assert publish_result(run.pk,mapped,artifact_id=__import__('uuid').uuid4()) is False
    source=run.source_observation.source
    original=source.fingerprint
    type(source).objects.filter(pk=source.pk).update(fingerprint={'changed':True})
    assert publish_result(run.pk,mapped,artifact_id=run.artifact_id) is False
    try: export_published_runs()
    except EvaluationError: pass
    else: raise AssertionError('stale source export')
    type(source).objects.filter(pk=source.pk).update(fingerprint=original)
    assert PhotoSpecies.objects.count()==0
'''
        code=DRIVER.replace("    print('WORKSPACE_RESULT='+json.dumps(published))",checks)
        completed=self.driver(code)
        self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)

    def test_session_rejects_changed_inputs_and_does_not_overwrite_foreign_output(self):
        checks=r'''
    from archive.services.evaluation_workspace import write_publication
    from archive.services.recognition_evaluation import EvaluationError
    destination=root/'data'/'published.json'
    write_publication(destination,published)
    write_publication(destination,published)
    assert destination.stat().st_mode&0o777==0o600
    assert (root/'data'/'evaluation-session.json').stat().st_mode&0o777==0o600
    destination.write_text('foreign file')
    try: write_publication(destination,published)
    except EvaluationError: pass
    else: raise AssertionError('foreign file overwritten')
    assert destination.read_text()=='foreign file'
    try: bootstrap_cohort(**{**args,'cohort':'manual'})
    except EvaluationError: pass
    else: raise AssertionError('session takeover')
    file=Path(args['protocol'])
    raw=file.read_text()
    file.write_text(raw.replace('control-v1','changed-v1'))
    try: enqueue_candidate_runs(session['artifact_id'])
    except EvaluationError: pass
    else: raise AssertionError('changed protocol accepted')
    file.write_text(raw)
    package=ModelArtifact.objects.get(pk=session['artifact_id'])
    ModelArtifact.objects.filter(pk=package.pk).update(manifest={**package.manifest,'threshold_version':'changed'})
    try: export_published_runs()
    except EvaluationError: pass
    else: raise AssertionError('mutable registered candidate')
'''
        completed=self.driver(DRIVER.replace("    print('WORKSPACE_RESULT='+json.dumps(published))",checks))
        self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)


class WorkspaceSafetyTests(WorkspaceFixture):
    def test_three_roots_have_independent_photos_and_manual_never_enqueues(self):
        publications=[]
        for cohort in ('single','own_candidates','manual'):
            temp=tempfile.TemporaryDirectory(prefix='birds-tests-cohort-')
            self.addCleanup(temp.cleanup)
            root=Path(temp.name).resolve()
            (root/'.birds-test-root').write_text('birds isolated v1')
            args={**self.args,'cohort':cohort}
            if cohort != 'manual':
                code=DRIVER
            else:
                worker_setup='worker_code = '+DRIVER.split('worker_code = ',1)[1].split('supervisor=Supervisor',1)[0]
                code=DRIVER.split("root=Path",1)[0]+worker_setup+r'''
import subprocess
call=subprocess.run([sys.executable,'-c',worker_code,json.dumps(manifest),'--once'],
    env=os.environ.copy(),capture_output=True,text=True,timeout=5)
assert call.returncode==0,call.stderr
assert Photo.objects.count()==1
assert not RecognitionRun.objects.exclude(artifact=None).exists()
try: enqueue_candidate_runs(session['artifact_id'])
except ValueError: pass
else: raise AssertionError('manual queued')
published=export_published_runs()
assert all(x['run_id'] is None and x['generation'] is None and x['artifact_id'] is None for x in published['images'])
assert not PhotoSpecies.objects.exists()
print('WORKSPACE_RESULT='+json.dumps(published))
'''
            completed=self.driver(code,args=args,root=root)
            self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)
            publications.append(json.loads(completed.stdout.split('WORKSPACE_RESULT=')[1].splitlines()[0]))
        self.assertEqual(len({p['session_id'] for p in publications}),3)
        self.assertEqual(len({p['workspace_digest'] for p in publications}),3)
        self.assertEqual(len({p['images'][0]['photo_id'] for p in publications}),3)
        from archive.services.recognition_evaluation import validate_traces, digest
        traces=[]
        for p in publications:
            row=p['images'][0]
            traces.append(dict(trace_id=p['cohort'],session_id=p['session_id'],cohort=p['cohort'],partition='evaluation',
                operator='test',observation_type='simulated_browser',browser_evidence_ref='control-only',
                manifest_digest=p['manifest_digest'],protocol_digest=p['protocol_digest'],publication=p,
                publication_digest=p['publication_digest'],images=[dict(photo_id=row['image_id'],sha256=row['sha256'],
                    final_set=['bird'],workspace_photo_id=row['photo_id'],source_observation_id=row['source_observation_id'],
                    **{key:row[key] for key in ('run_id','generation','artifact_id')})],
                actions=[dict(action='confirm',photo_ids=[row['image_id']],at_seconds=1)]))
        bundle=dict(schema_version=1,traces=traces)
        self.assertEqual(validate_traces(bundle,protocol(),self.corpus),bundle)
        for key,value in [('run_id',str(uuid.uuid4())),('generation',999),('artifact_id',str(uuid.uuid4())),
                          ('sha256','f'*64),('source_observation_id',str(uuid.uuid4()))]:
            broken=copy.deepcopy(bundle)
            broken['traces'][0]['images'][0][key]=value
            with self.assertRaises(ValueError):
                validate_traces(broken,protocol(),self.corpus)
        broken=copy.deepcopy(bundle)
        broken['traces'][0]['execution_evidence']={'schema_version':1,'evidence_type':'nas_cpu'}
        with self.assertRaises(ValueError):
            validate_traces(broken,protocol(),self.corpus)

    def test_other_current_stays_unchanged_and_is_not_automatically_dispatched(self):
        # Inject an unrelated current record during this new DB's initialization;
        # no foreign database is copied or commandeered by bootstrap.
        base=DRIVER.split("out=io.StringIO()",1)[0]
        worker_setup='worker_code = '+DRIVER.split('worker_code = ',1)[1].split('supervisor=Supervisor',1)[0]
        code=base+worker_setup+r'''
from archive.services import evaluation_workspace as workspace
original=workspace.call_command
def register_with_other(name,*a,**kw):
    result=original(name,*a,**kw)
    if name=='preparemodel':
        ModelArtifact.objects.create(manifest_digest='d'*64,provider='unrelated',version='1',revision='other',
            manifest={},selection_state='selected',is_current=True)
    return result
with patch.object(workspace,'call_command',side_effect=register_with_other):
    session=bootstrap_cohort(**args)
other=ModelArtifact.objects.get(is_current=True)
import subprocess
call=subprocess.run([sys.executable,'-c',worker_code,json.dumps(manifest),'--once'],
    env=os.environ.copy(),capture_output=True,text=True,timeout=5)
assert call.returncode==0,call.stderr
assert RecognitionRun.objects.get(reason='import').artifact_id is None
assert RecognitionRun.objects.get(reason='import').state=='unavailable'
assert ModelArtifact.objects.get(is_current=True).pk==other.pk
if args['cohort']=='manual':
    export_published_runs()
else:
    enqueue_candidate_runs(session['artifact_id'])
assert ModelArtifact.objects.get(pk=other.pk).selection_state=='selected'
assert ModelArtifact.objects.get(is_current=True).pk==other.pk
'''
        for cohort in ('single','manual'):
            temp=tempfile.TemporaryDirectory(prefix='birds-tests-current-')
            self.addCleanup(temp.cleanup)
            root=Path(temp.name).resolve()
            (root/'.birds-test-root').write_text('birds isolated v1')
            completed=self.driver(code,args={**self.args,'cohort':cohort},root=root)
            self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)

    def guard_code(self, mutation):
        return r'''
import os,sys,json
from pathlib import Path
os.environ['DJANGO_SETTINGS_MODULE']='config.settings_test'
import django; django.setup()
from django.conf import settings
from django.db import connection
from archive.services.evaluation_workspace import assert_evaluation_workspace
root=Path(os.environ['BIRDS_TEST_ROOT'])
''' + mutation + r'''
try: assert_evaluation_workspace()
except (ValueError,OSError): pass
else: raise AssertionError('unsafe workspace accepted')
assert not (root/'data'/'evaluation-session.json').exists()
assert not (root/'data'/'.evaluation.lock').exists()
'''

    def test_settings_explicit_root_marker_owner_and_all_media_roots_fail_before_write(self):
        mutations=["os.environ['DJANGO_SETTINGS_MODULE']='config.settings'", "os.environ.pop('BIRDS_TEST_ROOT')",
            "(root/'.birds-test-root').unlink()", "(root/'.birds-test-root').write_text('wrong marker')",
            "settings.BIRDS_TEST_ROOT=root/'different'",
            "connection.settings_dict['NAME']=root.parent/'external-sentinel.sqlite3'",
            "connection.settings_dict['NAME']=':memory:'", "uid=os.getuid(); os.getuid=lambda:uid+1"]
        mutations += [f"settings.{key}=root.parent/'external-sentinel'" for key in
            ('BIRDS_INBOX_ROOT','BIRDS_DATA_ROOT','BIRDS_ARCHIVE_ROOT','BIRDS_CLAIM_ROOT','STATIC_ROOT')]
        for mutation in mutations:
            (self.workspace/'.birds-test-root').write_text('birds isolated v1')
            completed=self.driver(self.guard_code(mutation))
            self.assertEqual(completed.returncode,0,mutation+completed.stdout+completed.stderr)

    def test_db_and_media_symlinks_are_rejected_without_touching_targets(self):
        for name in ('inbox','archive','claims','static','data/test.sqlite3'):
            code=self.guard_code("target=root.parent/('birds-sentinel-'+root.name)\n"
                "target.mkdir(exist_ok=True)\n"
                f"path=root/'{name}'\n"
                "if path.is_dir(): path.rmdir()\n"
                "path.symlink_to(target,target_is_directory=True)\n")
            completed=self.driver(code)
            self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)
            path=self.workspace/name
            path.unlink()
            if '.' not in path.name:
                path.mkdir()
        sentinel=self.workspace.parent/('birds-sentinel-'+self.workspace.name)
        self.assertEqual(list(sentinel.iterdir()),[])
        sentinel.rmdir()

    def test_bootstrap_rejects_unfamiliar_database_and_manifest_escape_before_session(self):
        base=DRIVER.split("out=io.StringIO()",1)[0]
        checks=r'''
import sqlite3
root=Path(os.environ['BIRDS_TEST_ROOT'])
with sqlite3.connect(root/'data'/'test.sqlite3') as db:
    db.execute('create table private_sentinel (value text)')
    db.execute("insert into private_sentinel values ('preserve')")
try: bootstrap_cohort(**args)
except ValueError: pass
else: raise AssertionError('unfamiliar DB taken over')
assert not (root/'data'/'evaluation-session.json').exists()
with sqlite3.connect(root/'data'/'test.sqlite3') as db:
    assert db.execute('select value from private_sentinel').fetchone()==('preserve',)
'''
        completed=self.driver(base+checks)
        self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)
        for path in ('../one.jpg','/etc/passwd','https://example.org/image'):
            bad=copy.deepcopy(self.corpus)
            bad['images'][0]['path']=path
            self.write('corpus.json',bad)
            completed=self.driver(base+"\ntry: bootstrap_cohort(**args)\nexcept ValueError: pass\nelse: raise AssertionError('unsafe manifest')\n")
            self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)

    def test_bootstrap_interruption_resumes_only_same_frozen_session(self):
        base=DRIVER.split("out=io.StringIO()",1)[0]
        checks=r'''
from unittest.mock import patch
from archive.services import evaluation_workspace as workspace
original=workspace.call_command
def interrupted(name,*a,**kw):
    if name=='importcatalog': raise RuntimeError('controlled interruption after migrate')
    return original(name,*a,**kw)
with patch.object(workspace,'call_command',side_effect=interrupted):
    try: bootstrap_cohort(**args)
    except RuntimeError: pass
root=Path(os.environ['BIRDS_TEST_ROOT'])
first=json.loads((root/'data'/'evaluation-session.json').read_text())
assert first['phase']=='initializing'
second=bootstrap_cohort(**args)
assert first['session_id']==second['session_id']
assert len(list((root/'inbox').glob('*.jpg')))==1
'''
        completed=self.driver(base+checks)
        self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)

    def test_foreign_session_and_unlisted_import_are_not_taken_over(self):
        base=DRIVER.split("out=io.StringIO()",1)[0]
        checks=r'''
root=Path(os.environ['BIRDS_TEST_ROOT'])
session_file=root/'data'/'evaluation-session.json'
session_file.write_text('foreign session sentinel')
try: bootstrap_cohort(**args)
except ValueError: pass
else: raise AssertionError('foreign session accepted')
assert session_file.read_text()=='foreign session sentinel'
assert not (root/'data'/'test.sqlite3').exists()
'''
        completed=self.driver(base+checks)
        self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)
        (self.workspace/'data'/'evaluation-session.json').unlink()
        worker_setup='worker_code = '+DRIVER.split('worker_code = ',1)[1].split('supervisor=Supervisor',1)[0]
        checks=r'''
session=bootstrap_cohort(**args)
root=Path(os.environ['BIRDS_TEST_ROOT'])
file=root/'data'/'evaluation-session.json'
value=json.loads(file.read_text())
value['images'][0]['sha256']='f'*64
file.write_text(json.dumps(value))
import subprocess
call=subprocess.run([sys.executable,'-c',worker_code,json.dumps(manifest),'--once'],env=os.environ.copy(),
    capture_output=True,text=True,timeout=5)
assert not Photo.objects.exists()
assert not RecognitionRun.objects.exists()
'''
        completed=self.driver(base+worker_setup+checks)
        self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)
