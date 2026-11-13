"""真实双HTTPS入口探针；标准库、严格证书验证、导入无网络副作用。

由主执行者提供独立合成夹具的schema1 JSON，TCP只连接指定bridgeIP。
没有配置时非零blocked；不会启动Docker、生成夹具或修改NAS/DNS。
"""
import argparse
import hashlib
import http.client
from http.cookies import SimpleCookie
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import unittest
import uuid


class ProbeBlocked(ValueError):
    pass


def load_config(path=None):
    path = path or os.environ.get('BIRDS_PROXY_PROBE_CONFIG')
    if not path:
        raise ProbeBlocked('blocked: BIRDS_PROXY_PROBE_CONFIG缺失，必须提供真实TLS目标')
    source = Path(path)
    if not source.is_absolute() or source.is_symlink() or not source.is_file():
        raise ProbeBlocked('blocked: 探针配置必须为绝对路径普通文件')
    config = json.loads(source.read_text())
    if config.get('schema') != 1:
        raise ProbeBlocked('blocked: schema必须为1')
    dns = r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}'
    for name in ('lan', 'remote'):
        endpoint = config[name]
        ipaddress.ip_address(endpoint['connect_ip'])
        if (type(endpoint['port']) is not int or not 1 <= endpoint['port'] <= 65535
                or not re.fullmatch(dns, endpoint['server_name'])):
            raise ProbeBlocked('blocked: TLS目标格式无效')
    if config['lan']['server_name'] == config['remote']['server_name']:
        raise ProbeBlocked('blocked: LAN和remote必须为不同名称')
    for name in ('server_ca', 'client_cert', 'client_key', 'wrong_client_cert', 'wrong_client_key'):
        material = Path(config[name])
        if not material.is_absolute() or material.is_symlink() or not material.is_file():
            raise ProbeBlocked('blocked: TLS材料必须为绝对路径普通文件')
    if str(uuid.UUID(config['photo_id'])) != config['photo_id']:
        raise ProbeBlocked('blocked: 合成照片ID无效')
    if not re.fullmatch('[0-9a-f]{64}', config['original_sha256']):
        raise ProbeBlocked('blocked: 独立原图SHA无效')
    if type(config['original_bytes']) is not int or not 16 <= config['original_bytes'] <= 1024*1024:
        raise ProbeBlocked('blocked: 合成原图长度须为16至1048576字节')
    return config


def tls_context(config, identity=None):
    """默认仅验证服务器；valid/wrong分别加载正式/错误CA签发设备身份。"""
    if identity not in (None, 'valid', 'wrong'):
        raise ValueError('identity_invalid')
    context = ssl.create_default_context(cafile=config['server_ca'])
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if identity is not None:
        prefix = '' if identity == 'valid' else 'wrong_'
        context.load_cert_chain(config[prefix+'client_cert'], config[prefix+'client_key'])
    return context


class StrictHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, endpoint, context, server_name=None, timeout=15):
        if context.verify_mode != ssl.CERT_REQUIRED or context.check_hostname is not True:
            raise ValueError('verified_tls_context_required')
        ipaddress.ip_address(endpoint['connect_ip'])
        self.endpoint = endpoint
        self.server_name = server_name or endpoint['server_name']
        super().__init__(self.server_name, port=endpoint['port'], context=context, timeout=timeout)

    def connect(self):
        # 禁用系统代理/DNS路径；SNI和hostname校验仍使用真实目标名称。
        if self._tunnel_host:
            raise ValueError('proxy_tunnel_not_allowed')
        transport = socket.create_connection(
            (self.endpoint['connect_ip'], self.endpoint['port']), self.timeout)
        try:
            self.sock = self._context.wrap_socket(transport, server_hostname=self.server_name)
        except BaseException:
            transport.close()
            raise


def request(config, name, path, *, identity=None, method='GET', headers=None, body=None, server_name=None):
    endpoint = config[name]
    connection = StrictHTTPSConnection(endpoint, tls_context(config, identity), server_name)
    values = {'Host': endpoint['server_name'], **(headers or {})}
    try:
        connection.request(method, path, body=body, headers=values)
        response = connection.getresponse()
        payload = response.read(1024*1024+1)
        if len(payload) > 1024*1024:
            raise ValueError('synthetic_response_too_large')
        return {'status': response.status, 'headers': dict(response.getheaders()), 'body': payload,
                'set_cookies': response.headers.get_all('Set-Cookie', [])}
    finally:
        connection.close()


class ProxyBoundaryTests(unittest.TestCase):
    """每个断言访问真实NGINX；不使用Django客户端或配置文本替身。"""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.config = load_config()
        cls.download = '/photos/'+cls.config['photo_id']+'/download/'
        cls.detail = '/photos/'+cls.config['photo_id']+'/'
        cls.observations = []

    @classmethod
    def tearDownClass(cls):
        try:
            target = os.environ.get('BIRDS_PROXY_PROBE_OBSERVATIONS')
            if target:
                if not Path(target).is_absolute():
                    raise ValueError('absolute_observation_path_required')
                # 仅记录既有响应；独占新建0600文件，不能覆盖先前证据或跟随链接。
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'w') as stream:
                    json.dump({'schema': 1, 'observations': cls.observations}, stream, indent=2)
                    stream.write('\n')
        finally:
            super().tearDownClass()

    def call(self, name, path, *, identity='default', **kwargs):
        if identity == 'default':
            identity = 'valid' if name == 'remote' else None
        result = request(self.config, name, path, identity=identity, **kwargs)
        self.observations.append({'entry': name, 'path': path, 'method': kwargs.get('method', 'GET'),
                                  'status': result['status'], 'bytes': len(result['body']),
                                  'sha256': hashlib.sha256(result['body']).hexdigest()})
        return result

    def private(self, response):
        headers = {key.lower(): value for key, value in response['headers'].items()}
        self.assertIn('no-store', headers.get('cache-control', ''))
        self.assertIn('private', headers.get('cache-control', ''))
        self.assertNotIn('x-accel-redirect', headers)
        return headers

    def denied(self, name, path, **kwargs):
        try:
            response = self.call(name, path, **kwargs)
        except (ssl.SSLError, ConnectionResetError, http.client.RemoteDisconnected):
            return
        self.assertIn(response['status'], (400, 401, 403, 421))
        self.private(response)

    def test_lan_without_identity_and_remote_with_identity_download_same_original(self):
        for name in ('lan', 'remote'):
            with self.subTest(entry=name):
                page = self.call(name, '/')
                self.assertEqual(page['status'], 200)
                self.private(page)
                original = self.call(name, self.download)
                self.assertEqual(original['status'], 200)
                headers = self.private(original)
                self.assertIn('attachment;', headers.get('content-disposition', ''))
                self.assertEqual(len(original['body']), self.config['original_bytes'])
                self.assertEqual(hashlib.sha256(original['body']).hexdigest(), self.config['original_sha256'])
                preview = self.call(name, '/media/'+self.config['photo_id']+'/preview/')
                self.assertEqual(preview['status'], 200)
                self.assertTrue(preview['body'].startswith(b'\xff\xd8'))
                self.private(preview)

    def test_head_and_range_preserve_original_contract(self):
        for name in ('lan', 'remote'):
            with self.subTest(entry=name):
                head = self.call(name, self.download, method='HEAD')
                self.assertEqual(head['status'], 200)
                self.assertEqual(head['body'], b'')
                headers = self.private(head)
                self.assertEqual(int(headers['content-length']), self.config['original_bytes'])
                full = self.call(name, self.download)
                ranged = self.call(name, self.download, headers={'Range': 'bytes=0-15'})
                headers = self.private(ranged)
                # 既有Django FileResponse可忽略Range并返回完整200；不假称支持206。
                self.assertIn(ranged['status'], (200, 206))
                if ranged['status'] == 206:
                    self.assertEqual(headers['content-range'], 'bytes 0-15/'+str(self.config['original_bytes']))
                    self.assertEqual(ranged['body'], full['body'][:16])
                else:
                    self.assertEqual(hashlib.sha256(ranged['body']).hexdigest(), self.config['original_sha256'])

    def test_remote_no_identity_or_wrong_ca_cannot_read_any_private_surface(self):
        for identity in (None, 'wrong'):
            for path in ('/', self.detail, self.download, '/media/'+self.config['photo_id']+'/preview/'):
                with self.subTest(identity=identity, path=path):
                    self.denied('remote', path, identity=identity)

    def test_host_sni_and_alias_port_cannot_cross_entries(self):
        for name, other in (('lan', 'remote'), ('remote', 'lan')):
            for host in (self.config[other]['server_name'], 'unknown.invalid',
                         self.config[name]['server_name']+':8443'):
                with self.subTest(entry=name, host=host):
                    self.denied(name, self.download, headers={'Host': host})
            for sni in ('unknown.invalid', self.config[other]['server_name']):
                with self.subTest(entry=name, sni=sni):
                    self.denied(name, self.download, server_name=sni)
        # 同remote TCP入口上伪装LAN Host/SNI也不能绕过证书门。
        self.denied('remote', self.download, identity=None,
                    headers={'Host': self.config['lan']['server_name']},
                    server_name=self.config['lan']['server_name'])

    def test_forwarded_headers_are_overwritten_and_exact_443_host_works(self):
        for name in ('lan', 'remote'):
            response = self.call(name, '/', headers={
                'Host': self.config[name]['server_name']+':443', 'X-Forwarded-Proto': 'http',
                'X-Forwarded-Host': 'unknown.invalid', 'X-Forwarded-Port': '80',
                'Forwarded': 'host=unknown.invalid;proto=http'})
            self.assertEqual(response['status'], 200)
            self.private(response)

    def test_remote_delivery_is_forbidden_even_with_valid_identity(self):
        for path in ('/delivery', '/delivery/', '/delivery/token/', '/delivery/files/',
                     '/delivery/receipts/'+str(uuid.UUID(int=1))+'/'):
            with self.subTest(path=path):
                result = self.call('remote', path)
                self.assertEqual(result['status'], 403)
                self.private(result)
        token = self.call('lan', '/delivery/token/')
        self.assertEqual(token['status'], 200)
        self.private(token)
        self.assertTrue(json.loads(token['body'])['csrf_token'])

    def test_csrf_and_secure_cookie_remain_enforced(self):
        for name in ('lan', 'remote'):
            with self.subTest(entry=name):
                page = self.call(name, self.detail)
                self.assertEqual(page['status'], 200)
                self.private(page)
                cookies = SimpleCookie()
                for header in page['set_cookies']:
                    cookies.load(header)
                self.assertIn('csrftoken', cookies)
                self.assertTrue(cookies['csrftoken']['secure'])
                token = cookies['csrftoken'].value
                cookie = 'csrftoken='+token
                for headers in ({'Origin': 'https://'+self.config[name]['server_name']},
                                {'Origin': 'https://unknown.invalid', 'X-CSRFToken': token,
                                 'Cookie': cookie}):
                    response = self.call(name, '/metadata/location/', method='POST', body=b'', headers=headers)
                    self.assertEqual(response['status'], 403)
                    self.private(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--result', required=True)
    args = parser.parse_args()
    os.environ['BIRDS_PROXY_PROBE_CONFIG'] = args.config
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ProxyBoundaryTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    record = {'schema': 1, 'status': 'passed' if result.wasSuccessful() and result.testsRun else 'failed',
              'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
              'skipped': len(result.skipped), 'observations': getattr(ProxyBoundaryTests, 'observations', [])}
    Path(args.result).write_text(json.dumps(record, indent=2)+'\n')
    return 0 if record['status'] == 'passed' and not result.skipped else 1


if __name__ == '__main__':
    sys.exit(main())
