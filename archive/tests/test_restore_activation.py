import fcntl
import io
import json
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from archive.management.commands.restorearchive import Command
from archive.services.deployment import (DeploymentError, activate_restored_deployment,
                                         RESTORE_LIFECYCLE_LOCK)
from archive.services.paths import open_root, root_identity, verify_root_identity, UnsafePath


class RestoreActivationTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='birds-restore-activation-')
        self.addCleanup(self.temp.cleanup)
        self.roots = {name: Path(self.temp.name).resolve() / name
                      for name in ('inbox', 'data', 'archive', 'claim')}
        for root in self.roots.values():
            root.mkdir(mode=0o700)
        self.session_id = uuid.uuid4()
        self.manifest = 'a' * 64
        self.session = SimpleNamespace(pk=self.session_id, status='verified', backup_set_id='set-1',
            backup_manifest_sha256=self.manifest,
            source_roots={name: {'path': str(Path(self.temp.name) / 'old' / name)} for name in self.roots},
            target_roots={name: str(root) for name, root in self.roots.items()})
        for name, root in self.roots.items():
            (root / '.birds-tracer-root.json').write_text(json.dumps(
                {'deployment_id': 'restore-test', 'role': name}))
            (root / '.birds-restore-v1.json').write_text(json.dumps(
                {'session_id': str(self.session_id), 'manifest_sha256': self.manifest, 'role': name}))
        with sqlite3.connect(self.roots['data'] / 'archive.sqlite3') as database:
            database.execute('create table django_migrations (app text, name text)')
        self.settings = override_settings(
            **{'BIRDS_' + name.upper() + '_ROOT': str(root) for name, root in self.roots.items()},
            DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3',
                                   'NAME': self.roots['data'] / 'archive.sqlite3'}})
        self.settings.enable()
        self.addCleanup(self.settings.disable)
        self.env = patch.dict(os.environ, {'BIRDS_DEPLOYMENT_ID': 'restore-test'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def activation_patches(self):
        activated = SimpleNamespace(**vars(self.session))
        activated.status = 'activated'
        return (
            patch('archive.models.RestoreSession.objects.get', return_value=self.session),
            patch('archive.services.restore.verify_restore', return_value=self.session),
            patch('archive.services.restore.activate_restore', return_value=activated),
            patch('archive.services.deployment.execution_identity', return_value={'uid': os.getuid(), 'gid': os.getgid()}),
            patch('archive.services.deployment.runtime_identity', return_value={'runtime': 'test'}),
            patch('archive.services.deployment.resource_budget', return_value={'total_mb': 1}),
            patch('archive.services.deployment.root_snapshot', return_value={name: {'test': name} for name in self.roots}),
            patch('archive.services.deployment.required_migrations', return_value=[]),
            patch('archive.services.deployment.validate_deployment', return_value={'status': 'verified'}),
        )

    def test_prepare_cli_requires_configured_new_roots_and_runs_offline_migration_before_registration(self):
        command = Command(stdout=io.StringIO(), stderr=io.StringIO())
        prepared = {'session_id': str(self.session_id)}
        with patch('archive.services.restore.prepare_restore', return_value=prepared) as prepare, \
                patch('archive.services.restore.register_prepared_restore', return_value=self.session) as register, \
                patch('archive.management.commands.restorearchive.call_command') as migrate:
            command.handle(verb='prepare', backup_set='/backup/set-1', session=str(self.session_id),
                           **{name + '_root': str(root) for name, root in self.roots.items()})
        prepare.assert_called_once_with('/backup/set-1', {name: str(root) for name, root in self.roots.items()},
                                        session_id=str(self.session_id))
        migrate.assert_called_once_with('migrate', interactive=False, verbosity=0, stdout=command.stderr)
        register.assert_called_once_with(prepared)
        self.assertEqual(json.loads(command.stdout.getvalue())['status'], 'verified')
        with self.assertRaisesRegex(CommandError, 'restore_target_roots_not_configured'):
            command.handle(verb='prepare', backup_set='/backup/set-1', session=str(self.session_id),
                           **{name + '_root': str(root) + ('-wrong' if name == 'claim' else '')
                              for name, root in self.roots.items()})

    def test_activation_publishes_only_matching_verified_session_and_is_lifecycle_locked(self):
        patches = self.activation_patches()
        with patches[0], patches[1], patches[2] as activate, patches[3], patches[4], patches[5], \
                patches[6], patches[7], patches[8]:
            self.assertEqual(activate_restored_deployment(self.session_id)['status'], 'verified')
        record = json.loads((self.roots['data'] / 'deployment-v1.json').read_text())
        self.assertEqual(record['restore'], {'session_id': str(self.session_id),
                                             'manifest_sha256': self.manifest})
        activate.assert_called_once_with(self.session_id)

        activated_session = SimpleNamespace(**vars(self.session))
        activated_session.status = 'activated'
        patches = self.activation_patches()
        with patch('archive.models.RestoreSession.objects.get', return_value=activated_session), \
                patch('archive.services.restore.verify_restore', return_value=activated_session), \
                patch('archive.services.restore.activate_restore') as repeated_activate, \
                patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
            self.assertEqual(activate_restored_deployment(self.session_id)['status'], 'verified')
        repeated_activate.assert_not_called()

        (self.roots['data'] / 'deployment-v1.json').unlink()
        lock = (self.roots['data'] / RESTORE_LIFECYCLE_LOCK).open('r+b')
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        patches = self.activation_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
            with self.assertRaises(DeploymentError) as error:
                activate_restored_deployment(self.session_id)
        self.assertEqual(error.exception.code, 'restore_lifecycle_busy')

    def test_restored_inbox_identity_requires_explicit_active_binding(self):
        inbox_fd, data_fd = open_root(self.roots['inbox']), open_root(self.roots['data'])
        self.addCleanup(os.close, inbox_fd)
        self.addCleanup(os.close, data_fd)
        old = {'dev': 1, 'ino': 2}
        (self.roots['data'] / 'inbox-root.json').write_text(json.dumps(old))
        current = root_identity(inbox_fd)
        with patch('archive.services.restore.effective_fingerprint', return_value=current):
            verify_root_identity(inbox_fd, str(self.roots['inbox']), data_fd)
        with patch('archive.services.restore.effective_fingerprint', side_effect=ValueError('missing')):
            with self.assertRaisesRegex(UnsafePath, 'root_identity_changed'):
                verify_root_identity(inbox_fd, str(self.roots['inbox']), data_fd)
