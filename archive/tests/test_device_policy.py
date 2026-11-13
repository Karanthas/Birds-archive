"""仅验证离线凭据和待部署策略；不替代NAS撤销或四端实测。"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase


SCRIPT = Path(__file__).resolve().parents[2] / 'deploy/manage-device.sh'


class DevicePolicyTests(SimpleTestCase):
    def run_device(self, *args, success=True):
        result = subprocess.run(['sh', str(SCRIPT), *map(str, args)],
                                capture_output=True, text=True, timeout=100)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def policy(self):
        target = {'ipv4': '10.203.0.2', 'protocol': 'tcp', 'port': 443}
        return {'remote_ipv4': '10.203.0.2', 'remote_port': 443,
                'remote_hostname': 'remote.birds.test', 'lan_ipv4': '10.203.1.2',
                'lan_hostname': 'lan.birds.test', 'route_cidrs': ['10.203.0.2/32'],
                'split_include': ['10.203.0.2/32'], 'gateway_allow': [target],
                'gateway_fallback': 'block', 'tls_inspection': False, 'public_hostname': False,
                'registration_is_authorization': False, 'connector_networks': ['remote_edge'],
                'remote_edge_members': ['cloudflared', 'proxy-remote'], 'connector_private_egress': [target]}

    def test_broad_network_route_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory) / 'policy.json'
            policy.write_text(json.dumps({**self.policy(), 'route_cidrs': ['10.203.0.0/24']}))
            self.run_device('check-policy', '--policy', policy, success=False)

    def test_exact_private_policy_and_bypass_rejections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'policy.json'
            path.write_text(json.dumps(self.policy()))
            result = self.run_device('check-policy', '--policy', path)
            self.assertFalse(json.loads(result.stdout)['deployed'])
            for changes in ({'remote_ipv4': ''}, {'remote_port': 8443},
                            {'remote_ipv4': '10.203.1.2'}, {'remote_hostname': '*.birds.test'},
                            {'split_include': ['0.0.0.0/0']}, {'gateway_fallback': 'allow'},
                            {'tls_inspection': True}, {'registration_is_authorization': True},
                            {'public_hostname': True}, {'connector_networks': ['remote_backend']},
                            {'remote_edge_members': ['cloudflared', 'proxy-remote', 'web']},
                            {'connector_private_egress': [{'ipv4': '10.203.1.2', 'protocol': 'tcp', 'port': 443}]},
                            {'gateway_allow': [{'ipv4': '10.203.0.2', 'protocol': 'tcp', 'port': 8000}]}):
                with self.subTest(changes=changes):
                    path.write_text(json.dumps({**self.policy(), **changes}))
                    self.run_device('check-policy', '--policy', path, success=False)

    def test_unique_pfx_serials_revoke_and_publication(self):
        self.assertIsNotNone(shutil.which('openssl'), 'blocked: 目标缺OpenSSL；禁止skip/自动安装')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            store, tls = root / 'offline', root / 'tls'
            tls.mkdir()
            ca_pass, pfx_pass = root / 'ca.pass', root / 'pfx.pass'
            for path in (ca_pass, pfx_pass):
                path.write_bytes(os.urandom(24).hex().encode() + b'\n')
                path.chmod(0o600)
            common = ['--store', store, '--ca-pass-file', ca_pass]
            self.run_device('init', *common)
            self.run_device('init', *common, success=False)
            serials = []
            for name in ('device-a', 'device-b'):
                result = self.run_device('issue', *common, '--device', name,
                                         '--pfx-pass-file', pfx_pass)
                serials.append(json.loads(result.stdout)['serial'])
                device = store / 'devices' / name
                self.assertFalse((device / 'device.key').exists())
                self.assertEqual((device / 'device.pfx').stat().st_mode & 0o777, 0o600)
                verify = subprocess.run(['openssl', 'pkcs12', '-in', str(device / 'device.pfx'),
                                         '-passin', 'file:' + str(pfx_pass), '-noout'],
                                        capture_output=True)
                self.assertEqual(verify.returncode, 0, 'PFX必须可解密且MAC有效')
            self.assertNotEqual(*serials)
            self.run_device('issue', *common, '--device', 'device-a', '--pfx-pass-file', pfx_pass, success=False)
            self.run_device('publish', '--store', store, '--tls-dir', tls)
            before = (tls / 'clients.crl').read_bytes()
            result = self.run_device('revoke', *common, '--device', 'device-a')
            data = json.loads(result.stdout)
            self.assertEqual(data['status'], 'offline_revocation_material_updated')
            self.assertFalse(data['live_revocation_verified'])
            self.assertEqual((tls / 'clients.crl').read_bytes(), before, '离线撤销不得冒充入口已更新')
            self.run_device('revoke', *common, '--device', 'device-a')
            self.run_device('refresh-crl', *common)
            self.run_device('publish', '--store', store, '--tls-dir', tls)
            self.assertNotEqual((tls / 'clients.crl').read_bytes(), before)
            self.assertEqual({p.name for p in tls.iterdir()}, {'client-ca.crt', 'clients.crl'})
            for name, code in (('device-a', 2), ('device-b', 0)):
                verify = subprocess.run(['openssl', 'verify', '-purpose', 'sslclient', '-crl_check',
                                         '-CAfile', str(tls / 'client-ca.crt'), '-CRLfile', str(tls / 'clients.crl'),
                                         str(store / 'devices' / name / 'device.crt')], capture_output=True)
                self.assertEqual(verify.returncode, code, verify.stderr.decode())
                if code:
                    self.assertIn(b'certificate revoked', verify.stderr)
            registry = json.loads(self.run_device('status', '--store', store).stdout)['devices']
            self.assertEqual(registry['device-a']['status'], 'revoked')
            self.assertEqual(registry['device-b']['status'], 'valid')
            # 不允许用另一个CA默默覆盖已绑定入口。
            (tls / 'client-ca.crt').write_text('different CA')
            self.run_device('publish', '--store', store, '--tls-dir', tls, success=False)

    def test_unsafe_store_and_password_file_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            store, password = root / 'offline', root / 'pass'
            store.mkdir(mode=0o755)
            password.write_text('a-long-test-only-password\n')
            password.chmod(0o600)
            self.run_device('init', '--store', store, '--ca-pass-file', password, success=False)
            store.chmod(0o700)
            password.chmod(0o644)
            self.run_device('init', '--store', store, '--ca-pass-file', password, success=False)
            password.chmod(0o600)
            link = root / 'link'
            link.symlink_to(store, target_is_directory=True)
            self.run_device('init', '--store', link, '--ca-pass-file', password, success=False)
