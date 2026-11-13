"""专用临时根上的部署契约；不运行模型或既有业务旅程。"""
import json
import errno
import fcntl
import hashlib
import io
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from archive.services.deployment import (DeploymentError, register_deployment,
                                         validate_deployment, migrate_deployment, finalize_deployment, VERIFIED_BASE_IMAGE,
                                         execution_identity, check_linux_acl, linux_mount_identity, validate_claim_mount)
from deploy.materials import verify_materials as verify_material_tree


class DeploymentRootTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='birds-deployment-')
        self.addCleanup(self.temp.cleanup)
        self.roots = {name: Path(self.temp.name).resolve() / name for name in ('inbox', 'data', 'archive', 'claim')}
        for name, root in self.roots.items():
            root.mkdir(mode=0o700)
            (root / '.birds-tracer-root.json').write_text(json.dumps({'deployment_id': 'isolated-test', 'role': name}))
        self.options = override_settings(**{'BIRDS_' + name.upper() + '_ROOT': str(root)
                                          for name, root in self.roots.items()})
        self.options.enable()
        self.addCleanup(self.options.disable)
        database = patch.dict(settings.DATABASES['default'], {'NAME': self.roots['data'] / 'archive.sqlite3'})
        database.start()
        self.addCleanup(database.stop)
        self.env = patch.dict(os.environ, {'BIRDS_DEPLOYMENT_ID': 'isolated-test',
                                          'BIRDS_UID': str(os.getuid()), 'BIRDS_GID': str(os.getgid()),
                                          'BIRDS_SOURCE_SHA': 'a' * 64,
                                          'BIRDS_BASE_IMAGE_ID': VERIFIED_BASE_IMAGE,
                                          'BIRDS_PYTHON_TREE_SHA': 'd' * 64,
                                          'BIRDS_DEPLOYMENT_MEMORY_MB': '2048',
                                          'BIRDS_DEPLOYMENT_RESERVE_MB': '512'})
        self.env.start()
        self.addCleanup(self.env.stop)
        material = patch('archive.services.deployment.verify_materials', return_value='d' * 64)
        material.start()
        self.addCleanup(material.stop)

    def initialize(self):
        register_deployment()
        database = sqlite3.connect(self.roots['data'] / 'archive.sqlite3')
        database.execute('create table django_migrations (app text, name text)')
        from archive.services.deployment import required_migrations
        database.executemany('insert into django_migrations values (?,?)', required_migrations())
        database.commit()
        database.close()
        finalize_deployment()

    def assert_rejected(self, code, action=validate_deployment):
        with self.assertRaises(DeploymentError) as error:
            action()
        self.assertEqual(error.exception.code, code)
        self.assertNotIn(self.temp.name, str(error.exception))

    def test_explicit_registration_then_read_only_start_and_missing_database(self):
        self.assert_rejected('deployment_not_registered')
        self.initialize()
        before = {name: sorted(p.name for p in root.iterdir()) for name, root in self.roots.items()}
        self.assertEqual(validate_deployment()['status'], 'verified')
        self.assertEqual(before, {name: sorted(p.name for p in root.iterdir()) for name, root in self.roots.items()})
        self.assert_rejected('deployment_already_registered', register_deployment)
        path = self.roots['data'] / 'archive.sqlite3'
        path.rename(path.with_suffix('.old'))
        with sqlite3.connect(path) as db:
            db.execute('create table django_migrations (app text, name text)')
        self.assert_rejected('deployment_database_changed')
        (self.roots['data'] / 'archive.sqlite3').unlink()
        self.assert_rejected('deployment_database_missing')

    def test_existing_root_path_does_not_authorize_replacement_volume(self):
        self.initialize()
        self.roots['archive'].rename(Path(self.temp.name) / 'old-archive')
        self.roots['archive'].mkdir(mode=0o700)
        self.assert_rejected('deployment_root_changed')

    def test_alias_and_contained_roots_are_rejected(self):
        with override_settings(BIRDS_CLAIM_ROOT=str(self.roots['inbox'])):
            self.assert_rejected('deployment_roots_not_isolated', register_deployment)
        nested = self.roots['inbox'] / 'nested'
        nested.mkdir(mode=0o700)
        with override_settings(BIRDS_CLAIM_ROOT=str(nested)):
            self.assert_rejected('deployment_roots_not_isolated', register_deployment)

    def test_network_filesystem_and_real_rename_failure_are_rejected(self):
        with patch('archive.services.deployment.filesystem_identity', return_value={'type': 'nfs4', 'device': 'test', 'root': '/'}):
            self.assert_rejected('deployment_filesystem_rejected', register_deployment)
        with patch('archive.services.deployment.os.rename', side_effect=OSError(18, 'cross device')):
            self.assert_rejected('deployment_claim_rename_failed', register_deployment)
        self.assertTrue(all(sorted(p.name for p in root.iterdir()) == ['.birds-tracer-root.json']
                            for root in self.roots.values()))

    def test_permissions_uid_runtime_and_budget_are_rejected(self):
        self.roots['archive'].chmod(0o777)
        self.assert_rejected('deployment_permissions_invalid', register_deployment)
        self.roots['archive'].chmod(0o700)
        with patch.dict(os.environ, {'BIRDS_UID': str(os.getuid() + 1)}):
            self.assert_rejected('deployment_user_mismatch', register_deployment)
        self.initialize()
        with patch.dict(os.environ, {'BIRDS_SOURCE_SHA': 'c' * 64}):
            self.assert_rejected('deployment_runtime_changed')
        with patch.dict(os.environ, {'BIRDS_DEPLOYMENT_MEMORY_MB': '512'}):
            self.assert_rejected('deployment_memory_budget_invalid')

    def test_schema_missing_and_nonempty_init_are_rejected(self):
        (self.roots['inbox'] / 'precious.jpg').write_bytes(b'original')
        self.assert_rejected('deployment_init_not_empty', register_deployment)
        self.assertEqual((self.roots['inbox'] / 'precious.jpg').read_bytes(), b'original')
        (self.roots['inbox'] / 'precious.jpg').unlink()
        self.initialize()
        with sqlite3.connect(self.roots['data'] / 'archive.sqlite3') as db:
            db.execute('delete from django_migrations')
        self.assert_rejected('deployment_schema_mismatch')

    def test_upgrade_requires_explicit_entry_and_an_idle_writer(self):
        self.initialize()
        with patch.dict(os.environ, {'BIRDS_SOURCE_SHA': 'c' * 64}):
            self.assert_rejected('deployment_runtime_changed')
            with (self.roots['data'] / 'worker.lock').open('wb') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assert_rejected('deployment_writer_busy', lambda: migrate_deployment(stdout=io.StringIO()))
            with patch('django.core.management.call_command') as migrate:
                self.assertEqual(migrate_deployment(stdout=io.StringIO())['status'], 'verified')
                migrate.assert_called_once()
            self.assertEqual(validate_deployment()['status'], 'verified')
        with patch.dict(os.environ, {'BIRDS_PYTHON_TREE_SHA': 'e' * 64}):
            self.assert_rejected('deployment_runtime_changed', lambda: validate_deployment(migration=True))

    def test_current_mount_relation_rejects_split_binds_with_identical_persistent_identity(self):
        parent = self.roots['inbox'].parent
        common = f'10 1 8:1 /owned {parent} rw - ext4 /dev/test rw\n'
        separate = common + (f'11 10 8:1 /owned/inbox {self.roots["inbox"]} rw - ext4 /dev/test rw\n'
                             f'12 10 8:1 /owned/claim {self.roots["claim"]} rw - ext4 /dev/test rw\n')
        validate_claim_mount(self.roots['inbox'], self.roots['claim'], common)
        for name in ('inbox', 'claim'):
            stable = lambda value: {key: item for key, item in value.items() if key != 'mount_id'}
            self.assertEqual(stable(linux_mount_identity(self.roots[name], common)),
                             stable(linux_mount_identity(self.roots[name], separate)))
        self.assert_rejected('deployment_claim_separate_mount',
                             lambda: validate_claim_mount(self.roots['inbox'], self.roots['claim'], separate))

    def test_root_uid_or_gid_is_forbidden_even_when_the_effective_identity_matches(self):
        for uid, gid in ((0, os.getgid()), (os.getuid(), 0), (0, 0)):
            with self.subTest(uid=uid, gid=gid), patch.dict(os.environ, {'BIRDS_UID': str(uid), 'BIRDS_GID': str(gid)}), \
                    patch('archive.services.deployment.os.geteuid', return_value=uid), \
                    patch('archive.services.deployment.os.getegid', return_value=gid):
                self.assert_rejected('deployment_root_user_forbidden', execution_identity)

    def test_linux_access_and_default_acl_are_both_rejected(self):
        for forbidden in ('system.posix_acl_access', 'system.posix_acl_default'):
            def read_acl(fd, name):
                if name == forbidden:
                    return b'extended acl'
                raise OSError(errno.ENODATA, 'no acl')
            with self.subTest(acl=forbidden), patch('archive.services.deployment.os.getxattr', side_effect=read_acl, create=True):
                self.assert_rejected('deployment_acl_rejected', lambda: check_linux_acl(42))

    def test_material_manifest_checks_bytes_and_rejects_extras_and_links(self):
        root = Path(self.temp.name).resolve() / 'python-material'
        root.mkdir()
        source = root / 'package.py'
        source.write_bytes(b'approved package')
        rows = [{'path': source.name, 'bytes': source.stat().st_size,
                 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}]
        manifest = root.parent / 'material-manifest.json'
        manifest.write_text(json.dumps(rows))
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(verify_material_tree(root, manifest, digest), digest)
        for fault in ('changed_bytes', 'extra_file', 'symlink'):
            with self.subTest(fault=fault):
                if fault == 'changed_bytes':
                    source.write_bytes(b'changed package!')
                elif fault == 'extra_file':
                    (root / 'sitecustomize.py').write_bytes(b'extra startup code')
                else:
                    (root / 'linked.py').symlink_to(source)
                with self.assertRaisesRegex(ValueError, '^deployment_python_materials_invalid$'):
                    verify_material_tree(root, manifest, digest)
                source.write_bytes(b'approved package')
                for name in ('sitecustomize.py', 'linked.py'):
                    (root / name).unlink(missing_ok=True)
