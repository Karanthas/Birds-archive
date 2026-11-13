"""Standard-library-only process boundary. No Django, DB, image or model imports.

The supervisor is the budget-preserving ancestor. Only role children lower their
hard address-space limit. Channels carry bounded JSON, never executable commands.
The probe is constructor-injected by tests; the management command cannot enable it.
"""
from dataclasses import asdict, dataclass
import array
import fcntl
import json
import os
from pathlib import Path
import queue
import resource
import select
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 16384


class ProcessBoundaryError(Exception):
    """Only fixed codes cross the process boundary."""


@dataclass(frozen=True)
class InferenceLimits:
    memory_mb: int
    timeout_seconds: int
    intra_threads: int = 2
    inter_threads: int = 1
    nice: int = 10

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in asdict(self).values()) or self.nice > 19:
            raise ProcessBoundaryError('invalid_resource_budget')


def address_space(memory_mb, *, preserve_hard=False):
    target = memory_mb * 1024 * 1024
    if not sys.platform.startswith('linux'):
        return {'memory_control': 'unsupported_unverified', 'memory_requested_bytes': target,
                'soft_bytes': None, 'hard_bytes': None}
    _, hard = resource.getrlimit(resource.RLIMIT_AS)
    if hard != resource.RLIM_INFINITY and hard < target:
        raise ProcessBoundaryError('inherited_hard_limit_insufficient')
    resource.setrlimit(resource.RLIMIT_AS, (target, hard if preserve_hard else target))
    soft, actual_hard = resource.getrlimit(resource.RLIMIT_AS)
    return {'memory_control': 'enforced', 'memory_requested_bytes': target,
            'soft_bytes': soft, 'hard_bytes': actual_hard}


def frame(message):
    try:
        raw = json.dumps(message, allow_nan=False, separators=(',', ':')).encode('utf-8') + b'\n'
    except (TypeError, ValueError):
        raise ProcessBoundaryError('protocol_invalid') from None
    if len(raw) > MAX_FRAME_BYTES:
        raise ProcessBoundaryError('protocol_oversized')
    return raw


class FrameReader:
    def __init__(self, *, allow_buffered=False):
        self.buffer = bytearray()
        self.received_fd = None
        self.allow_buffered = allow_buffered

    def take_fd(self):
        fd, self.received_fd = self.received_fd, None
        return fd

    def read(self, channel):
        capacity = MAX_FRAME_BYTES * (2 if self.allow_buffered else 1)
        if b'\n' not in self.buffer:
            try:
                data, ancillary, flags, _ = channel.recvmsg(capacity + 1 - len(self.buffer),
                    socket.CMSG_SPACE(array.array('i').itemsize * 2))
                for level, kind, raw in ancillary:
                    if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                        fds = array.array('i')
                        fds.frombytes(raw[:len(raw)-len(raw) % fds.itemsize])
                        for fd in fds:
                            os.set_inheritable(fd, False)
                            if self.received_fd is not None:
                                os.close(fd)
                                os.close(self.take_fd())
                                raise ProcessBoundaryError('protocol_invalid')
                            self.received_fd = fd
                if flags & socket.MSG_CTRUNC:
                    if self.received_fd is not None:
                        os.close(self.take_fd())
                    raise ProcessBoundaryError('protocol_invalid')
            except BlockingIOError:
                return None
            if not data:
                raise ProcessBoundaryError('protocol_truncated' if self.buffer else 'child_closed')
            self.buffer.extend(data)
        if len(self.buffer) > capacity:
            raise ProcessBoundaryError('protocol_oversized')
        if b'\n' not in self.buffer:
            return None
        raw, _, remainder = self.buffer.partition(b'\n')
        if len(raw) + 1 > MAX_FRAME_BYTES:
            raise ProcessBoundaryError('protocol_oversized')
        # Strict channels keep one frame per read. The FD-free relay receive side
        # may retain one bounded coalesced remainder from the same socket read.
        if remainder and not self.allow_buffered:
            raise ProcessBoundaryError('protocol_invalid')
        self.buffer[:] = remainder
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise ProcessBoundaryError('protocol_invalid') from None
        if not isinstance(result, dict):
            raise ProcessBoundaryError('protocol_invalid')
        return result


def send_frame(channel, message, input_fd=None):
    raw = frame(message)
    ancillary = [] if input_fd is None else [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [input_fd]))]
    try:
        if channel.sendmsg([raw], ancillary) != len(raw):
            raise ProcessBoundaryError('channel_backpressure')
    except (BlockingIOError, BrokenPipeError):
        raise ProcessBoundaryError('channel_backpressure') from None


def terminate_owned(process, grace=1):
    """Popen owns/reaps the child; never signal an unchecked persisted numeric PID."""
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            process.kill()
    process.wait(timeout=3)


def valid_session_id(value):
    return (isinstance(value, str) and len(value) == 32
            and all(character in '0123456789abcdef' for character in value))


class InferenceChannel:
    def __init__(self, *, python, lock_path, limits, probe=False, test_provider=None):
        self.python, self.lock_path, self.limits, self.probe = python, Path(lock_path), limits, probe
        self.process = self.socket = None
        self.reader = FrameReader()
        self.nonce = uuid.uuid4().hex
        self.pending = None
        self.deadline = None
        self.ready = None
        self.test_provider = test_provider

    def start(self):
        if self.process is not None:
            raise ProcessBoundaryError('child_already_started')
        if sys.platform.startswith('linux'):
            _, hard = resource.getrlimit(resource.RLIMIT_AS)
            if hard != resource.RLIM_INFINITY and hard < self.limits.memory_mb * 1024 * 1024:
                raise ProcessBoundaryError('inherited_hard_limit_insufficient')
        lock_fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        parent = child = None
        try:
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise ProcessBoundaryError('model_lock_invalid')
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ProcessBoundaryError('model_busy') from None
            parent, child = socket.socketpair()
            # No app paths, credentials, writable directory FDs or fileworker lock.
            env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'SYSTEMROOT') if key in os.environ}
            env.update(OMP_NUM_THREADS=str(self.limits.intra_threads), MKL_NUM_THREADS=str(self.limits.intra_threads),
                       OPENBLAS_NUM_THREADS=str(self.limits.intra_threads), PYTHONNOUSERSITE='1')
            self.process = subprocess.Popen([self.python, '-m', 'archive.services.recognition_process',
                '--child', str(child.fileno()), str(lock_fd), self.nonce, json.dumps(asdict(self.limits)),
                json.dumps(self.test_provider) if self.test_provider is not None else ('probe' if self.probe else 'model')], cwd=Path(__file__).resolve().parents[2], env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, pass_fds=(child.fileno(), lock_fd), start_new_session=True)
            self.socket = parent
            parent.setblocking(False)
        except BaseException:
            if parent is not None:
                parent.close()
            raise
        finally:
            if child is not None:
                child.close()
            os.close(lock_fd)
        return self

    def poll(self):
        if self.socket is None:
            raise ProcessBoundaryError('child_not_started')
        if self.pending is not None and time.monotonic() >= self.deadline:
            answer = dict(type='error', error_code='model_timeout', protocol_version=PROTOCOL_VERSION,
                          nonce=self.nonce, pid=self.process.pid, **self.pending)
            self.close()
            self.pending = None
            return answer
        message = self.reader.read(self.socket)
        if message is None:
            return None
        unexpected_fd = self.reader.take_fd()
        if unexpected_fd is not None:
            os.close(unexpected_fd)
            raise ProcessBoundaryError('protocol_invalid')
        if (type(message.get('protocol_version')) is not int or message.get('protocol_version') != PROTOCOL_VERSION
                or message.get('nonce') != self.nonce or type(message.get('pid')) is not int
                or message.get('pid') != self.process.pid):
            raise ProcessBoundaryError('protocol_identity_mismatch')
        if message.get('type') == 'ready' and self.ready is None and self.pending is None:
            self.ready = message
        elif message.get('type') in ('result', 'error') and self.pending is not None:
            if (type(message.get('generation')) is not int
                    or any(message.get(key) != value for key, value in self.pending.items())):
                raise ProcessBoundaryError('protocol_identity_mismatch')
            self.pending = None
        else:
            raise ProcessBoundaryError('protocol_unexpected')
        return message

    def wait_ready(self, timeout=5):
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                result = self.poll()
                if result:
                    return result
                select.select([self.socket], [], [], .02)
        except BaseException:
            self.close()
            raise
        self.close()
        raise ProcessBoundaryError('child_start_timeout')

    def submit(self, *, run_id, generation, artifact_digest, input_fd=None, **payload):
        if self.ready is None or self.pending is not None:
            raise ProcessBoundaryError('channel_busy')
        identity = {'run_id': str(run_id), 'generation': generation, 'artifact_digest': artifact_digest}
        try:
            valid_run = str(uuid.UUID(str(run_id))) == str(run_id)
        except (ValueError, TypeError, AttributeError):
            valid_run = False
        if (not valid_run or type(generation) is not int or generation < 1
                or not isinstance(artifact_digest, str) or len(artifact_digest) != 64
                or any(c not in '0123456789abcdef' for c in artifact_digest)):
            raise ProcessBoundaryError('request_invalid')
        allowed = {'probe_seconds', 'probe_allocate_mb'} if self.probe else {'input_sha256', 'input_size', 'artifact'}
        if set(payload) - allowed:
            raise ProcessBoundaryError('request_invalid')
        message = dict(type='infer', protocol_version=PROTOCOL_VERSION, nonce=self.nonce, **identity, **payload)
        # A local small frame is atomic for this idle channel in normal operation;
        # partial/non-writable sends close the channel rather than block the worker.
        send_frame(self.socket, message, input_fd)
        identity.update({key: payload[key] for key in ('input_sha256', 'input_size') if key in payload})
        self.pending = identity
        self.deadline = time.monotonic() + self.limits.timeout_seconds

    def close(self):
        if self.reader.received_fd is not None:
            os.close(self.reader.take_fd())
        if self.socket is not None:
            try:
                self.socket.send(frame(dict(type='stop', protocol_version=PROTOCOL_VERSION, nonce=self.nonce)))
            except OSError:
                pass
            self.socket.close()
            self.socket = None
            if self.process is not None:
                try:
                    self.process.wait(timeout=.3)
                except subprocess.TimeoutExpired:
                    pass
        terminate_owned(self.process)

    def drain(self, timeout=5):
        """Request a graceful owned-child stop without falling back to signals."""
        if self.pending is not None:
            raise ProcessBoundaryError('model_request_pending')
        if self.process is None:
            return {'started': False, 'returncode': None}
        if self.process.poll() is not None:
            raise ProcessBoundaryError('model_unexpected_exit')
        if self.socket is None:
            raise ProcessBoundaryError('child_not_started')
        try:
            self.socket.send(frame(dict(type='stop', protocol_version=PROTOCOL_VERSION, nonce=self.nonce)))
        except OSError:
            raise ProcessBoundaryError('model_drain_failed') from None
        self.socket.close()
        self.socket = None
        try:
            returncode = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ProcessBoundaryError('model_drain_timeout') from None
        if returncode != 0:
            raise ProcessBoundaryError('model_unexpected_exit')
        return {'started': True, 'returncode': returncode}


class RelayClient:
    """Fileworker endpoint; never creates a model child or changes its own budget."""
    def __init__(self, fd):
        self.socket = socket.socket(fileno=os.dup(fd))
        self.socket.setblocking(False)
        # Supervisor-to-worker frames never carry FDs. Keep at most the global
        # frame bound, but permit a reply/drain pair coalesced by SOCK_STREAM.
        self.reader = FrameReader(allow_buffered=True)
        self.pending = None
        self.gone = False
        self.ready = False
        self.drain_session_id = None
        self.drained_sent = False

    def submit(self, *, input_fd, **identity):
        if self.pending is not None:
            raise ProcessBoundaryError('channel_busy')
        send_frame(self.socket, dict(type='infer', **identity), input_fd)
        self.pending = {key: identity[key] for key in ('run_id', 'generation',
            'artifact_digest', 'input_sha256', 'input_size')}

    def poll(self):
        try:
            message = self.reader.read(self.socket)
            unexpected_fd = self.reader.take_fd()
            if unexpected_fd is not None:
                os.close(unexpected_fd)
                raise ProcessBoundaryError('protocol_invalid')
            if message is not None:
                if message.get('type') == 'started' and self.pending is None and not self.ready:
                    self.ready = True
                    return None
                if (message.get('type') == 'drain' and set(message) == {'type', 'session_id'}
                        and valid_session_id(message.get('session_id')) and not self.drained_sent
                        and self.drain_session_id in (None, message['session_id'])):
                    self.drain_session_id = message['session_id']
                    return None
                if (self.pending is None or message.get('type') not in ('result', 'error')
                        or any(message.get(key) != value for key, value in self.pending.items())):
                    raise ProcessBoundaryError('protocol_identity_mismatch')
                self.pending = None
            return message
        except (ProcessBoundaryError, OSError):
            self.gone = True
            raise

    def acknowledge_drained(self, session_id):
        if (session_id != self.drain_session_id or self.pending is not None or self.drained_sent
                or not valid_session_id(session_id)):
            raise ProcessBoundaryError('drain_state_invalid')
        send_frame(self.socket, {'type': 'drained', 'session_id': session_id, 'pid': os.getpid()})
        self.drained_sent = True

    def close(self):
        if self.reader.received_fd is not None:
            os.close(self.reader.take_fd())
        self.socket.close()


class Supervisor:
    """Light control role: no ORM, image decode or heavy optional imports."""
    def __init__(self, *, worker_args, worker_env, worker_memory_mb, inference=None):
        self.worker_args, self.worker_env = worker_args, worker_env
        self.worker_memory_mb, self.inference = worker_memory_mb, inference
        self.worker = self.control = None
        self.reader = FrameReader()
        self.relay_pending = None
        self.drain_session_id = None
        self.drain_ack = None

    def start(self):
        # Validate the larger inherited budget before lowering only our soft value.
        if self.inference and sys.platform.startswith('linux'):
            _, hard = resource.getrlimit(resource.RLIMIT_AS)
            if hard != resource.RLIM_INFINITY and hard < self.inference.limits.memory_mb * 1024 * 1024:
                raise ProcessBoundaryError('inherited_hard_limit_insufficient')
        address_space(self.worker_memory_mb, preserve_hard=True)
        parent, child = socket.socketpair()
        try:
            self.worker = subprocess.Popen([*self.worker_args, '--supervisor-fd', str(child.fileno())],
                env=self.worker_env, close_fds=True, pass_fds=(child.fileno(),),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            self.control = parent
            parent.setblocking(False)
        except BaseException:
            parent.close()
            raise

        finally:
            child.close()
        reader, deadline = FrameReader(), time.monotonic() + 8
        try:
            while time.monotonic() < deadline:
                message = reader.read(parent)
                if message:
                    if message.get('type') != 'ready' or message.get('pid') != self.worker.pid:
                        raise ProcessBoundaryError('worker_not_ready')
                    if self.inference:
                        self.inference.start().wait_ready()
                    # Do not let ready and the first inference request share a frame read.
                    send_frame(parent, {'type': 'started'})
                    return dict(type='ready', worker_pid=self.worker.pid,
                                model_status='probe_control_only' if self.inference and self.inference.probe else 'model_not_configured',
                                worker_resources=message['resources'])
                if self.worker.poll() is not None:
                    raise ProcessBoundaryError('worker_not_ready')
                select.select([parent], [], [], .02)
            raise ProcessBoundaryError('worker_start_timeout')
        except BaseException:
            self.close()
            raise

    def tick(self):
        """Relay bounded requests/FDs; only this budget-preserving ancestor restarts a model."""
        request = self.reader.read(self.control)
        if request is not None:
            input_fd = self.reader.take_fd()
            try:
                if request.get('type') == 'drained':
                    if (input_fd is not None or set(request) != {'type', 'session_id', 'pid'}
                            or request.get('session_id') != self.drain_session_id
                            or request.get('pid') != self.worker.pid or self.relay_pending is not None):
                        raise ProcessBoundaryError('drain_ack_invalid')
                    self.drain_ack = request
                    return
                if (request.get('type') != 'infer' or self.relay_pending is not None
                        or set(request) != {'type', 'run_id', 'generation', 'artifact_digest',
                            'input_sha256', 'input_size', 'artifact'} or input_fd is None):
                    raise ProcessBoundaryError('request_invalid')
                self.relay_pending = {key: request[key] for key in ('run_id', 'generation',
                    'artifact_digest', 'input_sha256', 'input_size')}
                if self.inference is None:
                    send_frame(self.control, dict(type='error', error_code='model_not_configured', **self.relay_pending))
                    self.relay_pending = None
                else:
                    if self.inference.socket is None or self.inference.process.poll() is not None:
                        if self.drain_session_id is not None:
                            raise ProcessBoundaryError('model_unexpected_exit')
                        old = self.inference
                        old.close()
                        self.inference = InferenceChannel(python=old.python, lock_path=old.lock_path,
                            limits=old.limits, probe=old.probe, test_provider=old.test_provider)
                        self.inference.start().wait_ready()
                    self.inference.submit(input_fd=input_fd, **{key: value for key, value in request.items() if key != 'type'})
            finally:
                if input_fd is not None:
                    os.close(input_fd)
        if self.inference is not None and self.relay_pending is not None:
            try:
                result = self.inference.poll()
                if result is not None:
                    send_frame(self.control, {key: value for key, value in result.items()
                        if key not in ('protocol_version', 'nonce', 'pid')})
                    self.relay_pending = None
            except (ProcessBoundaryError, OSError) as error:
                self.inference.close()
                # A killed process may be OOM; do not automatically repeat it as a temporary exit.
                killed = self.inference.process.returncode is not None and self.inference.process.returncode < 0
                code = ('model_memory_exceeded' if killed else 'child_exited') if str(error) == 'child_closed' else 'protocol_invalid'
                send_frame(self.control, dict(type='error', error_code=code, **self.relay_pending))
                self.relay_pending = None

    def request_drain(self, session_id):
        if (not valid_session_id(session_id) or self.drain_session_id is not None
                or self.worker is None or self.worker.poll() is not None):
            raise ProcessBoundaryError('drain_state_invalid')
        send_frame(self.control, {'type': 'drain', 'session_id': session_id})
        self.drain_session_id = session_id

    def drain(self, session_id, timeout=30):
        """Drain accepted work, then prove both owned children reached terminal state."""
        self.request_drain(session_id)
        deadline = time.monotonic() + timeout
        while self.drain_ack is None and time.monotonic() < deadline:
            # Read a buffered ACK before classifying the worker's immediate clean
            # exit; the ACK and EOF may become observable in either order.
            self.tick()
            if self.drain_ack is None and self.worker.poll() is not None:
                raise ProcessBoundaryError('worker_unexpected_exit')
            if self.drain_ack is None:
                select.select([self.control], [], [], min(.05, max(0, deadline-time.monotonic())))
        if self.drain_ack is None:
            raise ProcessBoundaryError('supervisor_drain_timeout')
        remaining = max(0, deadline-time.monotonic())
        try:
            worker_exit = self.worker.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise ProcessBoundaryError('worker_drain_timeout') from None
        if worker_exit != 0:
            raise ProcessBoundaryError('worker_unexpected_exit')
        model = ({'started': False, 'returncode': None} if self.inference is None
                 else self.inference.drain(timeout=max(0.01, deadline-time.monotonic())))
        if self.control is not None:
            self.control.close()
            self.control = None
        return {'session_id': session_id, 'worker_pid': self.drain_ack['pid'],
                'worker_exit': worker_exit, 'model': model, 'forced': False}

    def close(self):
        if self.reader.received_fd is not None:
            os.close(self.reader.take_fd())
        if self.inference is not None:
            self.inference.close()
        if self.control is not None:
            self.control.close()
            self.control = None
        terminate_owned(self.worker, grace=3)


def _child(channel_fd, lock_fd, nonce, limits, probe, test_provider=None):
    # All imports above are stdlib. Set independent hard limit before any adapter.
    control = socket.socket(fileno=channel_fd)
    control.set_inheritable(False)
    os.set_inheritable(lock_fd, False)
    status = address_space(limits.memory_mb)
    current_nice = os.getpriority(os.PRIO_PROCESS, 0)
    priority_control = 'observed'
    if current_nice < limits.nice:
        try:
            os.nice(limits.nice - current_nice)
            priority_control = 'enforced'
        except OSError:
            if sys.platform.startswith('linux'):
                raise
            priority_control = 'permission_denied_unverified'
    identity = dict(protocol_version=PROTOCOL_VERSION, nonce=nonce, pid=os.getpid())
    requests = queue.Queue(maxsize=1)

    def watch_parent():
        reader = FrameReader()
        try:
            while True:
                value = reader.read(control)
                if value is not None:
                    if value.get('nonce') != nonce or value.get('type') != 'infer' or value.get('protocol_version') != PROTOCOL_VERSION:
                        break
                    requests.put_nowait((value, reader.take_fd()))
        except (ProcessBoundaryError, OSError, queue.Full):
            pass
        # Works during a long computation or a blocked result send too.
        os._exit(0)

    threading.Thread(target=watch_parent, daemon=True).start()
    control.sendall(frame(dict(type='ready', resources=status, intra_threads=limits.intra_threads,
                              inter_threads=limits.inter_threads, priority_control=priority_control,
                              optional_imports_present=any(name in sys.modules for name in ('django', 'PIL', 'torch')),
                              inherited_fds=[fd for fd in range(3, 256) if _fd_open(fd)],
                              nice_observed=os.getpriority(os.PRIO_PROCESS, 0), **identity)))
    while True:
        request, input_fd = requests.get()
        answer = {key: request[key] for key in ('run_id', 'generation', 'artifact_digest',
            'input_sha256', 'input_size') if key in request}
        code = 'model_not_configured'
        result = {}
        if probe:
            delay, allocation = request.get('probe_seconds', 0), request.get('probe_allocate_mb', 0)
            if (type(delay) not in (int, float) or not 0 <= delay <= 300
                    or type(allocation) is not int or not 0 <= allocation <= 1024):
                code = 'request_invalid'
            else:
                try:
                    data = bytearray(allocation * 1024 * 1024)
                    time.sleep(delay)
                    del data
                    code = ''
                except MemoryError:
                    code = 'model_memory_exceeded'
        elif input_fd is not None:
            from archive.services.local_model import infer_readonly
            try:
                result = infer_readonly(input_fd, input_sha256=request.get('input_sha256'),
                    input_size=request.get('input_size'), artifact=request.get('artifact'), test_provider=test_provider)
                code = result.pop('error_code', '')
            finally:
                os.close(input_fd)
        control.sendall(frame(dict(type='error' if code else 'result', error_code=code,
                                  evidence='process_control_probe' if probe else 'model_input_checked',
                                  **result, **answer, **identity)))


def _fd_open(fd):
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


if __name__ == '__main__':
    if len(sys.argv) != 7 or sys.argv[1] != '--child':
        raise SystemExit(2)
    try:
        _child(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4],
               InferenceLimits(**json.loads(sys.argv[5])), sys.argv[6] == 'probe',
               json.loads(sys.argv[6]) if sys.argv[6].startswith('{') else None)
    except (ProcessBoundaryError, OSError, ValueError):
        raise SystemExit(3)
