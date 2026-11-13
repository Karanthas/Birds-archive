#!/bin/sh
# 离线设备策略入口；只用已有Python3/OpenSSL，不连接NAS或更改Tunnel。
set -eu
exec "${BIRDS_DEVICE_PYTHON:-python3}" - "$@" <<'PY'
import argparse
import datetime
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile


def require(condition, message):
    if not condition:
        raise ValueError(message)


def normal_path(value):
    path = Path(value)
    require(path.is_absolute() and str(path) == str(path.resolve()), '路径必须绝对且无符号链接/..')
    require(re.fullmatch(r'/[A-Za-z0-9_./-]+', str(path)), '凭据路径仅允许ASCII字母、数字、/._-')
    return path


def password_file(value):
    path = normal_path(value)
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and info.st_mode & 0o077 == 0, '口令文件必须为当前用户所有、普通文件、仅所有者可读写')
    data = path.read_bytes()
    require(len(data.splitlines()) == 1 and len(data.strip()) >= 16, '口令须单行且至少16字节')
    return path


def openssl(*args):
    result = subprocess.run(['openssl', *map(str, args)], capture_output=True, timeout=90,
                            env={**os.environ, 'LC_ALL': 'C'})
    require(result.returncode == 0, 'OpenSSL操作失败：' + str(args[0]) + '；未输出凭据/口令')
    return result.stdout.decode()


def atomic(path, data, mode=0o600):
    require(not path.is_symlink(), '拒绝符号链接目标')
    fd, name = tempfile.mkstemp(prefix='.device-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data if isinstance(data, bytes) else data.encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def check_policy(path):
    policy = json.loads(Path(path).read_text())
    keys = {'remote_ipv4', 'remote_port', 'remote_hostname', 'lan_ipv4', 'lan_hostname',
            'route_cidrs', 'split_include', 'gateway_allow', 'gateway_fallback',
            'tls_inspection', 'public_hostname', 'registration_is_authorization',
            'connector_networks', 'remote_edge_members', 'connector_private_egress'}
    require(set(policy) == keys, '策略字段缺失或未知，必须完成精确策略')
    remote, lan = (ipaddress.IPv4Address(policy[key]) for key in ('remote_ipv4', 'lan_ipv4'))
    private = [ipaddress.IPv4Network(cidr) for cidr in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
    require(all(any(address in network for network in private) for address in (remote, lan))
            and remote != lan, 'remote/LAN必须为不同RFC1918地址')
    for key in ('remote_hostname', 'lan_hostname'):
        hostname = policy[key]
        require(isinstance(hostname, str) and len(hostname) <= 253 and '.' in hostname
                and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
                        for part in hostname.split('.')), '需要精确小写DNS名称')
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise ValueError('Host不能为IP')
    require(policy['remote_hostname'] != policy['lan_hostname'], 'LAN/remote须不同Host')
    require(type(policy['remote_port']) is int and policy['remote_port'] == 443, '远程只允许TCP443')
    route = str(remote) + '/32'
    target = {'ipv4': str(remote), 'protocol': 'tcp', 'port': 443}
    require(policy['route_cidrs'] == [route] and policy['split_include'] == [route], '只允许remote单地址/32路由')
    require(policy['gateway_allow'] == [target] and policy['connector_private_egress'] == [target],
            'Gateway及connector私网出口必须只允许remote:443')
    require(policy['gateway_fallback'] == 'block', 'Gateway必须兜底阻断')
    require(all(policy[key] is False for key in ('tls_inspection', 'public_hostname',
                                                'registration_is_authorization')),
            '禁止TLS解密、公开hostname或仅登记即授权')
    require(policy['connector_networks'] == ['remote_edge']
            and sorted(policy['remote_edge_members']) == ['cloudflared', 'proxy-remote'],
            'connector只能连接独立remote_edge；不得接含web的backend网络')
    return {'status': 'policy_validated', 'route': route, 'target_port': 443,
            'deployed': False, 'note': '仅验证声明，实际路由/ACL/出口由06-08核验'}


def config():
    return '''[ca]
default_ca = devices
[devices]
dir = .
database = $dir/index.txt
new_certs_dir = $dir/newcerts
certificate = $dir/client-ca.crt
private_key = $dir/private/ca.key
serial = $dir/serial
crlnumber = $dir/crlnumber
default_md = sha256
default_days = 90
default_crl_days = 7
policy = device_subject
unique_subject = yes
copy_extensions = none
x509_extensions = client
crl_extensions = crl_ext
[device_subject]
commonName = supplied
[client]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = clientAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
[crl_ext]
authorityKeyIdentifier = keyid:always
'''


def crl(root, ca_pass):
    temporary = root / 'clients.next.crl'
    openssl('ca', '-batch', '-config', 'openssl.cnf', '-passin', 'file:' + str(ca_pass),
            '-gencrl', '-out', temporary)
    openssl('crl', '-in', temporary, '-noout', '-verify', '-CAfile', 'client-ca.crt')
    atomic(root / 'clients.crl', temporary.read_bytes())
    temporary.unlink()


def init(root, ca_pass):
    require(not any(path.name != '.lock' for path in root.iterdir()), '拒绝覆盖现有CA/凭据库')
    for name in ('private', 'newcerts', 'devices'):
        (root / name).mkdir(mode=0o700)
    for name, data in (('index.txt', ''), ('serial', '1000\n'), ('crlnumber', '1000\n'),
                       ('openssl.cnf', config()), ('registry.json', '{}\n')):
        atomic(root / name, data)
    openssl('genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072',
            '-aes-256-cbc', '-pass', 'file:' + str(ca_pass), '-out', 'private/ca.key')
    openssl('req', '-new', '-x509', '-sha256', '-days', '3650', '-key', 'private/ca.key',
            '-passin', 'file:' + str(ca_pass), '-subj', '/CN=Birds-Archive-Device-CA',
            '-addext', 'basicConstraints=critical,CA:TRUE,pathlen:0',
            '-addext', 'keyUsage=critical,keyCertSign,cRLSign', '-out', 'client-ca.crt')
    crl(root, ca_pass)
    return {'status': 'initialized', 'ca_key_offline_required': True}


def operate(args, root):
    if args.command == 'init':
        return init(root, password_file(args.ca_pass_file))
    require((root / 'registry.json').is_file(), '凭据库尚未初始化')
    registry = json.loads((root / 'registry.json').read_text())
    if args.command == 'status':
        return {'status': 'registry', 'devices': registry, 'live_revocation_verified': False}
    if args.command == 'refresh-crl':
        crl(root, password_file(args.ca_pass_file))
        return {'status': 'offline_crl_refreshed', 'live_revocation_verified': False}
    if args.command == 'publish':
        target = normal_path(args.tls_dir)
        require(target.is_dir() and target != root and root not in target.parents
                and target not in root.parents, 'TLS目录必须已存在且与离线CA库分离')
        openssl('crl', '-in', 'clients.crl', '-noout', '-verify', '-CAfile', 'client-ca.crt')
        openssl('x509', '-in', 'client-ca.crt', '-noout', '-checkend', '0')
        next_update = openssl('crl', '-in', 'clients.crl', '-noout', '-nextupdate').strip().split('=', 1)[1]
        expiry = datetime.datetime.strptime(next_update, '%b %d %H:%M:%S %Y %Z').replace(tzinfo=datetime.timezone.utc)
        require(expiry > datetime.datetime.now(datetime.timezone.utc), 'CRL已过期，须在离线库刷新')
        existing = target / 'client-ca.crt'
        ca = (root / 'client-ca.crt').read_bytes()
        require(not existing.exists() or existing.read_bytes() == ca, '拒绝替换不同客户端CA')
        atomic(existing, ca, 0o644)
        atomic(target / 'clients.crl', (root / 'clients.crl').read_bytes(), 0o644)
        return {'status': 'published', 'crl_sha256': hashlib.sha256((target / 'clients.crl').read_bytes()).hexdigest(),
                'live_revocation_verified': False, 'next': 'nginx -t; reload; 核验旧worker退出和新旧连接拒绝'}
    require(re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,47}', args.device), '设备ID仅限小写ASCII字母数字_-，最长48')
    ca_pass = password_file(args.ca_pass_file)
    device = root / 'devices' / args.device
    if args.command == 'issue':
        pfx_pass = password_file(args.pfx_pass_file)
        require(args.device not in registry and not device.exists(), '设备ID已存在；重发须新ID且撤销旧序列')
        require(1 <= args.days <= 365, '设备证书有效期须1～365天')
        openssl('x509', '-in', 'client-ca.crt', '-noout', '-checkend', str(args.days * 86400))
        device.mkdir(mode=0o700)
        key, csr, cert = (device / name for name in ('device.key', 'device.csr', 'device.crt'))
        try:
            openssl('genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048', '-out', key)
            openssl('req', '-new', '-sha256', '-key', key, '-subj', '/CN=' + args.device, '-out', csr)
            openssl('ca', '-batch', '-notext', '-config', 'openssl.cnf', '-passin', 'file:' + str(ca_pass),
                    '-days', args.days, '-in', csr, '-out', cert)
            openssl('pkcs12', '-export', '-inkey', key, '-in', cert, '-certfile', 'client-ca.crt',
                    '-name', args.device, '-passout', 'file:' + str(pfx_pass), '-out', device / 'device.pfx')
        finally:
            # 即使导出失败也不保留明文私钥；失败ID不自动重试或覆盖。
            for temporary in (key, csr):
                temporary.unlink(missing_ok=True)
        serial = openssl('x509', '-in', cert, '-noout', '-serial').strip().split('=', 1)[1]
        validity = openssl('x509', '-in', cert, '-noout', '-dates').strip().splitlines()
        registry[args.device] = {'serial': serial, 'status': 'valid', 'issued_at': now(),
                                 'validity': validity, 'certificate_sha256': hashlib.sha256(cert.read_bytes()).hexdigest()}
    elif args.command == 'revoke':
        require(args.device in registry, '未知设备，拒绝撤销')
        entry = registry[args.device]
        # index为OpenSSL权威状态。若先前撤销成功但CRL写失败，可重试生成CRL。
        lines = (root / 'index.txt').read_text().splitlines()
        row = next((line.split('\t') for line in lines if line.split('\t')[3] == entry['serial']), None)
        require(row is not None, '序列登记不一致，停止并检查离线库')
        if row[0] != 'R':
            openssl('ca', '-batch', '-config', 'openssl.cnf', '-passin', 'file:' + str(ca_pass),
                    '-revoke', device / 'device.crt', '-crl_reason', 'keyCompromise')
        crl(root, ca_pass)
        entry.update(status='revoked', revoked_at=entry.get('revoked_at', now()))
    atomic(root / 'registry.json', json.dumps(registry, indent=2) + '\n')
    return {**registry[args.device], 'credential_status': registry[args.device]['status'],
            'status': 'offline_revocation_material_updated' if args.command == 'revoke' else 'issued',
            'device': args.device, 'live_revocation_verified': False}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='离线设备CA/证书/CRL；不自动修改网络或重载入口')
    parser.add_argument('command', choices=['init', 'issue', 'revoke', 'status', 'publish', 'refresh-crl', 'check-policy'])
    parser.add_argument('--store')
    parser.add_argument('--device')
    parser.add_argument('--ca-pass-file')
    parser.add_argument('--pfx-pass-file')
    parser.add_argument('--days', type=int, default=90)
    parser.add_argument('--tls-dir')
    parser.add_argument('--policy')
    args = parser.parse_args()
    if args.command == 'check-policy':
        require(args.policy, '必须提供--policy')
        return check_policy(args.policy)
    require(shutil.which('openssl'), 'blocked: 缺少OpenSSL，不自动安装')
    require(args.store, '必须提供离线--store')
    root = normal_path(args.store)
    if args.command == 'init':
        root.mkdir(mode=0o700, exist_ok=True)
    require(root.is_dir() and root.stat().st_uid == os.getuid()
            and root.stat().st_mode & 0o077 == 0, '凭据库须当前用户所有且权限0700')
    # 拒绝凭据树内链接，避免OpenSSL覆写树外文件；库仅供当前可信维护者使用。
    require(not any(path.is_symlink() for path in root.rglob('*')), '凭据库中不允许符号链接')
    with (root / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.chdir(root)
        if args.command in ('init', 'issue', 'revoke', 'refresh-crl'):
            require(args.ca_pass_file, '必须提供--ca-pass-file')
        if args.command in ('issue', 'revoke'):
            require(args.device, '必须提供--device')
        if args.command == 'issue':
            require(args.pfx_pass_file, '必须提供--pfx-pass-file')
        if args.command == 'publish':
            require(args.tls_dir, '必须提供--tls-dir')
        return operate(args, root)


try:
    print(json.dumps(main(), ensure_ascii=False, sort_keys=True))
except (ValueError, OSError, subprocess.SubprocessError) as error:
    print(json.dumps({'status': 'blocked', 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
    sys.exit(1)
PY
