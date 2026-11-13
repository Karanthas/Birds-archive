import csv
import hashlib
import io
import json
import tempfile
import sqlite3
import os
import subprocess
import sys
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from django.apps import apps
from django.core.management import call_command, CommandError
from django.test import TransactionTestCase
from django.db import connection, close_old_connections
from unittest import mock


class CatalogTests(TransactionTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='birds-catalog-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fixture(self, rows=None, version='one', **extra):
        rows = rows or [('concept-a', 'Ardea alba', '白鹭'), ('concept-b', 'Cardinalis cardinalis', '')]
        path = self.root / 'catalog.csv'
        with path.open('w', encoding='utf-8-sig', newline='') as output:
            writer = csv.writer(output)
            writer.writerow(['Concept', 'Scientific', 'Chinese'])
            writer.writerows(rows)
        manifest = dict(source='synthetic-test-only', version=version,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(), acquired_at='2026-10-14',
            download_page_url='https://example.org/test-fixture', license_url='https://example.org/license',
            attribution='Synthetic fixture, not global data', row_count=len(rows), species_count=len(rows),
            columns={'external_id':'Concept', 'scientific_name':'Scientific', 'chinese_name':'Chinese'},
            concept_evidence='Explicit synthetic concepts, names are not keys',
            chinese_name_evidence='Synthetic test data only', chinese_name_license_url='https://example.org/license')
        manifest.update(extra)
        mpath = self.root / 'manifest.json'
        mpath.write_text(json.dumps(manifest), encoding='utf-8')
        return path, mpath

    def load(self, *args, **kwargs):
        output = io.StringIO()
        call_command('importcatalog', file=str(args[0]), manifest=str(args[1]), stdout=output, **kwargs)
        return json.loads(output.getvalue())

    def test_cli_import_is_available_and_imports_two_concepts(self):
        path, manifest = self.fixture()
        succeeded = False
        try:
            result = self.load(path, manifest)
            succeeded = result['species_count'] == 2
        except CommandError:
            pass
        self.assertTrue(succeeded, 'CLI必须完整导入两个合成概念而不是不存在或假成功')

    def test_import_committed_between_snapshot_and_concepts_is_rechecked(self):
        from django.db import close_old_connections
        from archive.models import SourceSnapshot, Species, SpeciesConcept
        from archive.services.catalog import import_catalog, CatalogError
        files = self.fixture()
        main_thread = threading.get_ident()
        interleaved = []
        original_filter = SpeciesConcept.objects.filter
        def competing_import():
            close_old_connections()
            try:
                return import_catalog(*files)['status']
            finally:
                close_old_connections()
        def after_missing_snapshot(*args, **kwargs):
            if threading.get_ident() == main_thread and not interleaved:
                interleaved.append(True)
                # The first reader saw no snapshot, but a genuine second
                # connection commits before that reader loads concepts.
                with ThreadPoolExecutor(max_workers=1) as pool:
                    self.assertEqual(pool.submit(competing_import).result(timeout=10), 'imported')
            return original_filter(*args, **kwargs)
        with mock.patch.object(SpeciesConcept.objects, 'filter', side_effect=after_missing_snapshot):
            try:
                outcome = import_catalog(*files)['status']
            except CatalogError as error:
                outcome = str(error)
        self.assertEqual(outcome, 'unchanged', '写事务内必须重新核验另一导入器刚提交的同快照')
        self.assertEqual(interleaved, [True])
        self.assertEqual((SourceSnapshot.objects.count(), Species.objects.count(), SpeciesConcept.objects.count()), (1, 2, 2))

    def test_idempotency_and_foreign_keys_survive_rename(self):
        from archive.models import Species, SpeciesConcept, Alias, SourceSnapshot
        from archive.services.catalog import search_species
        files=self.fixture(); self.load(*files); self.load(*files)
        self.assertEqual(Species.objects.count(),2)
        self.assertEqual(SourceSnapshot.objects.count(),1)
        original=Species.objects.get(scientific_name='Ardea alba')
        held=SpeciesConcept.objects.get(species=original)
        mapping={'concept-a':{'species_id':str(original.pk),'evidence':'Same synthetic concept explicitly verified'},
                 'concept-b':{'species_id':str(Species.objects.get(scientific_name='Cardinalis cardinalis').pk),
                              'evidence':'Same synthetic concept explicitly verified'}}
        newer=self.fixture([('concept-a','Ardea test','测试新名'),('concept-b','Cardinalis cardinalis','')],version='two',concept_mapping=mapping)
        self.load(*newer); self.load(*newer)
        held.refresh_from_db(); original.refresh_from_db()
        self.assertEqual(held.species_id,original.pk)
        self.assertEqual(original.scientific_name,'Ardea test')
        self.assertEqual(list(search_species('白鹭').values_list('pk',flat=True)),[original.pk])
        self.assertEqual(Species.objects.count(),2)
        self.assertEqual(Alias.objects.count(),3)

    def test_same_names_remain_two_concepts(self):
        from archive.models import Species
        self.load(*self.fixture([('a','Ardea alba','同名'),('b','Ardea other','同名')]))
        self.assertEqual(Species.objects.count(),2)
        self.assertEqual(len(set(Species.objects.values_list('pk',flat=True))),2)

    def test_invalid_packages_are_rejected_without_any_partial_rows(self):
        from archive.models import Species, SourceSnapshot
        for reason in ('hash','columns','encoding','count','duplicate','chinese-license','english-fallback','bad-last-row'):
            with self.subTest(reason=reason):
                path,mpath=self.fixture()
                manifest=json.loads(mpath.read_text())
                if reason=='hash': manifest['sha256']='0'*64
                elif reason=='columns': manifest['columns']['external_id']='Not present'
                elif reason=='encoding': path.write_bytes(b'\xff\xfe')
                elif reason=='count': manifest['row_count']=20
                elif reason=='duplicate':
                    path,mpath=self.fixture([('a','Ardea alba','同名'),('a','Ardea other','同名')]); manifest=json.loads(mpath.read_text())
                elif reason=='chinese-license': manifest.pop('chinese_name_license_url')
                elif reason=='english-fallback':
                    path,mpath=self.fixture([('a','Ardea alba','Great Egret')]); manifest=json.loads(mpath.read_text())
                else:
                    path,mpath=self.fixture([('a','Ardea alba','白鹭'),('b','', '')]); manifest=json.loads(mpath.read_text())
                if reason!='hash': manifest['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
                mpath.write_text(json.dumps(manifest))
                with self.assertRaises(CommandError): self.load(path,mpath)
                self.assertEqual(Species.objects.count(),0)
                self.assertEqual(SourceSnapshot.objects.count(),0)

    def test_same_version_with_different_hash_refused(self):
        from archive.models import Species
        self.load(*self.fixture())
        with self.assertRaisesMessage(CommandError,'version_conflict'):
            self.load(*self.fixture([('a','Changed name','')]))
        self.assertEqual(Species.objects.count(),2)

    def test_dry_run_and_ambiguous_changes_do_not_reassign(self):
        from archive.models import Species, SourceSnapshot
        self.load(*self.fixture())
        original=list(Species.objects.values_list('pk','scientific_name'))
        files=self.fixture(version='two')
        report=self.load(*files,dry_run=True)
        self.assertEqual(report['status'],'review_required')
        self.assertEqual(len(report['unresolved']),2)
        with self.assertRaisesMessage(CommandError,'concept_review_required'): self.load(*files)
        self.assertEqual(list(Species.objects.values_list('pk','scientific_name')),original)
        self.assertEqual(SourceSnapshot.objects.count(),1)

    def test_split_or_merge_mapping_refused(self):
        from archive.models import Species
        self.load(*self.fixture())
        ident=str(Species.objects.first().pk)
        files=self.fixture([('new-a','Split one',''),('new-b','Split two','')],version='two',
            concept_mapping={k:{'species_id':ident,'evidence':'attempted split'} for k in ('new-a','new-b')})
        self.assertEqual(self.load(*files,dry_run=True)['status'],'review_required')
        with self.assertRaises(CommandError): self.load(*files)
        self.assertEqual(Species.objects.count(),2)

    def test_invalid_mapping_json_types_are_safe_and_leave_catalog_unchanged(self):
        from archive.models import Alias, Species, SpeciesConcept, SourceSnapshot
        self.load(*self.fixture())
        ident = str(Species.objects.get(scientific_name='Ardea alba').pk)
        before = [list(model.objects.order_by('pk').values())
                  for model in (Species, SpeciesConcept, Alias, SourceSnapshot)]
        invalid_entries = [123, None, [], {}, {'species_id': ident},
            {'evidence': 'explicit evidence'},
            *[{'species_id': value, 'evidence': 'explicit evidence'}
              for value in (123, None, [], '', 'not-a-uuid')],
            *[{'species_id': ident, 'evidence': value}
              for value in (123, None, [], '', 'bad\x00evidence', 'x' * 4001)]]
        for entry in invalid_entries:
            with self.subTest(entry=entry):
                files = self.fixture(version='two', concept_mapping={'concept-a': entry})
                for dry_run in (True, False):
                    with self.subTest(dry_run=dry_run):
                        with self.assertRaisesMessage(CommandError, 'invalid_concept_mapping'):
                            self.load(*files, dry_run=dry_run)
                self.assertEqual([list(model.objects.order_by('pk').values())
                    for model in (Species, SpeciesConcept, Alias, SourceSnapshot)], before)

    def test_real_cli_rejects_mapping_types_and_preserves_existing_species_uuid(self):
        inbox = self.root / 'inbox'
        data = self.root / 'data'
        inbox.mkdir()
        data.mkdir()
        project = Path(__file__).resolve().parents[2]
        env = {'BIRDS_INBOX_ROOT': str(inbox), 'BIRDS_DATA_ROOT': str(data),
               'BIRDS_SECRET_KEY': 'isolated-cli-catalog-only',
               'BIRDS_OWNER_USERNAME': 'isolated-owner', 'BIRDS_LOCAL_HTTP': '1'}
        def command(*args):
            return subprocess.run([sys.executable, str(project / 'manage.py'), *args,
                '--settings=config.settings'], cwd=project, env=env,
                capture_output=True, text=True, timeout=30)
        migration = command('migrate', '--noinput')
        self.assertEqual(migration.returncode, 0, migration.stderr)
        first = self.fixture()
        imported = command('importcatalog', '--file', str(first[0]), '--manifest', str(first[1]))
        self.assertEqual(imported.returncode, 0, imported.stderr)
        database = data / 'archive.sqlite3'
        def snapshot():
            with sqlite3.connect(database) as reader:
                return {table: reader.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()
                        for table in ('archive_species', 'archive_speciesconcept',
                                      'archive_alias', 'archive_sourcesnapshot')}
        before = snapshot()
        with sqlite3.connect(database) as reader:
            mapping = {external: {'species_id': str(uuid.UUID(ident)),
                                  'evidence': 'same synthetic concept verified'}
                       for external, ident in reader.execute(
                           'SELECT external_id, species_id FROM archive_speciesconcept')}
        ident = mapping['concept-a']['species_id']
        invalid_entries = [123, None, [], {}, {'species_id': ident},
            {'evidence': 'explicit evidence'},
            *[{'species_id': value, 'evidence': 'explicit evidence'}
              for value in (123, None, [], '', 'not-a-uuid')],
            {'species_id': ident, 'evidence': 123}]
        for entry in invalid_entries:
            with self.subTest(entry=entry):
                invalid = self.fixture(version='two', concept_mapping={
                    'concept-a': entry})
                rejected = command('importcatalog', '--file', str(invalid[0]),
                                   '--manifest', str(invalid[1]))
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn('invalid_concept_mapping', rejected.stderr)
                self.assertNotIn('Traceback', rejected.stderr)
                self.assertEqual(snapshot(), before)
        updated = self.fixture([('concept-a', 'Ardea renamed', '新白鹭'),
                                ('concept-b', 'Cardinalis cardinalis', '')],
                               version='two', concept_mapping=mapping)
        succeeded = command('importcatalog', '--file', str(updated[0]), '--manifest', str(updated[1]))
        self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
        self.assertEqual(json.loads(succeeded.stdout)['status'], 'imported')
        after = snapshot()
        self.assertEqual({row[0] for row in after['archive_species']},
                         {row[0] for row in before['archive_species']})
        self.assertEqual(len(after['archive_species']), 2)
        self.assertEqual(len(after['archive_sourcesnapshot']), 2)
        with sqlite3.connect(database) as reader:
            renamed_id = reader.execute(
                'SELECT id FROM archive_species WHERE scientific_name = ?', ('Ardea renamed',)).fetchone()
        self.assertEqual(str(uuid.UUID(renamed_id[0])), mapping['concept-a']['species_id'])

    def test_transaction_failure_rolls_back_entire_snapshot(self):
        from archive.models import Species, SpeciesConcept, SourceSnapshot
        files=self.fixture()
        with mock.patch.object(SpeciesConcept.objects,'bulk_create',side_effect=RuntimeError('controlled failure')):
            with self.assertRaises(RuntimeError): self.load(*files)
        self.assertEqual(Species.objects.count(),0)
        self.assertEqual(SourceSnapshot.objects.count(),0)

    def test_reconnect_persists_cli_import_and_unicode_search(self):
        from archive.models import Species
        from archive.services.catalog import search_species
        self.load(*self.fixture())
        connection.close()
        self.assertEqual(Species.objects.count(),2)
        self.assertEqual(search_species(' ＣＡＲＤＩＮＡＬＩＳ ').get().scientific_name,'Cardinalis cardinalis')
        self.assertEqual(search_species('白鹭').count(),1)
        self.assertEqual(search_species('').count(),0)

    def test_another_connection_never_sees_partial_import(self):
        from archive.models import SpeciesConcept
        from django.conf import settings
        original=SpeciesConcept.objects.bulk_create
        observed=[]
        def check_read(*args,**kwargs):
            with sqlite3.connect(settings.DATABASES['default']['NAME'],timeout=2) as reader:
                observed.append(reader.execute('select count(*) from archive_species').fetchone()[0])
                observed.append(reader.execute('select count(*) from archive_sourcesnapshot').fetchone()[0])
            return original(*args,**kwargs)
        with mock.patch.object(SpeciesConcept.objects,'bulk_create',side_effect=check_read):
            self.load(*self.fixture())
        self.assertEqual(observed,[0,0])

    def test_two_importers_converge_on_one_snapshot(self):
        from archive.models import Species, SourceSnapshot
        from archive.services.catalog import import_catalog
        files=self.fixture()
        def run(_):
            close_old_connections()
            try: return import_catalog(*files)['status']
            finally: connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool: statuses=list(pool.map(run,range(2)))
        self.assertEqual(sorted(statuses),['imported','unchanged'])
        self.assertEqual(Species.objects.count(),2)
        self.assertEqual(SourceSnapshot.objects.count(),1)

    def test_local_inputs_refuse_symlinks_fifos_and_oversized_files(self):
        from archive.models import Species
        files=self.fixture()
        link=self.root/'link.csv'; link.symlink_to(files[0])
        with self.assertRaises(CommandError): self.load(link,files[1])
        fifo=self.root/'input.fifo'; os.mkfifo(fifo)
        with self.assertRaises(CommandError): self.load(fifo,files[1])
        with mock.patch('archive.services.catalog.os.fstat') as fstat:
            fstat.return_value.st_mode=0o100600
            fstat.return_value.st_size=65*1024*1024
            with self.assertRaises(CommandError): self.load(*files)
        self.assertEqual(Species.objects.count(),0)

    def test_manifest_only_change_cannot_mutate_a_registered_version(self):
        self.load(*self.fixture())
        files=self.fixture(attribution='changed metadata')
        with self.assertRaisesMessage(CommandError,'version_conflict'): self.load(*files)


class CatalogPageTests(TransactionTestCase):
    fixture=CatalogTests.fixture
    load=CatalogTests.load

    def setUp(self):
        CatalogTests.setUp(self)
        from django.contrib.auth.models import User
        from django.conf import settings

    def test_private_catalog_page_is_real_and_distinguishes_empty_states(self):
        response=self.client.get('/species/')
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'尚无可用名录')
        self.load(*self.fixture())
        self.assertContains(self.client.get('/species/'),'输入中文名或学名')
        self.assertContains(self.client.get('/species/',{'q':'not-an-existing-bird'}),'未找到相关鸟种')

    def test_global_unicode_names_aliases_and_truthful_count(self):
        from archive.models import Species, Photo
        self.load(*self.fixture([('a','Ardea alba','同名'),('b','Ardea other','同名'),('c','Cardinalis cardinalis','')]))
        before=list(Photo.objects.values())
        response=self.client.get('/species/',{'q':'同名'})
        self.assertContains(response,'2 种')
        for species in Species.objects.filter(chinese_name='同名'):
            self.assertContains(response,str(species.pk))
            self.assertContains(response,species.scientific_name)
        response=self.client.get('/species/',{'q':' ＣＡＲＤＩＮＡＬＩＳ '})
        self.assertContains(response,'1 种'); self.assertContains(response,'中文名暂缺')
        self.assertContains(response,'synthetic-test-only · one')
        self.assertEqual(list(Photo.objects.values()),before)
        self.assertIn('no-store',response['Cache-Control'])

    def test_pagination_stable_order_url_and_limits(self):
        self.load(*self.fixture([(str(i),f'Test bird {i:02d}','') for i in range(25)]))
        response=self.client.get('/species/',{'q':'TEST BIRD','page':1})
        self.assertEqual(len(response.context['page_obj']),24)
        self.assertContains(response,'25 种')
        self.assertContains(response,'q=TEST%20BIRD&amp;page=2')
        second=self.client.get('/species/',{'q':'test bird','page':2})
        self.assertEqual(len(second.context['page_obj']),1)
        self.assertEqual(second.context['page_obj'][0].scientific_name,'Test bird 24')
        for page in ('bogus','-1','9999'):
            self.assertEqual(self.client.get('/species/',{'q':'Test','page':page}).status_code,200)
        self.assertEqual(self.client.get('/species/',{'q':'鸟'*200}).status_code,200)
        self.assertContains(self.client.get('/species/',{'q':'鸟'*201}),'查询不能超过',status_code=400)

    def test_long_text_is_escaped_and_full_details_are_accessible(self):
        long='长中文'*50+'<script>alert(1)</script>'
        self.load(*self.fixture([('a','Test bird',long)]))
        response=self.client.get('/species/',{'q':'Test'})
        self.assertContains(response,'&lt;script&gt;')
        self.assertNotContains(response,'<script>alert')
        self.assertContains(response,'<details')
        self.assertContains(response,'来源／详情')
        self.assertContains(response,'method="get"')

    def test_anonymous_queries_and_old_cookies_read_catalog_without_identity(self):
        from django.test import Client
        from django.conf import settings
        self.load(*self.fixture())
        strangers=[Client(),Client()]
        strangers[1].cookies[settings.SESSION_COOKIE_NAME]='old-session-cookie'
        for client in strangers:
            for method,headers in [('get',{}),('head',{}),('get',{'HTTP_RANGE':'bytes=0-20'}),('get',{'HTTP_HX_REQUEST':'true'})]:
                with self.subTest(method=method,headers=headers):
                    response=getattr(client,method)('/species/?q=Ardea',**headers)
                self.assertEqual(response.status_code,405 if method=='head' else 200)
                if method=='get':
                    self.assertContains(response,'Ardea alba')
                self.assertIn('no-store',response['Cache-Control'])
        response=self.client.get('/species/?q=Ardea',HTTP_X_BIRDS_SESSION_CHECK='1',HTTP_HX_REQUEST='true')
        self.assertContains(response,'Ardea alba')
        self.assertNotIn('HX-Redirect',response)

    def test_database_error_has_safe_copy_and_query_is_preserved(self):
        from django.db import OperationalError
        with mock.patch('archive.views.SourceSnapshot.objects.exists',side_effect=OperationalError('secret path password sentinel')):
            response=self.client.get('/species/',{'q':'白鹭'})
        self.assertContains(response,'暂时无法读取数据',status_code=503)
        self.assertContains(response,'value="白鹭"',status_code=503)
        self.assertNotContains(response,'sentinel',status_code=503)
        self.assertEqual(self.client.post('/species/',{'q':'Ardea'}).status_code,405)
