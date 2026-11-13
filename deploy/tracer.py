"""06-01单张合成JPEG旅程。控制端仅用标准库及既有SSH，不调用本机Docker。

配置／归属／源码／材料先核对；任何失败都不是部署通过。保留本次根及结果，
只停止本项目容器，不删除宿主目录、卷或镜像。完整根登记由checkruntime消费。
"""
import argparse
import base64
import hashlib
import http.cookiejar
import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import subprocess
import sys
import tarfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE_ID = 'sha256:605098b794ffa9ea41b9681ccd0159f40667079b5fe13d867b1fa4109e0f6452'
SOURCE_ID = '2026-05-05 10:34:17 c88b22011a54b4f6fbd149e9f8e4de77658ce58143a1af0e3785e4e6475127e9'
NAMES = ('inbox', 'data', 'archive', 'claim')
SOURCE_ROOTS = ('archive', 'config', 'deploy')
SOURCE_FILES = ('Dockerfile', 'compose.yaml', 'manage.py', 'requirements.in', 'requirements.lock',
                'deploy/nginx-lan.conf', 'deploy/nginx-remote.conf',
                'archive/data/bird_guide/assets/northern-cardinal.jpg',
                'archive/data/bird_guide/assets/pyrrhuloxia.jpg')


def require(condition, code):
    if not condition:
        raise ValueError(code)


def absolute(value):
    require(isinstance(value, str) and re.fullmatch(r'/[A-Za-z0-9_./-]+', value), 'absolute_path_invalid')
    path = PurePosixPath(value)
    require('..' not in path.parts and str(path) == value and value != '/', 'absolute_path_invalid')
    return value


def container_roots(mode):
    """最小实验登记：禁止自动mkdir，正常启动必须已有DB。"""
    deployment = os.environ['BIRDS_DEPLOYMENT_ID']
    roots = [Path(os.environ['BIRDS_' + name.upper() + '_ROOT']) for name in NAMES]
    require(len({str(p.resolve()) for p in roots}) == 4, 'tracer_roots_invalid')
    for name, path in zip(NAMES, roots):
        require(path.is_dir() and not path.is_symlink(), 'tracer_root_missing')
        marker = path / '.birds-tracer-root.json'
        require(not marker.is_symlink() and marker.is_file(), 'tracer_ownership_missing')
        require(json.loads(marker.read_text()) == {'deployment_id': deployment, 'role': name}, 'tracer_ownership_mismatch')
        if mode == 'init':
            require(set(p.name for p in path.iterdir()) == {marker.name}, 'tracer_init_not_empty')
    require(roots[0].stat().st_dev == roots[3].stat().st_dev, 'tracer_claim_cross_device')
    if mode != 'init':
        database = roots[1] / 'archive.sqlite3'
        require(database.is_file() and not database.is_symlink(), 'tracer_database_missing')


def source_snapshot(root):
    # conf/yaml仅接受显式白名单；不能递归打包TLS材料或任意部署配置。
    paths = {root / name for name in SOURCE_FILES}
    for directory in SOURCE_ROOTS:
        paths.update(p for p in (root / directory).rglob('*') if p.is_file()
                     and '__pycache__' not in p.parts and p.suffix in {'.py', '.html', '.js', '.css', '.json', '.sh', '.example'}
                     and not p.name.startswith('.'))
    manifest = []
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        for path in sorted(paths):
            require(path.is_file() and not path.is_symlink(), 'source_file_invalid')
            relative = path.relative_to(root).as_posix()
            payload = path.read_bytes()
            manifest.append({'path': relative, 'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload)})
            info = tarfile.TarInfo(relative)
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
    payload = stream.getvalue()
    return payload, hashlib.sha256(payload).hexdigest(), manifest


# Constant program; all mutable inputs arrive as JSON on stdin. No remote bash fragments.
PREPARE = r'''
import base64,hashlib,io,json,os,pathlib,shutil,stat,tarfile,sys
c=json.load(sys.stdin)
def need(ok,code):
 if not ok: raise ValueError(code)
lab=pathlib.Path(c['lab_root'])
need(lab.is_dir() and not lab.is_symlink(),'lab_missing')
need(lab.resolve()==lab,'lab_alias_rejected')
need(json.loads((lab/'.birds-phase6-lab.json').read_text())=={'owner':c['owner']},'lab_owner_mismatch')
run=lab/'runs'/c['run_id']; run.parent.mkdir(exist_ok=True)
run.mkdir(mode=0o700)
(run/'owner.json').write_text(json.dumps({'owner':c['owner'],'run_id':c['run_id'],'project':c['project']}))
payload=base64.b64decode(c['source_tar'],validate=True)
need(hashlib.sha256(payload).hexdigest()==c['source_sha'],'source_sha_mismatch')
namespace=c['source_sha']+'-'+c['python_tree_sha256']
need(len(namespace)==129 and all(part and all(v in '0123456789abcdef' for v in part) for part in namespace.split('-')),
     'code_namespace_invalid')
code=lab/'code'/namespace; code.parent.mkdir(exist_ok=True)
fresh=not code.exists()
if fresh: code.mkdir(mode=0o700)
need(code.is_dir() and not code.is_symlink(),'code_directory_invalid')
with tarfile.open(fileobj=io.BytesIO(payload),mode='r:') as archive:
 for member in archive.getmembers():
  path=pathlib.PurePosixPath(member.name)
  need(member.isfile() and not path.is_absolute() and '..' not in path.parts,'source_tar_invalid')
  target=code/path; content=archive.extractfile(member).read()
  if fresh:
   target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(content)
  else: need(target.is_file() and not target.is_symlink() and target.read_bytes()==content,'existing_source_changed')
packages=pathlib.Path(c['python_root'])
need(packages.is_dir() and not packages.is_symlink(),'python_material_missing')
need(packages.resolve()==packages,'python_material_alias_rejected')
material=[]
for p in sorted(packages.rglob('*')):
 need(not p.is_symlink(),'material_symlink_rejected')
 if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc':
  name=p.relative_to(packages).as_posix(); digest=hashlib.sha256(p.read_bytes()).hexdigest()
  material.append({'path':name,'sha256':digest,'bytes':p.stat().st_size})
digest=hashlib.sha256(json.dumps(material,sort_keys=True,separators=(',',':')).encode()).hexdigest()
need(digest==c['python_tree_sha256'],'python_material_sha_mismatch')
need(sum(row['bytes'] for row in material)<512*1024*1024,'python_material_budget_exceeded')
target=code/'materials'/'python'
if fresh: target.mkdir(parents=True)
for row in material:
 destination=target/row['path']
 if fresh:
  destination.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(packages/row['path'],destination)
 else: need(destination.is_file() and not destination.is_symlink() and hashlib.sha256(destination.read_bytes()).hexdigest()==row['sha256'],'existing_material_changed')
manifest_path=code/'materials'/'python-manifest.json'
manifest_bytes=json.dumps(material,indent=2).encode()
if fresh: manifest_path.write_bytes(manifest_bytes)
else: need(manifest_path.is_file() and not manifest_path.is_symlink() and manifest_path.read_bytes()==manifest_bytes,
           'existing_material_manifest_changed')
expected={row['path'] for row in c['source_manifest']}|{'materials/python-manifest.json'}|{
 'materials/python/'+row['path'] for row in material}
directories={str(parent) for name in expected for parent in pathlib.PurePosixPath(name).parents if str(parent)!='.'}
actual=set();actual_directories=set()
for path in code.rglob('*'):
 mode=path.lstat().st_mode;relative=path.relative_to(code).as_posix()
 if stat.S_ISDIR(mode):actual_directories.add(relative)
 elif stat.S_ISREG(mode):actual.add(relative)
 else:raise ValueError('code_tree_link_or_special_file')
need(actual==expected and actual_directories==directories,'code_tree_unexpected_entry')
for name in ('inbox','data','archive','claim'):
 p=run/name; p.mkdir(mode=0o700)
 (p/'.birds-tracer-root.json').write_text(json.dumps({'deployment_id':c['run_id'],'role':name}))
 os.chown(p,c['uid'],c['gid']); os.chown(p/'.birds-tracer-root.json',c['uid'],c['gid'])
env=dict(c['environment'])
env['BIRDS_ROOTS_HOST']=str(run)
os.chown(run,0,0);run.chmod(0o711)
env['BIRDS_SOURCE_SHA']=c['source_sha']
need(all('\n' not in str(v) and '\r' not in str(v) for v in env.values()),'env_invalid')
(run/'tracer.env').write_text(''.join(k+'='+str(v)+'\n' for k,v in sorted(env.items())))
(run/'tracer.env').chmod(0o600)
(run/'source-manifest.json').write_text(json.dumps(c['source_manifest'],indent=2))
(run/'python-manifest.json').write_text(json.dumps(material,indent=2))
print(json.dumps({'prepared':True,'source_sha256':c['source_sha'],'python_tree_sha256':digest,'code_namespace':namespace}))
'''

FIXTURE = r'''
import hashlib,json,os
from pathlib import Path
from PIL import Image
p=Path('/roots/inbox')/'synthetic-phase6.part'
with p.open('xb') as stream:
 Image.new('RGB',(96,64),(71,122,83)).save(stream,format='JPEG');stream.flush();os.fsync(stream.fileno())
digest=hashlib.sha256(p.read_bytes()).hexdigest()
final=p.with_suffix('.jpg');os.link(p,final);p.unlink()
fd=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
print(json.dumps({'sha256':digest,'bytes':final.stat().st_size}))
'''

DATABASE = r'''
import hashlib,json,os,pathlib,sqlite3,sys
digest=sys.argv[1]
db=sqlite3.connect('file:/roots/data/archive.sqlite3?mode=ro',uri=True)
db.row_factory=sqlite3.Row
rows=[dict(r) for r in db.execute('select id,sha256,state,metadata_version,user_location from archive_photo where sha256=?',(digest,))]
result={'photos':rows,'completion_count':0,'managed_count':0}
if len(rows)==1:
 ident=rows[0]['id']
 result['completion_count']=db.execute('select count(*) from archive_archivecompletion where photo_id=?',(ident,)).fetchone()[0]
 managed=[dict(r) for r in db.execute('select relative_path,actual_basename from archive_managedoriginal where photo_id=?',(ident,))]
 result['managed_count']=len(managed)
 if len(managed)==1:
  p=pathlib.Path('/roots/archive')/managed[0]['relative_path']
  assert not p.is_symlink() and p.resolve().is_relative_to('/roots/archive')
  result['disk_sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
  result['basename']=managed[0]['actual_basename']
result['foreign_key_errors']=len(db.execute('pragma foreign_key_check').fetchall())
print(json.dumps(result))
'''


class Lab:
    def __init__(self, config, deadline):
        self.config = config
        self.deadline = deadline
        self.last_failure = None
        self.secret_values = []
        self.ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=yes',
                    '-p', str(config['ssh_port']), config['ssh_target']]

    def execute(self, arguments, *, payload=None, timeout=60):
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, 'tracer_deadline_exceeded')
        command = [*self.ssh, shlex.join(arguments)]
        result = subprocess.run(command, input=payload, capture_output=True, timeout=min(timeout, remaining))
        if result.returncode:
            output = self.redact((result.stdout + result.stderr).decode(errors='replace')[-6000:])
            self.last_failure = {'exit': result.returncode, 'output_tail': output}
            # Do not print remote env, full command, secrets or absolute roots.
            raise RuntimeError('remote_command_failed:' + str(result.returncode))
        return result.stdout

    def redact(self, output):
        for value in (*self.secret_values, self.config['lab_root'], self.config['python_root']):
            if value:
                output = output.replace(value, '<redacted>')
        return output


def run(args):
    config_path = os.environ.get('BIRDS_P6_LAB_CONFIG', '')
    require(config_path and Path(config_path).is_absolute(), 'BIRDS_P6_LAB_CONFIG_required')
    config = json.loads(Path(config_path).read_text())
    require(config['schema'] == 1, 'config_schema_invalid')
    require(re.fullmatch(r'[A-Za-z0-9_.-]+(?:@[A-Za-z0-9_.-]+)?', config['ssh_target']), 'ssh_target_invalid')
    require(not config['ssh_target'].startswith('-'), 'ssh_target_invalid')
    for key in ('lab_root', 'python_root'):
        absolute(config[key])
    require(config['owner'] and re.fullmatch(r'[a-z0-9-]{8,64}', config['owner']), 'owner_invalid')
    require(re.fullmatch(r'birds-p6-[a-z0-9-]{8,64}', config['project']), 'project_invalid')
    require(config['base_image_id'] == BASE_ID, 'fixed_image_id_required')
    require(re.fullmatch(r'[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9_.-]+', config['base_image']), 'base_image_tag_invalid')
    require(re.fullmatch(r'[a-f0-9]{64}', config['python_tree_sha256']), 'material_sha_required')
    for key in ('uid', 'gid', 'ssh_port', 'local_port'):
        require(type(config[key]) is int and config[key] > 0, 'numeric_config_invalid')
    require(all(config[key] <= 65535 for key in ('ssh_port', 'local_port')), 'port_invalid')
    root = Path(__file__).resolve().parent.parent
    require(Path(args.compose).resolve() == root / 'compose.yaml', 'compose_path_invalid')
    payload, source_sha, manifest = source_snapshot(root)
    require(len(payload) < 32 * 1024 * 1024, 'source_budget_exceeded')
    run_id = uuid.uuid4().hex
    project = config['project']
    run_root = config['lab_root'] + '/runs/' + run_id
    code = config['lab_root'] + '/code/' + source_sha + '-' + config['python_tree_sha256']
    result_dir = Path(config.get('result_dir', '.planning/phases/06-fnos/evidence/06-01'))
    result_dir = result_dir.resolve()
    require(result_dir.is_relative_to(root / '.planning/phases/06-fnos/evidence/06-01'), 'result_dir_invalid')
    result_dir.mkdir(parents=True, exist_ok=True)
    result_file = result_dir / (run_id + '.json')
    report = {'status': 'running', 'run_id': run_id, 'project': project, 'source_sha256': source_sha,
              'base_image_id': BASE_ID, 'python_tree_sha256': config['python_tree_sha256'],
              'recognition': 'disabled_manual_path_no_model_evaluation', 'checks': {},
              'cleanup': 'not_started', 'commit': 'skipped_commit_docs_false'}
    lab = Lab(config, time.monotonic() + 600)
    compose = ['docker', 'compose', '--project-name', project, '--env-file', run_root + '/tracer.env', '-f', code + '/compose.yaml']
    prepared = False
    forward = None

    def close_forward():
        nonlocal forward
        if forward is not None:
            forward.terminate()
            try:
                forward.wait(timeout=10)
            except subprocess.TimeoutExpired:
                forward.kill(); forward.wait(timeout=5)
            forward = None

    def connect_web():
        """Only an inspected endpoint of this project's sole internal backend."""
        nonlocal forward
        identifiers = lab.execute([*compose, 'ps', '-q', 'web']).decode().split()
        require(len(identifiers) == 1 and re.fullmatch(r'[a-f0-9]{64}', identifiers[0]), 'web_container_not_unique')
        container = json.loads(lab.execute(['docker', 'inspect', identifiers[0]]))[0]
        labels = container['Config']['Labels']
        require(labels.get('com.docker.compose.project') == project
                and labels.get('com.docker.compose.service') == 'web'
                and labels.get('org.birds.source-sha') == source_sha, 'web_container_owner_invalid')
        networks = container['NetworkSettings']['Networks']
        require(len(networks) == 1, 'web_network_count_invalid')
        name, endpoint = next(iter(networks.items()))
        require(name == project + '_backend', 'web_network_name_invalid')
        network_id = endpoint['NetworkID']
        require(re.fullmatch(r'[a-f0-9]{64}', network_id), 'web_network_id_invalid')
        network = json.loads(lab.execute(['docker', 'network', 'inspect', network_id]))[0]
        require(network['Labels'].get('com.docker.compose.project') == project
                and network['Labels'].get('com.docker.compose.network') == 'backend'
                and network['Driver'] == 'bridge' and network['Internal'] is True, 'web_network_owner_invalid')
        address = ipaddress.IPv4Address(endpoint['IPAddress'])
        require(not (address.is_unspecified or address.is_loopback or address.is_multicast or address.is_link_local),
                'web_ipv4_invalid')
        subnets = [ipaddress.ip_network(row['Subnet']) for row in network['IPAM']['Config'] if row.get('Subnet')]
        require(any(subnet.version == 4 and address in subnet for subnet in subnets), 'web_ipv4_outside_network')
        close_forward()
        forward = subprocess.Popen([*lab.ssh[:-1], '-N', '-o', 'ExitOnForwardFailure=yes', '-L',
                                    f"127.0.0.1:{config['local_port']}:{address}:8000", lab.ssh[-1]],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {'container_id': identifiers[0], 'network_id': network_id, 'ipv4': str(address)}
    try:
        image = json.loads(lab.execute(['docker', 'image', 'inspect', config['base_image']]))[0]
        require(image['Id'] == BASE_ID and image['Architecture'] == 'amd64', 'fixed_image_unavailable')
        require(not lab.execute(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project]).strip(),
                'owned_project_already_has_containers')
        report['docker_version'] = lab.execute(['docker', 'version', '--format', '{{.Server.Version}}']).decode().strip()
        report['compose_version'] = lab.execute(['docker', 'compose', 'version', '--short']).decode().strip()
        env = {'BIRDS_PROJECT': project, 'BIRDS_APP_IMAGE': project + ':source', 'BIRDS_UID': config['uid'], 'BIRDS_GID': config['gid'],
               'BIRDS_DEPLOYMENT_ID': run_id, 'BIRDS_SECRET_KEY': secrets.token_hex(32), 'BIRDS_LOCAL_HTTP': '1',
               'BIRDS_ALLOWED_HOSTS': 'localhost,127.0.0.1'}
        lab.secret_values.append(env['BIRDS_SECRET_KEY'])
        prepare_config = {**config, 'run_id': run_id, 'project': project, 'source_tar': base64.b64encode(payload).decode(),
                          'source_sha': source_sha, 'source_manifest': manifest, 'environment': env}
        report['checks']['prepare'] = json.loads(lab.execute(['python3', '-c', PREPARE], payload=json.dumps(prepare_config).encode(), timeout=180))
        prepared = True
        lab.execute([*compose, 'config', '--quiet'])
        lab.execute(['docker', 'build', '--pull=false', '--network=none', '--build-arg', 'BIRDS_BASE_IMAGE=' + config['base_image'],
                     '--build-arg', 'BIRDS_SOURCE_SHA=' + source_sha, '--build-arg', 'BIRDS_BASE_IMAGE_ID=' + BASE_ID,
                     '--build-arg', 'BIRDS_PYTHON_TREE_SHA=' + config['python_tree_sha256'],
                     '-t', project + ':source', code], timeout=180)
        app_image = json.loads(lab.execute(['docker', 'image', 'inspect', project + ':source']))[0]
        require(app_image['Config']['Labels']['org.birds.source-sha'] == source_sha, 'built_source_label_mismatch')
        require(app_image['Size'] < 1024 * 1024 * 1024, 'image_budget_exceeded')
        report['app_image_id'] = app_image['Id']
        lab.execute([*compose, '--profile', 'initialize', 'run', '--rm', '--no-deps', 'init'], timeout=90)
        lab.execute([*compose, 'up', '-d', '--no-build', '--pull', 'never', 'web', 'worker'])
        report['checks']['web_endpoint'] = connect_web()
        cookies = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
        base = 'http://127.0.0.1:' + str(config['local_port'])

        def http_request(path, data=None, headers=None):
            request = urllib.request.Request(base + path, data=data, headers=headers or {})
            try:
                with opener.open(request, timeout=10) as response:
                    require(response.status == 200, 'http_status_invalid:' + str(response.status))
                    require('no-store' in response.headers.get('Cache-Control', ''), 'http_no_store_missing')
                    return response.read()
            except urllib.error.HTTPError as error:
                report['http_error'] = {'status': error.code, 'path': path}
                error.close()
                raise RuntimeError('http_error_status:' + str(error.code)) from None

        def wait_for(function, predicate, label):
            until = min(time.monotonic() + 60, lab.deadline)
            while time.monotonic() < until:
                require(forward.poll() is None, 'ssh_forward_exited')
                try:
                    value = function()
                    if predicate(value):
                        return value
                except (urllib.error.URLError, ConnectionError):
                    pass
                time.sleep(1)
            raise RuntimeError(label + '_timeout')

        wait_for(lambda: http_request('/review/'), lambda v: bool(v), 'web_ready')
        fixture = json.loads(lab.execute([*compose, 'exec', '-T', 'web', 'python', '-c', FIXTURE]))
        digest = fixture['sha256']
        report['checks']['fixture'] = fixture
        result_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')

        def database():
            return json.loads(lab.execute([*compose, 'exec', '-T', 'web', 'python', '-c', DATABASE, digest]))

        before = wait_for(database, lambda v: len(v['photos']) == 1, 'import')
        require(before['completion_count'] == before['managed_count'] == 0, 'archive_before_confirmation')
        report['checks']['before_confirmation'] = before
        result_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        ident = str(uuid.UUID(before['photos'][0]['id']))
        http_request('/photos/' + ident + '/')
        token = next(c.value for c in cookies if c.name == 'csrftoken')
        body = urllib.parse.urlencode({'metadata_version': before['photos'][0]['metadata_version'], 'request_id': str(uuid.uuid4()),
                                       'classification': 'species', 'local_names': '部署合成鸟'}).encode()
        accepted = json.loads(http_request('/photos/' + ident + '/', body, {'Content-Type': 'application/x-www-form-urlencoded',
                                  'X-CSRFToken': token, 'X-Archive-Confirm': 'true', 'Origin': base}))
        require(accepted.get('accepted') is True, 'confirmation_not_accepted')
        archived = wait_for(database, lambda v: v['completion_count'] == v['managed_count'] == 1, 'archive')
        require(archived['disk_sha256'] == digest and archived['foreign_key_errors'] == 0, 'archive_integrity_failed')
        require(hashlib.sha256(http_request('/photos/' + ident + '/download/')).hexdigest() == digest, 'download_sha_mismatch')
        roles = lab.execute([*compose, 'ps', '--format', 'json']).decode()
        worker_processes = lab.execute([*compose, 'top', 'worker']).decode()
        require(worker_processes.count('/app/manage.py runrecognitionworker') == 1, 'supervisor_count_invalid')
        require(worker_processes.count('/app/manage.py runworker') == 1, 'file_worker_count_invalid')
        report['checks'].update({'before_confirmation': before, 'archived': archived, 'photo_id': ident,
                                  'download_sha256': digest, 'roles': roles, 'worker_processes': worker_processes})
        lab.execute([*compose, 'up', '-d', '--no-build', '--pull', 'never', '--force-recreate', 'web', 'worker'])
        report['checks']['recreated_web_endpoint'] = connect_web()
        wait_for(lambda: http_request('/photos/' + ident + '/'), lambda v: bool(v), 'recreated_web')
        after = database()
        require(after == archived, 'recreated_database_changed')
        require(hashlib.sha256(http_request('/photos/' + ident + '/download/')).hexdigest() == digest, 'recreated_download_sha_mismatch')
        report['checks']['after_recreate'] = after
        report['status'] = 'passed'
    except Exception as error:
        report['status'] = 'blocked_or_failed'
        report['error'] = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
        if not isinstance(error, (ValueError, RuntimeError)):
            report['error_frames'] = [{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
                                      for frame in traceback.extract_tb(error.__traceback__)]
        report['remote_failure'] = lab.last_failure
    finally:
        close_forward()
        if prepared:
            if report['status'] != 'passed':
                report['owned_diagnostics'] = {}
                lab.deadline = max(lab.deadline, time.monotonic() + 20)
                for label, arguments in (
                    ('ps', [*compose, 'ps', '--all', '--format', 'json']),
                    ('logs', [*compose, 'logs', '--no-color', '--tail', '80', '--timestamps', 'web', 'worker']),
                ):
                    try:
                        output = lab.execute(arguments, timeout=8).decode(errors='replace')
                        report['owned_diagnostics'][label] = lab.redact(output[-12000:])
                    except Exception as diagnostic_error:
                        report['owned_diagnostics'][label] = {'error_type': type(diagnostic_error).__name__}
            try:
                lab.deadline = max(lab.deadline, time.monotonic() + 45)
                lab.execute([*compose, 'down', '--timeout', '15'], timeout=40)
                require(not lab.execute(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project]).strip(),
                        'owned_containers_remain')
                report['cleanup'] = 'owned_compose_down_roots_and_image_retained'
            except Exception:
                report['cleanup'] = 'failed_requires_owned_project_review'
                report['status'] = 'blocked_or_failed'
        result_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'result': str(result_file.relative_to(root))}, ensure_ascii=False))
    return 0 if report['status'] == 'passed' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compose', default='compose.yaml')
    parser.add_argument('--isolated', action='store_true')
    parser.add_argument('--container-root-check', choices=('init', 'web', 'worker'))
    args = parser.parse_args()
    if args.container_root_check:
        container_roots(args.container_root_check)
        return 0
    require(args.isolated, 'isolated_flag_required')
    return run(args)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError) as error:
        print(json.dumps({'status': 'blocked', 'error': str(error) if isinstance(error, ValueError) else type(error).__name__}))
        sys.exit(2)
