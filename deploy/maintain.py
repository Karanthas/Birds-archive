"""Sequence maintenance using the existing Compose and Django lifecycle commands."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import uuid


APP_ROLES = ('web', 'worker')
PROXIES = ('lan', 'remote')
STORAGE_ROLES = ('inbox', 'data', 'archive', 'claim')
STABLE_MATERIALS = ('BIRDS_BASE_IMAGE_ID', 'BIRDS_PYTHON_TREE_SHA')
BACKUP_MOUNT = '/backup'


class LifecycleError(RuntimeError):
    def __init__(self, code, returncode=None):
        super().__init__(code)
        self.returncode = returncode


def require(condition, code):
    if not condition:
        raise LifecycleError(code)


def _json(raw):
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        raise LifecycleError('invalid_command_response') from None


def _json_lines(raw):
    """Accept Compose array output and the newline format of older Compose releases."""
    if raw.lstrip().startswith('['):
        result = _json(raw)
    else:
        result = [_json(line) for line in raw.splitlines() if line.strip()]
    require(isinstance(result, list) and all(isinstance(row, dict) for row in result),
            'invalid_command_response')
    return result


def _receipt(raw):
    value = _json(raw)
    require(isinstance(value, dict), 'invalid_command_response')
    return value


def _quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def database_baseline(path, expected=None):
    """Hash existing archive rows by primary key, comparing only pre-upgrade columns."""
    require(path.is_file() and not path.is_symlink(), 'database_unavailable')
    result = {}
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            db.execute('BEGIN')
            require(db.execute('PRAGMA foreign_key_check').fetchone() is None, 'database_foreign_key_violation')
            names = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name GLOB 'archive_*'")}
            require({'archive_photo', 'archive_job'} <= names, 'database_baseline_unavailable')
            require(expected is None or expected.keys() <= names, 'database_baseline_changed')
            for name in sorted(names if expected is None else expected):
                table = _quote(name)
                info = list(db.execute(f'PRAGMA table_info({table})'))
                columns = [row[1] for row in info] if expected is None else expected[name]['columns']
                primary_key = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
                require(columns and primary_key and set(columns) <= {row[1] for row in info},
                        'database_baseline_changed')
                selected = ', '.join(map(_quote, columns))
                order = ', '.join(map(_quote, primary_key))
                digest = hashlib.sha256()
                count = 0
                for row in db.execute(f'SELECT {selected} FROM {table} ORDER BY {order}'):
                    values = [{'bytes': value.hex()} if isinstance(value, bytes) else value for value in row]
                    digest.update(json.dumps(values, ensure_ascii=False, separators=(',', ':')).encode() + b'\n')
                    count += 1
                result[name] = {'columns': columns, 'count': count, 'sha256': digest.hexdigest()}
    except sqlite3.Error:
        raise LifecycleError('database_baseline_unavailable') from None
    return result


def _directory(value, code):
    require(isinstance(value, str) and ':' not in value, code)
    path = Path(value)
    require(path.is_absolute() and path.resolve() == path and path.is_dir(), code)
    return path


class Lifecycle:
    def __init__(self, config_path):
        self.report = {'status': 'failed', 'stage': 'configuration', 'completed': []}
        self.config_path = Path(config_path).resolve()
        self.config = _receipt(self.config_path.read_text())
        self.compose_file = self.config_file('compose_file')
        self.old_env = self.config_file('deploy_env_file')
        self.backup_path = self.config.get('backup_directory')
        self.command_timeout = self.config.get('command_timeout_seconds', 300)
        self.drain_timeout = self.config.get('drain_timeout_seconds', 90)
        require(all(type(value) is int and 1 <= value <= 86400
                    for value in (self.command_timeout, self.drain_timeout)), 'configuration_timeout_invalid')
        # Shell interpolation and Compose defaults must not override the reviewed env file.
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(('BIRDS_', 'COMPOSE_'))}

    def config_file(self, name):
        value = self.config.get(name)
        require(isinstance(value, str) and value, name + '_unconfigured')
        path = self.config_path.parent / value
        require(path.is_file(), name + '_unavailable')
        return path.resolve()

    def stage(self, name):
        self.report['stage'] = name

    def done(self):
        self.report['completed'].append(self.report['stage'])

    def run(self, args, *, timeout=None):
        try:
            result = subprocess.run(args, env=self.env, cwd=self.compose_file.parent, capture_output=True,
                                    text=True, errors='replace', timeout=timeout or self.command_timeout)
        except subprocess.TimeoutExpired:
            raise LifecycleError('command_timeout') from None
        except OSError:
            raise LifecycleError('command_unavailable') from None
        if result.returncode:
            match = re.search(
                r'(?m)^CommandError: ((?:backup|maintenance|deployment|offline|runtime)_[a-z0-9_]+)',
                result.stderr)
            code = match.group(1) if match else 'command_failed'
            raise LifecycleError(code, result.returncode)
        return result.stdout

    def compose(self, env, *args, timeout=None):
        return self.run(['docker', 'compose', '--env-file', str(env), '-f', str(self.compose_file), *args],
                        timeout=timeout)

    def one_off(self, env, *args, volumes=(), timeout=None):
        mount_args = [arg for volume in volumes for arg in ('--volume', volume)]
        return self.compose(env, '--profile', 'maintenance', 'run', '--rm', '--no-deps', '-T',
                            *mount_args, 'maintenance', *args, timeout=timeout)

    def compose_config(self, env):
        return _receipt(self.compose(env, '--profile', 'maintenance', '--profile', 'initialize',
                                     'config', '--format', 'json'))

    def image(self, reference):
        rows = _json_lines(self.run(['docker', 'image', 'inspect', reference]))
        require(len(rows) == 1, 'application_image_unavailable')
        image = rows[0]
        metadata = image.get('Config') or {}
        variables = dict(item.split('=', 1) for item in metadata.get('Env') or [] if '=' in item)
        labels = metadata.get('Labels') or {}
        source = variables.get('BIRDS_SOURCE_SHA', '')
        require(re.fullmatch(r'[a-f0-9]{64}', source) and labels.get('org.birds.source-sha') == source
                and re.fullmatch(r'sha256:[a-f0-9]{64}', image.get('Id', ''))
                and re.fullmatch(r'sha256:[a-f0-9]{64}', variables.get(STABLE_MATERIALS[0], ''))
                and re.fullmatch(r'[a-f0-9]{64}', variables.get(STABLE_MATERIALS[1], '')),
                'application_image_materials_unverified')
        return image['Id'], source, tuple(variables[key] for key in STABLE_MATERIALS)

    def preflight(self, *, upgrade=False, check_backup=True):
        endpoint = self.env.get('DOCKER_HOST') if not self.env.get('DOCKER_CONTEXT') else None
        if not endpoint:
            endpoint = self.run(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}']).strip()
        require(endpoint.startswith('unix:///'), 'docker_local_engine_required')
        config = self.compose_config(self.old_env)
        services = config.get('services') or {}
        roles = {'init': ['init'], 'web': ['web'], 'worker': ['worker'],
                 'maintenance': ['backuparchive', 'status']}
        require(re.fullmatch(r'[a-z0-9][a-z0-9_-]*', config.get('name', '')) is not None,
                'compose_project_invalid')
        for role, command in roles.items():
            service = services.get(role) or {}
            require(service.get('command') == command and not service.get('entrypoint')
                    and service.get('pull_policy') == 'never', 'compose_roles_invalid')
        require('maintenance' in services['maintenance'].get('profiles', []), 'compose_roles_invalid')
        web = services['web']
        image_reference = web.get('image')
        require(isinstance(image_reference, str) and image_reference
                and all(services[role].get('image') == image_reference for role in roles),
                'compose_application_images_differ')
        environment = web.get('environment') or {}
        uid, gid = (str(environment.get('BIRDS_' + field, '')) for field in ('UID', 'GID'))
        require(uid.isdecimal() and gid.isdecimal() and int(uid) > 0 and int(gid) > 0
                and re.fullmatch(r'[a-zA-Z0-9_-]{8,64}', environment.get('BIRDS_DEPLOYMENT_ID', ''))
                and all(services[role].get('user') == f'{uid}:{gid}'
                        and services[role].get('environment') == environment
                        and services[role].get('volumes') == web.get('volumes') for role in roles),
                'compose_application_identity_invalid')
        mounts = web.get('volumes') or []
        require(len(mounts) == 1 and mounts[0].get('type') == 'bind' and mounts[0].get('target') == '/roots'
                and not mounts[0].get('read_only') and not (mounts[0].get('bind') or {}).get('create_host_path'),
                'compose_roots_mount_invalid')
        roots = _directory(mounts[0].get('source'), 'compose_roots_unavailable')
        for role in STORAGE_ROLES:
            require(environment.get('BIRDS_' + role.upper() + '_ROOT') == '/roots/' + role,
                    'compose_roots_mount_invalid')
            _directory(str(roots / role), 'compose_root_unavailable')
        backup_dir = _directory(self.backup_path, 'backup_directory_unavailable') if check_backup else None
        if backup_dir is not None:
            require(roots != backup_dir and roots not in backup_dir.parents and backup_dir not in roots.parents,
                    'backup_directory_overlaps_roots')
        self.old_image_id, old_source, old_materials = self.image(image_reference)
        self.new_image_id = self.old_image_id
        new_env = self.old_env
        if upgrade:
            new_env = self.config_file('upgrade_env_file')
            new_config = self.compose_config(new_env)
            new_services = new_config.get('services') or {}
            new_ref = (new_services.get('web') or {}).get('image')
            require(isinstance(new_ref, str) and new_ref and new_ref != image_reference
                    and all((new_services.get(role) or {}).get('image') == new_ref for role in roles),
                    'upgrade_image_invalid')
            for role in roles:
                new_services[role]['image'] = image_reference
            require(new_config == config, 'upgrade_compose_configuration_changed')
            self.new_image_id, new_source, new_materials = self.image(new_ref)
            require(self.new_image_id != self.old_image_id and new_source != old_source
                    and new_materials == old_materials, 'upgrade_image_materials_changed')
        return self.old_env, new_env, roots / 'data' / 'archive.sqlite3', backup_dir

    def containers(self, env, *, timeout=None):
        rows = _json_lines(self.compose(env, 'ps', '--all', '--format', 'json', *APP_ROLES, *PROXIES,
                                        timeout=timeout))
        by_role = {}
        for row in rows:
            role, identifier, state = row.get('Service'), row.get('ID'), row.get('State')
            require(role in APP_ROLES + PROXIES and role not in by_role
                    and isinstance(identifier, str) and re.fullmatch(r'[a-f0-9]{12,64}', identifier)
                    and state in ('created', 'running', 'exited', 'restarting', 'paused', 'removing', 'dead'),
                    'compose_container_roles_unexpected')
            by_role[role] = (identifier, state)
        return by_role

    def check_image(self, containers, image_id):
        ids = [containers[role][0] for role in APP_ROLES]
        identities = self.run(['docker', 'inspect', '--format', '{{.Image}}', *ids]).splitlines()
        require(identities == [image_id] * len(ids), 'compose_running_image_mismatch')

    def maintenance_status(self, env, *, timeout=None):
        return _receipt(self.one_off(env, 'backuparchive', 'status', timeout=timeout))

    def check_runtime(self, env):
        runtime = _receipt(self.compose(env, 'exec', '-T', 'web', 'python', '/app/manage.py',
                                       'checkruntime', '--deployment-start'))
        require(runtime.get('errors') == [] and runtime.get('deployment', {}).get('status') == 'verified',
                'deployment_runtime_unverified')

    def await_drain(self, env, session_id):
        deadline = time.monotonic() + self.drain_timeout
        while (remaining := deadline - time.monotonic()) > 0:
            status = self.maintenance_status(env, timeout=min(self.command_timeout, remaining))
            require(status.get('session_id') == session_id and status.get('status') == 'requested',
                    'maintenance_session_changed')
            ack = status.get('supervisor_ack')
            if isinstance(ack, dict) and ack.get('session_id') == session_id:
                if ack.get('status') == 'drain_failed' or ack.get('forced') is True:
                    raise LifecycleError('maintenance_supervisor_drain_failed')
                if ack.get('status') == 'drained' and ack.get('forced') is False:
                    return
            time.sleep(min(.5, max(0, deadline - time.monotonic())))
        raise LifecycleError('maintenance_drain_timeout')

    def verified_backup(self, env, session_id, backup_dir):
        self.stage('backup_create')
        created = _receipt(self.one_off(env, 'backuparchive', 'create', '--session', session_id,
                                       '--destination', BACKUP_MOUNT,
                                       volumes=(f'{backup_dir}:{BACKUP_MOUNT}',)))
        set_path = created.get('set')
        require(created.get('status') == 'complete' and isinstance(set_path, str), 'backup_create_unconfirmed')
        try:
            set_id = str(uuid.UUID(set_path.rsplit('/', 1)[-1]))
        except ValueError:
            raise LifecycleError('backup_set_id_invalid') from None
        require(set_path == f'{BACKUP_MOUNT}/{set_id}', 'backup_set_id_invalid')
        self.report['backup_set_id'] = set_id
        self.done()
        self.stage('backup_verify')
        verified = _receipt(self.one_off(env, 'backuparchive', 'verify', '--set', set_path,
                                        volumes=(f'{backup_dir}:{BACKUP_MOUNT}:ro',)))
        require(verified.get('status') == 'verified' and verified.get('set_id') == set_id
                and verified.get('set') == set_path, 'backup_verify_unconfirmed')
        self.done()
        return backup_dir / set_id / 'database' / 'archive.sqlite3'

    def await_start(self, env, old_containers, proxy_roles):
        deadline = time.monotonic() + self.drain_timeout
        active_roles = APP_ROLES + proxy_roles
        while (remaining := deadline - time.monotonic()) > 0:
            containers = self.containers(env, timeout=min(self.command_timeout, remaining))
            if all(role in containers and containers[role][1] == 'running'
                   and containers[role][0] != old_containers[role][0] for role in active_roles):
                try:
                    # The web entrypoint starts Gunicorn only after its runtime check passes.
                    self.compose(env, 'exec', '-T', 'web', 'python', '-c',
                                 'import socket; socket.create_connection(("127.0.0.1", 8000), 1).close()',
                                 timeout=min(self.command_timeout, remaining, 5))
                except LifecycleError as error:
                    if error.returncode is None and str(error) != 'command_timeout':
                        raise
                else:
                    processes = self.compose(env, 'top', 'worker', timeout=min(self.command_timeout, remaining))
                    if sum('runrecognitionworker' in line.split() for line in processes.splitlines()) == 1:
                        self.check_image(containers, self.new_image_id)
                        return
            time.sleep(min(.5, max(0, deadline - time.monotonic())))
        raise LifecycleError('compose_start_timeout')

    def execute(self, action):
        upgrading = action == 'upgrade'
        self.stage('preflight')
        old_env, new_env, database, backup_dir = self.preflight(
            upgrade=upgrading, check_backup=action in ('check', 'upgrade'))
        self.done()

        if action == 'check':
            self.stage('runtime')
            containers = self.containers(old_env)
            if containers.get('web', (None, None))[1] == 'running':
                self.check_runtime(old_env)
                self.report['runtime'] = 'verified'
            else:
                self.report['runtime'] = 'not_running'
            self.done()
            return
        if action == 'status':
            self.stage('status')
            state = self.maintenance_status(old_env)
            self.report['maintenance'] = {key: state[key] for key in ('session_id', 'status') if key in state}
            action_state = state.get('protected_action')
            if isinstance(action_state, dict) and action_state.get('kind') == 'offline':
                verb, status = action_state.get('verb'), action_state.get('status')
                safe = {}
                if verb in ('migrate', 'importcatalog', 'loadguidebundle', 'preparemodel'):
                    safe['verb'] = verb
                if status in ('running', 'completed', 'failed', 'unclosed'):
                    safe['status'] = status
                for key, kind in (('returncode', int), ('process_group_gone', bool)):
                    if type(action_state.get(key)) is kind:
                        safe[key] = action_state[key]
                self.report['offline_action'] = safe
            self.report['services'] = {role: value[1] for role, value in self.containers(old_env).items()}
            self.done()
            return

        self.stage('old_runtime')
        containers = self.containers(old_env)
        require(all(role in containers and containers[role][1] == 'running' for role in APP_ROLES),
                'compose_old_services_not_running')
        require(all(containers[role][1] in ('running', 'exited', 'created') for role in PROXIES
                    if role in containers), 'compose_proxy_state_unexpected')
        self.check_image(containers, self.old_image_id)
        self.check_runtime(old_env)
        self.done()

        self.stage('maintenance_request')
        requested = _receipt(self.one_off(old_env, 'backuparchive', 'request', '--owner', 'deploy-maintain'))
        session_id = requested.get('session_id')
        require(isinstance(session_id, str) and re.fullmatch(r'[a-f0-9]{32}', session_id)
                and requested.get('status') == 'requested', 'maintenance_session_unconfirmed')
        self.report['session_id'] = session_id
        self.done()
        self.stage('maintenance_drain')
        self.await_drain(old_env, session_id)
        self.done()

        if upgrading:
            baseline_file = self.verified_backup(old_env, session_id, backup_dir)
            self.stage('backup_baseline')
            baseline = database_baseline(baseline_file)
            self.done()
        else:
            self.stage('maintenance_quiesce')
            quiesced = _receipt(self.one_off(old_env, 'backuparchive', 'quiesce', '--session', session_id))
            require(quiesced.get('status') == 'quiescent' and quiesced.get('session_id') == session_id,
                    'maintenance_quiesce_unconfirmed')
            self.done()

        self.stage('stop_old')
        proxies = tuple(role for role in PROXIES if role in containers and containers[role][1] == 'running')
        self.compose(old_env, 'stop', *proxies, *APP_ROLES)
        stopped = self.containers(old_env)
        require(all(role in stopped and stopped[role] == (containers[role][0], 'exited')
                    for role in APP_ROLES + proxies)
                and all(state in ('exited', 'created') for _, state in stopped.values()),
                'compose_old_services_not_stopped')
        self.done()

        if upgrading:
            self.stage('migrate')
            output = self.one_off(new_env, 'offline', session_id, 'migrate')
            reports = [_json(line) for line in output.splitlines() if line.startswith('{')]
            require({'status': 'offline_completed', 'session_id': session_id,
                     'verb': 'migrate', 'returncode': 0} in reports, 'offline_migration_unconfirmed')
            self.done()
            self.stage('new_runtime')
            require(any(isinstance(row, dict) and row.get('errors') == []
                        and row.get('deployment', {}).get('status') == 'verified' for row in reports),
                    'deployment_runtime_unverified')
            self.done()
        else:
            self.stage('restart_baseline')
            baseline = database_baseline(database)
            self.done()

        self.stage('start_new')
        self.compose(new_env, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate',
                     '--scale', 'worker=1', *APP_ROLES)
        if proxies:
            self.compose(new_env, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never',
                         '--force-recreate', *proxies)
        self.await_start(new_env, containers, proxies)
        self.done()

        self.stage('compare_baseline')
        require(database_baseline(database, expected=baseline) == baseline, 'database_baseline_changed')
        self.done()
        self.stage('resume')
        resumed = _receipt(self.one_off(new_env, 'backuparchive', 'resume', '--session', session_id))
        require(resumed.get('status') == 'resumed' and resumed.get('session_id') == session_id,
                'maintenance_resume_unconfirmed')
        self.done()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'status', 'restart', 'upgrade'))
    parser.add_argument('--config', required=True)
    args = parser.parse_args(argv)
    report = {'status': 'failed', 'stage': 'configuration', 'completed': []}
    try:
        lifecycle = Lifecycle(args.config)
        report = lifecycle.report
        lifecycle.execute(args.action)
        report['status'] = 'ok'
    except LifecycleError as error:
        report['error'] = str(error)
        if error.returncode is not None:
            report['command_exit_code'] = error.returncode
    except KeyboardInterrupt:
        report['error'] = 'interrupted'
    except (OSError, TypeError, ValueError, KeyError):
        report['error'] = 'configuration_or_response_invalid'
    print(json.dumps(report, sort_keys=True))
    return 0 if report['status'] == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
