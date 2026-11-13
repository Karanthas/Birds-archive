import fcntl
import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from django.http import HttpResponse, StreamingHttpResponse
from django.test import RequestFactory, SimpleTestCase

from archive.access import PrivateResponseMiddleware
from archive.services import maintenance
from archive.tests.fixtures import command_options, jpeg
from archive.tests.test_archive_recovery import ArchiveFixture


class MaintenanceBarrierTests(SimpleTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="birds-maintenance-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.override = patch.object(maintenance.settings, "BIRDS_DATA_ROOT", self.root)
        self.override.start()
        self.addCleanup(self.override.stop)

    def acknowledge(self, session):
        def update(current):
            current["supervisor_ack"] = {
                "session_id": session["session_id"], "status": "drained", "worker_pid": 123,
            }
            return current
        maintenance._mutate_state(session["session_id"], update)

    def test_request_acquire_and_explicit_resume_are_durable(self):
        session = maintenance.request("test-owner")
        self.assertEqual(maintenance.status()["status"], "requested")
        with maintenance.acquire(session["session_id"], "test-owner"):
            current = maintenance.status()
            self.assertEqual(current["status"], "acquired")
            self.assertTrue(maintenance.worker_should_stop())
            worker = os.open(self.root / maintenance.WORKER_LOCK_NAME, os.O_RDWR)
            self.addCleanup(os.close, worker)
            with self.assertRaises(BlockingIOError):
                fcntl.flock(worker, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(maintenance.status()["status"], "active")
        self.assertTrue((self.root / maintenance.LOCK_NAME).exists())
        maintenance.resume(session["session_id"], "test-owner")
        self.assertEqual(maintenance.status()["status"], "resumed")
        self.assertFalse(maintenance.worker_should_stop())
        with maintenance.request_barrier():
            pass

    def test_second_session_direct_command_and_stale_session_are_rejected(self):
        session = maintenance.request("owner")
        with self.assertRaisesRegex(maintenance.MaintenanceError, "already_active"):
            maintenance.request("other")
        with self.assertRaisesRegex(maintenance.MaintenanceError, "not_allowed"):
            maintenance.run_offline(session["session_id"], "shell", ["-c", "write"])
        with self.assertRaisesRegex(maintenance.MaintenanceError, "session_invalid"):
            maintenance.resume("0" * 32)

    def test_shared_request_barrier_rechecks_state_and_stream_holds_lock_until_close(self):
        response = PrivateResponseMiddleware(
            lambda _request: StreamingHttpResponse(iter([b"jpeg-bytes"]), content_type="image/jpeg")
        )(RequestFactory().get("/photos/example/download/", HTTP_HOST="testserver"))
        session = maintenance.request("owner")
        with self.assertRaisesRegex(maintenance.MaintenanceError, "requests_busy"):
            with maintenance.acquire(session["session_id"], timeout=0.01):
                pass
        response.close()
        with maintenance.acquire(session["session_id"], timeout=0.2):
            pass
        maintenance.resume(session["session_id"])

        normal = PrivateResponseMiddleware(lambda _request: HttpResponse("ok"))(
            RequestFactory().get("/", HTTP_HOST="testserver")
        )
        self.assertEqual(normal.status_code, 200)

    def test_offline_process_group_finishes_and_state_omits_arguments(self):
        session = maintenance.request("owner")
        self.acknowledge(session)
        command = [sys.executable, "-c", "print('offline child complete')"]
        with patch.object(maintenance, "_command", return_value=command), \
                patch("archive.services.deployment.validate_deployment", return_value={"status": "verified"}):
            result = maintenance.run_offline(
                session["session_id"], "importcatalog", ["--file", "secret-path"], timeout=5,
            )
        self.assertEqual(result["status"], "offline_completed")
        current = maintenance.status()
        self.assertEqual(current["status"], "active")
        self.assertEqual(current["protected_action"]["verb"], "importcatalog")
        self.assertEqual(current["protected_action"]["status"], "completed")
        self.assertTrue(current["protected_action"]["process_group_gone"])
        self.assertNotIn("argv", json.dumps(current))
        self.assertNotIn("secret-path", json.dumps(current))

    def test_offline_failure_keeps_maintenance_active_and_diagnostic(self):
        session = maintenance.request("owner")
        self.acknowledge(session)
        command = [sys.executable, "-c", "raise SystemExit(3)"]
        with patch.object(maintenance, "_command", return_value=command), \
                patch("archive.services.deployment.validate_deployment", return_value={"status": "verified"}):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "offline_command_failed"):
                maintenance.run_offline(session["session_id"], "loadguidebundle", timeout=5)
        current = maintenance.status()
        self.assertEqual(current["status"], "failed")
        self.assertEqual(current["failure"], "offline_command_failed")
        self.assertEqual(current["protected_action"]["status"], "failed")
        self.assertTrue(current["protected_action"]["process_group_gone"])
        self.assertTrue(maintenance.worker_should_stop())
        maintenance.resume(session["session_id"])

    def test_offline_descendant_is_stopped_before_terminal_failure(self):
        session = maintenance.request("owner")
        self.acknowledge(session)
        command = [
            sys.executable,
            "-c",
            "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])",
        ]
        with patch.object(maintenance, "_command", return_value=command), \
                patch("archive.services.deployment.validate_deployment", return_value={"status": "verified"}):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "offline_descendant_survived"):
                maintenance.run_offline(session["session_id"], "preparemodel", timeout=5)
        action = maintenance.status()["protected_action"]
        self.assertEqual(action["status"], "failed")
        self.assertEqual(action["failure"], "offline_descendant_survived")
        self.assertTrue(action["process_group_gone"])
        maintenance.resume(session["session_id"])

    def test_compare_by_session_update_preserves_supervisor_fields(self):
        session = maintenance.request("owner")

        self.acknowledge(session)
        with maintenance.acquire(session["session_id"], require_supervisor=True):
            self.assertEqual(maintenance.status()["supervisor_ack"]["worker_pid"], 123)
        self.assertEqual(maintenance.status()["supervisor_ack"]["status"], "drained")

    def test_unclosed_action_cannot_resume_or_start_another_action(self):
        session = maintenance.request("owner")

        def running(current):
            current["protected_action"] = {
                "kind": "offline", "verb": "preparemodel", "status": "running", "pid": 456,
            }
            current["status"] = "failed"
            return current

        maintenance._mutate_state(session["session_id"], running)
        with self.assertRaisesRegex(maintenance.MaintenanceError, "action_unclosed"):
            maintenance.resume(session["session_id"], timeout=0.1)
        with self.assertRaisesRegex(maintenance.MaintenanceError, "action_unclosed"):
            with maintenance.acquire(session["session_id"], timeout=0.1):
                pass
        self.assertEqual(maintenance.status()["protected_action"]["status"], "running")

    def test_entrypoint_routes_only_explicit_maintenance_modes(self):
        source = (Path(maintenance.settings.BASE_DIR) / "deploy" / "entrypoint.sh").read_text()
        self.assertIn('exec python -m archive.services.maintenance "$session_id" "$verb" "$@"', source)
        self.assertIn('backuparchive|restorearchive) exec python /app/manage.py "$mode" "$@"', source)
        result = subprocess.run(
            ["sh", str(Path(maintenance.settings.BASE_DIR) / "deploy" / "entrypoint.sh"), "shell"],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("entrypoint_mode_invalid", result.stderr)


class SupervisorDrainProtocolMixin:
    def test_relay_accepts_coalesced_started_drain_and_existing_reply(self):
        from archive.services.recognition_process import RelayClient, frame

        parent, child = socket.socketpair()
        self.addCleanup(parent.close)
        self.addCleanup(child.close)
        relay = RelayClient(parent.fileno())
        self.addCleanup(relay.close)
        session_id = "a" * 32
        identity = {
            "run_id": "run", "generation": 1, "artifact_digest": "b" * 64,
            "input_sha256": "c" * 64, "input_size": 12,
        }
        child.sendall(frame({"type": "started"}) + frame({"type": "drain", "session_id": session_id})
                      + frame({"type": "error", "error_code": "model_not_configured", **identity}))
        self.assertIsNone(relay.poll())
        self.assertTrue(relay.ready)
        relay.pending = dict(identity)
        self.assertIsNone(relay.poll())
        self.assertEqual(relay.drain_session_id, session_id)
        self.assertEqual(relay.poll()["type"], "error")
        self.assertIsNone(relay.pending)

    def test_supervisor_reads_buffered_ack_before_classifying_clean_worker_exit(self):
        from archive.services.recognition_process import Supervisor, send_frame

        parent, child = socket.socketpair()
        parent.setblocking(False)
        self.addCleanup(child.close)
        supervisor = Supervisor(worker_args=[], worker_env={}, worker_memory_mb=1)
        supervisor.control = parent
        supervisor.worker = mock.Mock(pid=321)
        supervisor.worker.poll.side_effect = [None, 0]
        supervisor.worker.wait.return_value = 0
        session_id = "d" * 32
        send_frame(child, {"type": "drained", "session_id": session_id, "pid": 321})
        result = supervisor.drain(session_id, timeout=.2)
        self.assertEqual(result["worker_exit"], 0)
        self.assertFalse(result["forced"])

    def test_drain_never_restarts_model_after_unexpected_exit(self):
        from archive.services.recognition_process import ProcessBoundaryError, Supervisor, send_frame

        parent, child = socket.socketpair()
        parent.setblocking(False)
        self.addCleanup(parent.close)
        self.addCleanup(child.close)
        inference = mock.Mock(socket=None)
        inference.process.poll.return_value = 1
        supervisor = Supervisor(worker_args=[], worker_env={}, worker_memory_mb=1, inference=inference)
        supervisor.control = parent
        supervisor.worker = mock.Mock(pid=654)
        supervisor.drain_session_id = "e" * 32
        with tempfile.TemporaryFile() as source:
            send_frame(child, {
                "type": "infer", "run_id": "run", "generation": 1,
                "artifact_digest": "a" * 64, "input_sha256": "b" * 64,
                "input_size": 0, "artifact": {},
            }, source.fileno())
        with self.assertRaisesRegex(ProcessBoundaryError, "model_unexpected_exit"):
            supervisor.tick()
        inference.start.assert_not_called()

    def test_drain_timeout_is_failure_not_terminal_proof(self):
        from archive.services.recognition_process import ProcessBoundaryError, Supervisor

        parent, child = socket.socketpair()
        parent.setblocking(False)
        self.addCleanup(parent.close)
        self.addCleanup(child.close)
        supervisor = Supervisor(worker_args=[], worker_env={}, worker_memory_mb=1)
        supervisor.control = parent
        supervisor.worker = mock.Mock(pid=777)
        supervisor.worker.poll.return_value = None
        with self.assertRaisesRegex(ProcessBoundaryError, "supervisor_drain_timeout"):
            supervisor.drain("f" * 32, timeout=.01)
        self.assertIsNone(supervisor.drain_ack)

    def test_maintenance_tick_suppresses_startup_recovery_and_new_claim(self):
        from archive.services.recognition import RecognitionDispatcher

        channel = mock.Mock(ready=True, pending=None)
        channel.poll.return_value = None
        dispatcher = RecognitionDispatcher(channel, None, None, recover_on_ready=True)
        with patch("archive.services.recognition.recover_runs") as recover, \
                patch("archive.services.recognition.claim_next_run") as claim:
            dispatcher.tick(allow_claim=False)
        recover.assert_not_called()
        claim.assert_not_called()
        self.assertTrue(dispatcher.drained)


class SupervisorDrainTests(SupervisorDrainProtocolMixin, ArchiveFixture):
    def setUp(self):
        super().setUp()
        (self.data / maintenance.STATE_NAME).unlink(missing_ok=True)

    @staticmethod
    def _alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def _start(self):
        options = command_options("runrecognitionworker")
        options["env"].update(SCAN_INTERVAL_MS="25", WORKER_JOB_SLEEP_MS="1")
        process = subprocess.Popen(**options, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_process, process)
        return process

    def _ready(self, process, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if select.select([process.stdout], [], [], .05)[0]:
                line = process.stdout.readline()
                if line:
                    value = json.loads(line)
                    if value.get("type") == "ready":
                        return value
            if process.poll() is not None:
                self.fail("监督入口提前退出：" + process.stderr.read())
        self.fail("监督入口 ready 超时")

    def _until(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.025)
        self.fail("维护排空条件超时")

    def test_failed_drain_persists_diagnostic_but_never_satisfies_barrier(self):
        from archive.management.commands.runrecognitionworker import Command

        session = maintenance.request("test")
        Command._record_failure(session["session_id"], "1" * 32, "supervisor_drain_timeout")
        ack = maintenance.status()["supervisor_ack"]
        self.assertEqual((ack["session_id"], ack["status"], ack["failure"], ack["forced"]),
                         (session["session_id"], "drain_failed", "supervisor_drain_timeout", True))
        with self.assertRaisesRegex(maintenance.MaintenanceError, "supervisor_not_drained"):
            with maintenance.acquire(session["session_id"], timeout=.1, require_supervisor=True):
                pass
        self.assertTrue(maintenance.worker_should_stop())

    def test_real_controlled_children_finish_preexisting_inference_before_drain_ack(self):
        from archive.services.recognition_process import InferenceChannel, InferenceLimits, Supervisor

        source = self.inbox / "drain-probe.jpg"
        digest = jpeg(source)
        marker = self.root / "drain-infer-sent"
        marker.unlink(missing_ok=True)
        script = r'''
import os, socket, sys, time
from archive.services.recognition_process import FrameReader, send_frame
fd = int(sys.argv[sys.argv.index('--supervisor-fd') + 1])
control = socket.socket(fileno=fd)
control.setblocking(False)
send_frame(control, {'type': 'ready', 'pid': os.getpid(), 'resources': {}})
reader = FrameReader(allow_buffered=True)
started = sent = False
drain_session = result = None
deadline = time.monotonic() + 5
while time.monotonic() < deadline:
    message = reader.read(control)
    if message is not None:
        if message.get('type') == 'started':
            started = True
        elif message.get('type') == 'drain':
            drain_session = message['session_id']
        elif message.get('type') in ('result', 'error'):
            result = message
    if started and not sent:
        with open(os.environ['DRAIN_SOURCE'], 'rb') as source:
            send_frame(control, {'type': 'infer', 'run_id': os.environ['DRAIN_RUN_ID'],
                'generation': 1, 'artifact_digest': 'a' * 64,
                'input_sha256': os.environ['DRAIN_SHA'],
                'input_size': int(os.environ['DRAIN_SIZE']), 'artifact': {}}, source.fileno())
        open(os.environ['DRAIN_MARKER'], 'w').write('sent')
        sent = True
    if drain_session and result:
        send_frame(control, {'type': 'drained', 'session_id': drain_session, 'pid': os.getpid()})
        raise SystemExit(0)
    time.sleep(.005)
raise SystemExit(4)
'''
        environment = os.environ.copy()
        environment.update(
            DRAIN_SOURCE=str(source), DRAIN_SHA=digest, DRAIN_SIZE=str(source.stat().st_size),
            DRAIN_RUN_ID="12345678-1234-1234-1234-123456789abc", DRAIN_MARKER=str(marker),
        )
        inference = InferenceChannel(
            python=sys.executable, lock_path=self.data / "drain-model.lock",
            limits=InferenceLimits(512, 3),
            test_provider={"delay": .1, "candidates": []},
        )
        supervisor = Supervisor(
            worker_args=[sys.executable, "-c", script], worker_env=environment,
            worker_memory_mb=512, inference=inference,
        )
        self.addCleanup(supervisor.close)
        supervisor.start()
        self._until(marker.exists)
        result = supervisor.drain("9" * 32, timeout=4)
        self.assertEqual((result["worker_exit"], result["model"], result["forced"]),
                         (0, {"started": True, "returncode": 0}, False))

    def test_actual_supervisor_drains_stands_by_and_resumes_fresh_worker(self):
        process = self._start()
        first = self._ready(process)
        session = maintenance.request("test")
        ack = self._until(lambda: maintenance.status().get("supervisor_ack"))
        self.assertEqual((ack["session_id"], ack["status"], ack["worker_exit"], ack["forced"]),
                         (session["session_id"], "drained", 0, False))
        self._until(lambda: not self._alive(first["worker_pid"]))
        self.assertIsNone(process.poll())

        restarted = self._start()
        time.sleep(.2)
        self.assertIsNone(restarted.poll())
        self.assertFalse(select.select([restarted.stdout], [], [], 0)[0])
        self.assertEqual(maintenance.status()["supervisor_ack"], ack)
        restarted.terminate()
        restarted.communicate(timeout=5)

        maintenance.resume(session["session_id"], "test")
        second = self._ready(process)
        self.assertNotEqual(first["worker_pid"], second["worker_pid"])
        self.assertNotEqual(first["supervisor_instance_id"], second["supervisor_instance_id"])
