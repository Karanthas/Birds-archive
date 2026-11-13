"""Recognition scheduling and publication never write human classifications or move originals."""
import hashlib
import json
import math
import unicodedata
import uuid
import os
from datetime import timedelta

from django.conf import settings
from django.db import transaction, OperationalError
from django.utils import timezone

from archive.models import ModelArtifact, RecognitionState, RecognitionRun, RecognitionCandidate, Species, Photo

SAFE_ERRORS = {
    'model_not_configured': '本地识别模型未配置，可手动选种归档。',
    'invalid_result': '识别结果不可用，可手动选种归档。',
    'result_stale': '照片信息已变化，本次识别结果未采用。',
    'model_package_missing': '本地识别模型不可用，可手动选种归档。',
    'model_package_invalid': '本地识别模型不可用，可手动选种归档。',
}

RETRYABLE = frozenset({'database_busy', 'temporarily_unavailable', 'child_exited'})
TERMINAL_ERRORS = frozenset({'model_not_configured', 'model_package_missing', 'model_package_invalid',
    'source_changed', 'source_permission', 'model_memory_exceeded', 'model_timeout',
    'invalid_result', 'protocol_invalid', 'input_invalid'})


def _load_run(run_id):
    return RecognitionRun.objects.select_related('photo', 'artifact',
        'source_observation__source').get(pk=run_id)


def _current(run):
    state = RecognitionState.objects.select_related('active_run').filter(photo_id=run.photo_id).first()
    return _publication_is_current(run, state, run.artifact_id)


def claim_next_run(now=None):
    """One durable in-flight run; backoff changes readiness, never FIFO age."""
    now = now or timezone.now()
    with transaction.atomic():
        if RecognitionRun.objects.filter(state='running').exists():
            return None
        for run in RecognitionRun.objects.filter(state__in=['queued', 'retry_wait'],
                next_attempt_at__lte=now).select_related('photo', 'artifact',
                'source_observation__source').order_by('photo__created_at', 'photo_id', 'generation'):
            if not _current(run):
                _finish_rejected(run, 'stale', 'result_stale')
                continue
            changed = RecognitionRun.objects.filter(pk=run.pk, state=run.state).update(
                state='running', attempts=run.attempts+1, cycle_attempts=run.cycle_attempts+1,
                started_at=now, finished_at=None, safe_error='', updated_at=now)
            if changed:
                return _load_run(run.pk)
    return None


def fail_run(run_id, code, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        run = _load_run(run_id)
        if run.state != 'running':
            return False
        if not _current(run) or code == 'source_changed':
            return _finish_rejected(run, 'stale', 'result_stale')
        code = code if isinstance(code, str) and code in RETRYABLE | TERMINAL_ERRORS else 'protocol_invalid'
        retry = (code in RETRYABLE and run.cycle_attempts < settings.RECOGNITION_RETRY_LIMIT
            and (code != 'child_exited' or run.cycle_attempts == 1))
        state = 'retry_wait' if retry else ('unavailable' if code.startswith('model_package') or
            code == 'model_not_configured' else 'failed')
        RecognitionRun.objects.filter(pk=run.pk, state='running').update(state=state, safe_error=code,
            next_attempt_at=now+timedelta(milliseconds=settings.RECOGNITION_RETRY_BASE_MS *
                2**min(run.cycle_attempts-1, 20)) if retry else now,
            finished_at=None if retry else now, updated_at=now)
        return True


def recover_runs(*, lock_held=False):
    # The sole worker calls this only after obtaining its lifetime flock. No heartbeat takeover.
    if not lock_held:
        raise ValueError('executor_lock_required')
    for run_id in list(RecognitionRun.objects.filter(state='running').values_list('pk', flat=True)):
        fail_run(run_id, 'child_exited')


def _effective_source_fingerprint(run, original=None):
    """Resolve a restored inode without rewriting the immutable run payload."""
    from archive.services import restore
    source = run.source_observation.source
    try:
        return restore.effective_replay_fingerprint(
            'inbox', source.relative_path,
            run.source_fingerprint if original is None else original)
    except restore.RestoreError as error:
        raise ValueError('source_changed') from error


def validate_source(run, inbox_fd):
    from archive.services.paths import open_regular_beneath, fingerprint, open_root, root_identity
    from archive.services.images import content_hash
    current_fd = open_root(settings.BIRDS_INBOX_ROOT)
    try:
        if root_identity(current_fd) != root_identity(inbox_fd):
            raise ValueError('source_changed')
    finally:
        os.close(current_fd)
    expected = _effective_source_fingerprint(run)
    with open_regular_beneath(inbox_fd, run.source_observation.source.relative_path) as source:
        before = fingerprint(os.fstat(source.fileno()))
        digest, size = content_hash(source)
        if (before != expected or before != fingerprint(os.fstat(source.fileno()))
                or (digest, size) != (run.photo_sha256, run.byte_size)):
            raise ValueError('source_changed')


def prepare_input(run, inbox_fd, data_fd):
    """Anonymous read-only byte copy. No original path/DB/root FD crosses to the child."""
    from archive.services.paths import open_regular_beneath, fingerprint
    from archive.services.images import content_hash
    expected = _effective_source_fingerprint(run)
    name = '.recognition-' + uuid.uuid4().hex
    output_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600, dir_fd=data_fd)
    readonly = None
    try:
        readonly = open_regular_beneath(data_fd, name)
        os.fchmod(output_fd, 0o400)
        # Unlink before writing JPEG bytes: even SIGKILL cannot leave a named private copy.
        os.unlink(name, dir_fd=data_fd)
        output = os.fdopen(output_fd, 'wb')
        output_fd = None
        with output, open_regular_beneath(inbox_fd,
                run.source_observation.source.relative_path) as source:
            before = fingerprint(os.fstat(source.fileno()))
            digest, size = content_hash(source)
            if before != expected or (digest, size) != (run.photo_sha256, run.byte_size):
                raise ValueError('source_changed')
            copied = 0
            while block := source.read(1024*1024):
                copied += len(block)
                if copied > run.byte_size or copied > settings.MAX_FILE_BYTES:
                    raise ValueError('source_changed')
                output.write(block)
            output.flush()
            if before != fingerprint(os.fstat(source.fileno())):
                raise ValueError('source_changed')
        if content_hash(readonly) != (run.photo_sha256, run.byte_size):
            raise ValueError('source_changed')
        validate_source(run, inbox_fd)
        return readonly
    except BaseException:
        if readonly is not None:
            readonly.close()
        raise
    finally:
        if output_fd is not None:
            os.close(output_fd)
        try:
            os.unlink(name, dir_fd=data_fd)
        except FileNotFoundError:
            pass


def map_model_candidates(run, candidates):
    """Bind a verified vocabulary concept to this application's catalog UUID.

    Package UUIDs identify the database that prepared the vocabulary. The model
    process already verifies the full package; this file-worker boundary checks
    only the small frozen label/snapshot bytes, never loads weights or a model.
    """
    from pathlib import Path
    from archive.models import SourceSnapshot, SpeciesConcept
    from archive.services import model_packages as packages
    manifest = run.artifact.manifest
    packages.require(run.artifact.provider == packages.PROVIDER
        and manifest.get('provider') == packages.PROVIDER
        and packages.digest(manifest) == run.artifact.manifest_digest, 'candidate_package_identity')
    from archive.services.restore import effective_model_manifest
    effective_manifest = effective_model_manifest(run.artifact)
    values = _candidate_values(candidates)
    root = Path(effective_manifest.get('local_root', ''))
    packages.require(root.is_absolute(), 'candidate_package_path')
    metadata = {}
    for role, field in (('labels', 'label_digest'), ('snapshot', 'snapshot_digest')):
        rows = [row for row in manifest.get('files', []) if row.get('role') == role]
        packages.require(len(rows) == 1, 'candidate_metadata_missing')
        row = rows[0]
        raw = packages.read_local(packages.checked_path(root, row.get('path')), maximum=16*1024*1024)
        packages.require(len(raw) == row.get('size_bytes')
            and hashlib.sha256(raw).hexdigest() == row.get('sha256') == manifest.get(field),
            'candidate_metadata_digest')
        metadata[role] = json.loads(raw, object_pairs_hook=packages._pairs,
            parse_constant=lambda value: packages.require(False, 'candidate_metadata_nonfinite'))
    labels, snapshot = metadata['labels'], metadata['snapshot']
    packages.validate_labels(labels, snapshot)
    local = SourceSnapshot.objects.filter(source=snapshot['source'], version=snapshot['version'],
        sha256=snapshot['sha256'], species_count=snapshot['species_count']).first()
    packages.require(local is not None, 'candidate_catalog_snapshot')
    by_name = {row['scientific_name']: row for row in labels}
    result, seen = [], set()
    for item in values:
        label = by_name.get(item['raw_label'])
        packages.require(label is not None and item['raw_label'] not in seen, 'candidate_label_identity')
        seen.add(item['raw_label'])
        expected_id = label.get('species_id')
        packages.require((str(item['species_id']) if item['species_id'] else None) == expected_id
            and item['mapping_status'] == ('mapped' if expected_id else 'unmapped')
            and item['mapping_evidence'] == label.get('mapping_evidence', ''), 'candidate_provider_identity')
        if expected_id:
            concept = SpeciesConcept.objects.select_related('species').filter(snapshot=local,
                external_id=label['external_id'], species__source_kind='catalog',
                species__source_snapshot=local, species__scientific_name=label['scientific_name']).first()
            packages.require(concept is not None and bool(concept.evidence.strip()), 'candidate_catalog_concept')
            item = {**item, 'species_id': concept.species_id,
                'mapping_evidence': 'Frozen catalog '+snapshot['sha256']+' concept '+label['external_id']}
        result.append(item)
    return result


class RecognitionDispatcher:
    """Runs after a file action, then polls during idle waits; never waits for inference."""
    def __init__(self, channel, inbox_fd, data_fd, *, recover_on_ready=False):
        self.channel, self.inbox_fd, self.data_fd = channel, inbox_fd, data_fd
        self.run = self.input = None
        self.reply = self.pending_error = None
        self.needs_recovery = recover_on_ready

    def tick(self, *, allow_claim=True):
        from archive.services.recognition_process import ProcessBoundaryError
        try:
            if self.pending_error is not None:
                fail_run(self.run.pk, self.pending_error)
                self.close_input()
                return
            if self.reply is None:
                self.reply = self.channel.poll()
            message = self.reply
            if self.needs_recovery and self.channel.ready:
                # started acknowledges model lifecycle-lock acquisition by the ancestor.
                # Maintenance suppresses startup recovery just like a new claim.
                if allow_claim:
                    recover_runs(lock_held=True)
                self.needs_recovery = False
            if message and self.run:
                run = self.run
                try:
                    validate_source(run, self.inbox_fd)
                    if message.get('type') == 'result':
                        candidates = message.get('candidates')
                        if run.reason == 'evaluation':
                            from archive.services.evaluation_workspace import map_evaluation_candidates
                            candidates = map_evaluation_candidates(run, candidates)
                        elif run.artifact.provider == 'open_clip_cpu_bioclip2':
                            from archive.services.restore import RestoreError
                            try:
                                candidates = map_model_candidates(run, candidates)
                            except RestoreError:
                                fail_run(run.pk, 'model_package_invalid')
                                self.close_input()
                                return
                            except (OSError, ValueError, KeyError, TypeError, OverflowError):
                                fail_run(run.pk, 'invalid_result')
                                self.close_input()
                                return
                        publish_result(run.pk, candidates, artifact_id=run.artifact_id)
                    else:
                        fail_run(run.pk, message.get('error_code'))
                except (OSError, ValueError):
                    fail_run(run.pk, 'source_changed')
                self.close_input()
            if allow_claim and self.run is None and self.channel.ready:
                run = claim_next_run()
                if run is not None:
                    self.run = run
                    self.input = prepare_input(run, self.inbox_fd, self.data_fd)
                    from archive.services.restore import effective_model_manifest, RestoreError
                    try:
                        artifact_manifest = effective_model_manifest(run.artifact)
                    except RestoreError:
                        fail_run(run.pk, 'model_package_invalid')
                        self.close_input()
                        return
                    self.channel.submit(run_id=str(run.pk), generation=run.generation,
                        artifact_digest=run.artifact.manifest_digest, input_fd=self.input.fileno(),
                        input_sha256=run.photo_sha256, input_size=run.byte_size,
                        artifact={'provider': run.artifact.provider, 'manifest': artifact_manifest})
        except OperationalError:
            # Keep an already received response for the next short publication attempt.
            # Model database contention must not stop the manual file executor.
            return
        except (ProcessBoundaryError, OSError, ValueError) as error:
            if self.run:
                code = str(error) if isinstance(error, ProcessBoundaryError) else (
                    'source_permission' if isinstance(error, PermissionError) else 'source_changed')
                self.pending_error = 'child_exited' if code == 'child_closed' else code
                try:
                    fail_run(self.run.pk, self.pending_error)
                except OperationalError:
                    return
                self.close_input()

    @property
    def drained(self):
        """True only after startup recovery and every accepted exchange are settled."""
        return (self.channel.ready and not self.needs_recovery and self.run is None
                and self.input is None and self.reply is None and self.pending_error is None
                and getattr(self.channel, 'pending', None) is None)

    def close_input(self):
        if self.input is not None:
            self.input.close()
        self.run = self.input = None
        self.reply = self.pending_error = None


def set_current_model(artifact_id):
    with transaction.atomic():
        artifact = ModelArtifact.objects.get(pk=artifact_id)
        if artifact.selection_state != 'selected':
            raise ValueError('模型尚未经过生产选型')
        ModelArtifact.objects.filter(is_current=True).exclude(pk=artifact.pk).update(is_current=False)
        ModelArtifact.objects.filter(pk=artifact.pk).update(is_current=True)
    return ModelArtifact.objects.get(pk=artifact.pk)


def request_rerun(photo_id, expected_version, request_id, *, retry=False):
    """Explicit single-photo CAS. Failed new generations retain the previous active snapshot."""
    photo_id, request_id = uuid.UUID(str(photo_id)), uuid.UUID(str(request_id))
    if type(expected_version) is not int or expected_version < 1:
        raise ValueError('invalid_request')
    reason = 'explicit_retry' if retry else 'explicit_rerun'
    with transaction.atomic():
        existing = RecognitionRun.objects.filter(request_id=request_id).first()
        if existing:
            if (existing.photo_id != photo_id or existing.metadata_version != expected_version
                    or existing.reason != reason):
                raise ValueError('request_conflict')
            return existing
        photo = Photo.objects.get(pk=photo_id)
        if photo.state != 'review_pending' or not photo.original_available or photo.metadata_version != expected_version:
            raise ValueError('photo_changed')
        state, _ = RecognitionState.objects.get_or_create(photo=photo)
        previous = RecognitionRun.objects.filter(photo=photo, generation=state.requested_generation).first()
        if retry:
            if (previous is None or previous.state != 'failed' or previous.safe_error not in RETRYABLE
                    or not _current(_load_run(previous.pk))):
                raise ValueError('retry_not_allowed')
            observation, artifact = previous.source_observation, previous.artifact
        else:
            from django.db.models import F
            observation = photo.observations.select_related('source').filter(available=True,
                state__in=['ready', 'duplicate'], sha256=photo.sha256, byte_size=photo.byte_size,
                source__current_observation_id=F('pk')).order_by('first_seen', 'id').first()
            artifact = ModelArtifact.objects.filter(is_current=True, selection_state='selected').first()
        if observation is None:
            raise ValueError('source_changed')
        source = observation.source
        if (source.photo_id != photo.pk or source.observed_version != observation.version
                or source.fingerprint != observation.fingerprint or source.sha256 != photo.sha256
                or source.state not in ('ready', 'duplicate')):
            raise ValueError('source_changed')
        now = timezone.now()
        run = RecognitionRun(photo=photo, request_id=request_id, source_observation=observation,
            source_version=observation.version, source_fingerprint=observation.fingerprint,
            photo_sha256=photo.sha256, byte_size=photo.byte_size, metadata_version=expected_version,
            artifact=artifact, generation=state.requested_generation+1, reason=reason,
            queue_age=photo.created_at, attempts=previous.attempts if retry else 0,
            state='queued' if artifact else 'unavailable',
            safe_error='' if artifact else 'model_not_configured', finished_at=None if artifact else now)
        run.payload_digest = run_payload_digest(run)
        run.save()
        changed = RecognitionState.objects.filter(pk=state.pk, requested_generation=state.requested_generation).update(
            requested_generation=run.generation)
        if changed != 1:
            raise ValueError('photo_changed')
        return run


def retry_run(photo_id, expected_version, request_id):
    # A new auditable cycle keeps the old request id immutable and cumulative attempts.
    # Source conflicts require explicit rerun, never a retry against a different source.
    return request_rerun(photo_id, expected_version, request_id, retry=True)


def run_payload_digest(run):
    payload = {key: str(getattr(run, key)) for key in (
        'photo_id', 'source_observation_id', 'source_version', 'photo_sha256',
        'byte_size', 'metadata_version', 'generation', 'artifact_id', 'reason')}
    payload['source_fingerprint'] = run.source_fingerprint
    payload['artifact_digest'] = run.artifact.manifest_digest if run.artifact_id else None
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode()).hexdigest()


def enqueue_import(photo, observation):
    """Called only for a newly created photo, inside finish_job's short transaction."""
    with transaction.atomic():
        state, created = RecognitionState.objects.get_or_create(photo=photo)
        if not created:
            return photo.recognition_runs.get(generation=state.requested_generation)
        from archive.services.evaluation_workspace import is_workspace_import
        artifact = (None if is_workspace_import(photo, observation) else
            ModelArtifact.objects.filter(is_current=True, selection_state='selected').first())
        now = timezone.now()
        run = RecognitionRun(photo=photo, source_observation=observation,
            request_id=uuid.uuid5(photo.pk, 'recognition-import-v1'),
            source_version=observation.version, source_fingerprint=observation.fingerprint,
            photo_sha256=photo.sha256, byte_size=photo.byte_size, metadata_version=photo.metadata_version,
            artifact=artifact, generation=1, reason='import', queue_age=photo.created_at,
            state='queued' if artifact else 'unavailable',
            safe_error='' if artifact else 'model_not_configured', finished_at=None if artifact else now)
        run.payload_digest = run_payload_digest(run)
        run.save()
        state.requested_generation = 1
        state.save(update_fields=['requested_generation'])
        return run


def recognition_summary(photo):
    state = RecognitionState.objects.filter(photo=photo).first()
    latest = photo.recognition_runs.order_by('-generation').first() if state else None
    if latest is None:
        return {'state': 'not_requested', 'message': '尚未识别', 'active_run': None}
    message = SAFE_ERRORS.get(latest.safe_error)
    if message is None:
        message = {'queued': '等待识别', 'running': '识别中', 'retry_wait': '等待重试',
            'succeeded': '识别候选已就绪'}.get(latest.state, '识别暂不可用，可手动选种归档。')
    active = state.active_run if state.active_run_id and state.active_run.photo_id == photo.pk else None
    return {'state': latest.state, 'message': message, 'active_run': active}


def _candidate_values(candidates):
    """Bound untrusted model output before opening a write transaction."""
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 3:
        raise ValueError('invalid_result')
    normalized = []
    allowed = {'rank', 'raw_label', 'score', 'species_id', 'mapping_status', 'mapping_evidence'}
    for index, item in enumerate(candidates, 1):
        if not isinstance(item, dict) or set(item) - allowed:
            raise ValueError('invalid_result')
        rank, label, score = item.get('rank'), item.get('raw_label'), item.get('score')
        evidence = item.get('mapping_evidence', '')
        status, species_id = item.get('mapping_status'), item.get('species_id')
        if (type(rank) is not int or rank != index or type(score) not in (int, float)
                or not math.isfinite(score) or not isinstance(label, str) or not label.strip()
                or len(label) > 300 or not isinstance(evidence, str) or len(evidence) > 1000
                or any(unicodedata.category(char).startswith('C') for char in label+evidence)
                or status not in ('mapped', 'unmapped')
                or (status == 'mapped' and (species_id is None or not evidence.strip()))
                or (status == 'unmapped' and species_id is not None)):
            raise ValueError('invalid_result')
        species_id = uuid.UUID(str(species_id)) if species_id is not None else None
        if normalized and normalized[-1]['score'] < score:
            raise ValueError('invalid_result')
        normalized.append(dict(rank=rank, raw_label=label, score=float(score), species_id=species_id,
            mapping_status=status, mapping_evidence=evidence))
    return normalized


def _finish_rejected(run, state, code):
    # Never rewrite a successful snapshot or discard its candidates on a late replay.
    RecognitionRun.objects.filter(pk=run.pk, state__in=['queued', 'running', 'retry_wait']).update(
        state=state, safe_error=code, finished_at=timezone.now(), updated_at=timezone.now())
    return False


def _publication_is_current(run, state, artifact_id):
    photo, observation = run.photo, run.source_observation
    source = observation.source
    try:
        run_fingerprint = _effective_source_fingerprint(run)
        observation_fingerprint = _effective_source_fingerprint(run, observation.fingerprint)
        source_fingerprint = _effective_source_fingerprint(run, source.fingerprint)
    except ValueError:
        return False
    return (photo.state == 'review_pending' and photo.original_available
        and photo.metadata_version == run.metadata_version
        and (photo.sha256, photo.byte_size) == (run.photo_sha256, run.byte_size)
        and observation.available and observation.state in ('ready', 'duplicate')
        and observation.photo_id == photo.pk
        and observation.version == run.source_version
        and observation_fingerprint == run_fingerprint
        and (observation.sha256, observation.byte_size) == (run.photo_sha256, run.byte_size)
        and source.current_observation_id == observation.pk
        and source.observed_version == run.source_version and source.photo_id == photo.pk
        and source.sha256 == run.photo_sha256 and source_fingerprint == run_fingerprint
        and source.state in ('ready', 'duplicate')
        and state is not None and state.requested_generation == run.generation
        and (state.active_run_id is None or state.active_run.photo_id == photo.pk)
        and run.artifact_id is not None and run.artifact_id == artifact_id
        and run.payload_digest == run_payload_digest(run))


class _PublicationConflict(Exception):
    pass


def publish_result(run_id, candidates, *, artifact_id):
    """CAS an entire candidate set. True also means an identical successful replay.

    The caller must validate actual JPEG input before inference (03-03); this
    boundary checks persisted identities again. No decoding, files or model API
    calls occur in this short IMMEDIATE SQLite transaction.
    """
    try:
        artifact_id = uuid.UUID(str(artifact_id))
    except (ValueError, TypeError, AttributeError):
        artifact_id = None
    try:
        values = _candidate_values(candidates)
    except (ValueError, TypeError, OverflowError, AttributeError):
        values = None
    try:
        with transaction.atomic():
            run = RecognitionRun.objects.select_related('photo', 'artifact',
                'source_observation__source').get(pk=run_id)
            state = RecognitionState.objects.select_related('active_run').filter(photo_id=run.photo_id).first()
            if not _publication_is_current(run, state, artifact_id):
                return _finish_rejected(run, 'stale', 'result_stale')
            species_ids = {item['species_id'] for item in values or [] if item['species_id'] is not None}
            if values is None or Species.objects.filter(pk__in=species_ids, source_kind='catalog').count() != len(species_ids):
                return _finish_rejected(run, 'failed', 'invalid_result')
            if run.state == 'succeeded':
                saved = list(run.candidates.values('rank', 'raw_label', 'score', 'species_id',
                    'mapping_status', 'mapping_evidence'))
                return state.active_run_id == run.pk and saved == values
            if run.state != 'running':
                return False
            changed = RecognitionRun.objects.filter(pk=run.pk, state='running',
                payload_digest=run.payload_digest).update(state='succeeded', safe_error='',
                    finished_at=timezone.now(), updated_at=timezone.now())
            if changed != 1:
                raise _PublicationConflict
            RecognitionCandidate.objects.bulk_create([RecognitionCandidate(run=run, **item) for item in values])
            changed = RecognitionState.objects.filter(pk=state.pk, requested_generation=run.generation,
                active_run_id=state.active_run_id).update(active_run=run)
            if changed != 1:
                raise _PublicationConflict
            return True
    except _PublicationConflict:
        return _finish_rejected(run, 'stale', 'result_stale')
