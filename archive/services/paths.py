"""固定根描述符；不沿路径字符串重新打开已检查的对象。"""
import os
import stat
import json
import uuid
from pathlib import Path, PurePosixPath


class UnsafePath(ValueError):
    pass


class RootsNotIsolated(UnsafePath):
    def __init__(self):
        super().__init__('roots_not_isolated')


def validate_root_isolation(inbox_fd, data_fd, inbox_path, data_path):
    """只读校验规范路径与持有FD；未知／更换／互相包含一律拒绝。"""
    try:
        paths = [Path(path).resolve(strict=True) for path in (inbox_path, data_path)]
        identities = [root_identity(fd) for fd in (inbox_fd, data_fd)]
        for index, (path, identity) in enumerate(zip(paths, identities)):
            current = open_root(path)
            try:
                if root_identity(current) != identity:
                    if index == 1:
                        raise RootsNotIsolated()
                    raise UnsafePath('root_identity_changed')
            finally:
                os.close(current)
        if paths[0].is_relative_to(paths[1]) or paths[1].is_relative_to(paths[0]):
            raise RootsNotIsolated()
        # FD祖先也必须独立：路径别名不使同一目录变成两个安全根。
        for fd, other in ((inbox_fd, identities[1]), (data_fd, identities[0])):
            current = os.dup(fd)
            try:
                seen = set()
                while True:
                    identity = root_identity(current)
                    key = (identity['dev'], identity['ino'])
                    if identity == other:
                        raise RootsNotIsolated()
                    if key in seen:
                        raise RootsNotIsolated()
                    seen.add(key)
                    parent = os.open('..', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                     dir_fd=current)
                    if root_identity(parent) == identity:
                        os.close(parent)
                        break
                    os.close(current)
                    current = parent
            finally:
                os.close(current)
    except UnsafePath:
        raise
    except (OSError, ValueError, TypeError):
        raise RootsNotIsolated() from None


def open_root(path):
    if not path or not os.path.isabs(path):
        raise UnsafePath("根目录须为已配置绝对路径")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    return fd


def parts_for(relative_path):
    if not isinstance(relative_path, str) or "\x00" in relative_path or "\\" in relative_path:
        raise UnsafePath("无效相对路径")
    parts = relative_path.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts) or PurePosixPath(relative_path).is_absolute():
        raise UnsafePath("拒绝越界路径")
    return parts


def open_regular_beneath(root_fd, relative_path):
    parts = parts_for(relative_path)
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current)
            os.close(current)
            current = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=current)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise UnsafePath("仅接受普通文件")
        return os.fdopen(fd, "rb")
    finally:
        os.close(current)


def fingerprint(stat_result):
    return {key: getattr(stat_result, "st_" + key) for key in ("dev", "ino", "size", "mtime_ns", "ctime_ns")}


def root_identity(fd):
    info = os.fstat(fd)
    return {"dev": info.st_dev, "ino": info.st_ino}


def verify_root_identity(root_fd, path, data_fd):
    """登记根inode；配置路径与固定FD均必须仍匹配，重启也不默默换根。"""
    identity = root_identity(root_fd)
    current = open_root(path)
    try:
        if root_identity(current) != identity:
            raise UnsafePath("root_identity_changed")
    finally:
        os.close(current)
    try:
        fd = os.open("inbox-root.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=data_fd)
    except FileExistsError:
        with open_regular_beneath(data_fd, "inbox-root.json") as stream:
            registered = json.loads(stream.read(4096))
        if registered != identity:
            try:
                from archive.services.restore import RestoreError, effective_fingerprint
                effective = effective_fingerprint('root', 'inbox', 'inbox', registered)
            except (ImportError, LookupError, TypeError, ValueError, RestoreError):
                raise UnsafePath("root_identity_changed") from None
            if effective != identity:
                raise UnsafePath("root_identity_changed")
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(identity).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(data_fd)


SCAN_MESSAGES = {
    "root_missing": "导入目录不可用，请检查目录或挂载。",
    "root_permission": "无法读取导入目录，请检查权限。",
    "root_identity_changed": "导入目录已更换，扫描已暂停。",
    "handoff_invalid": "交接设置不可用，请检查配置。",
    "scan_incomplete": "部分目录无法读取，将在下次补扫。",
}


def write_scan_status(data_fd, code):
    # 仅写应用自己的固定文件；不记录绝对路径或原始异常。
    name = f".scan-{uuid.uuid4().hex}"
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=data_fd)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps({"code": code}).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, "scan-status.json", src_dir_fd=data_fd, dst_dir_fd=data_fd)
        os.fsync(data_fd)
    finally:
        try:
            os.unlink(name, dir_fd=data_fd)
        except FileNotFoundError:
            pass


def read_scan_code():
    from django.conf import settings
    fd = None
    try:
        fd = open_root(settings.BIRDS_DATA_ROOT)
        with open_regular_beneath(fd, "scan-status.json") as stream:
            value = json.loads(stream.read(4096))
            code = value.get("code", "") if isinstance(value, dict) else ""
        return code if code in SCAN_MESSAGES else ""
    except (OSError, UnsafePath, ValueError, TypeError):
        return ""
    finally:
        if fd is not None:
            os.close(fd)


def read_scan_status():
    return SCAN_MESSAGES.get(read_scan_code(), "")


def write_worker_heartbeat(data_fd):
    from django.utils import timezone
    name=f'.heartbeat-{uuid.uuid4().hex}'
    fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=data_fd)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(json.dumps({'updated_at':timezone.now().isoformat()}).encode())
            stream.flush();os.fsync(stream.fileno())
        os.replace(name,'worker-heartbeat.json',src_dir_fd=data_fd,dst_dir_fd=data_fd)
        os.fsync(data_fd)
    finally:
        try:os.unlink(name,dir_fd=data_fd)
        except FileNotFoundError:pass


def read_worker_heartbeat():
    from django.conf import settings
    from django.utils.dateparse import parse_datetime
    from django.utils import timezone
    fd=None
    try:
        fd=open_root(settings.BIRDS_DATA_ROOT)
        with open_regular_beneath(fd,'worker-heartbeat.json') as stream:
            raw=json.loads(stream.read(4096))
            value=parse_datetime(raw['updated_at'])
        if value is not None and timezone.is_aware(value):
            return value
    except (OSError,UnsafePath,ValueError,TypeError,KeyError):
        pass
    finally:
        if fd is not None:os.close(fd)
    return None


def ensure_directory_beneath(root_fd, relative_path):
    current = os.dup(root_fd)
    try:
        for part in parts_for(relative_path):
            try:
                os.mkdir(part, mode=0o700, dir_fd=current)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise
