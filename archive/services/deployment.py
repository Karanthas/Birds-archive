"""显式登记新专用根；日常启动只读验证，不修复或接管现有图库。"""
import errno
import fcntl
import json
import os
import platform
import re
import sqlite3
import stat
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.db.migrations.loader import MigrationLoader
from deploy.materials import verify_materials

from archive.services.paths import (UnsafePath, open_regular_beneath, open_root,
                                   root_identity, validate_root_isolation)

NAMES = ('inbox', 'data', 'archive', 'claim')
REGISTRY = 'deployment-v1.json'
OWNER = '.birds-tracer-root.json'
RESTORE_MARKER = '.birds-restore-v1.json'
RESTORE_LIFECYCLE_LOCK = 'restore-lifecycle.lock'
LOCAL_FILESYSTEMS = {'apfs', 'hfs', 'ext4', 'ext3', 'ext2', 'xfs', 'btrfs', 'zfs', 'f2fs', 'tmpfs'}
VERIFIED_SQLITE_SOURCE = '2026-05-05 10:34:17 c88b22011a54b4f6fbd149e9f8e4de77658ce58143a1af0e3785e4e6475127e9'
VERIFIED_BASE_IMAGE = 'sha256:605098b794ffa9ea41b9681ccd0159f40667079b5fe13d867b1fa4109e0f6452'


class DeploymentError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(ok, code):
    if not ok:
        raise DeploymentError(code)


def linux_mount_identity(path, mountinfo):
    path = Path(path).resolve(strict=True)
    candidates = []
    unescape = lambda value: re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)
    for line in mountinfo.splitlines():
        left, right = line.split(' - ', 1)
        fields, filesystem = left.split(), right.split()
        mount = Path(unescape(fields[4]))
        if path.is_relative_to(mount):
            candidates.append((len(mount.parts), {'type': filesystem[0], 'device': fields[2],
                              'root': str(Path(unescape(fields[3])) / path.relative_to(mount)),
                              'mount_id': fields[0]}))
    require(bool(candidates), 'deployment_filesystem_unverified')
    return max(candidates, key=lambda row: row[0])[1]


def validate_claim_mount(inbox, claim, mountinfo):
    # mount ID仅用于当前namespace内关系比较，不能作为跨容器持久身份。
    require(linux_mount_identity(inbox, mountinfo)['mount_id']
            == linux_mount_identity(claim, mountinfo)['mount_id'], 'deployment_claim_separate_mount')


def filesystem_identity(path):
    """Linux忽略随容器重建变化的mount ID，保留实际设备及该根在卷上的位置。"""
    path = Path(path).resolve(strict=True)
    candidates = []
    if sys.platform.startswith('linux'):
        observed = linux_mount_identity(path, Path('/proc/self/mountinfo').read_text())
        return {key: value for key, value in observed.items() if key != 'mount_id'}
    elif sys.platform == 'darwin':
        observed = subprocess.run(['/sbin/mount'], capture_output=True, text=True, timeout=3, check=True)
        for line in observed.stdout.splitlines():
            device, rest = line.split(' on ', 1)
            mount, flags = rest.rsplit(' (', 1)
            mount = Path(mount)
            if path.is_relative_to(mount):
                options = flags.rstrip(')').split(', ')
                candidates.append((len(mount.parts), {'type': options[0] if 'local' in options else 'unknown',
                                  'device': device, 'root': str(path.relative_to(mount))}))
    require(bool(candidates), 'deployment_filesystem_unverified')
    return max(candidates, key=lambda row: row[0])[1]


def required_migrations():
    return sorted(MigrationLoader(None, ignore_no_migrations=True).disk_migrations)


def runtime_identity():
    values = {key: os.environ.get(key, '') for key in ('BIRDS_SOURCE_SHA', 'BIRDS_BASE_IMAGE_ID', 'BIRDS_PYTHON_TREE_SHA')}
    require(bool(re.fullmatch('[a-f0-9]{64}', values['BIRDS_SOURCE_SHA']))
            and bool(re.fullmatch('sha256:[a-f0-9]{64}', values['BIRDS_BASE_IMAGE_ID']))
            and bool(re.fullmatch('[a-f0-9]{64}', values['BIRDS_PYTHON_TREE_SHA'])),
            'deployment_runtime_unconfigured')
    try:
        verify_materials('/opt/birds-python', '/opt/birds-python-manifest.json', values['BIRDS_PYTHON_TREE_SHA'])
    except ValueError:
        raise DeploymentError('deployment_python_materials_invalid') from None
    with sqlite3.connect(':memory:') as db:
        source = db.execute('select sqlite_source_id()').fetchone()[0]
    require(source == VERIFIED_SQLITE_SOURCE, 'deployment_sqlite_source_mismatch')
    require(values['BIRDS_BASE_IMAGE_ID'] == VERIFIED_BASE_IMAGE, 'deployment_base_image_mismatch')
    return {**values, 'python': platform.python_version(), 'sqlite_source_id': source}


def execution_identity():
    try:
        uid, gid = (int(os.environ[key]) for key in ('BIRDS_UID', 'BIRDS_GID'))
    except (KeyError, ValueError):
        raise DeploymentError('deployment_user_unconfigured') from None
    require(uid > 0 and gid > 0, 'deployment_root_user_forbidden')
    require(uid == os.geteuid() and gid == os.getegid(), 'deployment_user_mismatch')
    return {'uid': uid, 'gid': gid}


def resource_budget():
    try:
        total = int(os.environ['BIRDS_DEPLOYMENT_MEMORY_MB'])
        reserve = int(os.environ['BIRDS_DEPLOYMENT_RESERVE_MB'])
    except (KeyError, ValueError):
        raise DeploymentError('deployment_memory_budget_unconfigured') from None
    model = settings.RECOGNITION_MEMORY_MB if settings.BIRDS_RECOGNITION_ENABLED else 0
    require(total > 0 and reserve >= 512 and settings.WORKER_MEMORY_MB + (model or 0) + reserve <= total,
            'deployment_memory_budget_invalid')
    # 正常读取cgroup可用上限，未知不冒充已测；容器内的实际进程限制仍由既有监督入口施加。
    limit = None
    for filename in ('/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory/memory.limit_in_bytes'):
        try:
            value = Path(filename).read_text().strip()
            if value != 'max':
                limit = int(value)
            break
        except (OSError, ValueError):
            continue
    # web/init使用较小独立预算，worker须容纳文件worker及配置的模型预算。
    if os.environ.get('BIRDS_RUNTIME_ROLE') == 'worker' and limit is not None:
        require((settings.WORKER_MEMORY_MB + (model or 0) + 256) * 1024**2 <= limit,
                'deployment_container_memory_insufficient')
    return {'total_mb': total, 'reserve_mb': reserve, 'model_mb': model, 'cgroup_limit_bytes': limit}


@contextmanager
def roots_open():
    roots = {}
    try:
        for name in NAMES:
            path = getattr(settings, 'BIRDS_' + name.upper() + '_ROOT')
            # 拒绝中间组件符号链接及非规范别名；open_root自身拒绝末段链接。
            require(str(Path(path).resolve(strict=True)) == str(path), 'deployment_root_alias')
            roots[name] = (str(path), open_root(path))
        for index, name in enumerate(NAMES):
            for other in NAMES[index + 1:]:
                try:
                    validate_root_isolation(roots[name][1], roots[other][1], roots[name][0], roots[other][0])
                except UnsafePath:
                    raise DeploymentError('deployment_roots_not_isolated') from None
        yield roots
    except DeploymentError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise DeploymentError('deployment_root_unavailable') from None
    finally:
        for _, fd in roots.values():
            os.close(fd)


def check_linux_acl(fd):
    for name in ('system.posix_acl_access', 'system.posix_acl_default'):
        try:
            acl = os.getxattr(fd, name)
        except OSError as error:
            require(error.errno in (errno.ENODATA, errno.ENOTSUP), 'deployment_acl_unverified')
        else:
            require(not acl, 'deployment_acl_rejected')


def root_snapshot(roots, user):
    result = {}
    for name, (path, fd) in roots.items():
        info = os.fstat(fd)
        require(info.st_uid == user['uid'] and info.st_gid == user['gid']
                and stat.S_IMODE(info.st_mode) == 0o700
                and os.access(path, os.R_OK | os.W_OK | os.X_OK, effective_ids=True),
                'deployment_permissions_invalid')
        if sys.platform.startswith('linux'):
            check_linux_acl(fd)
        filesystem = filesystem_identity(path)
        require(filesystem['type'] in LOCAL_FILESYSTEMS, 'deployment_filesystem_rejected')
        result[name] = {'identity': root_identity(fd), 'filesystem': filesystem}
    require(result['inbox']['identity']['dev'] == result['claim']['identity']['dev'],
            'deployment_claim_cross_device')
    if sys.platform.startswith('linux'):
        validate_claim_mount(roots['inbox'][0], roots['claim'][0], Path('/proc/self/mountinfo').read_text())
    return result


def read_json(fd, name):
    with open_regular_beneath(fd, name) as stream:
        payload = stream.read(65537)
    require(len(payload) <= 65536, 'deployment_record_invalid')
    return json.loads(payload)


def probe_roots(roots):
    """只操作自己O_EXCL/mkdir新建的随机探针；不操作任何已有文件。"""
    created = {}
    token = '.birds-probe-' + uuid.uuid4().hex
    try:
        for name, (_, root_fd) in roots.items():
            os.mkdir(token, mode=0o700, dir_fd=root_fd)
            fd = os.open(token, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            created[name] = fd
            file = os.open('source', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            with os.fdopen(file, 'wb') as stream:
                stream.write(b'birds deployment probe v1'); stream.flush(); os.fsync(stream.fileno())
            os.link('source', 'published', src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            try:
                os.link('source', 'published', src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            except FileExistsError:
                pass
            else:
                raise DeploymentError('deployment_no_clobber_unverified')
            os.fsync(fd); os.fsync(root_fd)
        try:
            os.rename('source', 'claimed', src_dir_fd=created['inbox'], dst_dir_fd=created['claim'])
            os.fsync(created['inbox']); os.fsync(created['claim'])
        except OSError:
            raise DeploymentError('deployment_claim_rename_failed') from None
    finally:
        for name, fd in created.items():
            try:
                for filename in ('source', 'published', 'claimed'):
                    try:
                        os.unlink(filename, dir_fd=fd)
                    except FileNotFoundError:
                        pass
                os.fsync(fd)
            finally:
                os.close(fd)
                os.rmdir(token, dir_fd=roots[name][1]); os.fsync(roots[name][1])


def register_deployment():
    deployment_id = os.environ.get('BIRDS_DEPLOYMENT_ID', '')
    require(bool(re.fullmatch('[A-Za-z0-9_-]{8,64}', deployment_id)), 'deployment_id_invalid')
    user = execution_identity()
    runtime = runtime_identity()
    budget = resource_budget()
    with roots_open() as roots:
        require(REGISTRY not in os.listdir(roots['data'][1]), 'deployment_already_registered')
        snapshot = root_snapshot(roots, user)
        for name, (_, fd) in roots.items():
            require(set(os.listdir(fd)) == {OWNER}, 'deployment_init_not_empty')
            require(read_json(fd, OWNER) == {'deployment_id': deployment_id, 'role': name},
                    'deployment_ownership_mismatch')
        probe_roots(roots)
        record = {'schema': 1, 'deployment_id': deployment_id, 'user': user, 'roots': snapshot,
                  'runtime': runtime, 'migrations': required_migrations()}
        fd = os.open(REGISTRY, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                     dir_fd=roots['data'][1])
        with os.fdopen(fd, 'wb') as stream:
            stream.write(json.dumps(record, sort_keys=True).encode()); stream.flush(); os.fsync(stream.fileno())
        os.fsync(roots['data'][1])
    return {'status': 'registered', 'schema': 1, 'resources': budget}


def validate_deployment(*, migration=False, initializing=False):
    user = execution_identity()
    budget = resource_budget()
    with roots_open() as roots:
        try:
            record = read_json(roots['data'][1], REGISTRY)
        except FileNotFoundError:
            raise DeploymentError('deployment_not_registered') from None
        except (ValueError, UnsafePath):
            raise DeploymentError('deployment_record_invalid') from None
        require(isinstance(record, dict) and record.get('schema') == 1, 'deployment_record_invalid')
        require(record.get('deployment_id') == os.environ.get('BIRDS_DEPLOYMENT_ID')
                and record.get('user') == user, 'deployment_ownership_mismatch')
        require(record.get('roots') == root_snapshot(roots, user), 'deployment_root_changed')
        runtime = runtime_identity()
        expected = record.get('runtime', {})
        if migration:
            # 显式升级只允许新源码；Python/SQLite/基础材料变更须另行核验，不自动重绑。
            require({k: v for k, v in expected.items() if k != 'BIRDS_SOURCE_SHA'}
                    == {k: v for k, v in runtime.items() if k != 'BIRDS_SOURCE_SHA'},
                    'deployment_runtime_changed')
        else:
            require(expected == runtime, 'deployment_runtime_changed')
        path = Path(roots['data'][0]) / 'archive.sqlite3'
        require(Path(settings.DATABASES['default']['NAME']) == path, 'deployment_database_wrong_root')
        try:
            with open_regular_beneath(roots['data'][1], 'archive.sqlite3') as stream:
                if not initializing:
                    require('database' in record, 'deployment_initialization_incomplete')
                    require(record['database'] == root_identity(stream.fileno()), 'deployment_database_changed')
                require(stream.read(16) == b'SQLite format 3\x00', 'deployment_database_invalid')
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
                applied = set(db.execute('select app,name from django_migrations'))
        except FileNotFoundError:
            raise DeploymentError('deployment_database_missing') from None
        except (sqlite3.Error, OSError, UnsafePath):
            raise DeploymentError('deployment_database_invalid') from None
        if not migration:
            require(set(required_migrations()) == applied, 'deployment_schema_mismatch')
        else:
            require(applied <= set(required_migrations()), 'deployment_schema_mismatch')
        restore_record = record.get('restore')
        if restore_record is not None:
            require(isinstance(restore_record, dict)
                    and set(restore_record) == {'session_id', 'manifest_sha256'},
                    'deployment_restore_record_invalid')
            try:
                from archive.models import RestoreSession
                session = RestoreSession.objects.get(pk=restore_record['session_id'])
            except (RestoreSession.DoesNotExist, TypeError, ValueError):
                raise DeploymentError('deployment_restore_session_invalid') from None
            require(session.status == 'activated'
                    and session.backup_manifest_sha256 == restore_record['manifest_sha256'],
                    'deployment_restore_session_invalid')
            require(_restore_target_paths(session) == {name: path for name, (path, _) in roots.items()},
                    'deployment_restore_roots_changed')
    return {'status': 'verified', 'schema': 1, 'roots': {name: 'verified' for name in NAMES},
            'resources': budget}


def _restore_target_paths(session):
    result = {}
    values = session.target_roots if isinstance(session.target_roots, dict) else {}
    for name in NAMES:
        value = values.get(name)
        if isinstance(value, dict):
            value = value.get('path')
        require(isinstance(value, str) and Path(value).is_absolute(), 'restore_target_roots_invalid')
        result[name] = str(Path(value))
    return result


def _source_path(value):
    if isinstance(value, dict):
        value = value.get('path')
    return Path(value) if isinstance(value, str) and Path(value).is_absolute() else None


def _validate_restore_markers(roots, session):
    targets = _restore_target_paths(session)
    require(targets == {name: path for name, (path, _) in roots.items()}, 'restore_target_roots_changed')
    for name, (_, root_fd) in roots.items():
        try:
            marker = read_json(root_fd, RESTORE_MARKER)
            owner = read_json(root_fd, OWNER)
        except (FileNotFoundError, ValueError, UnsafePath):
            raise DeploymentError('restore_target_ownership_invalid') from None
        require(isinstance(marker, dict)
                and str(marker.get('session_id', marker.get('session', ''))) == str(session.pk)
                and marker.get('role') == name
                and marker.get('backup_manifest_sha256',
                               marker.get('manifest_sha256', marker.get('manifest'))) == session.backup_manifest_sha256,
                'restore_target_marker_invalid')
        require(owner == {'deployment_id': os.environ.get('BIRDS_DEPLOYMENT_ID'), 'role': name},
                'restore_target_ownership_invalid')
    sources = session.source_roots if isinstance(session.source_roots, dict) else {}
    target_paths = [Path(value) for value in targets.values()]
    target_filesystems = [filesystem_identity(value) for value in targets.values()]
    for value in sources.values():
        source_filesystem = value.get('filesystem') if isinstance(value, dict) else None
        if isinstance(source_filesystem, dict):
            require(all(not _filesystem_roots_overlap(source_filesystem, target)
                        for target in target_filesystems), 'restore_target_overlaps_source')
            continue
        source = _source_path(value)
        if source is None:
            continue
        require(all(source != target and source not in target.parents and target not in source.parents
                    for target in target_paths), 'restore_target_overlaps_source')


def _filesystem_roots_overlap(left, right):
    if left.get('device') != right.get('device'):
        return False
    left_root, right_root = (Path('/') / str(value.get('root', '')).lstrip('/')
                             for value in (left, right))
    return (left_root == right_root or left_root in right_root.parents
            or right_root in left_root.parents)


def activate_restored_deployment(session_id):
    """在唯一目标锁内复核同一会话，最后才发布正常启动可见的部署登记。"""
    from archive.models import RestoreSession
    from archive.services.restore import activate_restore, verify_restore

    try:
        session = RestoreSession.objects.get(pk=session_id)
    except (RestoreSession.DoesNotExist, TypeError, ValueError):
        raise DeploymentError('restore_session_missing') from None
    user = execution_identity()
    runtime = runtime_identity()
    budget = resource_budget()
    with roots_open() as roots:
        lock_fd = os.open(RESTORE_LIFECYCLE_LOCK,
                          os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                          0o600, dir_fd=roots['data'][1])
        worker_lock = None
        try:
            require(stat.S_ISREG(os.fstat(lock_fd).st_mode), 'restore_lifecycle_lock_invalid')
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DeploymentError('restore_lifecycle_busy') from None
            data_fd = roots['data'][1]
            try:
                existing = read_json(data_fd, REGISTRY)
            except FileNotFoundError:
                existing = None
            except (ValueError, UnsafePath):
                raise DeploymentError('deployment_record_invalid') from None
            expected_restore = {'session_id': str(session.pk),
                                'manifest_sha256': session.backup_manifest_sha256}
            if existing is not None:
                require(existing.get('restore') == expected_restore, 'restore_different_session_active')
                return validate_deployment()
            worker_lock = os.open('worker.lock',
                                  os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                  0o600, dir_fd=roots['data'][1])
            require(stat.S_ISREG(os.fstat(worker_lock).st_mode), 'deployment_lock_invalid')
            try:
                fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DeploymentError('deployment_writer_busy') from None
            session = verify_restore(session.pk, for_activation=True)
            require(session.status in ('verified', 'activated'), 'restore_session_not_verified')
            _validate_restore_markers(roots, session)
            snapshot = root_snapshot(roots, user)
            path = Path(roots['data'][0]) / 'archive.sqlite3'
            require(Path(settings.DATABASES['default']['NAME']) == path, 'deployment_database_wrong_root')
            try:
                with open_regular_beneath(data_fd, 'archive.sqlite3') as stream:
                    database_identity = root_identity(stream.fileno())
                    require(stream.read(16) == b'SQLite format 3\x00', 'deployment_database_invalid')
                with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
                    applied = set(db.execute('select app,name from django_migrations'))
            except (FileNotFoundError, sqlite3.Error, OSError, UnsafePath):
                raise DeploymentError('deployment_database_invalid') from None
            require(set(required_migrations()) == applied, 'deployment_schema_mismatch')
            record = {'schema': 1, 'deployment_id': os.environ.get('BIRDS_DEPLOYMENT_ID'),
                      'user': user, 'roots': snapshot, 'runtime': runtime,
                      'migrations': required_migrations(), 'database': database_identity,
                      'restore': expected_restore}
            session = activate_restore(session.pk)
            require(session.status == 'activated', 'restore_activation_failed')
            temporary = '.deployment-restore-' + uuid.uuid4().hex
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=data_fd)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(json.dumps(record, sort_keys=True).encode())
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temporary, REGISTRY, src_dir_fd=data_fd, dst_dir_fd=data_fd,
                        follow_symlinks=False)
                os.fsync(data_fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=data_fd)
                    os.fsync(data_fd)
                except FileNotFoundError:
                    pass
        finally:
            if worker_lock is not None:
                os.close(worker_lock)
            os.close(lock_fd)
    return validate_deployment()


def replace_record(data_fd, record):
    temporary = '.deployment-' + uuid.uuid4().hex
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=data_fd)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(json.dumps(record, sort_keys=True).encode()); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, REGISTRY, src_dir_fd=data_fd, dst_dir_fd=data_fd)
        os.fsync(data_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=data_fd)
        except FileNotFoundError:
            pass


def finalize_deployment():
    """只供显式init在迁移成功后登记数据库；普通启动不能补写。"""
    validate_deployment(initializing=True)
    with roots_open() as roots:
        data_fd = roots['data'][1]
        record = read_json(data_fd, REGISTRY)
        require('database' not in record, 'deployment_already_initialized')
        with open_regular_beneath(data_fd, 'archive.sqlite3') as stream:
            record['database'] = root_identity(stream.fileno())
        replace_record(data_fd, record)
    return validate_deployment()


def migrate_deployment(*, stdout):
    """显式停服升级入口；共享既有执行者锁，成功后才原子更新部署源码登记。"""
    from django.core.management import call_command
    validate_deployment(migration=True)
    with roots_open() as roots:
        data_fd = roots['data'][1]
        locks = []
        try:
            for name in ('deployment-migrate.lock', 'worker.lock', 'recognition.lock'):
                fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=data_fd)
                locks.append(fd)
                require(stat.S_ISREG(os.fstat(fd).st_mode), 'deployment_lock_invalid')
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise DeploymentError('deployment_writer_busy') from None
            validate_deployment(migration=True)
            call_command('migrate', interactive=False, stdout=stdout)
            record = read_json(data_fd, REGISTRY)
            record.update(runtime=runtime_identity(), migrations=required_migrations())
            replace_record(data_fd, record)
        finally:
            for fd in reversed(locks):
                os.close(fd)
    return validate_deployment()
