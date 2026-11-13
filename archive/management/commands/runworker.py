import fcntl
import os
import signal
import time
import json
import select
from archive.management.commands.checkruntime import resource_status
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError
from archive.services.intake import scan_once, valid_handoff
from archive.services.jobs import process_source, recover_running, claim_next, database_retry
from archive.services.archiving import process_archive
from archive.services.management import process_management
from archive.models import ManagementOperation
from archive.services.paths import (open_root, UnsafePath, verify_root_identity, write_worker_heartbeat,
                                   validate_root_isolation, RootsNotIsolated)
from archive.services.maintenance import worker_should_stop

class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--supervisor-fd", type=int, help=__import__('argparse').SUPPRESS)

    def handle(self, *args, **options):
        inbox_fd = data_fd = lock_fd = None
        stop = False
        previous_handlers = {}
        supervisor_fd = options.get('supervisor_fd')
        relay = dispatcher = None

        def parent_gone():
            if supervisor_fd is None:
                return False
            if relay is not None:
                # Readability may be an inference reply, not EOF. tick consumes it.
                return relay.gone
            return bool(select.select([supervisor_fd], [], [], 0)[0])

        def request_stop(*_):
            nonlocal stop
            stop = True

        try:
            data_fd = open_root(settings.BIRDS_DATA_ROOT)
            try:
                inbox_fd = open_root(settings.BIRDS_INBOX_ROOT)
            except OSError:
                pass
            if inbox_fd is not None:
                validate_root_isolation(inbox_fd, data_fd, settings.BIRDS_INBOX_ROOT, settings.BIRDS_DATA_ROOT)
            lock_fd = os.open("worker.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                              0o600, dir_fd=data_fd)
            import stat
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise UnsafePath("锁须为普通文件")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CommandError("worker_busy：后台已在运行") from None
            try:
                resources = resource_status(apply=True)
                self.stdout.write(json.dumps(resources,ensure_ascii=False))
            except (OSError,ValueError):
                raise CommandError('worker_resource_unavailable：资源限制未能落实，请检查运行环境。') from None
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[signum] = signal.signal(signum, request_stop)
            if supervisor_fd is not None:
                os.set_inheritable(supervisor_fd, False)
                os.write(supervisor_fd, (json.dumps(dict(type='ready', pid=os.getpid(), resources=resources))+'\n').encode())
                from archive.services.recognition_process import RelayClient
                from archive.services.recognition import RecognitionDispatcher
                relay = RelayClient(supervisor_fd)
                dispatcher = RecognitionDispatcher(relay, inbox_fd, data_fd, recover_on_ready=True)
            # A maintenance request may race the supervisor's startup handshake.
            # Recovery is a new database claim and must not start after that request.
            if not worker_should_stop():
                database_retry(lambda: recover_running(data_fd))
            next_scan=0
            management_turn = True
            while not stop:
                if parent_gone():
                    break
                draining = worker_should_stop()
                if draining:
                    if dispatcher is None:
                        stop = True
                        continue
                    dispatcher.inbox_fd = inbox_fd
                    dispatcher.tick(allow_claim=False)
                    if (relay.drain_session_id is not None and dispatcher.drained
                            and relay.pending is None):
                        relay.acknowledge_drained(relay.drain_session_id)
                        stop = True
                    else:
                        time.sleep(.02)
                    continue
                try:
                    write_worker_heartbeat(data_fd)
                except OSError:
                    self.stderr.write('heartbeat_unavailable：后台更新时间暂不可读')
                if inbox_fd is None:
                    try:
                        inbox_fd = open_root(settings.BIRDS_INBOX_ROOT)
                    except OSError:
                        pass
                if time.monotonic() >= next_scan and not worker_should_stop():
                    database_retry(lambda: scan_once(inbox_fd, data_fd))
                    next_scan=time.monotonic()+settings.BIRDS_SCAN_SECONDS
                usable = False
                if inbox_fd is not None and valid_handoff():
                    try:
                        verify_root_identity(inbox_fd, settings.BIRDS_INBOX_ROOT, data_fd)
                        usable = True
                    except (OSError, UnsafePath, ValueError):
                        pass
                can_claim = not stop and not parent_gone() and not worker_should_stop()
                management = ManagementOperation.objects.filter(status__in=['pending', 'running']).first() if can_claim else None
                job = None
                if management is None or not management_turn:
                    job = database_retry(lambda: claim_next(import_usable=usable)) if can_claim else None
                if management and job is None:
                    process_management(management.pk)
                    management_turn = False
                elif job:
                    management_turn = True
                if job:
                    if job.kind == 'archive':
                        process_archive(job)
                    elif job.kind == 'import':
                        process_source(job, inbox_fd, data_fd, claimed=True)
                    else:
                        raise CommandError('worker_unknown_job：任务类型不可用')
                if dispatcher is not None:
                    dispatcher.inbox_fd = inbox_fd
                    dispatcher.tick(allow_claim=not worker_should_stop())
                if options["once"]:
                    break
                delay = settings.WORKER_JOB_SLEEP_MS/1000 if job else settings.BIRDS_SCAN_SECONDS
                if delay:
                    deadline=time.monotonic()+delay
                    while not stop and not parent_gone() and time.monotonic()<deadline:
                        if dispatcher is not None:
                            dispatcher.tick(allow_claim=not worker_should_stop())
                        if worker_should_stop():
                            break
                        time.sleep(min(.1,max(0,deadline-time.monotonic())))
        except RootsNotIsolated:
            raise CommandError('roots_not_isolated：导入目录与应用数据目录必须互相独立，请检查配置。') from None
        except OperationalError:
            raise CommandError('worker_database_busy：数据库持续忙，请检查其他数据库写入者后重试。') from None
        except (OSError, UnsafePath):
            raise CommandError("worker_root_unavailable：请检查已配置的根目录与权限") from None
        finally:
            if dispatcher is not None:
                dispatcher.close_input()
            if relay is not None:
                relay.close()
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            # 不删除锁 inode；关闭 FD 才释放生命周期锁。
            for fd in (lock_fd, data_fd, inbox_fd, supervisor_fd):
                if fd is not None:
                    os.close(fd)
