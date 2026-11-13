"""持久维护屏障。宿主管理员绕过受控入口后，备份证据必须作废。"""
import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys
import time

from django.conf import settings


STATE_NAME = "maintenance-v1.json"
STATE_LOCK_NAME = "maintenance-state.lock"
LOCK_NAME = "maintenance.lock"
SHARED_NAME = "maintenance-shared.lock"
WORKER_LOCK_NAME = "worker.lock"
ACTIVE_STATES = frozenset({"requested", "acquired", "active", "offline_running", "failed"})
OFFLINE_COMMANDS = frozenset({"importcatalog", "loadguidebundle", "preparemodel", "migrate"})


class MaintenanceError(RuntimeError):
    pass


def _root():
    root = Path(settings.BIRDS_DATA_ROOT)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise MaintenanceError("maintenance_data_root_unavailable")
    return root


def _open_lock(root, name):
    fd = os.open(root / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise MaintenanceError("maintenance_lock_invalid")
    return fd


def _flock(fd, operation, deadline, code):
    while True:
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise MaintenanceError(code) from None
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))


def _atomic(path, value):
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _read_state(root):
    try:
        fd = os.open(root / STATE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return {"status": "idle"}
    except OSError:
        raise MaintenanceError("maintenance_state_invalid") from None
    try:
        with os.fdopen(fd, encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise MaintenanceError("maintenance_state_invalid")
            value = json.load(stream)
    except (ValueError, OSError):
        raise MaintenanceError("maintenance_state_invalid") from None
    if (not isinstance(value, dict) or value.get("schema") != 1
            or not isinstance(value.get("status"), str)
            or value["status"] not in ACTIVE_STATES | {"resumed"}):
        raise MaintenanceError("maintenance_state_invalid")
    session_id = value.get("session_id")
    if not isinstance(session_id, str) or len(session_id) != 32 or any(
        digit not in "0123456789abcdef" for digit in session_id
    ):
        raise MaintenanceError("maintenance_state_invalid")
    return value


@contextmanager
def _state_mutex(root, timeout=5):
    fd = _open_lock(root, STATE_LOCK_NAME)
    try:
        _flock(fd, fcntl.LOCK_EX, time.monotonic() + timeout, "maintenance_state_busy")
        yield
    finally:
        os.close(fd)


def _mutate_state(session_id, transform):
    """按session比较后更新，供后续监督者ACK在不覆盖其他字段时复用。"""
    root = _root()
    with _state_mutex(root):
        current = _read_state(root)
        if current.get("session_id") != session_id:
            raise MaintenanceError("maintenance_session_invalid")
        updated = transform(dict(current))
        if updated.get("session_id") != session_id or updated.get("schema") != 1:
            raise MaintenanceError("maintenance_state_invalid")
        _atomic(root / STATE_NAME, updated)
        return updated


def status():
    root = _root()
    with _state_mutex(root):
        return _read_state(root)


def request(owner=None):
    root = _root()
    with _state_mutex(root):
        current = _read_state(root)
        if current.get("status") not in ("idle", "resumed"):
            raise MaintenanceError("maintenance_already_active")
        value = {
            "schema": 1,
            "session_id": secrets.token_hex(16),
            "owner": owner or str(os.getpid()),
            "status": "requested",
            "requested_at": time.time(),
            "protected_action": None,
            "supervisor_ack": None,
        }
        _atomic(root / STATE_NAME, value)
        return value


def _require_terminal_action(current):
    action = current.get("protected_action")
    if action is None:
        return
    if not isinstance(action, dict):
        raise MaintenanceError("maintenance_action_invalid")
    terminal = action.get("status") in ("completed", "failed")
    if not terminal or action.get("process_group_gone") is not True:
        raise MaintenanceError("maintenance_action_unclosed")


def resume(session_id, owner=None, *, timeout=30):
    root = _root()
    lifecycle = _open_lock(root, LOCK_NAME)
    try:
        _flock(lifecycle, fcntl.LOCK_EX, time.monotonic() + timeout, "maintenance_busy")

        def update(current):
            if current.get("status") not in ACTIVE_STATES:
                raise MaintenanceError("maintenance_not_active")
            _require_terminal_action(current)
            current.update(status="resumed", resumed_at=time.time(), resumed_by=owner or str(os.getpid()))
            return current
        return _mutate_state(session_id, update)
    finally:
        os.close(lifecycle)


@contextmanager
def acquire(session_id, owner=None, *, timeout=30, acquire_worker=True, require_supervisor=False):
    """独占HTTP屏障与生命周期锁；成功后仍保持维护，直到显式resume。"""
    root = _root()
    deadline = time.monotonic() + timeout
    descriptors = []
    try:
        for name, code in ((LOCK_NAME, "maintenance_busy"), (SHARED_NAME, "maintenance_requests_busy")):
            fd = _open_lock(root, name)
            descriptors.append(fd)
            _flock(fd, fcntl.LOCK_EX, deadline, code)
        if acquire_worker:
            worker_fd = _open_lock(root, WORKER_LOCK_NAME)
            descriptors.append(worker_fd)
            _flock(worker_fd, fcntl.LOCK_EX, deadline, "maintenance_worker_busy")

        def started(current):
            if current.get("status") not in ACTIVE_STATES:
                raise MaintenanceError("maintenance_session_invalid")
            _require_terminal_action(current)
            ack = current.get("supervisor_ack")
            if require_supervisor and not (
                isinstance(ack, dict) and ack.get("session_id") == session_id and ack.get("status") == "drained"
            ):
                raise MaintenanceError("maintenance_supervisor_not_drained")
            current.update(status="acquired", acquired_at=time.time(), acquired_by=owner or str(os.getpid()))
            return current

        current = _mutate_state(session_id, started)
        try:
            yield current
        except BaseException as error:
            def failed(latest):
                reason = error.args[0] if isinstance(error, MaintenanceError) and error.args else type(error).__name__
                latest.update(status="failed", failed_at=time.time(), failure=reason)
                return latest
            _mutate_state(session_id, failed)
            raise
        else:
            def active(latest):
                latest.update(status="active", action_finished_at=time.time())
                return latest
            _mutate_state(session_id, active)
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


@contextmanager
def request_barrier():
    """共享锁覆盖完整HTTP生命周期，并以锁后复读消除请求/维护竞态。"""
    root = _root()
    if status().get("status") in ACTIVE_STATES:
        raise MaintenanceError("maintenance_in_progress")
    fd = _open_lock(root, SHARED_NAME)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        if status().get("status") in ACTIVE_STATES:
            raise MaintenanceError("maintenance_in_progress")
        yield
    finally:
        os.close(fd)


def _command(verb, arguments):
    if verb not in OFFLINE_COMMANDS:
        raise MaintenanceError("offline_command_not_allowed")
    if not isinstance(arguments, (list, tuple)) or not all(isinstance(value, str) for value in arguments):
        raise MaintenanceError("offline_arguments_invalid")
    manage = str(Path(settings.BASE_DIR) / "manage.py")
    if verb == "migrate":
        if arguments:
            raise MaintenanceError("offline_arguments_invalid")
        return [sys.executable, manage, "checkruntime", "--deployment-migrate"]
    return [sys.executable, manage, verb, *arguments]


def _group_gone(process_group):
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _stop_group(process, grace=10):
    if not _group_gone(process.pid):
        os.killpg(process.pid, signal.SIGTERM)
    if process.poll() is None:
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
    deadline = time.monotonic() + grace
    while not _group_gone(process.pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if not _group_gone(process.pid):
        os.killpg(process.pid, signal.SIGKILL)
        if process.poll() is None:
            process.wait(timeout=grace)
    return _group_gone(process.pid)


def _finish_offline(session_id, *, status, returncode, process_group_gone, failure=None):
    def update(current):
        action = dict(current.get("protected_action") or {})
        action.update(
            status=status,
            completed_at=time.time(),
            returncode=returncode,
            process_group_gone=process_group_gone,
        )
        if failure:
            action["failure"] = failure
        current["protected_action"] = action
        return current
    return _mutate_state(session_id, update)


def run_offline(session_id, verb, arguments=(), *, timeout=3600):
    command = _command(verb, arguments)
    # migrate_deployment本身取得worker/recognition锁并原子更新部署登记。
    with acquire(
        session_id,
        owner=str(os.getpid()),
        acquire_worker=verb != "migrate",
        require_supervisor=True,
    ):
        if verb != "migrate":
            # 在屏障和worker锁内核验部署身份，避免验证自身成为未保护的数据库读取者。
            from archive.services.deployment import validate_deployment
            validate_deployment()
        process = subprocess.Popen(command, start_new_session=True)
        def running(current):
            current.update(status="offline_running", protected_action={
                "kind": "offline", "verb": verb, "pid": process.pid, "started_at": time.time(), "status": "running",
            })
            return current
        _mutate_state(session_id, running)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            gone = _stop_group(process)
            _finish_offline(
                session_id,
                status="failed" if gone else "unclosed",
                returncode=process.poll(),
                process_group_gone=gone,
                failure="offline_command_timeout" if gone else "offline_process_unclosed",
            )
            if not gone:
                raise MaintenanceError("offline_process_unclosed") from None
            raise MaintenanceError("offline_command_timeout") from None
        if not _group_gone(process.pid):
            gone = _stop_group(process)
            _finish_offline(
                session_id,
                status="failed" if gone else "unclosed",
                returncode=process.returncode,
                process_group_gone=gone,
                failure="offline_descendant_survived" if gone else "offline_process_unclosed",
            )
            if not gone:
                raise MaintenanceError("offline_process_unclosed")
            raise MaintenanceError("offline_descendant_survived")
        if process.returncode:
            _finish_offline(
                session_id,
                status="failed",
                returncode=process.returncode,
                process_group_gone=True,
                failure="offline_command_failed",
            )
            raise MaintenanceError("offline_command_failed")
        _finish_offline(session_id, status="completed", returncode=0, process_group_gone=True)
        return {"status": "offline_completed", "session_id": session_id, "verb": verb, "returncode": 0}


def worker_should_stop():
    return status().get("status") in ACTIVE_STATES


def main(argv=None):
    import django

    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) < 2:
        raise MaintenanceError("offline_entry_invalid")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
    session_id, verb, *arguments = values
    result = run_offline(session_id, verb, arguments)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MaintenanceError as error:
        print(f"{error}：离线维护命令未完成。", file=sys.stderr)
        raise SystemExit(1) from None
