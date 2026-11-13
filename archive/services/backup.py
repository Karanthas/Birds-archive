"""维护屏障内的一致备份；集合以最后写入的 COMPLETE.json 为唯一发布边界。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import time
import uuid
from collections import Counter
from contextlib import closing
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.db import connections

from archive.services import maintenance, model_packages
from archive.services.deployment import NAMES, REGISTRY, filesystem_identity, validate_deployment


SCHEMA = 1
MANIFEST = "manifest-v1.json"
COMPLETE = "COMPLETE.json"
BLOCK = 1024 * 1024
EPHEMERAL_DATA = {
    "archive.sqlite3", "archive.sqlite3-wal", "archive.sqlite3-shm",
    "maintenance-v1.json", "maintenance-state.lock", "maintenance.lock",
    "maintenance-shared.lock", "worker.lock", "recognition.lock", "deployment-migrate.lock",
}


class BackupError(RuntimeError):
    pass


def _fail(code):
    raise BackupError(code)


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(BLOCK):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path, value, *, exclusive=True):
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    flags |= os.O_EXCL if exclusive else os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_canonical_json(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _path_overlap(left, right):
    left, right = Path(left), Path(right)
    return left == right or left in right.parents or right in left.parents


def _identity_overlap(left, right):
    if left.get("device") != right.get("device"):
        return False
    a, b = PurePosixPath("/" + str(left.get("root", "")).lstrip("/")), PurePosixPath(
        "/" + str(right.get("root", "")).lstrip("/"))
    return a == b or a in b.parents or b in a.parents


def _roots_and_destination(destination):
    roots = {}
    for role in NAMES:
        raw = Path(getattr(settings, "BIRDS_" + role.upper() + "_ROOT"))
        if not raw.is_absolute() or raw.is_symlink():
            _fail("backup_source_root_invalid")
        resolved = raw.resolve(strict=True)
        if resolved != raw:
            _fail("backup_source_root_alias")
        roots[role] = {"path": resolved, "filesystem": filesystem_identity(resolved)}
    destination = Path(destination)
    if not destination.is_absolute() or destination.is_symlink():
        _fail("backup_destination_invalid")
    destination = destination.resolve(strict=True)
    if not destination.is_dir():
        _fail("backup_destination_invalid")
    identity = filesystem_identity(destination)
    for value in roots.values():
        if _path_overlap(destination, value["path"]) or _identity_overlap(identity, value["filesystem"]):
            _fail("backup_destination_overlaps_source")
    return roots, destination, identity


def _fingerprint(info):
    return {"dev": info.st_dev, "ino": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def _same_file(left, right):
    return (_fingerprint(left) == _fingerprint(right) and stat.S_ISREG(left.st_mode)
            and stat.S_ISREG(right.st_mode))


def _scan(roots, directory_rows=None):
    rows = []
    database = Path(connections["default"].settings_dict["NAME"])
    for role in NAMES:
        root = roots[role]["path"]
        def failed(error):
            raise BackupError("backup_source_walk_failed") from error
        for directory, directories, files in os.walk(root, topdown=True, followlinks=False, onerror=failed):
            base = Path(directory)
            for name in list(directories):
                info = os.lstat(base / name)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    _fail("backup_source_special_file")
                if directory_rows is not None:
                    directory_rows.append({"root_role": role, "relative_path": (base / name).relative_to(root).as_posix(),
                                           "identity": {"dev": info.st_dev, "ino": info.st_ino}})
            for name in sorted(files):
                path = base / name
                relative = path.relative_to(root).as_posix()
                if role == "data" and (relative in EPHEMERAL_DATA or
                        path.parent == database.parent and path.name in {
                            database.name, database.name + "-wal", database.name + "-shm", database.name + "-journal"}):
                    continue
                info = os.lstat(path)
                if not stat.S_ISREG(info.st_mode):
                    _fail("backup_source_special_file")
                rows.append({"role": role, "relative_path": relative, "source": path,
                             "stat": info, "inode": (info.st_dev, info.st_ino)})
    rows.sort(key=lambda row: (row["role"], row["relative_path"]))
    observed = Counter(row["inode"] for row in rows)
    if any(row["stat"].st_nlink != observed[row["inode"]] for row in rows):
        _fail("backup_external_hardlink")
    return rows


def _copy_inventory(rows, set_path):
    copied, links, result = {}, {}, []
    for row in rows:
        target = set_path / "roots" / row["role"] / row["relative_path"]
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        group = links.setdefault(row["inode"], f"link-{len(links) + 1}")
        if row["inode"] in copied:
            os.link(copied[row["inode"]][0], target, follow_symlinks=False)
            digest = result[copied[row["inode"]][1]]["sha256"]
        else:
            source_fd = os.open(row["source"], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            destination_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            digest = hashlib.sha256()
            try:
                before = os.fstat(source_fd)
                with os.fdopen(source_fd, "rb", closefd=False) as source, os.fdopen(
                    destination_fd, "wb", closefd=False) as destination:
                    while block := source.read(BLOCK):
                        digest.update(block)
                        destination.write(block)
                    destination.flush()
                    os.fsync(destination.fileno())
                after = os.fstat(source_fd)
                if not _same_file(before, after) or not _same_file(row["stat"], after):
                    _fail("backup_source_changed")
            finally:
                os.close(source_fd)
                os.close(destination_fd)
            digest = digest.hexdigest()
            copied[row["inode"]] = (target, len(result))
        result.append({"root_role": row["role"], "relative_path": row["relative_path"],
                       "size": row["stat"].st_size, "sha256": digest,
                       "source_fingerprint": _fingerprint(row["stat"]), "hardlink_group": group})
    return result


def _snapshot_database(destination):
    source = Path(connections["default"].settings_dict["NAME"])
    connections["default"].close()
    if not source.is_absolute() or source.is_symlink() or not source.is_file():
        _fail("backup_database_source_invalid")
    destination.parent.mkdir(parents=True, mode=0o700)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as live, \
            closing(sqlite3.connect(destination)) as copy:
        live.backup(copy)
        copy.execute("pragma journal_mode=DELETE")
        copy.commit()
    fd = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return _database_baseline(destination)


def _database_baseline(path):
    tables = []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as database:
        if database.execute("pragma integrity_check").fetchone() != ("ok",):
            _fail("backup_database_integrity")
        if database.execute("pragma foreign_key_check").fetchone() is not None:
            _fail("backup_database_foreign_key")
        names = [row[0] for row in database.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            columns = [row[1] for row in database.execute(f"pragma table_info({quoted})")]
            digest = hashlib.sha256()
            count = 0
            for row in database.execute(f"select * from {quoted} order by rowid"):
                encoded = [{"bytes": value.hex()} if isinstance(value, bytes) else value for value in row]
                digest.update(_canonical_json(encoded) + b"\n")
                count += 1
            tables.append({"table": name, "columns": columns, "rows": count,
                           "logical_digest": digest.hexdigest()})
    return {"path": "database/archive.sqlite3", "size": path.stat().st_size,
            "sha256": _sha256(path), "tables": tables}


def _require_path(index, role, relative, code, *, sha256=None, size=None):
    row = index.get((role, relative))
    if row is None:
        _fail(code)
    if sha256 and row["sha256"] != sha256:
        _fail(code)
    if size is not None and row["size"] != size:
        _fail(code)


def _candidate_matches(index, role, relative, *, sha256, size):
    row = index.get((role, relative))
    return row is not None and row["sha256"] == sha256 and row["size"] == size


def _decode(value):
    try:
        return json.loads(value or "{}")
    except (ValueError, TypeError):
        _fail("backup_database_journal_invalid")


def _validate_active_dependencies(database_path, inventory):
    index = {(row["root_role"], row["relative_path"]): row for row in inventory}
    counts = {}
    with closing(sqlite3.connect(database_path.as_uri() + "?mode=ro&immutable=1", uri=True)) as database:
        originals = list(database.execute(
            "select root_key,relative_path,sha256,byte_size from archive_managedoriginal"))
        counts["managed_originals"] = len(originals)
        for role, relative, digest, size in originals:
            _require_path(index, role, relative, "backup_managed_original_missing", sha256=digest, size=size)

        archive_rows = list(database.execute("""
            select o.source_observation_id,o.stage,o.claim_relative_path,o.temporary_relative_path,
                   o.target_relative_path,o.expected_sha256,o.byte_size,s.root_key,s.relative_path,
                   m.root_key,m.relative_path
            from archive_archiveoperation o
            join archive_sourceobservation so on so.id=o.source_observation_id
            join archive_importsource s on s.id=so.source_id
            left join archive_managedoriginal m on m.operation_id=o.id
        """))
        active_archive = [row for row in archive_rows if row[1] != "complete"]
        counts["active_archive_operations"] = len(active_archive)
        consumed_observations = set()
        for (observation_id, stage, claim, temporary, target, digest, size, source_role,
             source, managed_role, managed_path) in archive_rows:
            if stage in ("queued", "protecting", "protected"):
                _require_path(index, source_role, source, "backup_archive_source_missing", sha256=digest, size=size)
            elif stage == "claim_intent":
                source_ok = _candidate_matches(index, source_role, source, sha256=digest, size=size)
                claim_ok = _candidate_matches(index, "claim", claim, sha256=digest, size=size)
                if not source_ok and not claim_ok:
                    _fail("backup_claim_intent_missing")
                if claim_ok:
                    consumed_observations.add(observation_id)
            elif stage in ("claimed", "verified", "publish_intent", "published", "registered", "temp_cleaned"):
                _require_path(index, "claim", claim, "backup_claim_missing", sha256=digest, size=size)
                consumed_observations.add(observation_id)
            if stage in ("protected", "claim_intent", "claimed", "verified", "publish_intent"):
                _require_path(index, "archive", temporary, "backup_archive_temporary_missing",
                              sha256=digest, size=size)
            if stage in ("published", "registered", "temp_cleaned", "source_cleanup_intent", "source_cleaned"):
                _require_path(index, "archive", target, "backup_published_original_missing",
                              sha256=digest, size=size)
                consumed_observations.add(observation_id)
            if stage == "complete":
                # A completed archive may have moved again through correction or trash.
                _require_path(index, managed_role, managed_path, "backup_managed_original_missing",
                              sha256=digest, size=size)
                consumed_observations.add(observation_id)

        active_management = list(database.execute("""
            select stage,source_snapshot,target_relative_path,target_fingerprint,retry_context
            from archive_managementoperation where status in ('pending','running','failed','blocked')
        """))
        counts["active_management_operations"] = len(active_management)
        for stage, source_value, target, target_fingerprint, retry_value in active_management:
            source, retry = _decode(source_value), _decode(retry_value)
            if stage in ("accepted", "prepared", "published"):
                _require_path(index, source.get("root_key", "archive"), source.get("relative_path", ""),
                              "backup_management_source_missing", sha256=source.get("sha256"),
                              size=source.get("byte_size"))
            if stage in ("published", "registered", "cleaned", "completed") or _decode(target_fingerprint):
                _require_path(index, "archive", target, "backup_management_target_missing",
                              sha256=source.get("sha256"), size=source.get("byte_size"))
            if stage in ("prepared", "published") and retry.get("temporary_fingerprint"):
                _require_path(index, "archive", retry.get("temporary_relative_path", ""),
                              "backup_management_temporary_missing", sha256=source.get("sha256"),
                              size=source.get("byte_size"))

        available = list(database.execute("""
            select o.id,s.root_key,s.relative_path,o.sha256,o.byte_size from archive_importsource s
            join archive_sourceobservation o on o.id=s.current_observation_id where o.available=1
        """))
        counts["available_sources"] = len(available)
        for observation_id, role, relative, digest, size in available:
            if observation_id in consumed_observations:
                continue
            _require_path(index, role, relative, "backup_available_source_missing", sha256=digest, size=size)

        recognition = list(database.execute("""
            select o.id,s.root_key,s.relative_path,r.photo_sha256,r.byte_size from archive_recognitionrun r
            join archive_sourceobservation o on o.id=r.source_observation_id
            join archive_importsource s on s.id=o.source_id
            where r.state in ('queued','running','retry_wait')
        """))
        counts["active_recognition_runs"] = len(recognition)
        for observation_id, role, relative, digest, size in recognition:
            if observation_id in consumed_observations:
                continue
            _require_path(index, role, relative, "backup_recognition_source_missing", sha256=digest, size=size)
    for row in inventory:
        if row["root_role"] != "data" or not row["relative_path"].startswith("uploads/") \
                or not row["relative_path"].endswith(".json"):
            continue
        try:
            intent = json.loads((Path(settings.BIRDS_DATA_ROOT) / row["relative_path"]).read_text())
            state, temporary, target = intent["state"], intent.get("temporary", ""), intent.get("target", "")
        except (OSError, ValueError, KeyError, TypeError):
            _fail("backup_upload_intent_invalid")
        request = intent.get("request", {})
        digest, size = request.get("sha256"), request.get("length")
        if not isinstance(digest, str) or len(digest) != 64 or type(size) is not int or size < 0:
            _fail("backup_upload_intent_invalid")
        temporary_key = ".delivery/" + temporary
        if state == "receiving":
            if ("inbox", temporary_key) in index and not _candidate_matches(
                    index, "inbox", temporary_key, sha256=digest, size=size):
                _fail("backup_upload_temporary_mismatch")
        if state == "prepared":
            _require_path(index, "inbox", temporary_key, "backup_upload_temporary_missing",
                          sha256=digest, size=size)
        if state == "publishing":
            temporary_ok = _candidate_matches(index, "inbox", temporary_key, sha256=digest, size=size)
            target_ok = _candidate_matches(index, "inbox", target, sha256=digest, size=size)
            if not temporary_ok and not target_ok:
                _fail("backup_upload_publish_evidence_missing")
    return counts


def _copy_models(database_path, set_path, destination_identity):
    result = []
    with closing(sqlite3.connect(database_path.as_uri() + "?mode=ro&immutable=1", uri=True)) as database:
        artifacts = list(database.execute("""
            select manifest_digest,provider,version,revision,manifest from archive_modelartifact
            where is_current=1 or id in (
                select artifact_id from archive_recognitionrun
                where artifact_id is not null and state in ('queued','running','retry_wait')
            )
        """))
    for manifest_digest, provider, version, revision, manifest_value in artifacts:
        manifest = _decode(manifest_value)
        checked = model_packages.verify_manifest(manifest, require_features=True)
        if model_packages.digest(manifest) != manifest_digest:
            _fail("backup_model_manifest_digest_mismatch")
        model_root = Path(manifest["local_root"]).resolve(strict=True)
        if _identity_overlap(filesystem_identity(model_root), destination_identity):
            _fail("backup_destination_overlaps_dependency")
        target_root = set_path / "dependencies" / "models" / manifest_digest
        target_root.mkdir(parents=True, mode=0o700)
        files = []
        for row in manifest["files"]:
            source, target = model_root / row["path"], target_root / row["path"]
            shutil.copyfile(source, target, follow_symlinks=False)
            with target.open("rb") as copied:
                os.fsync(copied.fileno())
            if target.stat().st_size != row["size_bytes"] or _sha256(target) != row["sha256"]:
                _fail("backup_model_copy_mismatch")
            files.append({key: row[key] for key in ("role", "path", "sha256", "size_bytes")})
        result.append({"manifest_digest": manifest_digest, "provider": provider,
                       "version": version, "revision": revision,
                       "relative_root": f"dependencies/models/{manifest_digest}", "files": files,
                       "verified_labels": len(checked.get("labels", []))})
    return result


def _deployment_snapshot(roots):
    try:
        record = json.loads((roots["data"]["path"] / REGISTRY).read_text())
    except (OSError, ValueError, TypeError):
        _fail("backup_deployment_record_invalid")
    runtime = record.get("runtime", {})
    return {"deployment_id": record.get("deployment_id"), "schema": record.get("schema"),
            "migrations": record.get("migrations", []),
            "runtime": {key: runtime.get(key) for key in (
                "BIRDS_SOURCE_SHA", "BIRDS_BASE_IMAGE_ID", "BIRDS_PYTHON_TREE_SHA",
                "python", "sqlite_source_id")}}


def create(session_id, destination):
    with maintenance.acquire(session_id, owner="backuparchive", require_supervisor=True):
        validate_deployment()
        roots, destination, destination_identity = _roots_and_destination(destination)
        set_id = str(uuid.uuid4())
        set_path = destination / set_id
        set_path.mkdir(mode=0o700)
        _fsync_directory(destination)
        directories, after_directories = [], []
        identities = {role: roots[role]["path"].stat() for role in NAMES}
        before = _scan(roots, directories)
        database = _snapshot_database(set_path / "database" / "archive.sqlite3")
        inventory = _copy_inventory(before, set_path)
        database_path = set_path / "database" / "archive.sqlite3"
        dependencies = _validate_active_dependencies(database_path, inventory)
        models = _copy_models(database_path, set_path, destination_identity)
        after = _scan(roots, after_directories)
        if [(r["role"], r["relative_path"], _fingerprint(r["stat"])) for r in before] != [
                (r["role"], r["relative_path"], _fingerprint(r["stat"])) for r in after] or sorted(
                    directories, key=lambda r: (r["root_role"], r["relative_path"])) != sorted(
                    after_directories, key=lambda r: (r["root_role"], r["relative_path"])):
            _fail("backup_source_changed")
        for role, before_root in identities.items():
            after_root = roots[role]["path"].stat()
            if (before_root.st_dev, before_root.st_ino) != (after_root.st_dev, after_root.st_ino):
                _fail("backup_source_changed")
        manifest = {"schema": SCHEMA, "format": "birds-backup-v1", "set_id": set_id,
                    "created_at": time.time(), "deployment": _deployment_snapshot(roots),
                    "source_roots": {role: {"filesystem": value["filesystem"], "path": str(value["path"]),
                        "identity": {"dev": identities[role].st_dev, "ino": identities[role].st_ino}}
                        for role, value in roots.items()},
                    "database": database, "files": inventory, "directories": directories, "dependencies": dependencies,
                    "models": models, "omissions": sorted(EPHEMERAL_DATA)}
        _write_json(set_path / MANIFEST, manifest)
        manifest_sha256 = _sha256(set_path / MANIFEST)
        _verify_set(set_path, require_complete=False)
        _fsync_tree(set_path)
        _write_json(set_path / COMPLETE, {"schema": SCHEMA, "manifest_sha256": manifest_sha256,
                                         "set_id": set_id, "published_at": time.time()})
        try:
            _fsync_directory(set_path)
            _fsync_directory(destination)
        except BaseException:
            try:
                (set_path / COMPLETE).unlink()
                _fsync_directory(set_path)
            except OSError:
                pass
            raise
        return {"status": "complete", "set": str(set_path)}


def _safe_set(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        _fail("backup_set_invalid")
    resolved = path.resolve(strict=True)
    if resolved != path or not resolved.is_dir():
        _fail("backup_set_invalid")
    return resolved


def _safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value:
        _fail("backup_manifest_path_invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        _fail("backup_manifest_path_invalid")
    return path


def _walk_regular(root):
    result = set()
    def failed(error):
        raise BackupError("backup_verify_walk_failed") from error
    for directory, directories, files in os.walk(root, followlinks=False, onerror=failed):
        base = Path(directory)
        for name in directories:
            if (base / name).is_symlink():
                _fail("backup_set_symlink")
        for name in files:
            path = base / name
            if path.is_symlink() or not path.is_file():
                _fail("backup_set_special_file")
            result.add(path.relative_to(root).as_posix())
    return result


def _preflight_set_tree(root):
    files, directories_seen = set(), set()
    def failed(error):
        raise BackupError("backup_verify_walk_failed") from error
    for directory, directories, names in os.walk(root, topdown=True, followlinks=False, onerror=failed):
        base = Path(directory)
        if base != root:
            directories_seen.add(base.relative_to(root).as_posix())
        for name in directories:
            info = os.lstat(base / name)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                _fail("backup_set_symlink_or_special")
        for name in names:
            path = base / name
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                _fail("backup_set_symlink_or_special")
            files.add(path.relative_to(root).as_posix())
    return files, directories_seen


def _parent_directories(files):
    result = set()
    for name in files:
        parent = PurePosixPath(name).parent
        while parent != PurePosixPath("."):
            result.add(parent.as_posix())
            parent = parent.parent
    return result


def _fsync_tree(root):
    directories = []
    for directory, _, files in os.walk(root, topdown=False, followlinks=False):
        base = Path(directory)
        directories.append(base)
        for name in files:
            fd = os.open(base / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    for directory in directories:
        _fsync_directory(directory)


def _verify_set(set_path, *, require_complete=True):
    set_path = _safe_set(set_path)
    observed_set_files, observed_set_directories = _preflight_set_tree(set_path)
    try:
        manifest = json.loads((set_path / MANIFEST).read_text())
    except (OSError, ValueError, TypeError):
        _fail("backup_manifest_invalid")
    if manifest.get("schema") != SCHEMA or manifest.get("format") != "birds-backup-v1" \
            or manifest.get("set_id") != set_path.name:
        _fail("backup_manifest_invalid")
    if require_complete:
        try:
            complete = json.loads((set_path / COMPLETE).read_text())
        except (OSError, ValueError, TypeError):
            _fail("backup_incomplete")
        if complete.get("schema") != SCHEMA or complete.get("set_id") != set_path.name \
                or complete.get("manifest_sha256") != _sha256(set_path / MANIFEST):
            _fail("backup_complete_marker_invalid")
    if manifest.get("database", {}).get("path") != "database/archive.sqlite3":
        _fail("backup_manifest_path_invalid")
    groups, group_paths, inode_groups, expected = {}, {}, {}, set()
    expected_set_files = {MANIFEST, "database/archive.sqlite3"}
    if require_complete:
        expected_set_files.add(COMPLETE)
    for row in manifest.get("files", []):
        if row.get("root_role") not in NAMES:
            _fail("backup_manifest_role_invalid")
        relative = _safe_relative(row.get("relative_path"))
        expected.add(f"{row['root_role']}/{relative.as_posix()}")
        expected_set_files.add(f"roots/{row['root_role']}/{relative.as_posix()}")
        path = set_path / "roots" / row["root_role"] / Path(*relative.parts)
        try:
            info = os.lstat(path)
        except OSError:
            _fail("backup_file_missing")
        if not stat.S_ISREG(info.st_mode) or info.st_size != row["size"] or _sha256(path) != row["sha256"]:
            _fail("backup_file_mismatch")
        inode = (info.st_dev, info.st_ino)
        groups.setdefault(row["hardlink_group"], []).append(inode)
        group_paths.setdefault(row["hardlink_group"], path)
        if inode in inode_groups and inode_groups[inode] != row["hardlink_group"]:
            _fail("backup_hardlink_mismatch")
        inode_groups[inode] = row["hardlink_group"]
    if any(len(set(inodes)) != 1 or group_paths[group].stat().st_nlink != len(inodes)
           for group, inodes in groups.items()):
        _fail("backup_hardlink_mismatch")
    roots_path = set_path / "roots"
    if _walk_regular(roots_path) != expected:
        _fail("backup_extra_or_missing_file")
    database = _database_baseline(set_path / manifest["database"]["path"])
    if database != manifest["database"]:
        _fail("backup_database_mismatch")
    for model in manifest.get("models", []):
        relative_root = _safe_relative(model["relative_root"])
        if relative_root.parts[:2] != ("dependencies", "models") or len(relative_root.parts) != 3:
            _fail("backup_manifest_path_invalid")
        root = set_path / Path(*relative_root.parts)
        expected_models = set()
        for row in model["files"]:
            relative = _safe_relative(row["path"])
            expected_models.add(relative.as_posix())
            expected_set_files.add(f"{relative_root.as_posix()}/{relative.as_posix()}")
            path = root / Path(*relative.parts)
            if not path.is_file() or path.stat().st_size != row["size_bytes"] or _sha256(path) != row["sha256"]:
                _fail("backup_model_mismatch")
        if _walk_regular(root) != expected_models:
            _fail("backup_model_mismatch")
    if observed_set_files != expected_set_files:
        _fail("backup_extra_or_missing_file")
    if observed_set_directories != _parent_directories(expected_set_files):
        _fail("backup_extra_or_missing_directory")
    for relative in observed_set_files:
        if not relative.startswith("roots/") and os.lstat(set_path / relative).st_nlink != 1:
            _fail("backup_unexpected_hardlink")
    return manifest


def verify(set_path):
    manifest = _verify_set(set_path)
    return {"status": "verified", "set": str(Path(set_path)), "set_id": manifest["set_id"]}
