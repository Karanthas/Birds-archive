"""只读补扫；稳定观察是普通复制的启发式，不证明写句柄已关闭。"""
import os
import stat
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from archive.models import ImportSource, SourceObservation, Job, Photo, ManagedOriginal
from archive.services.images import content_hash, ImageRejected
from archive.services.paths import (open_root, open_regular_beneath, fingerprint, UnsafePath,
                                    verify_root_identity, write_scan_status, root_identity,
                                    validate_root_isolation, RootsNotIsolated)


def jpeg_paths(root_fd, prefix="", issues=None, forbidden_identity=None, ancestors=(), forbidden_identities=()):
    identity = root_identity(root_fd)
    key = (identity['dev'], identity['ino'])
    if identity == forbidden_identity or identity in forbidden_identities or key in ancestors:
        raise RootsNotIsolated()
    try:
        with os.scandir(root_fd) as entries:
            names = sorted(entry.name for entry in entries)
    except OSError:
        if issues is None or not prefix:
            raise
        issues.append(prefix)
        return
    for name in names:
        relative = f"{prefix}/{name}" if prefix else name
        try:
            info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except OSError:
            if issues is None:
                raise
            issues.append(relative)
            continue
        if stat.S_ISDIR(info.st_mode):
            try:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root_fd)
            except OSError:
                if issues is None:
                    raise
                issues.append(relative)
                continue
            try:
                yield from jpeg_paths(child, relative, issues, forbidden_identity, (*ancestors, key), forbidden_identities)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode) and name.lower().endswith((".jpg", ".jpeg")):
            yield relative


def valid_handoff():
    return (settings.BIRDS_HANDOFF_MODE in ("copy_compatible", "published")
            and (settings.BIRDS_HANDOFF_MODE != "published" or settings.BIRDS_PUBLISHED_CONTRACT)
            and 0 <= settings.BIRDS_STABILITY_SECONDS < float("inf")
            and 0 < settings.BIRDS_SCAN_SECONDS < float("inf"))


def open_managed_original(managed):
    """交接核验过并回到开头的同一FD；失败时关闭，成功后由调用者关闭。

    已绑定操作必须仍指向登记的图库根。外部写入者仍须在部署时被权限隔离，
    此次核验不能使持有其他写句柄的同一inode永久不可变。
    """
    root = getattr(settings, 'BIRDS_ARCHIVE_ROOT', '')
    if not root or managed.root_key != 'archive':
        raise UnsafePath('original_unavailable')
    root_fd, original = None, None
    try:
        snapshot = managed.operation.roots_snapshot
        binding = snapshot.get('archive') if snapshot.get('binding_state') == 'bound' else None
        if managed.location_revision:
            latest = managed.last_management_operation
            if (latest is None or latest.photo_id != managed.photo_id
                    or latest.stage not in ('registered', 'cleaned', 'completed')
                    or latest.source_location_revision + 1 != managed.location_revision
                    or latest.target_relative_path != managed.relative_path
                    or latest.target_fingerprint != managed.fingerprint):
                raise UnsafePath('original_unavailable')
            binding = latest.root_identity
        expected_fingerprint = managed.fingerprint
        root_fd = open_root(root)
        actual_root = root_identity(root_fd)
        if binding:
            from archive.services.restore import RestoreError, effective_managed_location
            try:
                binding, expected_fingerprint = effective_managed_location(managed, binding)
            except RestoreError:
                raise UnsafePath('original_unavailable') from None
            if os.path.abspath(root) != binding['path'] or actual_root != binding['identity']:
                raise UnsafePath('original_unavailable')
        original = open_regular_beneath(root_fd, managed.relative_path)
        before = fingerprint(os.fstat(original.fileno()))
        journal_link_change = managed.location_revision > 0 or managed.photo.management_operations.filter(
            status__in=['pending', 'running', 'failed', 'blocked'],
            source_location_revision=managed.location_revision,
            stage__in=['prepared', 'published']).exists()
        matches = before == expected_fingerprint
        if journal_link_change:
            matches = all(before.get(key) == expected_fingerprint.get(key)
                          for key in ('dev', 'ino', 'size', 'mtime_ns'))
        if not matches:
            raise UnsafePath('original_unavailable')
        digest, size = content_hash(original)
        if (digest != managed.sha256 or size != managed.byte_size
                or before != fingerprint(os.fstat(original.fileno()))):
            raise UnsafePath('original_unavailable')
        with open_regular_beneath(root_fd, managed.relative_path) as current:
            if fingerprint(os.fstat(current.fileno())) != before:
                raise UnsafePath('original_unavailable')
        current_root = open_root(root)
        try:
            if root_identity(current_root) != root_identity(root_fd):
                raise UnsafePath('original_unavailable')
        finally:
            os.close(current_root)
        original.seek(0)
        result, original = original, None
        return result
    finally:
        if original is not None:
            original.close()
        if root_fd is not None:
            os.close(root_fd)


def managed_original_available(managed):
    """只读可用性探针；下载须交接open_managed_original返回的FD，不能再打开路径。"""
    try:
        from archive.services.archiving import open_current_original
        original, _ = open_current_original(managed.photo)
        with original:
            return True
    except (OSError, UnsafePath, ImageRejected, ValueError, KeyError, TypeError):
        return False


def refresh_original_availability(photo_ids):
    for photo_id in set(photo_ids) - {None}:
        managed = ManagedOriginal.objects.filter(photo_id=photo_id).first()
        available = managed_original_available(managed) if managed else SourceObservation.objects.filter(
            photo_id=photo_id, available=True).exists()
        Photo.objects.filter(pk=photo_id).update(original_available=available)
        if managed is not None and not available:
            # 扫描发现异常也留下可重启读取的待对账事实，而非仅在下载时记录。
            from archive.services.archiving import record_original_unavailable
            record_original_unavailable(managed.photo)


def mark_sources_unavailable(sources):
    observations = SourceObservation.objects.filter(current_for_sources__in=sources)
    photo_ids = list(observations.values_list("photo_id", flat=True))
    observations.update(available=False)
    Job.objects.filter(source__in=sources, kind='import', state__in=["pending", "queued", "running", "retry_wait"]).update(
        state="stale", stage="stale", error_code="source_changed",
        safe_message="来源已变化，将重新检查。", updated_at=timezone.now())
    sources.update(state="unavailable")
    refresh_original_availability(photo_ids)


def new_observation(source, observed, now):
    previous = source.current_observation
    if previous is not None:
        previous.available = False
        previous.save(update_fields=["available"])
        Job.objects.filter(source=source, source_version=previous.version, kind='import',
                           state__in=["pending", "queued", "running", "retry_wait"]).update(
                               state="stale", stage="stale", error_code="source_changed",
                               safe_message="来源已变化，将重新检查。", updated_at=now)
        source.observed_version = previous.version + 1
    observation = SourceObservation.objects.create(source=source, version=source.observed_version,
        fingerprint=observed, byte_size=observed["size"], first_seen=now, last_seen=now, stable_since=now)
    source.current_observation = observation
    source.fingerprint = observed
    source.sha256 = ""
    source.photo = None
    source.state = "waiting"
    source.save()
    if previous is not None:
        refresh_original_availability([previous.photo_id])
    return observation


def observe_file(root_fd, relative):
    now = timezone.now()
    with open_regular_beneath(root_fd, relative) as source_file:
        observed = fingerprint(os.fstat(source_file.fileno()))
        with transaction.atomic():
            source, _ = ImportSource.objects.get_or_create(root_key="inbox", relative_path=relative,
                defaults={"fingerprint": observed, "state": "waiting"})
            observation = source.current_observation
            if observation is None or observation.fingerprint != observed or source.state in ("unavailable", "stale"):
                observation = new_observation(source, observed, now)
            else:
                observation.last_seen = now
                observation.stable_count = min(2, observation.stable_count + 1)
                observation.save(update_fields=["last_seen", "stable_count"])
            ready = settings.BIRDS_HANDOFF_MODE == "published" or (
                observation.stable_count >= 2 and (now - observation.stable_since).total_seconds()
                >= settings.BIRDS_STABILITY_SECONDS)
            version = observation.version
        if not ready:
            return 0
        try:
            digest, size = content_hash(source_file)
        except ImageRejected:
            # worker会给出精确有限解码错误；不将无法哈希输入认作已有资产。
            digest, size = "", observed["size"]
        if fingerprint(os.fstat(source_file.fileno())) != observed:
            return 0
        with open_regular_beneath(root_fd, relative) as current:
            if fingerprint(os.fstat(current.fileno())) != observed:
                return 0
    with transaction.atomic():
        source = ImportSource.objects.select_related("current_observation").get(pk=source.pk)
        observation = source.current_observation
        if observation.version != version or observation.fingerprint != observed:
            return 0
        if observation.sha256 and observation.sha256 != digest:
            observation = new_observation(source, observed, now)
            if settings.BIRDS_HANDOFF_MODE == "copy_compatible":
                return 0
        observation.sha256, observation.byte_size = digest, size
        source.sha256 = digest
        if observation.state == "waiting":
            observation.state = source.state = "pending"
            source.save(update_fields=["sha256", "state", "updated_at"])
            observation.save(update_fields=["sha256", "byte_size", "state"])
            _, added = Job.objects.get_or_create(source=source, source_version=observation.version, kind='import',
                                                 defaults={"expected_sha256": digest})
            return int(added)
        observation.save(update_fields=["sha256", "byte_size"])
    return 0


def scan_once(root_fd=None, data_fd=None):
    owned_root = root_fd is None
    owned_data = data_fd is None
    discovered = 0
    try:
        if owned_data:
            data_fd = open_root(settings.BIRDS_DATA_ROOT)
        if owned_root:
            root_fd = open_root(settings.BIRDS_INBOX_ROOT)
        validate_root_isolation(root_fd, data_fd, settings.BIRDS_INBOX_ROOT, settings.BIRDS_DATA_ROOT)
        if not valid_handoff():
            write_scan_status(data_fd, "handoff_invalid")
            return 0
        # 先检查整次遍历的实际FD，挂载别名被拒绝时还没有来源／Job写入。
        issues = []
        forbidden = []
        for setting in ('BIRDS_ARCHIVE_ROOT', 'BIRDS_CLAIM_ROOT'):
            configured = getattr(settings, setting, '')
            if configured:
                try:
                    managed_fd = open_root(configured)
                except FileNotFoundError:
                    continue
                try:
                    forbidden.append(root_identity(managed_fd))
                finally:
                    os.close(managed_fd)
        relatives = list(jpeg_paths(root_fd, issues=issues, forbidden_identity=root_identity(data_fd),
                                    forbidden_identities=forbidden))
        verify_root_identity(root_fd, settings.BIRDS_INBOX_ROOT, data_fd)
        # 不完整扫描不能把未走到的条目当消失；仅完整遍历才失效缺失来源。
        seen = set()
        for relative in relatives:
            seen.add(relative)
            try:
                # 局部导入避免intake→upload→archiving→intake循环。
                # 接收器持锁发布／清anchor期间不可登记尚会改变ctime的来源。
                from archive.services.upload import scan_guard
                with scan_guard(root_fd, data_fd, relative) as ready:
                    if ready:
                        discovered += observe_file(root_fd, relative)
            except (OSError, UnsafePath):
                issues.append(relative)
                with transaction.atomic():
                    mark_sources_unavailable(ImportSource.objects.filter(root_key="inbox", relative_path=relative))
        verify_root_identity(root_fd, settings.BIRDS_INBOX_ROOT, data_fd)
        if issues:
            with transaction.atomic():
                affected = Q(pk__in=[])
                for issue in issues:
                    affected |= Q(relative_path=issue) | Q(relative_path__startswith=issue + "/")
                mark_sources_unavailable(ImportSource.objects.filter(affected, root_key="inbox"))
        if not issues:
            with transaction.atomic():
                missing = ImportSource.objects.filter(root_key="inbox").exclude(relative_path__in=seen)
                mark_sources_unavailable(missing)
        write_scan_status(data_fd, "scan_incomplete" if issues else "")
    except RootsNotIsolated:
        raise
    except FileNotFoundError:
        if data_fd is not None:
            write_scan_status(data_fd, "root_missing")
    except PermissionError:
        if data_fd is not None:
            write_scan_status(data_fd, "root_permission")
    except UnsafePath:
        if data_fd is not None:
            write_scan_status(data_fd, "root_identity_changed")
    except (OSError, ValueError):
        if data_fd is not None:
            write_scan_status(data_fd, "scan_incomplete")
    finally:
        if owned_root and root_fd is not None:
            os.close(root_fd)
        if owned_data and data_fd is not None:
            os.close(data_fd)
    return discovered
