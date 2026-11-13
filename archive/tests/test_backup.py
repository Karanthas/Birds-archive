import io
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TransactionTestCase, override_settings

from archive.models import Photo
from archive.services import backup, maintenance


class ConsistentBackupTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="birds-backup-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.roots = {name: self.base / name for name in ("inbox", "data", "archive", "claim")}
        for root in self.roots.values():
            root.mkdir(mode=0o700)
        self.destination = self.base / "backup"
        self.destination.mkdir(mode=0o700)
        (self.roots["data"] / "deployment-v1.json").write_text(json.dumps({
            "schema": 1, "deployment_id": "backup-test", "migrations": [["archive", "0001_initial"]],
            "runtime": {"BIRDS_SOURCE_SHA": "a" * 64, "BIRDS_BASE_IMAGE_ID": "sha256:" + "b" * 64,
                        "BIRDS_PYTHON_TREE_SHA": "c" * 64, "python": "test", "sqlite_source_id": "test"},
        }))
        self.settings = override_settings(**{
            "BIRDS_INBOX_ROOT": str(self.roots["inbox"]), "BIRDS_DATA_ROOT": str(self.roots["data"]),
            "BIRDS_ARCHIVE_ROOT": str(self.roots["archive"]), "BIRDS_CLAIM_ROOT": str(self.roots["claim"]),
        })
        self.settings.enable()
        self.addCleanup(self.settings.disable)
        deployment = patch.object(backup, "validate_deployment", return_value={"status": "verified"})
        deployment.start()
        self.addCleanup(deployment.stop)
        identity = patch.object(backup, "filesystem_identity", side_effect=lambda path: {
            "type": "testfs", "device": "test-device", "root": str(Path(path).resolve()),
        })
        identity.start()
        self.addCleanup(identity.stop)

    def session(self, *, acknowledge=True):
        session = maintenance.request("backup-test")
        if acknowledge:
            maintenance._mutate_state(session["session_id"], lambda current: {
                **current, "supervisor_ack": {"session_id": session["session_id"], "status": "drained"},
            })
        return session

    def test_complete_set_has_sqlite_baseline_inventory_hardlinks_and_no_private_manifest_fields(self):
        source = self.roots["inbox"] / "sample.jpg"
        source.write_bytes(b"synthetic-jpeg")
        os.link(source, self.roots["claim"] / "claimed.jpg")
        Photo.objects.create(sha256="d" * 64, byte_size=14, preview_key="preview", notes="private-note-value")
        session = self.session()

        result = backup.create(session["session_id"], self.destination)
        set_path = Path(result["set"])
        self.assertEqual(result["status"], "complete")
        self.assertTrue((set_path / backup.COMPLETE).is_file())
        manifest_text = (set_path / backup.MANIFEST).read_text()
        self.assertNotIn("private-note-value", manifest_text)
        manifest = json.loads(manifest_text)
        photos = next(row for row in manifest["database"]["tables"] if row["table"] == "archive_photo")
        self.assertEqual(photos["rows"], 1)
        copied = [set_path / "roots" / row["root_role"] / row["relative_path"]
                  for row in manifest["files"] if row["relative_path"] in ("sample.jpg", "claimed.jpg")]
        self.assertEqual(len(copied), 2)
        self.assertEqual(copied[0].stat().st_ino, copied[1].stat().st_ino)
        self.assertEqual(backup.verify(set_path)["status"], "verified")
        maintenance.resume(session["session_id"])

    def test_supervisor_ack_is_required_and_incomplete_source_never_publishes_marker(self):
        session = self.session(acknowledge=False)
        with self.assertRaisesRegex(maintenance.MaintenanceError, "supervisor_not_drained"):
            backup.create(session["session_id"], self.destination)
        maintenance._mutate_state(session["session_id"], lambda current: {
            **current, "supervisor_ack": {"session_id": session["session_id"], "status": "drained"},
        })
        (self.roots["inbox"] / "outside").symlink_to(self.base)
        with self.assertRaisesRegex(backup.BackupError, "special_file"):
            backup.create(session["session_id"], self.destination)
        sets = list(self.destination.iterdir())
        self.assertEqual(len(sets), 1)
        self.assertFalse((sets[0] / backup.COMPLETE).exists())

    def test_destination_overlap_and_external_hardlink_are_rejected(self):
        nested = self.roots["archive"] / "backup"
        nested.mkdir()
        with self.assertRaisesRegex(backup.BackupError, "overlaps_source"):
            backup._roots_and_destination(nested)
        source = self.roots["inbox"] / "original.jpg"
        source.write_bytes(b"original")
        os.link(source, self.base / "untracked-link.jpg")
        with self.assertRaisesRegex(backup.BackupError, "external_hardlink"):
            backup._scan({role: {"path": path} for role, path in self.roots.items()})

    def test_sqlite_backup_reads_committed_wal_and_emits_no_sidecars(self):
        source = self.base / "wal.sqlite3"
        live = sqlite3.connect(source)
        self.addCleanup(live.close)
        live.execute("pragma journal_mode=WAL")
        live.execute("create table evidence(value text)")
        live.execute("insert into evidence values ('committed-in-wal')")
        live.commit()
        target = self.base / "snapshot" / "archive.sqlite3"
        with patch.dict(backup.connections["default"].settings_dict, {"NAME": source}):
            baseline = backup._snapshot_database(target)
        with sqlite3.connect(target.as_uri() + "?mode=ro&immutable=1", uri=True) as copied:
            self.assertEqual(copied.execute("select value from evidence").fetchone(), ("committed-in-wal",))
        self.assertEqual(baseline["path"], "database/archive.sqlite3")
        self.assertFalse(Path(str(target) + "-wal").exists())
        self.assertFalse(Path(str(target) + "-shm").exists())

    def test_snapshot_journal_checks_managed_original_hash_and_unclosed_action_blocks_create(self):
        database = self.base / "dependency.sqlite3"
        with sqlite3.connect(database) as db:
            db.executescript("""
                create table archive_managedoriginal(root_key text,relative_path text,sha256 text,byte_size integer,operation_id text);
                create table archive_archiveoperation(id text,stage text,claim_relative_path text,temporary_relative_path text,
                    target_relative_path text,expected_sha256 text,byte_size integer,source_observation_id text);
                create table archive_sourceobservation(id text,source_id text,available integer,sha256 text,byte_size integer);
                create table archive_importsource(id text,root_key text,relative_path text,current_observation_id text);
                create table archive_managementoperation(stage text,source_snapshot text,target_relative_path text,
                    target_fingerprint text,retry_context text,status text);
                create table archive_recognitionrun(source_observation_id text,photo_sha256 text,byte_size integer,state text);
                insert into archive_managedoriginal values('archive','bird.jpg','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',4,'archive-op');
            """)
        inventory = [{"root_role": "archive", "relative_path": "bird.jpg", "sha256": "b" * 64, "size": 4}]
        with self.assertRaisesRegex(backup.BackupError, "managed_original_missing"):
            backup._validate_active_dependencies(database, inventory)

        session = self.session()
        maintenance._mutate_state(session["session_id"], lambda current: {
            **current, "status": "failed", "protected_action": {"kind": "offline", "status": "running"},
        })
        with self.assertRaisesRegex(maintenance.MaintenanceError, "action_unclosed"):
            backup.create(session["session_id"], self.destination)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_completed_archive_proves_stale_available_and_recognition_source_was_consumed(self):
        database = self.base / "consumed.sqlite3"
        digest = "a" * 64
        with sqlite3.connect(database) as db:
            db.executescript(f"""
                create table archive_managedoriginal(root_key text,relative_path text,sha256 text,byte_size integer,operation_id text);
                create table archive_archiveoperation(id text,source_observation_id text,stage text,claim_relative_path text,
                    temporary_relative_path text,target_relative_path text,expected_sha256 text,byte_size integer);
                create table archive_sourceobservation(id text,source_id text,available integer,sha256 text,byte_size integer);
                create table archive_importsource(id text,root_key text,relative_path text,current_observation_id text);
                create table archive_managementoperation(stage text,source_snapshot text,target_relative_path text,
                    target_fingerprint text,retry_context text,status text);
                create table archive_recognitionrun(source_observation_id text,photo_sha256 text,byte_size integer,state text);
                insert into archive_importsource values('source','inbox','consumed.jpg','observation');
                insert into archive_sourceobservation values('observation','source',1,'{digest}',4);
                insert into archive_archiveoperation values('archive-op','observation','complete','','','birds/consumed.jpg','{digest}',4);
                insert into archive_managedoriginal values('archive','.archive-trash/consumed.jpg','{digest}',4,'archive-op');
                insert into archive_recognitionrun values('observation','{digest}',4,'queued');
            """)
        inventory = [{"root_role": "archive", "relative_path": ".archive-trash/consumed.jpg",
                      "sha256": digest, "size": 4}]
        result = backup._validate_active_dependencies(database, inventory)
        self.assertEqual(result["available_sources"], 1)
        self.assertEqual(result["active_recognition_runs"], 1)

    def test_verify_rejects_symlink_directory_before_reading_manifest(self):
        set_path = self.destination / "00000000-0000-0000-0000-000000000000"
        set_path.mkdir()
        (set_path / "manifest-v1.json").write_text("not-json")
        (set_path / "roots").symlink_to(self.roots["archive"], target_is_directory=True)
        with self.assertRaisesRegex(backup.BackupError, "symlink_or_special"):
            backup.verify(set_path)

    def test_verify_detects_file_tampering_and_missing_complete(self):
        (self.roots["archive"] / "bird.jpg").write_bytes(b"original")
        session = self.session()
        set_path = Path(backup.create(session["session_id"], self.destination)["set"])
        (set_path / "roots" / "archive" / "bird.jpg").write_bytes(b"changed")
        with self.assertRaisesRegex(backup.BackupError, "file_mismatch"):
            backup.verify(set_path)
        (set_path / backup.COMPLETE).unlink()
        with self.assertRaisesRegex(backup.BackupError, "incomplete"):
            backup.verify(set_path)

    def test_command_contract_uses_json_for_request_status_verify_and_resume(self):
        output = io.StringIO()
        call_command("backuparchive", "request", stdout=output)
        requested = json.loads(output.getvalue())
        self.assertEqual(requested["status"], "requested")
        output = io.StringIO()
        call_command("backuparchive", "status", stdout=output)
        self.assertEqual(json.loads(output.getvalue())["session_id"], requested["session_id"])
        with self.assertRaisesRegex(CommandError, "maintenance_supervisor_not_drained"):
            call_command("backuparchive", "quiesce", "--session", requested["session_id"])
        maintenance._mutate_state(requested["session_id"], lambda current: {
            **current, "supervisor_ack": {"session_id": requested["session_id"], "status": "drained"},
        })
        output = io.StringIO()
        call_command("backuparchive", "quiesce", "--session", requested["session_id"], stdout=output)
        self.assertEqual(json.loads(output.getvalue()),
                         {"status": "quiescent", "session_id": requested["session_id"]})
        self.assertEqual(maintenance.status()["status"], "active")
        set_path = Path(backup.create(requested["session_id"], self.destination)["set"])
        output = io.StringIO()
        call_command("backuparchive", "verify", "--set", str(set_path), stdout=output)
        self.assertEqual(json.loads(output.getvalue())["status"], "verified")
        output = io.StringIO()
        call_command("backuparchive", "resume", "--session", requested["session_id"], stdout=output)
        self.assertEqual(json.loads(output.getvalue())["status"], "resumed")
