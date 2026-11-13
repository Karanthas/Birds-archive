import os
import uuid
import errno
import logging
import time
import json
from datetime import timedelta
from django.db import OperationalError
from django.db import transaction
from django.utils import timezone
from archive.models import Photo, ImportSource, SourceObservation, Job, ArchiveOperation
from archive.services.images import content_hash, inspect_open_jpeg, write_derivatives, ImageRejected
from archive.services.paths import open_regular_beneath, fingerprint, UnsafePath, verify_root_identity
from django.conf import settings
from archive.services.intake import refresh_original_availability

SAFE_ERRORS = {
    "image_bytes_exceeded": "文件超过配置字节上限，请检查限额或使用较小的 JPEG。",
    "image_pixels_exceeded": "照片超过配置像素上限，请检查限额或使用较小的 JPEG。",
    "image_not_jpeg": "仅接受 JPEG，请检查文件格式。",
    "image_invalid": "JPEG 不完整或损坏，请重新复制完整文件。",
    "source_changed": "文件发生变化，请等待写入结束后重新检查。",
    "source_unavailable": "暂时无法读取文件，请检查导入目录权限。",
    "processing_failed": "导入未完成，请检查目录权限和可用空间后重试。",
    "source_permission": "无法读取文件，请修复导入目录权限后重试。",
    "source_missing": "原文件不存在，请恢复同一文件后重试。",
    "storage_full": "缓存空间不足，请释放数据目录空间后重试。",
    "transient_io": "读写暂时失败，将按计划重试；持续失败请检查存储。",
    "database_busy": "数据库暂忙，将按计划重试。",
}
STATE_LABELS = {'pending':'待处理','queued':'待处理','running':'处理中','retry_wait':'等待重试',
                'succeeded':'已完成','failed':'失败','stale':'来源已变化', 'blocked':'待恢复',
                'awaiting_executor':'等待归档执行器'}
STAGE_LABELS = {'queued':'等待领取','pending':'等待领取','reading':'读取照片','derivatives':'生成预览',
                'complete':'导入完成','failed':'处理失败','stale':'来源已变化','retry_wait':'等待重试'}
ARCHIVE_STAGES = {'queued':'等待归档', 'protecting':'保护原图', 'protected':'原图已保护',
    'claim_intent':'等待交接', 'claimed':'已接管原图', 'verified':'原图已校验',
    'publish_intent':'准备发布', 'published':'已发布，待登记', 'registered':'已登记，待清理',
    'temp_cleaned':'保护别名已清理', 'source_cleanup_intent':'准备清理来源',
    'source_cleaned':'来源已清理，待复核', 'complete':'归档完成'}


def _effective_replay_fingerprint(relative_path, original):
    """Resolve only restore-proven source identity; keep every persisted value immutable."""
    from archive.services import restore
    try:
        return restore.effective_replay_fingerprint('inbox', relative_path, original)
    except restore.RestoreError as error:
        raise ImageRejected('source_changed') from error


def archive_failure(operation, code):
    from archive.services.archiving import check_archive_retry, archive_error_code, MESSAGES
    transient = code in ('database_busy', 'temporarily_unavailable')
    operation.refresh_from_db()
    if transient:
        try:
            check_archive_retry(operation)
        except (OSError, UnsafePath, ImageRejected, OperationalError) as error:
            code, transient = archive_error_code(error), False
    if code not in MESSAGES:
        code = 'sync_failed'
    job = Job.objects.get(pk=operation.job_id)
    cycle_attempts = operation.retry_context.get('cycle_attempts', job.attempts)
    state = 'blocked' if code in ('handoff_not_ready', 'archive_root_unavailable',
        'root_changed', 'source_changed', 'target_conflict') else 'failed'
    if transient and cycle_attempts < getattr(settings, 'RETRY_LIMIT', 3):
        state = 'retry_wait'
    delay = getattr(settings, 'RETRY_BASE_MS', 1000) * 2**max(0, cycle_attempts-1)
    def save():
        with transaction.atomic():
            ArchiveOperation.objects.filter(pk=operation.pk).update(last_error=code, updated_at=timezone.now())
            Job.objects.filter(pk=job.pk, state='running').update(state=state, error_code=code,
                safe_message=MESSAGES[code], next_attempt_at=timezone.now()+timedelta(milliseconds=delay if state=='retry_wait' else 0),
                updated_at=timezone.now())
            Photo.objects.filter(pk=operation.photo_id).exclude(state='archived').update(state='archive_failed')
    database_retry(save)


def retry_archive_job(job, version):
    from archive.services.archiving import check_archive_retry, archive_error_code
    if version != job.source_version:
        return job, False, 'version_conflict'
    if job.state in ('queued', 'running', 'retry_wait'):
        return job, True, 'already_queued'
    if job.state not in ('failed', 'blocked'):
        return job, False, 'current_state'
    operation = ArchiveOperation.objects.select_related('source_observation__source').get(job=job)
    try:
        check_archive_retry(operation)
    except (OSError, UnsafePath, ImageRejected, OperationalError) as error:
        return job, False, archive_error_code(error)
    with transaction.atomic():
        current = ArchiveOperation.objects.get(pk=operation.pk)
        # A concurrent worker/retry must not lose any newly frozen journal evidence.
        if current.updated_at != operation.updated_at:
            job.refresh_from_db()
            return job, job.state in ('queued', 'running', 'retry_wait'), 'current_state'
        changed = Job.objects.filter(pk=job.pk, state__in=['failed', 'blocked'],
            attempts=job.attempts).update(state='queued', next_attempt_at=timezone.now(),
                error_code='', safe_message='', updated_at=timezone.now())
        if changed:
            ArchiveOperation.objects.filter(pk=operation.pk).update(retry_context={**current.retry_context,
                'cycle_start_attempt': job.attempts, 'cycle_attempts': 0}, updated_at=timezone.now())
        job.refresh_from_db()
    return job, bool(changed) or job.state in ('queued', 'running', 'retry_wait'), 'queued' if changed else 'already_queued'


def retry_job(job_id,version):
    job = Job.objects.get(pk=job_id)
    if job.kind == 'archive':
        return retry_archive_job(job, version)
    with transaction.atomic():
        job=Job.objects.select_related('source__current_observation').get(pk=job_id)
        source=job.source
        if (version != job.source_version or version != source.observed_version or source.current_observation_id is None
                or source.current_observation.version != version):
            return job,False,'version_conflict'
        if job.state != 'failed':
            return job,False,'current_state'
        # 累计attempt编号不重置；人工明确重试允许一次新尝试。
        changed=Job.objects.filter(pk=job_id,state='failed',source_version=version).update(
            state='queued',stage='queued',next_attempt_at=timezone.now(),updated_at=timezone.now())
        if changed:
            ImportSource.objects.filter(pk=source.pk,observed_version=version).update(state='queued')
            SourceObservation.objects.filter(pk=source.current_observation_id,version=version).update(state='queued')
        job.refresh_from_db()
        return job,bool(changed),'queued' if changed else 'current_state'


def fault_hook(stage):
    # 生产设置无法启用；仅隔离测试根允许进程暂停注入。
    from django.conf import UserSettingsHolder
    configured = settings._wrapped
    while isinstance(configured, UserSettingsHolder):
        configured = configured.default_settings
    if configured.SETTINGS_MODULE != 'config.settings_test':
        return
    callback = getattr(settings, 'BIRDS_TEST_FAULT_CALLBACK', None)
    if callback:
        callback(stage)
    if stage in getattr(settings, 'BIRDS_TEST_FAULT_STAGE', '').split(','):
        from pathlib import Path
        marker = Path(settings.BIRDS_TEST_ROOT) / 'fault-ready'
        marker.write_text(stage)
        deadline = time.monotonic() + 20
        while not (marker.parent / 'fault-release').exists():
            if time.monotonic() >= deadline:
                raise RuntimeError('test_fault_timeout')
            time.sleep(.025)
        (marker.parent / 'fault-release').unlink(missing_ok=True)


def safe_log(job, code=''):
    try:
        logging.getLogger('archive.jobs').info(json.dumps({
            'job_id': str(job.pk), 'source_id': str(job.source_id),
            'stage': job.stage if job.stage in STAGE_LABELS else 'unknown', 'attempt': job.attempts,
            'error_code': code if code in SAFE_ERRORS else ('processing_failed' if code else '')},sort_keys=True))
    except Exception:
        pass  # 事务外日志故障不能撤销成功或制造新任务。


def cleanup_job_cache(job, data_fd):
    """只清理此Job的应用缓存；不递归删未知文件、不跟随链接。"""
    import re
    relative = f'previews/jobs/{job.pk}'
    current = None
    try:
        from archive.services.paths import ensure_directory_beneath
        current = ensure_directory_beneath(data_fd, relative)
        for name in os.listdir(current):
            if re.fullmatch(r'\.build-[0-9a-f]{32}|[0-9a-f]{64}-v2-(?:preview|thumb)\.jpg', name):
                import stat
                if stat.S_ISREG(os.stat(name, dir_fd=current, follow_symlinks=False).st_mode):
                    os.unlink(name, dir_fd=current)
        os.fsync(current)
    finally:
        if current is not None:
            os.close(current)


def claim_job(job_id, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        job = Job.objects.select_related('source__current_observation').get(pk=job_id)
        if job.kind == 'archive':
            updated = Job.objects.filter(pk=job.pk, state__in=['queued', 'retry_wait'],
                next_attempt_at__lte=now, attempts=job.attempts).update(
                state='running', attempts=job.attempts+1, updated_at=now)
            if updated:
                operation = ArchiveOperation.objects.get(job=job)
                ArchiveOperation.objects.filter(pk=operation.pk).update(retry_context={**operation.retry_context,
                    'cycle_start_attempt': operation.retry_context.get('cycle_start_attempt', 0),
                    'cycle_attempts': job.attempts+1-operation.retry_context.get('cycle_start_attempt', 0)},
                    updated_at=now)
                job.refresh_from_db()
                return job
            return None
        source = job.source
        observation = source.current_observation
        if not observation or observation.version != job.source_version:
            Job.objects.filter(pk=job.pk, state__in=['pending','queued','retry_wait']).update(
                state='stale', stage='stale', error_code='source_changed',
                safe_message=SAFE_ERRORS['source_changed'], updated_at=now)
            return None
        updated = Job.objects.filter(pk=job.pk, state__in=['pending','queued','retry_wait'],
            next_attempt_at__lte=now, attempts=job.attempts).update(
                state='running', stage='reading', attempts=job.attempts+1, updated_at=now)
        if updated:
            SourceObservation.objects.filter(pk=observation.pk).update(state='running')
            ImportSource.objects.filter(pk=source.pk).update(state='running')
            job.refresh_from_db()
            return job
    return None


def claim_next(now=None, *, import_usable=True):
    now = now or timezone.now()
    Job.objects.filter(kind='archive', state='awaiting_executor').update(state='queued', next_attempt_at=now)
    # Only configuration/handoff gates may resume after read-only reconciliation.
    # Other failures always require an explicit, CAS-protected manual retry.
    from archive.services.archiving import check_archive_retry
    for operation in ArchiveOperation.objects.filter(job__state='blocked',
            last_error__in=['handoff_not_ready', 'archive_root_unavailable', 'root_changed']).select_related('source_observation__source'):
        try:
            check_archive_retry(operation)
        except (OSError, UnsafePath, ImageRejected, OperationalError):
            continue
        Job.objects.filter(pk=operation.job_id, state='blocked').update(state='queued', next_attempt_at=now)
    available = Job.objects.filter(state__in=['pending','queued','retry_wait'], next_attempt_at__lte=now)
    if not import_usable:
        available = available.filter(kind='archive')
    for job_id in available.values_list('pk', flat=True):
        job = claim_job(job_id, now)
        if job:
            return job
    return None


def recover_running(data_fd):
    # 调用者必须持有生命周期flock；心跳从不授权恢复。
    for job in Job.objects.filter(kind='import', state='running').select_related('source__current_observation'):
        current = job.source.current_observation
        valid = current is not None and current.version == job.source_version
        cleanup_job_cache(job, data_fd)
        code = 'processing_failed' if valid else 'source_changed'
        with transaction.atomic():
            state = 'queued' if valid and job.attempts < getattr(settings,'RETRY_LIMIT',3) else ('failed' if valid else 'stale')
            Job.objects.filter(pk=job.pk, state='running').update(state=state, stage=state,
                error_code=code if state != 'queued' else '', safe_message=SAFE_ERRORS[code] if state != 'queued' else '',
                next_attempt_at=timezone.now(), updated_at=timezone.now())
            ImportSource.objects.filter(pk=job.source_id,observed_version=job.source_version).update(state=state)
            if valid:
                SourceObservation.objects.filter(pk=current.pk).update(state=state)
    for job in Job.objects.filter(kind='archive', state='running').select_related('archive_operation'):
        exhausted = job.archive_operation.retry_context.get('cycle_attempts', job.attempts) >= getattr(settings, 'RETRY_LIMIT', 3)
        Job.objects.filter(pk=job.pk, state='running').update(
            state='failed' if exhausted else 'queued', next_attempt_at=timezone.now(),
            error_code='temporarily_unavailable' if exhausted else job.error_code,
            safe_message='多次中断后已停止，请查看任务并核对后重试。' if exhausted else job.safe_message)


def process_source(job, inbox_fd, data_fd, *, claimed=False):
    job = job if claimed else claim_job(job.pk)
    if job is None:
        return False
    source = ImportSource.objects.select_related('current_observation').get(pk=job.source_id)
    observation = SourceObservation.objects.get(source_id=job.source_id,version=job.source_version)
    fault_hook('claimed')
    safe_log(job)
    try:
        return _process_claimed(job, source, observation, inbox_fd, data_fd)
    except (OperationalError, OSError, ImageRejected, UnsafePath) as error:
        if isinstance(error, ImageRejected):
            code = error.code
        elif isinstance(error, OperationalError):
            code = 'database_busy' if any(word in str(error).lower() for word in ('locked','busy')) else 'processing_failed'
        elif isinstance(error, PermissionError):
            code = 'source_permission'
        elif isinstance(error, FileNotFoundError):
            code = 'source_missing'
        elif isinstance(error, UnsafePath):
            code = 'source_changed'
        elif error.errno == errno.ENOSPC:
            code = 'storage_full'
        elif error.errno in (errno.EIO,errno.EAGAIN,errno.EBUSY,errno.ETIMEDOUT,errno.EINTR):
            code = 'transient_io'
        else:
            code = 'source_unavailable'
    except Exception:
        code = 'processing_failed'
    try:
        cleanup_job_cache(job, data_fd)
    except OSError:
        pass
    state = 'stale' if code == 'source_changed' else 'failed'
    transient = code in ('transient_io','database_busy')
    if transient and job.attempts < getattr(settings,'RETRY_LIMIT',3):
        state = 'retry_wait'
    delay = getattr(settings,'RETRY_BASE_MS',1000) * 2**min(job.attempts-1,2)
    def save_failure():
        with transaction.atomic():
            changed = Job.objects.filter(pk=job.pk,state='running').update(state=state,stage=state,error_code=code,
                safe_message=SAFE_ERRORS.get(code,SAFE_ERRORS['processing_failed']),
                next_attempt_at=timezone.now()+timedelta(milliseconds=delay),updated_at=timezone.now())
            if changed != 1:
                return
            ImportSource.objects.filter(pk=source.pk,observed_version=job.source_version).update(state=state)
            SourceObservation.objects.filter(pk=observation.pk).update(state=state,available=False)
            refresh_original_availability([observation.photo_id])
    database_retry(save_failure)
    job.refresh_from_db()
    safe_log(job,code)
    return False


def database_retry(operation):
    for attempt in range(3):
        try:
            return operation()
        except OperationalError as error:
            if not any(word in str(error).lower() for word in ('locked','busy')) or attempt == 2:
                raise
            time.sleep(getattr(settings,'RETRY_BASE_MS',1000)/1000 * 2**attempt)


def _process_claimed(job, source, observation, inbox_fd, data_fd):
    if observation is None or observation.version != job.source_version:
        raise ImageRejected('source_changed')
    verify_root_identity(inbox_fd, settings.BIRDS_INBOX_ROOT, data_fd)
    expected_source = _effective_replay_fingerprint(source.relative_path, source.fingerprint)
    with open_regular_beneath(inbox_fd, source.relative_path) as source_file:
        before = fingerprint(os.fstat(source_file.fileno()))
        if before != expected_source:
            raise ImageRejected("source_changed")
        digest, size = content_hash(source_file)
        if job.expected_sha256 and digest != job.expected_sha256:
            raise ImageRejected("source_changed")
        image, metadata = inspect_open_jpeg(source_file)
        with image:
            if fingerprint(os.fstat(source_file.fileno())) != before:
                raise ImageRejected("source_changed")
            photo = Photo.objects.filter(sha256=digest, byte_size=size).first()
            if photo is None:
                Job.objects.filter(pk=job.pk,state='running').update(stage='derivatives',updated_at=timezone.now())
                photo_id = uuid.uuid4()
                preview_key = write_derivatives(image, data_fd, job.pk, digest)
            # 再哈希同一 FD，并核对当前路径仍指向同一普通文件。
            if content_hash(source_file) != (digest, size):
                raise ImageRejected("source_changed")
            with open_regular_beneath(inbox_fd, source.relative_path) as current:
                if fingerprint(os.fstat(current.fileno())) != before:
                    raise ImageRejected("source_changed")
            verify_root_identity(inbox_fd, settings.BIRDS_INBOX_ROOT, data_fd)
        fault_hook('before_finish')
        finish_job(job.pk, job.source_version, digest, observation=observation, before=before, size=size,
                   photo=photo, photo_id=photo_id if photo is None else None,
                   preview_key=preview_key if photo is None else "", metadata=metadata)
        job.state,job.stage='succeeded','complete'
        safe_log(job)
        return True


def finish_job(job_id, source_version, digest, *, observation, before, size, photo, photo_id, preview_key, metadata):
    job = Job.objects.get(pk=job_id, source_version=source_version)
    with transaction.atomic():
        current_source = ImportSource.objects.select_related("current_observation").get(pk=job.source_id)
        current_observation = current_source.current_observation
        if (current_source.current_observation_id != observation.pk
                or current_source.observed_version != source_version
                or _effective_replay_fingerprint(
                    current_source.relative_path, current_observation.fingerprint) != before
                or (current_observation.sha256 and current_observation.sha256 != digest)
                or not Job.objects.filter(pk=job.pk, source_version=source_version, state="running").exists()):
            raise ImageRejected("source_changed")
        duplicate = photo is not None
        if photo is None:
            photo, created = Photo.objects.get_or_create(
                sha256=digest, byte_size=size,
                defaults={"id": photo_id, "preview_key": preview_key, **metadata})
            duplicate = not created
        current_source.photo = photo
        current_source.sha256 = digest
        current_source.state = "duplicate" if duplicate else "ready"
        current_source.save(update_fields=["photo", "sha256", "state", "updated_at"])
        current_observation.photo = photo
        current_observation.sha256 = digest
        current_observation.byte_size = size
        current_observation.state = current_source.state
        current_observation.duplicate = duplicate
        current_observation.available = True
        if duplicate and current_observation.duplicate_first_seen is None:
            current_observation.duplicate_first_seen = timezone.now()
        current_observation.save(update_fields=["photo", "sha256", "byte_size", "state", "duplicate",
                                                "available", "duplicate_first_seen"])
        refresh_original_availability([photo.pk])
        Job.objects.filter(pk=job.pk, source_version=source_version, state="running").update(
            state="succeeded", stage="complete", error_code="", safe_message="", updated_at=timezone.now())
        if not duplicate:
            from archive.services.recognition import enqueue_import
            enqueue_import(photo, current_observation)
