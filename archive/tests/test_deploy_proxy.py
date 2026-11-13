"""双入口契约；TLS类必须消费真实NGINX，缺目标就是blocked，禁止skip。"""
import os
import importlib.util
import runpy
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase


class ProxySettingsTests(SimpleTestCase):
    def test_proxy_mode_rejects_local_http(self):
        with patch.dict(os.environ, {'BIRDS_PROXY_ONLY': '1', 'BIRDS_LOCAL_HTTP': '1'}):
            with self.assertRaisesMessage(ImproperlyConfigured, '代理模式禁止本地HTTP'):
                runpy.run_path(str(Path(__file__).resolve().parents[2] / 'config/settings.py'))

    def load(self, **changes):
        values = {'BIRDS_PROXY_ONLY': '1', 'BIRDS_LOCAL_HTTP': '0',
                  'BIRDS_LAN_HOST': 'lan.birds.test', 'BIRDS_REMOTE_HOST': 'remote.birds.test',
                  'BIRDS_ALLOWED_HOSTS': 'lan.birds.test,remote.birds.test'}
        with patch.dict(os.environ, {**values, **changes}):
            return runpy.run_path(str(Path(__file__).resolve().parents[2] / 'config/settings.py'))

    def test_only_exact_proxy_hosts_trust_https_header_and_origins(self):
        values = self.load()
        self.assertEqual(values['SECURE_PROXY_SSL_HEADER'], ('HTTP_X_FORWARDED_PROTO', 'https'))
        self.assertEqual(values['CSRF_TRUSTED_ORIGINS'], ['https://lan.birds.test', 'https://remote.birds.test'])
        self.assertFalse(values['USE_X_FORWARDED_HOST'])
        self.assertTrue(values['CSRF_COOKIE_SECURE'])
        self.assertTrue(values['SESSION_COOKIE_SECURE'])

    def test_invalid_proxy_names_and_host_allowlists_fail_closed(self):
        for changes in ({'BIRDS_LAN_HOST': '*.birds.test'}, {'BIRDS_LAN_HOST': '127.0.0.1'},
                        {'BIRDS_LAN_HOST': 'lan.birds.test:443'}, {'BIRDS_LAN_HOST': ''},
                        {'BIRDS_LAN_HOST': 'remote.birds.test'},
                        {'BIRDS_LAN_HOST': 'lan.birds.test;return 200;'},
                        {'BIRDS_ALLOWED_HOSTS': '*'},
                        {'BIRDS_ALLOWED_HOSTS': 'lan.birds.test,remote.birds.test,third.birds.test'}):
            with self.subTest(changes=changes), self.assertRaises(ImproperlyConfigured):
                self.load(**changes)

    def test_direct_mode_does_not_trust_external_proxy_header(self):
        self.assertIsNone(self.load(BIRDS_PROXY_ONLY='0')['SECURE_PROXY_SSL_HEADER'])


def _probe_class():
    # 生产镜像不打包planning；主执行者显式挂载并注入本次受控driver。
    path = Path(os.environ.get('BIRDS_PROXY_PROBE_DRIVER', str(
        Path(__file__).resolve().parents[2] / '.planning/phases/06-fnos/evidence/06-06/proxy_probe.py')))
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        class MissingProbe(SimpleTestCase):
            def test_real_tls_driver_is_required(self):
                self.fail('blocked: BIRDS_PROXY_PROBE_DRIVER必须指向实际TLS探针普通文件')
        return MissingProbe
    spec = importlib.util.spec_from_file_location('birds_real_proxy_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ProxyBoundaryTests


class ProxyBoundaryTests(_probe_class()):
    """仅消费真实TLS目标；缺driver或config均失败，禁止skip。"""
