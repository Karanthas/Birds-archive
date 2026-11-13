"""单一监督入口；缺模型时继续原有手动文件工作。"""
import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from archive.services.recognition_process import (Supervisor, ProcessBoundaryError,
                                                InferenceChannel, InferenceLimits)
from archive.services import maintenance


DRAIN_TIMEOUT_SECONDS = 30


class Command(BaseCommand):
    # This control role does not need ORM/path checks or access to original files.
    # The independently started runworker retains its normal startup checks.
    requires_system_checks = []

    def _new_supervisor(self):
        inference = None
        if settings.BIRDS_RECOGNITION_ENABLED:
            inference = InferenceChannel(python=settings.BIRDS_RECOGNITION_PYTHON,
                lock_path=Path(settings.BIRDS_DATA_ROOT)/'recognition.lock',
                limits=InferenceLimits(settings.RECOGNITION_MEMORY_MB, settings.RECOGNITION_TIMEOUT_SECONDS,
                    settings.RECOGNITION_INTRA_THREADS, settings.RECOGNITION_INTER_THREADS,
                    settings.RECOGNITION_NICE))
        return Supervisor(worker_args=[sys.executable, str(settings.BASE_DIR/'manage.py'), 'runworker',
                            '--settings=' + os.environ.get('DJANGO_SETTINGS_MODULE', 'config.settings')],
                          worker_env=os.environ.copy(), worker_memory_mb=settings.WORKER_MEMORY_MB,
                          inference=inference)

    @staticmethod
    def _record_drain(session_id, instance_id, result):
        def update(current):
            if current.get('status') not in maintenance.ACTIVE_STATES:
                raise maintenance.MaintenanceError('maintenance_not_active')
            current['supervisor_ack'] = {
                'session_id': session_id,
                'status': 'drained',
                'supervisor_instance_id': instance_id,
                'drained_at': time.time(),
                'worker_pid': result['worker_pid'],
                'worker_exit': result['worker_exit'],
                'model_started': result['model']['started'],
                'model_exit': result['model']['returncode'],
                'forced': False,
            }
            return current
        return maintenance._mutate_state(session_id, update)

    @staticmethod
    def _record_failure(session_id, instance_id, failure):
        def update(current):
            if current.get('status') not in maintenance.ACTIVE_STATES:
                raise maintenance.MaintenanceError('maintenance_not_active')
            current['supervisor_ack'] = {
                'session_id': session_id,
                'status': 'drain_failed',
                'supervisor_instance_id': instance_id,
                'failed_at': time.time(),
                'failure': failure,
                'forced': True,
            }
            return current
        return maintenance._mutate_state(session_id, update)

    def handle(self, *args, **options):
        stop = False
        previous = {}

        def request_stop(*_):
            nonlocal stop
            stop = True

        supervisor = None
        instance_id = None
        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous[signum] = signal.signal(signum, request_stop)
            while not stop:
                state = maintenance.status()
                active = state.get('status') in maintenance.ACTIVE_STATES
                if supervisor is None:
                    # Compose may restart this command during maintenance. Stay live
                    # and childless; only the supervisor that proved a drain may ACK.
                    if active:
                        time.sleep(.05)
                        continue
                    supervisor = self._new_supervisor()
                    instance_id = uuid.uuid4().hex
                    ready = supervisor.start()
                    ready['supervisor_instance_id'] = instance_id
                    self.stdout.write(json.dumps(ready), ending='\n')
                    self.stdout.flush()
                    continue
                if active:
                    session_id = state.get('session_id')
                    try:
                        result = supervisor.drain(session_id, timeout=DRAIN_TIMEOUT_SECONDS)
                        self._record_drain(session_id, instance_id, result)
                    except (ProcessBoundaryError, OSError) as error:
                        failure = str(error) if str(error) else type(error).__name__
                        try:
                            self._record_failure(session_id, instance_id, failure)
                        finally:
                            supervisor.close()
                            supervisor = None
                        raise
                    supervisor.close()
                    supervisor = None
                    instance_id = None
                    continue
                if supervisor.worker.poll() is not None:
                    raise ProcessBoundaryError('file_worker_exited')
                supervisor.tick()
                time.sleep(.05)
        except (ProcessBoundaryError, OSError, maintenance.MaintenanceError):
            raise CommandError('recognition_supervisor_unavailable：监督角色退出，请检查运行环境。') from None
        finally:
            if supervisor is not None:
                supervisor.close()
            for signum, handler in previous.items():
                signal.signal(signum, handler)
