"""Only the maintenance controller's order and actual SQLite upgrade are exercised."""
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase

from archive.tests.fixtures import jpeg
from deploy import maintain


RUN = subprocess.run
SESSION = 'a' * 32
SET_ID = '3c7d1201-a173-4d31-8ed2-527d75cb1727'
OLD_IMAGE = 'sha256:' + '1' * 64
NEW_IMAGE = 'sha256:' + '2' * 64
OLD_IDS = {'web': '1' * 64, 'worker': '2' * 64}
NEW_IDS = {'web': '3' * 64, 'worker': '4' * 64}
MIGRATE_FIXTURE = """
import sys

import django
from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

settings.DATABASES['default']['NAME'] = sys.argv[1]
django.setup()
target = ('archive', sys.argv[2])
call_command('migrate', *target, interactive=False, verbosity=0)
if target[1] == '0011_bird_guide':
    apps = MigrationExecutor(connection).loader.project_state([target]).apps
    photo = apps.get_model('archive', 'Photo').objects.create(
        sha256=sys.argv[3], byte_size=int(sys.argv[4]), preview_key='fixture-preview',
        notes='隔离维护测试备注', favorite=True, rating=4, user_location='测试地点',
        metadata_version=3, original_available=True,
    )
    source_model = apps.get_model('archive', 'ImportSource')
    observation_model = apps.get_model('archive', 'SourceObservation')
    source = source_model.objects.create(
        relative_path='confirmed.jpg', photo=photo, sha256=photo.sha256, state='ready',
    )
    source.current_observation = observation_model.objects.create(
        source=source, version=1, photo=photo, sha256=photo.sha256, byte_size=photo.byte_size,
        available=True, state='ready',
    )
    source.save(update_fields=['current_observation'])
    pending = source_model.objects.create(relative_path='pending.jpg')
    pending.current_observation = observation_model.objects.create(source=pending, version=1)
    pending.save(update_fields=['current_observation'])
    apps.get_model('archive', 'Job').objects.create(source=pending, source_version=1, state='pending')
"""


class ComposeProcess:
    """Provide Docker CLI receipts while the controller exercises the real files."""

    def __init__(self, fixture, *, failure=None, migrate=None, proxy=False):
        self.fixture = fixture
        self.failure = failure
        self.migrate = migrate
        self.events = []
        self.state = 'idle'
        self.containers = dict(OLD_IDS)
        if proxy:
            self.containers['lan'] = '5' * 64
        self.running = set(self.containers)
        self.image = OLD_IMAGE

    def _status(self):
        result = {'status': self.state}
        if self.state != 'idle':
            result.update(session_id=SESSION, supervisor_ack={
                'status': 'drained', 'session_id': SESSION, 'forced': False,
                'supervisor_instance_id': 'b' * 32,
                'worker_pid': 42, 'worker_exit': 0, 'model_started': False,
                'model_exit': None,
            })
        return result

    def _snapshot(self):
        target = self.fixture.backup / SET_ID / 'database' / 'archive.sqlite3'
        target.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.fixture.database)) as source, \
                closing(sqlite3.connect(target)) as destination:
            source.backup(destination)

    def _backuparchive(self, verb, options):
        if verb == 'status':
            return self._status()
        self.events.append(verb)
        if verb == 'request':
            self.state = 'requested'
            return self._status()
        if verb == 'create':
            self.fixture.assertIn(SESSION, options)
            self._snapshot()
            self.state = 'active'
            return {'status': 'complete', 'set': '/backup/' + SET_ID}
        if verb == 'verify':
            self.fixture.assertIn('/backup/' + SET_ID, options)
            return {'status': 'verified', 'set_id': SET_ID, 'set': '/backup/' + SET_ID}
        if verb == 'quiesce':
            self.fixture.assertIn(SESSION, options)
            self.state = 'active'
            return {'status': 'quiescent', 'session_id': SESSION}
        if verb == 'resume':
            self.fixture.assertIn(SESSION, options)
            self.state = 'resumed'
            return self._status()
        raise AssertionError('Unexpected backup operation: ' + verb)

    def _compose(self, env, command, args):
        if command == 'config':
            return self._configuration(env)
        if command == 'ps':
            return [
                {'ID': container, 'Service': service, 'Project': 'birds-fixture',
                 'State': 'running' if service in self.running else 'exited'}
                for service, container in self.containers.items()
            ]
        if command == 'stop':
            self.events.append('stop')
            self.running.difference_update(args)
            if self.failure == 'stop':
                self.running.add('worker')
            return ''
        if command == 'up':
            if 'web' in args:
                self.events.append('up')
                self.fixture.assertIn('worker=1', args)
                self.containers.update(NEW_IDS)
                self.running.update(NEW_IDS)
                self.image = NEW_IMAGE if env == str(self.fixture.new_env) else OLD_IMAGE
            else:
                self.fixture.assertEqual(args[-1], 'lan')
                self.events.append('up_lan')
                self.containers['lan'] = '6' * 64
                self.running.add('lan')
            return ''
        if command == 'top':
            return 'worker\nPID COMMAND\n42 python /app/manage.py runrecognitionworker\n'
        if command == 'exec':
            return {'deployment': {'status': 'verified'}, 'errors': []}
        if command != 'run':
            raise AssertionError('Unexpected Compose operation: ' + command)
        if 'backuparchive' in args:
            index = args.index('backuparchive')
            verb, *options = args[index + 1:]
            if verb == 'verify':
                self.fixture.assertIn(f'{self.fixture.backup}:/backup:ro', args)
            if self.failure == 'verify' and verb == 'verify':
                self.events.append(verb)
                raise subprocess.CalledProcessError(1, [], stderr='fixture-secret')
            return self._backuparchive(verb, options)
        if 'offline' in args:
            self.events.append('migrate')
            self.fixture.assertEqual(args[args.index('offline') + 1:], [SESSION, 'migrate'])
            self.fixture.assertEqual(env, str(self.fixture.new_env))
            if self.failure == 'migrate':
                self.state = 'failed'
                raise subprocess.CalledProcessError(1, [], stderr='fixture-secret')
            if self.migrate is not None:
                self.migrate()
            if self.failure == 'baseline':
                with closing(sqlite3.connect(self.fixture.database)) as db, db:
                    db.execute('update archive_photo set notes=?', ('changed',))
            return '\n'.join(map(json.dumps, (
                {'deployment': {'status': 'verified'}, 'errors': []},
                {'status': 'offline_completed', 'session_id': SESSION,
                 'verb': 'migrate', 'returncode': 0},
            )))
        raise AssertionError('Unexpected one-off Compose command')

    def _configuration(self, env):
        candidate = env == str(self.fixture.new_env)
        environment = {'BIRDS_UID': '1000', 'BIRDS_GID': '1000', 'BIRDS_DEPLOYMENT_ID': 'fixture-deploy'}
        environment.update({f'BIRDS_{role.upper()}_ROOT': '/roots/' + role
                            for role in ('inbox', 'data', 'archive', 'claim')})
        roots = self.fixture.backup if candidate and self.failure == 'candidate_mount' else self.fixture.roots
        application = {
            'image': 'birds-fixture:new' if candidate else 'birds-fixture:old',
            'pull_policy': 'never', 'user': '1000:1000',
            'environment': environment,
            'volumes': [{'type': 'bind', 'source': str(roots), 'target': '/roots',
                         'bind': {'create_host_path': False}}],
        }
        services = {role: {**application, 'command': [role]} for role in ('init', 'web', 'worker')}
        services['maintenance'] = {**application, 'profiles': ['maintenance'],
                                   'command': ['backuparchive', 'status']}
        project = 'candidate-project' if candidate and self.failure == 'candidate_project' else 'birds-fixture'
        return {'name': project, 'services': services}

    @staticmethod
    def _image_metadata(reference):
        candidate = reference == 'birds-fixture:new'
        source = ('f' if candidate else 'c') * 64
        return [{'Id': NEW_IMAGE if candidate else OLD_IMAGE, 'Config': {
            'Labels': {'org.birds.source-sha': source},
            'Env': ['BIRDS_SOURCE_SHA=' + source, 'BIRDS_BASE_IMAGE_ID=sha256:' + 'd' * 64,
                    'BIRDS_PYTHON_TREE_SHA=' + 'e' * 64],
        }}]

    def __call__(self, arguments, **kwargs):
        if arguments[:2] == ['docker', 'compose']:
            env = arguments[arguments.index('--env-file') + 1]
            index = next(i for i, arg in enumerate(arguments[2:], 2)
                         if arg in ('ps', 'stop', 'up', 'top', 'exec', 'run', 'config'))
            call = lambda: self._compose(env, arguments[index], arguments[index + 1:])
        elif arguments[:2] == ['docker', 'inspect']:
            call = lambda: (self.image + '\n') * len(arguments[4:])
        elif arguments[:3] == ['docker', 'image', 'inspect']:
            call = lambda: self._image_metadata(arguments[-1])
        elif arguments[:3] == ['docker', 'context', 'inspect']:
            call = lambda: 'unix:///var/run/docker.sock\n'
        else:
            raise AssertionError(f'Unexpected subprocess: {arguments[:3]}')
        try:
            payload = call()
        except subprocess.CalledProcessError as error:
            if kwargs.get('check'):
                raise
            return subprocess.CompletedProcess(arguments, error.returncode, '', error.stderr)
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.CompletedProcess(arguments, 0, stdout, '')


class DeployLifecycleTests(SimpleTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='birds-maintain-')
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name).resolve()
        self.backup = self.work / 'backup'
        self.roots = self.work / 'roots'
        self.backup.mkdir()
        self.roots.mkdir()
        for role in ('inbox', 'data', 'archive', 'claim'):
            (self.roots / role).mkdir()
        self.database = self.roots / 'data' / 'archive.sqlite3'
        self.old_env = self.work / 'old.env'
        self.new_env = self.work / 'new.env'
        for env in (self.old_env, self.new_env):
            env.touch(mode=0o600)
        self.config = self.work / 'config.json'
        self.config.write_text(json.dumps({
            'compose_file': str(Path(settings.BASE_DIR) / 'compose.yaml'),
            'deploy_env_file': str(self.old_env),
            'upgrade_env_file': str(self.new_env),
            'backup_directory': str(self.backup),
            'command_timeout_seconds': 1,
            'drain_timeout_seconds': 1,
        }))

    def migrate(self, target, *fixture_values):
        result = RUN(
            [sys.executable, '-c', MIGRATE_FIXTURE, str(self.database), target,
             *map(str, fixture_values)],
            cwd=settings.BASE_DIR,
            env={**os.environ, 'DJANGO_SETTINGS_MODULE': 'config.settings_test',
                 'BIRDS_TEST_ROOT': str(settings.BIRDS_TEST_ROOT)},
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def run_controller(self, action, process):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(maintain.subprocess, 'run', side_effect=process), \
                patch.dict(os.environ, {'DOCKER_CONTEXT': 'fixture-local'}), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = maintain.main([action, '--config', str(self.config)])
        report = json.loads(stdout.getvalue() or stderr.getvalue())
        self.assertNotIn(str(self.work), stdout.getvalue() + stderr.getvalue())
        self.assertNotIn('隔离维护测试备注', stdout.getvalue() + stderr.getvalue())
        self.assertNotIn('fixture-secret', stdout.getvalue() + stderr.getvalue())
        return exit_code, report

    def seed_minimal_database(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('create table archive_photo (id text primary key, notes text)')
            db.execute('insert into archive_photo values (?, ?)', ('fixture', '隔离维护测试备注'))
            db.execute('create table archive_job (id text primary key, state text)')
            db.execute('insert into archive_job values (?, ?)', ('fixture-job', 'pending'))

    def test_upgrade_verifies_backup_before_one_real_migration_and_resumes_after_readback(self):
        source = self.roots / 'inbox' / 'confirmed.jpg'
        pending = self.roots / 'inbox' / 'pending.jpg'
        digest = jpeg(source, color='blue')
        jpeg(pending, color='green')
        original = source.read_bytes()
        self.migrate('0011_bird_guide', digest, len(original))

        with closing(sqlite3.connect(self.database)) as db:
            photo = db.execute('select id, sha256, notes, favorite, rating, user_location, metadata_version '
                               'from archive_photo').fetchone()
            job = db.execute('select id, source_id, source_version, state from archive_job').fetchone()
            self.assertIsNone(db.execute("select 1 from django_migrations where name='0012_restore_bindings'").fetchone())
        process = ComposeProcess(self, migrate=lambda: self.migrate('0012_restore_bindings'), proxy=True)
        exit_code, report = self.run_controller('upgrade', process)

        self.assertEqual(exit_code, 0, report)
        self.assertEqual(process.events, ['request', 'create', 'verify', 'stop', 'migrate', 'up', 'up_lan', 'resume'])
        self.assertEqual(process.state, 'resumed')
        self.assertEqual(report['session_id'], SESSION)
        self.assertEqual(report['backup_set_id'], SET_ID)
        self.assertEqual(source.read_bytes(), original)
        self.assertTrue((self.backup / SET_ID / 'database' / 'archive.sqlite3').is_file())
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute('select id, sha256, notes, favorite, rating, user_location, metadata_version '
                                        'from archive_photo').fetchone(), photo)
            self.assertEqual(db.execute('select id, source_id, source_version, state from archive_job').fetchone(), job)
            self.assertEqual(db.execute("select count(*) from django_migrations where name='0012_restore_bindings'").fetchone(),
                             (1,))
            self.assertEqual(db.execute('select count(*) from archive_restoresession').fetchone(), (0,))

    def test_restart_drains_stops_and_resumes_without_migration(self):
        self.seed_minimal_database()
        process = ComposeProcess(self)
        exit_code, report = self.run_controller('restart', process)

        self.assertEqual(exit_code, 0, report)
        self.assertEqual(process.events, ['request', 'quiesce', 'stop', 'up', 'resume'])
        self.assertEqual(process.state, 'resumed')
        self.assertEqual(report['session_id'], SESSION)

    def test_upgrade_failure_keeps_maintenance_at_the_failed_boundary(self):
        self.seed_minimal_database()
        for failure, stage, events in (
            ('verify', 'backup_verify', ['request', 'create', 'verify']),
            ('stop', 'stop_old', ['request', 'create', 'verify', 'stop']),
            ('migrate', 'migrate', ['request', 'create', 'verify', 'stop', 'migrate']),
            ('baseline', 'compare_baseline', ['request', 'create', 'verify', 'stop', 'migrate', 'up']),
        ):
            with self.subTest(failure=failure):
                process = ComposeProcess(self, failure=failure)
                exit_code, report = self.run_controller('upgrade', process)

                self.assertNotEqual(exit_code, 0, report)
                self.assertEqual(process.events, events)
                self.assertIn(process.state, ('active', 'failed'))
                self.assertEqual(report['stage'], stage)
                self.assertEqual(report['session_id'], SESSION)
                self.assertEqual(report['backup_set_id'], SET_ID)
                snapshot = self.backup / SET_ID / 'database' / 'archive.sqlite3'
                self.assertTrue(snapshot.is_file())
                snapshot.unlink()
                snapshot.parent.rmdir()

    def test_check_validates_configured_roles_materials_and_root_presence(self):
        process = ComposeProcess(self)
        exit_code, report = self.run_controller('check', process)
        self.assertEqual(exit_code, 0, report)
        self.assertEqual(report['completed'], ['preflight', 'runtime'])

        (self.roots / 'claim').rmdir()
        exit_code, report = self.run_controller('check', process)
        self.assertNotEqual(exit_code, 0, report)
        self.assertEqual((report['stage'], report['error']), ('preflight', 'compose_root_unavailable'))
        self.assertEqual(process.events, [])

        (self.roots / 'claim').mkdir()
        for change in ('candidate_project', 'candidate_mount'):
            with self.subTest(change=change):
                process = ComposeProcess(self, failure=change)
                exit_code, report = self.run_controller('upgrade', process)
                self.assertNotEqual(exit_code, 0, report)
                self.assertEqual((report['stage'], report['error']),
                                 ('preflight', 'upgrade_compose_configuration_changed'))
                self.assertEqual(process.events, [])
