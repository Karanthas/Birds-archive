"""Real isolated OS control probes; these are not neural model/NAS evidence."""
import hashlib
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase
from archive.models import Photo
from archive.tests.fixtures import command, command_options, jpeg
from archive.tests.test_archive_recovery import ArchiveFixture
from archive.tests.test_recognition_state import RecognitionFixture

# Test-only injection through a separate Python caller, never a production CLI flag.
PROBE_SUPERVISOR = '''
import json, os, signal, sys, time, uuid
from pathlib import Path
from archive.services.recognition_process import Supervisor, InferenceChannel, InferenceLimits
channel = InferenceChannel(python=sys.executable, lock_path=Path(os.environ['BIRDS_TEST_ROOT'])/'data'/'recognition.lock',
    limits=InferenceLimits(2048, 20), probe=True)
supervisor = Supervisor(worker_args=[sys.executable, 'manage.py', 'runworker', '--settings=config.settings_test'],
    worker_env=os.environ.copy(), worker_memory_mb=1024, inference=channel)
stop = False
def stopping(*_):
    global stop
    stop = True
signal.signal(signal.SIGTERM, stopping)
signal.signal(signal.SIGINT, stopping)
try:
    ready = supervisor.start()
    channel.submit(run_id=str(uuid.uuid4()), generation=1, artifact_digest='a'*64, probe_seconds=15)
    ready.update(model_pid=channel.process.pid, model_resources=channel.ready['resources'])
    print(json.dumps(ready), flush=True)
    while not stop and supervisor.worker.poll() is None:
        time.sleep(.02)
finally:
    supervisor.close()
'''


def process_alive(pid):
    # A zombie has stopped executing and holds no FDs; its init reaping is OS-owned.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if sys.platform.startswith('linux'):
        try:
            if Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] == 'Z':
                return False
        except FileNotFoundError:
            return False
    return True


class RecognitionPipelineTests(RecognitionFixture):
    def setUp(self):
        super().setUp()
        from archive.models import ModelArtifact
        from archive.services.recognition import set_current_model
        self.artifact = ModelArtifact.objects.create(manifest_digest='c'*64, provider='test-only',
            version='1', revision='pipeline', manifest={}, selection_state='selected')
        set_current_model(self.artifact.pk)
        self.output = [{'rank': 1, 'raw_label': 'Synthetic bird', 'score': .5,
            'species_id': None, 'mapping_status': 'unmapped', 'mapping_evidence': ''}]

    def pipeline(self, *, production=False, delay=0):
        from archive.services.recognition import RecognitionDispatcher
        from archive.services.recognition_process import InferenceChannel, InferenceLimits
        from archive.services.paths import open_root
        channel = InferenceChannel(python=sys.executable, lock_path=self.data/'recognition.lock',
            limits=InferenceLimits(512, 5), test_provider=None if production else {
                'delay': delay, 'candidates': self.output})
        channel.start().wait_ready()
        self.addCleanup(channel.close)
        inbox_fd, data_fd = open_root(self.inbox), open_root(self.data)
        self.addCleanup(os.close, inbox_fd)
        self.addCleanup(os.close, data_fd)
        dispatcher = RecognitionDispatcher(channel, inbox_fd, data_fd)
        self.addCleanup(dispatcher.close_input)
        return dispatcher, channel

    def finish(self, dispatcher, run):
        deadline = time.monotonic()+4
        while time.monotonic() < deadline:
            dispatcher.tick()
            run.refresh_from_db()
            if run.state != 'running':
                return
            time.sleep(.01)
        self.fail('真实模型角色结果未及时返回')

    def test_actual_readonly_input_process_publishes_candidates_and_cleans_copy(self):
        from archive.models import RecognitionRun, RecognitionState, PhotoSpecies
        photo, path = self.imported()
        before = path.read_bytes()
        dispatcher, channel = self.pipeline()
        dispatcher.tick()
        run = RecognitionRun.objects.get(photo=photo)
        self.assertEqual(run.state, 'running')
        self.finish(dispatcher, run)
        self.assertEqual(run.state, 'succeeded')
        self.assertEqual(run.candidates.get().raw_label, 'Synthetic bird')
        self.assertEqual(RecognitionState.objects.get(photo=photo).active_run_id, run.pk)
        self.assertFalse(PhotoSpecies.objects.exists())
        self.assertEqual(path.read_bytes(), before)
        self.assertIsNone(dispatcher.input)
        self.assertFalse(list(self.data.glob('.recognition-*')))

    def test_publication_database_busy_keeps_received_reply_for_next_tick(self):
        from django.db import OperationalError
        from archive.models import RecognitionRun
        photo, _ = self.imported()
        dispatcher, channel = self.pipeline()
        dispatcher.tick()
        run = RecognitionRun.objects.get(photo=photo)
        deadline = time.monotonic()+3
        while time.monotonic() < deadline and not select.select([channel.socket], [], [], .02)[0]:
            pass
        with mock.patch('archive.services.recognition.publish_result', side_effect=OperationalError('busy')):
            dispatcher.tick()
        self.assertIsNotNone(dispatcher.reply)
        run.refresh_from_db()
        self.assertEqual(run.state, 'running')
        dispatcher.tick()
        run.refresh_from_db()
        self.assertEqual(run.state, 'succeeded')

    def test_actual_source_replacement_during_inference_is_stale(self):
        from archive.models import RecognitionRun, RecognitionState
        photo, path = self.imported()
        dispatcher, _ = self.pipeline(delay=.1)
        dispatcher.tick()
        run = RecognitionRun.objects.get(photo=photo)
        replacement = self.inbox/'replacement.jpg'
        jpeg(replacement, color='blue')
        replacement.replace(path)
        self.finish(dispatcher, run)
        self.assertEqual(run.state, 'stale')
        self.assertIsNone(RecognitionState.objects.get(photo=photo).active_run_id)
        self.assertFalse(run.candidates.exists())
        self.assertIsNone(dispatcher.input)

    def test_human_confirmation_during_real_process_cannot_publish(self):
        from archive.models import RecognitionRun, PhotoSpecies
        from archive.services.confirmation import confirm_photo
        photo, path = self.imported()
        before = path.read_bytes()
        dispatcher, _ = self.pipeline(delay=.1)
        dispatcher.tick()
        run = RecognitionRun.objects.get(photo=photo)
        confirm_photo(photo.pk, 1, uuid.uuid4(), 'species', [], ['人工测试鸟'])
        human = list(PhotoSpecies.objects.values())
        self.finish(dispatcher, run)
        self.assertEqual(run.state, 'stale')
        self.assertEqual(list(PhotoSpecies.objects.values()), human)
        self.assertEqual(path.read_bytes(), before)

    def test_production_cannot_select_synthetic_provider_from_artifact(self):
        from archive.models import RecognitionRun
        photo, _ = self.imported()
        dispatcher, _ = self.pipeline(production=True)
        dispatcher.tick()
        run = RecognitionRun.objects.get(photo=photo)
        self.finish(dispatcher, run)
        self.assertEqual((run.state, run.safe_error), ('unavailable', 'model_package_missing'))
        self.assertFalse(run.candidates.exists())

    def test_readonly_adapter_rejects_writable_input_and_wrong_digest(self):
        from archive.services.local_model import infer_readonly
        photo, path = self.imported()
        args = dict(input_sha256=photo.sha256, input_size=photo.byte_size, artifact={},
            test_provider={'candidates': self.output})
        with path.open('rb+') as stream:
            self.assertEqual(infer_readonly(stream.fileno(), **args), {'error_code': 'input_invalid'})
        with path.open('rb') as stream:
            self.assertEqual(infer_readonly(stream.fileno(), **{**args, 'input_sha256': '0'*64}),
                {'error_code': 'input_invalid'})

    def test_partial_frames_preserve_readonly_fd_until_complete(self):
        import array
        import socket
        from archive.services.recognition_process import FrameReader, frame
        photo, path = self.imported()
        parent, child = socket.socketpair()
        self.addCleanup(parent.close)
        self.addCleanup(child.close)
        parent.setblocking(False)
        reader = FrameReader()
        with path.open('rb') as stream:
            raw = frame({'type': 'infer'})
            child.sendmsg([raw[:4]], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [stream.fileno()]))])
        self.assertIsNone(reader.read(parent))
        child.sendall(raw[4:])
        self.assertEqual(reader.read(parent), {'type': 'infer'})
        received = reader.take_fd()
        try:
            self.assertEqual(os.read(received, 2), b'\xff\xd8')
            with self.assertRaises(OSError):
                os.write(received, b'bad')
        finally:
            os.close(received)

    def test_relay_rejects_out_of_order_identity_and_unexpected_fd(self):
        import socket
        from archive.services.recognition_process import RelayClient, ProcessBoundaryError, send_frame
        parent, child = socket.socketpair()
        self.addCleanup(parent.close)
        self.addCleanup(child.close)
        relay = RelayClient(parent.fileno())
        self.addCleanup(relay.close)
        relay.pending = dict(run_id='expected', generation=1)
        send_frame(child, dict(type='result', run_id='old', generation=1, candidates=self.output))
        with self.assertRaises(ProcessBoundaryError):
            relay.poll()
        self.assertTrue(relay.gone)

    def test_recovery_waits_for_ancestor_model_ready_acknowledgment(self):
        from archive.models import RecognitionRun
        from archive.services.recognition import claim_next_run, RecognitionDispatcher
        photo, _ = self.imported()
        run = claim_next_run()
        channel = mock.Mock(ready=False)
        channel.poll.return_value = None
        dispatcher = RecognitionDispatcher(channel, None, None, recover_on_ready=True)
        dispatcher.tick()
        run.refresh_from_db()
        self.assertEqual(run.state, 'running')
        channel.ready = True
        dispatcher.tick()
        run.refresh_from_db()
        self.assertEqual(run.state, 'retry_wait')
        self.assertEqual(run.attempts, 1)

    def test_relay_reaps_killed_child_then_ancestor_restarts_for_explicit_rerun(self):
        import socket
        from archive.models import RecognitionRun
        from archive.services.recognition import RecognitionDispatcher, request_rerun
        from archive.services.paths import open_root
        from archive.services.recognition_process import Supervisor, RelayClient, send_frame
        photo, _ = self.imported()
        _, channel = self.pipeline(delay=.2)
        parent, child = socket.socketpair()
        parent.setblocking(False)
        self.addCleanup(child.close)
        relay = RelayClient(child.fileno())
        self.addCleanup(relay.close)
        relay.ready = True
        supervisor = Supervisor(worker_args=[], worker_env={}, worker_memory_mb=1024, inference=channel)
        supervisor.control = parent
        self.addCleanup(supervisor.close)
        inbox_fd, data_fd = open_root(self.inbox), open_root(self.data)
        self.addCleanup(os.close, inbox_fd)
        self.addCleanup(os.close, data_fd)
        dispatcher = RecognitionDispatcher(relay, inbox_fd, data_fd)
        self.addCleanup(dispatcher.close_input)
        dispatcher.tick()
        supervisor.tick()
        old_pid = channel.process.pid
        channel.process.kill()
        channel.process.wait(timeout=2)
        supervisor.tick()
        dispatcher.tick()
        run = RecognitionRun.objects.get(photo=photo)
        self.assertEqual((run.state, run.safe_error), ('failed', 'model_memory_exceeded'))
        rerun = request_rerun(photo.pk, 1, uuid.uuid4())
        dispatcher.tick()
        supervisor.tick()
        self.assertNotEqual(supervisor.inference.process.pid, old_pid)
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            supervisor.tick()
            dispatcher.tick()
            rerun.refresh_from_db()
            if rerun.state == 'succeeded':
                break
            time.sleep(.01)
        self.assertEqual(rerun.state, 'succeeded')
        self.assertEqual(channel.process.returncode, -signal.SIGKILL)


class SupervisedWorkerTests(ArchiveFixture):
    def test_existing_queue_at_supervisor_start_waits_for_model_ready(self):
        from archive.models import ModelArtifact, RecognitionRun
        from archive.services.recognition import set_current_model
        from archive.services.intake import scan_once
        from archive.services.jobs import process_source
        from archive.services.paths import open_root
        artifact = ModelArtifact.objects.create(manifest_digest='a'*64, provider='test-only',
            version='1', revision='startup', manifest={}, selection_state='selected')
        set_current_model(artifact.pk)
        jpeg(self.inbox/'queued.jpg')
        scan_once()
        from archive.models import Job
        inbox_fd, data_fd = open_root(self.inbox), open_root(self.data)
        try:
            process_source(Job.objects.get(kind='import'), inbox_fd, data_fd)
        finally:
            os.close(inbox_fd)
            os.close(data_fd)
        self.assertEqual(RecognitionRun.objects.get().state, 'queued')
        process, _ = self.start_supervisor()
        self.until(lambda: RecognitionRun.objects.get().state == 'unavailable')
        self.assertIsNone(process.poll())
    def test_real_relay_publishes_checked_input_without_blocking_archive(self):
        from archive.models import ModelArtifact, RecognitionRun, RecognitionState
        from archive.services.recognition import set_current_model
        from archive.services.confirmation import confirm_photo
        artifact = ModelArtifact.objects.create(manifest_digest='a'*64, provider='test-only',
            version='1', revision='relay', manifest={}, selection_state='selected')
        set_current_model(artifact.pk)
        script = PROBE_SUPERVISOR.replace('probe=True', "test_provider={'delay': .7, 'candidates': "
            "[{'rank':1,'raw_label':'test bird','score':.5,'species_id':None,'mapping_status':'unmapped','mapping_evidence':''}]}")
        script = script.replace("    channel.submit(run_id=str(uuid.uuid4()), generation=1, artifact_digest='a'*64, probe_seconds=15)\n", '')
        script = script.replace('        time.sleep(.02)', '        supervisor.tick()\n        time.sleep(.02)')
        with mock.patch(__name__+'.PROBE_SUPERVISOR', script):
            process, ready = self.start_supervisor(probe=True)
        jpeg(self.inbox/'first.jpg', color='red')
        jpeg(self.inbox/'second.jpg', color='blue')
        originals = {name: (self.inbox/name).read_bytes() for name in ('first.jpg', 'second.jpg')}
        self.until(lambda: RecognitionRun.objects.filter(state='running').exists())
        self.until(lambda: Photo.objects.count() == 2)
        first = RecognitionRun.objects.get(state='running').photo
        other = Photo.objects.exclude(pk=first.pk).get()
        confirm_photo(other.pk, 1, uuid.uuid4(), 'undetermined', [], [])
        self.until(lambda: Photo.objects.get(pk=other.pk).state == 'archived')
        self.until(lambda: RecognitionState.objects.get(photo=first).active_run_id is not None)
        self.assertEqual(first.recognition_runs.get().candidates.get().raw_label, 'test bird')
        first_name = first.observations.get().source.relative_path
        self.assertEqual((self.inbox/first_name).read_bytes(), originals[first_name])
        from archive.models import ManagedOriginal
        managed = ManagedOriginal.objects.get(photo=other)
        other_name = other.observations.get().source.relative_path
        self.assertEqual((self.archive/managed.relative_path).read_bytes(), originals[other_name])
        self.assertFalse(list(self.data.glob('.recognition-*')))

    def start_supervisor(self, probe=False, **environment):
        options = command_options('runrecognitionworker')
        if probe:
            options['args'] = [sys.executable, '-c', PROBE_SUPERVISOR]
        options['env'].update(SCAN_INTERVAL_MS='25', WORKER_JOB_SLEEP_MS='1', **environment)
        process = subprocess.Popen(**options, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_tree, process)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if select.select([process.stdout], [], [], .05)[0]:
                line = process.stdout.readline()
                if line:
                    value = json.loads(line)
                    if value.get('type') == 'ready':
                        return process, value
            self.assertIsNone(process.poll(), '监督入口必须启动唯一文件worker并等待ready；' +
                              (process.stderr.read() if process.poll() is not None else ''))
        self.fail('监督ready超时')

    @staticmethod
    def stop_tree(process):
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=3)

    def until(self, predicate, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.025)
        self.fail('隔离进程条件超时')

    def test_supervised_missing_model_still_imports_confirms_and_archives(self):
        self.manual_flow(probe=False)

    def test_busy_model_probe_does_not_block_manual_file_executor(self):
        self.manual_flow(probe=True)

    def manual_flow(self, probe):
        process, ready = self.start_supervisor(probe=probe)
        self.assertEqual(ready['model_status'], 'probe_control_only' if probe else 'model_not_configured')
        if probe:
            self.assertNotEqual(ready['model_pid'], ready['worker_pid'])
            self.assertEqual(ready['model_resources']['memory_requested_bytes'], 2048*1024*1024)
        if sys.platform.startswith('linux'):
            self.assertEqual(ready['worker_resources']['address_space_limit_bytes'], 1024*1024*1024)
            self.assertEqual(ready['worker_resources']['memory_control'], 'enforced')
        self.assertNotEqual(process.pid, ready['worker_pid'])
        inode = (self.data/'worker.lock').stat().st_ino
        path = self.inbox/'监督.jpg'
        digest = jpeg(path)
        self.until(lambda: Photo.objects.filter(sha256=digest).exists())
        photo = Photo.objects.get(sha256=digest)
        response = self.client.post(f'/photos/{photo.pk}/', dict(metadata_version=photo.metadata_version,
            request_id=str(uuid.uuid4()), classification='species', species_ids=[str(self.species.pk)]))
        self.assertEqual(response.status_code, 303)
        self.until(lambda: Photo.objects.filter(pk=photo.pk, state='archived').exists())
        from archive.models import ArchiveOperation
        self.assert_complete(photo, path, ArchiveOperation.objects.get(photo=photo))
        self.assertEqual(hashlib.sha256(next(self.archive.rglob('*.jpg')).read_bytes()).hexdigest(), digest)
        second = command('runworker', '--once', timeout=4)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('worker_busy', second.stderr)
        self.assertEqual((self.data/'worker.lock').stat().st_ino, inode)
        process.terminate()
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(command('runworker', '--once').returncode, 0)

    def test_sigterm_supervisor_reaps_both_children_before_restart(self):
        process, ready = self.start_supervisor(probe=True)
        inode = (self.data/'worker.lock').stat().st_ino
        process.terminate()
        process.communicate(timeout=5)
        self.assertFalse(process_alive(ready['worker_pid']))
        self.assertFalse(process_alive(ready['model_pid']))
        self.assertEqual(command('runworker', '--once').returncode, 0)
        self.assertEqual((self.data/'worker.lock').stat().st_ino, inode)

    def test_sigkill_supervisor_eof_stops_busy_child_and_idle_worker(self):
        process, ready = self.start_supervisor(probe=True)
        process.kill()
        process.communicate(timeout=3)
        self.until(lambda: not process_alive(ready['model_pid']), timeout=3)
        self.until(lambda: not process_alive(ready['worker_pid']), timeout=3)
        replacement, next_ready = self.start_supervisor(probe=True)
        self.assertNotEqual(ready['worker_pid'], next_ready['worker_pid'])
        self.assertNotEqual(ready['model_pid'], next_ready['model_pid'])

    def test_sigkill_file_worker_stops_supervisor_and_model(self):
        process, ready = self.start_supervisor()
        os.kill(ready['worker_pid'], signal.SIGKILL)
        process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(command('runworker', '--once').returncode, 0)

    def test_sigterm_file_worker_is_reaped_before_new_executor(self):
        process, ready = self.start_supervisor(probe=True)
        os.kill(ready['worker_pid'], signal.SIGTERM)
        process.communicate(timeout=5)
        self.assertFalse(process_alive(ready['model_pid']))
        self.assertEqual(command('runworker', '--once').returncode, 0)

    def test_parent_death_paused_file_job_keeps_lock_until_real_exit(self):
        jpeg(self.inbox/'暂停父链.jpg')
        process, ready = self.start_supervisor(probe=True, BIRDS_TEST_FAULT_STAGE='claimed')
        marker = self.root/'fault-ready'
        self.until(marker.exists)
        inode = (self.data/'worker.lock').stat().st_ino
        process.kill()
        process.communicate(timeout=3)
        self.until(lambda: not process_alive(ready['model_pid']), timeout=3)
        try:
            second = command('runworker', '--once')
            self.assertIn('worker_busy', second.stderr)
            self.assertTrue(process_alive(ready['worker_pid']))
            self.assertEqual((self.data/'worker.lock').stat().st_ino, inode)
        finally:
            (self.root/'fault-release').write_text('release')
        self.until(lambda: not process_alive(ready['worker_pid']), timeout=5)
        self.assertEqual(command('runworker', '--once').returncode, 0)
        self.assertEqual(Photo.objects.count(), 1)

    def test_no_model_starts_when_file_lock_not_ready(self):
        first, _ = self.start_supervisor()
        options = command_options('runrecognitionworker')
        second = subprocess.run(**options, capture_output=True, text=True, timeout=4)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('recognition_supervisor_unavailable', second.stderr)
        self.assertNotIn('"type": "ready"', second.stdout)
        self.assertIsNone(first.poll())


class InferenceProcessTests(SimpleTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='birds-process-')
        self.addCleanup(self.temporary.cleanup)
        self.lock_path = Path(self.temporary.name)/'model.lock'

    def channel(self, *, memory_mb=256, timeout_seconds=1, probe=True):
        from archive.services.recognition_process import InferenceChannel, InferenceLimits
        channel = InferenceChannel(python=sys.executable, lock_path=self.lock_path,
            limits=InferenceLimits(memory_mb, timeout_seconds), probe=probe)
        self.addCleanup(channel.close)
        channel.start().wait_ready()
        return channel

    def answer(self, channel, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = channel.poll()
            if value:
                return value
            select.select([channel.socket], [], [], .02)
        self.fail('子角色响应超时')

    def submit(self, channel, **payload):
        channel.submit(run_id=str(uuid.uuid4()), generation=1, artifact_digest='a'*64, **payload)

    def test_independent_model_role_has_no_database_or_optional_imports(self):
        channel = self.channel(memory_mb=2048)
        ready = channel.ready
        self.assertEqual(ready['resources']['memory_requested_bytes'], 2048*1024*1024)
        self.assertFalse(ready['optional_imports_present'])
        if sys.platform.startswith('linux'):
            self.assertGreaterEqual(ready['nice_observed'], 10)
        else:
            self.assertIn(ready['priority_control'], ('enforced', 'permission_denied_unverified', 'observed'))
        self.submit(channel, probe_seconds=.03)
        response = self.answer(channel)
        self.assertEqual(response['type'], 'result')
        self.assertEqual(response['evidence'], 'process_control_probe')
        channel.close()
        self.assertIsNotNone(channel.process.returncode)

    def test_production_role_returns_unavailable_never_fake_candidate(self):
        channel = self.channel(probe=False)
        self.submit(channel)
        answer = self.answer(channel)
        self.assertEqual(answer['error_code'], 'model_not_configured')
        self.assertNotIn('candidates', answer)

    def test_single_model_lifetime_lock_stays_same_inode_on_restart(self):
        from archive.services.recognition_process import ProcessBoundaryError
        first = self.channel()
        inode = self.lock_path.stat().st_ino
        with self.assertRaisesRegex(ProcessBoundaryError, 'model_busy'):
            self.channel()
        first.close()
        second = self.channel()
        self.assertEqual(self.lock_path.stat().st_ino, inode)
        self.assertNotEqual(first.process.pid, second.process.pid)

    def test_resource_report_and_linux_allocation_oom_then_restart(self):
        import resource
        before = resource.getrlimit(resource.RLIMIT_AS)
        channel = self.channel(memory_mb=256)
        limits = channel.ready['resources']
        if not sys.platform.startswith('linux'):
            self.assertEqual(limits['memory_control'], 'unsupported_unverified')
            self.assertIsNone(limits['hard_bytes'])
            return  # Explicit unsupported assertion; this is NOT Linux/OOM proof.
        self.assertEqual((limits['soft_bytes'], limits['hard_bytes']), (256*1024*1024,)*2)
        self.submit(channel, probe_allocate_mb=512)
        self.assertEqual(self.answer(channel)['error_code'], 'model_memory_exceeded')
        channel.process.kill()
        channel.close()
        replacement = self.channel(memory_mb=2048)
        self.assertEqual(replacement.ready['resources']['hard_bytes'], 2048*1024*1024)
        self.assertEqual(resource.getrlimit(resource.RLIMIT_AS), before)

    def test_timeout_reaps_slow_model_before_replacement(self):
        channel = self.channel(timeout_seconds=1)
        self.submit(channel, probe_seconds=15)
        answer = self.answer(channel, timeout=2)
        self.assertEqual(answer['error_code'], 'model_timeout')
        self.assertIsNotNone(channel.process.poll())
        replacement = self.channel(memory_mb=2048)
        self.assertNotEqual(channel.process.pid, replacement.process.pid)

    def test_term_and_kill_model_reap_and_restart_without_pid_signals_after_reap(self):
        for signum in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=signum):
                channel = self.channel(memory_mb=2048)
                self.submit(channel, probe_seconds=15)
                channel.process.send_signal(signum)
                channel.process.wait(timeout=2)
                with mock.patch.object(channel.process, 'send_signal', side_effect=AssertionError('不可再signal已收割PID')):
                    channel.close()
                replacement = self.channel(memory_mb=2048)
                self.assertNotEqual(channel.process.pid, replacement.process.pid)
                if sys.platform.startswith('linux'):
                    self.assertEqual(replacement.ready['resources']['hard_bytes'], 2048*1024*1024)
                replacement.close()

    def test_channel_eof_interrupts_long_computation_and_releases_lifetime_lock(self):
        channel = self.channel()
        self.submit(channel, probe_seconds=15)
        channel.socket.close()
        channel.socket = None
        channel.process.wait(timeout=2)
        self.assertEqual(channel.process.returncode, 0)
        replacement = self.channel()
        self.assertIsNone(replacement.process.poll())

    def test_minimal_fds_and_repeated_start_stop_no_parent_fd_growth(self):
        directory = '/proc/self/fd' if sys.platform.startswith('linux') else '/dev/fd'
        count_before = len(os.listdir(directory))
        canary = os.open(self.temporary.name, os.O_RDONLY | os.O_DIRECTORY)
        os.set_inheritable(canary, True)
        try:
            for _ in range(10):
                channel = self.channel()
                self.assertEqual(len(channel.ready['inherited_fds']), 2, '只允许模型锁与控制socket')
                channel.close()
        finally:
            os.close(canary)
        self.assertLessEqual(len(os.listdir(directory)), count_before)

    def test_protocol_rejects_oversized_truncated_and_invalid_frames(self):
        from archive.services.recognition_process import FrameReader, ProcessBoundaryError, MAX_FRAME_BYTES
        for data, code in ((b'x'*(MAX_FRAME_BYTES+1), 'protocol_oversized'),
                           (b'{', 'protocol_truncated'), (b'[]\n', 'protocol_invalid'),
                           (b'{"type":1}\n{}\n', 'protocol_invalid')):
            with self.subTest(code=code):
                import socket
                import threading
                parent, child = socket.socketpair()
                def write_frame():
                    child.sendall(data)
                    child.shutdown(socket.SHUT_WR)
                writer = threading.Thread(target=write_frame, daemon=True)
                try:
                    writer.start()
                    reader = FrameReader()
                    with self.assertRaisesRegex(ProcessBoundaryError, code):
                        while True:
                            reader.read(parent)
                finally:
                    writer.join(timeout=2)
                    parent.close(); child.close()

    def test_pid_nonce_and_run_identity_mismatch_rejected(self):
        from archive.services.recognition_process import ProcessBoundaryError
        for field in ('nonce', 'pid', 'run_id', 'generation', 'artifact_digest'):
            with self.subTest(field=field):
                channel = self.channel()
                self.submit(channel, probe_seconds=15)
                answer = dict(type='result', protocol_version=1, nonce=channel.nonce,
                              pid=channel.process.pid, **channel.pending)
                answer[field] = 'stale' if field not in ('pid', 'generation') else -1
                with mock.patch.object(channel.reader, 'read', return_value=answer):
                    with self.assertRaisesRegex(ProcessBoundaryError, 'protocol_identity_mismatch'):
                        channel.poll()
                channel.close()

    def test_outstanding_request_rejects_backlog_and_arbitrary_payload(self):
        from archive.services.recognition_process import ProcessBoundaryError
        channel = self.channel()
        with self.assertRaisesRegex(ProcessBoundaryError, 'request_invalid'):
            self.submit(channel, shell='anything')
        self.submit(channel, probe_seconds=15)
        with self.assertRaisesRegex(ProcessBoundaryError, 'channel_busy'):
            self.submit(channel)

    def test_external_linux_hard_ceiling_is_not_bypassed(self):
        if not sys.platform.startswith('linux'):
            from archive.services.recognition_process import address_space
            self.assertEqual(address_space(256)['memory_control'], 'unsupported_unverified')
            return
        code = '''
import resource
from archive.services.recognition_process import address_space, ProcessBoundaryError
resource.setrlimit(resource.RLIMIT_AS, (256*1024*1024, 256*1024*1024))
try:
    address_space(2048)
except ProcessBoundaryError as error:
    print(str(error))
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'inherited_hard_limit_insufficient')

    def test_stopped_child_requires_kill_and_is_reaped(self):
        channel = self.channel()
        channel.process.send_signal(signal.SIGSTOP)
        # Wait until the kernel has stopped it, independent of scheduling order.
        os.waitpid(channel.process.pid, os.WUNTRACED)
        channel.close()
        # Darwin may deliver SIGTERM to a stopped process immediately.
        self.assertIn(channel.process.returncode, (-signal.SIGTERM, -signal.SIGKILL))
        self.assertIsNone(self.channel().process.poll())

    def test_unresponsive_process_requires_sigkill_and_wait_reaps_it(self):
        from archive.services.recognition_process import terminate_owned
        process = subprocess.Popen([sys.executable, '-c',
            'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(60)'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(terminate_owned, process)
        self.assertTrue(select.select([process.stdout], [], [], 3)[0])
        self.assertEqual(process.stdout.readline().strip(), 'ready')
        terminate_owned(process, grace=.1)
        self.assertEqual(process.returncode, -signal.SIGKILL)
        process.stdout.close()

    def test_supervisor_soft_limit_preserves_model_hard_capacity_across_restart(self):
        code = '''
import json, resource, sys, tempfile
from pathlib import Path
from archive.services.recognition_process import address_space, InferenceChannel, InferenceLimits
before = resource.getrlimit(resource.RLIMIT_AS)[1]
supervisor = address_space(1024, preserve_hard=True)
results = []
with tempfile.TemporaryDirectory(prefix='birds-budget-') as root:
    for _ in range(2):
        channel = InferenceChannel(python=sys.executable, lock_path=Path(root)/'model.lock',
            limits=InferenceLimits(2048, 2), probe=True)
        channel.start().wait_ready()
        results.append(channel.ready['resources'])
        channel.process.kill()
        channel.close()
print(json.dumps(dict(before=before, after=resource.getrlimit(resource.RLIMIT_AS)[1],
                     supervisor=supervisor, children=results)))
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(result.stdout)
        self.assertEqual(evidence['before'], evidence['after'])
        for child in evidence['children']:
            self.assertEqual(child['memory_requested_bytes'], 2048*1024*1024)
            if sys.platform.startswith('linux'):
                self.assertEqual(child['hard_bytes'], 2048*1024*1024)
                self.assertEqual(evidence['supervisor']['soft_bytes'], 1024*1024*1024)
            else:
                self.assertEqual(child['memory_control'], 'unsupported_unverified')
