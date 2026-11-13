"""Freeze explicit decisions, then accept each independently. No file I/O here."""
import copy
import hashlib
import json
import logging
import math
import unicodedata
import uuid
from datetime import timedelta

from django.core import signing
from django.db import transaction, DatabaseError
from django.db.models import F
from django.utils import timezone

from archive.models import (Photo, Species, RecognitionState, BatchReviewPreview, BatchReviewResult)
from archive.services.confirmation import (confirm_photo, ConfirmationConflict,
    ConfirmationValidation, local_name_key)
from archive.services.recognition_policy import candidate_ambiguity

SALT = 'bird-batch-review-v1'
MAX_AGE = 3600
logger = logging.getLogger(__name__)


class PreviewConflict(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def explicit_ids(values):
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 200:
        raise ValueError('请选择1至200张照片。')
    try:
        ids = [str(uuid.UUID(str(value))) for value in values]
    except (ValueError, TypeError, AttributeError):
        raise ValueError('照片选择无效。') from None
    if len(set(ids)) != len(ids):
        raise ValueError('照片选择重复，请重新核对。')
    return ids


def _identity(photo_id):
    photo = Photo.objects.filter(pk=photo_id).first()
    if photo is None:
        return {'photo_id': photo_id, 'missing': True}, None, None, []
    observation = photo.observations.select_related('source').filter(available=True,
        sha256=photo.sha256, byte_size=photo.byte_size,
        source__current_observation_id=F('pk')).order_by('first_seen', 'id').first()
    state = RecognitionState.objects.select_related('active_run__artifact').filter(photo=photo).first()
    run = state.active_run if state else None
    candidates = list(run.candidates.select_related('species__source_snapshot').order_by('rank')) if run else []
    identity = {'photo_id': str(photo.pk), 'sha256': photo.sha256, 'byte_size': photo.byte_size,
        'metadata_version': photo.metadata_version, 'lifecycle': photo.state,
        'original_available': photo.original_available, 'capture': photo.effective_capture(),
        'source': None, 'requested_generation': state.requested_generation if state else None,
        'candidate_run': None}
    if observation:
        source = observation.source
        identity['source'] = {'observation': str(observation.pk), 'version': observation.version,
            'fingerprint': observation.fingerprint, 'sha256': observation.sha256, 'size': observation.byte_size,
            'source_id': str(source.pk), 'root_key': source.root_key, 'relative_path': source.relative_path,
            'current_observation': str(source.current_observation_id), 'observed_version': source.observed_version,
            'current_fingerprint': source.fingerprint, 'current_sha256': source.sha256, 'state': source.state}
    if run:
        identity['candidate_run'] = {'id': str(run.pk), 'generation': run.generation, 'state': run.state,
            'source_observation': str(run.source_observation_id), 'source_version': run.source_version,
            'source_fingerprint': run.source_fingerprint, 'sha256': run.photo_sha256,
            'size': run.byte_size, 'metadata_version': run.metadata_version,
            'artifact': ({'id': str(run.artifact_id), 'digest': run.artifact.manifest_digest,
                'provider': run.artifact.provider, 'version': run.artifact.version,
                'revision': run.artifact.revision, 'manifest': run.artifact.manifest} if run.artifact_id else None),
            'candidates': [{'id': c.pk, 'rank': c.rank, 'species_id': str(c.species_id) if c.species_id else None,
                'score': c.score if isinstance(c.score, (int, float)) and math.isfinite(c.score) else None,
                'label': c.raw_label, 'mapping_status': c.mapping_status, 'mapping_evidence': c.mapping_evidence}
                for c in candidates]}
    return identity, photo, run, candidates


def _freeze(photo_id, *, uniform=None):
    identity, photo, run, candidates = _identity(photo_id)
    reason, ids = '', []
    manifest = run.artifact.manifest if run and run.artifact_id else {}
    ambiguity = candidate_ambiguity(manifest, [c.score for c in candidates])
    if photo is None:
        reason = 'photo_missing'
    elif photo.state != 'review_pending':
        reason = 'not_review_pending'
    elif not _source_current(identity) or not photo.original_available:
        reason = 'source_unavailable'
    elif not run or run.state != 'succeeded' or not candidates:
        reason = 'candidates_unavailable'
    elif (run.generation != identity['requested_generation'] or run.metadata_version != photo.metadata_version
            or run.photo_sha256 != photo.sha256 or run.byte_size != photo.byte_size
            or str(run.source_observation_id) != identity['source']['observation']
            or run.source_version != identity['source']['version']
            or run.source_fingerprint != identity['source']['fingerprint']):
        reason = 'candidate_stale'
    elif ambiguity != 'separated':
        reason = 'candidates_close' if ambiguity == 'close' else 'threshold_unknown'
    else:
        top = candidates[0]
        if (top.rank != 1 or top.mapping_status != 'mapped' or not top.species_id
                or top.species.source_kind != 'catalog' or not top.mapping_evidence.strip()):
            reason = 'candidate_unmapped'
        else:
            ids = [str(top.species_id)]
    names = []
    if uniform is not None:
        ids, names = uniform
        # A human set has no dependency on whether the model found a candidate.
        if photo and photo.state == 'review_pending' and _source_current(identity) and photo.original_available:
            reason = ''
    previous = list(photo.confirmed_species.select_related('species')) if photo else []
    return {'photo_id': photo_id, 'identity': identity, 'request_id': str(uuid.uuid4()),
        'final_species_ids': list(ids), 'local_names': list(names), 'classification': 'species',
        'previous_labels': [item.species.display_name for item in previous],
        'manual_override': uniform is not None, 'excluded': False, 'reason_code': reason,
        'ambiguity_version': manifest.get('threshold_version') if isinstance(manifest, dict) else None,
        'name': identity.get('source', {}).get('relative_path', photo_id) if identity.get('source') else photo_id,
        'original_state': photo.state_display if photo else '照片不可用',
        'species_labels': list(Species.objects.filter(pk__in=ids).values_list('chinese_name', 'scientific_name', 'local_name')),
        'source_label': f'{run.artifact.provider} {run.artifact.version}' if run and run.artifact_id else '未知'}


def _snapshot_digest(mode, ids, rows):
    return digest({'mode': mode, 'selection_ids': ids, 'rows': rows})


def _source_current(identity):
    source = identity.get('source')
    return bool(source and source['version'] == source['observed_version']
        and source['sha256'] == source['current_sha256'] == identity['sha256']
        and source['fingerprint'] == source['current_fingerprint'])


def build_preview(photo_ids, *, mode, species_ids=None, local_names=None):
    ids = explicit_ids(photo_ids)
    mode = {'own_candidates': 'own', 'uniform': 'uniform_species'}.get(mode, mode)
    if mode not in ('own', 'uniform_species'):
        raise ValueError('请选择批量鸟种操作。')
    with transaction.atomic():
        uniform = _manual_set(species_ids or [], local_names or []) if mode == 'uniform_species' else None
        if mode == 'own' and (species_ids or local_names):
            raise ValueError('各自候选模式不接受统一鸟种集合。')
        rows = [_freeze(ident, uniform=uniform) for ident in ids]
        return BatchReviewPreview.objects.create(mode=mode, selection_ids=ids, frozen_rows=rows,
            selection_digest=_snapshot_digest(mode, ids, rows), expires_at=timezone.now()+timedelta(seconds=MAX_AGE))


def preview_token(preview):
    return signing.dumps({'preview': str(preview.pk), 'revision': preview.revision,
        'digest': preview.selection_digest}, salt=SALT)


def checked_preview(preview_id, token):
    try:
        value = signing.loads(token, salt=SALT, max_age=MAX_AGE)
        preview = BatchReviewPreview.objects.get(pk=preview_id)
        if (value != {'preview': str(preview.pk), 'revision': preview.revision, 'digest': preview.selection_digest}
                or preview.expires_at <= timezone.now()
                or preview.selection_digest != _snapshot_digest(preview.mode, preview.selection_ids, preview.frozen_rows)
                or BatchReviewPreview.objects.filter(series_id=preview.series_id, revision__gt=preview.revision).exists()):
            raise PreviewConflict('预览已变化或过期，请重新核对。')
        return preview
    except (signing.BadSignature, BatchReviewPreview.DoesNotExist, TypeError, ValueError):
        raise PreviewConflict('预览已变化或过期，请重新核对。') from None


def _manual_set(species_ids, local_names):
    if not isinstance(species_ids, list) or not isinstance(local_names, list) or len(species_ids)+len(local_names) > 100:
        raise ValueError('鸟种集合无效。')
    try:
        ids = sorted({str(uuid.UUID(str(value))) for value in species_ids})
    except (ValueError, TypeError, AttributeError):
        raise ValueError('鸟种选择无效。') from None
    if Species.objects.filter(pk__in=ids).count() != len(ids):
        raise ValueError('鸟种不可用，请重新选择。')
    names = {}
    for name in local_names:
        if not isinstance(name, str) or len(name) > 300 or any(unicodedata.category(c).startswith('C') and not c.isspace() for c in name):
            raise ValueError('本地鸟名无效。')
        if local_name_key(name):
            names.setdefault(local_name_key(name), ' '.join(unicodedata.normalize('NFC', name).split()))
    if not ids and not names:
        raise ValueError('请补选鸟种或排除本项；空集合不会归档。')
    selected_local_keys = set(Species.objects.filter(pk__in=ids, source_kind='local').values_list('local_key', flat=True))
    return ids, [name for key, name in names.items() if key not in selected_local_keys]


def revise_preview(preview_id, token, changes):
    """Preserve shown identities: revising a set never silently rebases stale photos."""
    with transaction.atomic():
        preview = checked_preview(preview_id, token)
        if preview.results.exists():
            raise PreviewConflict('此预览已提交，请重新选择照片。')
        if not isinstance(changes, dict) or not changes or set(changes)-set(preview.selection_ids):
            raise ValueError('修订对象无效。')
        rows = copy.deepcopy(preview.frozen_rows)
        for row in rows:
            row['request_id'] = str(uuid.uuid4())
            change = changes.get(row['photo_id'])
            if change is None:
                continue
            if not isinstance(change, dict) or set(change)-{'excluded', 'species_ids', 'local_names'}:
                raise ValueError('修订内容无效。')
            if 'excluded' in change:
                if type(change['excluded']) is not bool:
                    raise ValueError('排除选择无效。')
                row['excluded'] = change['excluded']
            if 'species_ids' in change or 'local_names' in change:
                ids, names = _manual_set(change.get('species_ids', []), change.get('local_names', []))
                identity = row['identity']
                reason = ('photo_missing' if identity.get('missing') else
                    'not_review_pending' if identity.get('lifecycle') != 'review_pending' else
                    'source_unavailable' if not _source_current(identity) or not identity.get('original_available') else '')
                row.update(final_species_ids=ids, local_names=names, manual_override=True, reason_code=reason)
                row['species_labels'] = list(Species.objects.filter(pk__in=ids).values_list('chinese_name', 'scientific_name', 'local_name'))
        return BatchReviewPreview.objects.create(series_id=preview.series_id, revision=preview.revision+1,
            mode=preview.mode, selection_ids=preview.selection_ids, frozen_rows=rows,
            selection_digest=_snapshot_digest(preview.mode, preview.selection_ids, rows), expires_at=preview.expires_at)


def _result_dict(result):
    return {'photo_id': str(result.photo_id), 'status': result.status, 'reason_code': result.reason_code,
        'operation_id': str(result.operation_id) if result.operation_id else None}


def _record(preview, row, status, reason, operation=None):
    payload = digest(row)
    existing = BatchReviewResult.objects.filter(request_id=row['request_id']).first()
    if existing:
        if (existing.preview_id != preview.pk or existing.photo_id != uuid.UUID(row['photo_id'])
                or existing.payload_digest != payload):
            raise PreviewConflict('回执内容不一致，请重新核对。')
        return existing
    return BatchReviewResult.objects.create(preview=preview, revision=preview.revision,
        photo_id=row['photo_id'], request_id=row['request_id'], payload_digest=payload,
        status=status, reason_code=reason, operation=operation)


def submit_preview(preview_id, token):
    preview = checked_preview(preview_id, token)
    results = []
    for index, row in enumerate(preview.frozen_rows):
        try:
            with transaction.atomic():
                # Receipt precedes all mutable photo/CAS checks, including archive completion.
                existing = BatchReviewResult.objects.filter(request_id=row['request_id']).first()
                if existing:
                    result = _record(preview, row, existing.status, existing.reason_code)
                else:
                    checked_preview(preview_id, token)
                    identity, photo, _, _ = _identity(row['photo_id'])
                    reason = 'photo_changed' if identity != row['identity'] else ''
                    if not reason and (not photo or photo.state != 'review_pending' or not _source_current(identity) or not photo.original_available):
                        reason = 'source_unavailable'
                    if not reason and row['excluded']:
                        reason = 'excluded'
                    if not reason and not row['manual_override']:
                        reason = row['reason_code']
                    if not reason and not (row['final_species_ids'] or row['local_names']):
                        reason = 'empty_set'
                    if reason:
                        result = _record(preview, row, 'skipped', reason)
                    else:
                        operation, _ = confirm_photo(row['photo_id'], row['identity']['metadata_version'],
                            row['request_id'], 'species', row['final_species_ids'], row['local_names'])
                        result = _record(preview, row, 'accepted', 'awaiting_archive', operation)
            results.append(_result_dict(result))
        except DatabaseError:
            results.extend({'photo_id': pending['photo_id'], 'status': 'unprocessed',
                'reason_code': 'database_unavailable', 'operation_id': None} for pending in preview.frozen_rows[index:])
            break
        except PreviewConflict:
            raise
        except (ConfirmationConflict, Photo.DoesNotExist, ConfirmationValidation) as error:
            # A row failure rolls back confirm+receipt together, then a new short transaction records it.
            status = 'skipped' if isinstance(error, (ConfirmationConflict, Photo.DoesNotExist)) else 'failed'
            reason = 'confirmation_conflict' if status == 'skipped' else 'confirmation_failed'
            try:
                with transaction.atomic():
                    result = _record(preview, row, status, reason)
                results.append(_result_dict(result))
            except DatabaseError:
                results.extend({'photo_id': pending['photo_id'], 'status': 'unprocessed',
                    'reason_code': 'database_unavailable', 'operation_id': None} for pending in preview.frozen_rows[index:])
                break
            except Exception:
                logger.exception('Batch review program error preview=%s request=%s photo=%s',
                    preview.pk, row['request_id'], row['photo_id'])
                raise
        except Exception:
            # The row transaction has already rolled back. A program failure must
            # remain retryable under the original preview, not become a terminal receipt.
            logger.exception('Batch review program error preview=%s request=%s photo=%s',
                preview.pk, row['request_id'], row['photo_id'])
            raise
    return results
