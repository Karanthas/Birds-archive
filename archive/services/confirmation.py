"""明确人工决定的短事务；此模块不执行文件 I/O。"""
import hashlib
import json
import uuid
import unicodedata

from django.db import transaction
from django.db.models import F, Q

from archive.models import Photo, Species, Job, ArchiveOperation, PhotoSpecies


class ConfirmationConflict(Exception):
    pass


class ConfirmationValidation(ValueError):
    pass


def local_name_key(name):
    return ' '.join(unicodedata.normalize('NFC', name).split()).casefold()


def search_confirmable_species(query):
    from archive.services.catalog import search_species
    catalog = search_species(query).filter(source_kind='catalog')
    key = local_name_key(query)
    if not key:
        return Species.objects.none()
    return Species.objects.filter(Q(pk__in=catalog.values('pk')) | Q(source_kind='local',
        local_key__contains=key)).select_related('source_snapshot').order_by(
            'source_kind', 'scientific_key', 'local_key', 'id')


def confirmed_photos_for_species(species_id):
    return Photo.objects.filter(confirmed_species__species_id=uuid.UUID(str(species_id)),
        archive_completion__isnull=False, trash_requested_at__isnull=True,
        trashed_at__isnull=True).distinct().order_by('created_at', 'id')


def next_review_photo():
    return Photo.objects.filter(state='review_pending').order_by('created_at', 'id').first()


def replay_next(operation):
    next_id = operation.retry_context.get('next_photo_id')
    return Photo.objects.filter(pk=next_id).first() if next_id else None


def confirm_photo(photo_id, expected_version, request_id, classification, species_ids, local_names):
    request_id = uuid.UUID(str(request_id))
    photo_id = uuid.UUID(str(photo_id))
    if (classification not in ('species', 'undetermined') or len(species_ids) + len(local_names) > 100
            or type(expected_version) is not int or expected_version < 1):
        raise ConfirmationValidation('请明确选择鸟种或无法确定。')
    names = {}
    for name in local_names:
        if not isinstance(name, str) or len(name) > 300 or any(
                unicodedata.category(char).startswith('C') and not char.isspace() for char in name):
            raise ConfirmationValidation('本地鸟名不能超过300个字符或包含控制字符。')
        key = local_name_key(name)
        if key:
            names.setdefault(key, name)
    if (classification == 'undetermined' and (species_ids or names)) or (
            classification == 'species' and not (species_ids or names)):
        raise ConfirmationValidation('请选择鸟种，或清空鸟种后明确选择无法确定。')
    ids = sorted({str(uuid.UUID(str(value))) for value in species_ids})
    payload = {'photo_id': str(photo_id), 'classification': classification,
               'species_ids': ids, 'local_names': sorted(names)}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                       separators=(',', ':')).encode()).hexdigest()
    with transaction.atomic():
        from archive.services.management import require_editable, ManagementConflict
        photo = Photo.objects.get(pk=photo_id)
        try:
            require_editable(photo)
        except ManagementConflict as error:
            raise ConfirmationConflict(str(error)) from None
        # SQLite 使用现有 IMMEDIATE 短事务及 CAS，不能依赖无效行锁。
        existing = ArchiveOperation.objects.filter(request_id=request_id).first()
        if existing:
            if existing.payload_digest != digest:
                raise ConfirmationConflict('请求已用于不同的确认内容，请核对草稿。')
            return existing, replay_next(existing)
        if Species.objects.filter(pk__in=ids).count() != len(ids):
            raise ConfirmationValidation('鸟种不可用，请重新选择。')
        photo = Photo.objects.get(pk=photo_id)
        observation = photo.observations.select_related('source').filter(
            available=True, sha256=photo.sha256, byte_size=photo.byte_size,
            source__current_observation_id=F('pk')).order_by('first_seen', 'id').first()
        if observation is None:
            raise ConfirmationConflict('来源不可用，请检查任务。')
        changed = Photo.objects.filter(pk=photo.pk, state='review_pending',
            metadata_version=expected_version, trash_requested_at__isnull=True,
            trashed_at__isnull=True).update(state='confirmed', metadata_version=F('metadata_version') + 1)
        if changed != 1:
            raise ConfirmationConflict('照片信息已变化，请核对当前照片与草稿。')
        for key, name in names.items():
            # Unique local_key arbitrates concurrent normalized names. Catalog
            # homonyms never participate in this lookup or acquire local identity.
            species, _ = Species.objects.get_or_create(local_key=key, defaults={
                'source_kind': 'local', 'local_name': name, 'search_key': key,
                'scientific_name': None, 'scientific_key': None, 'source_snapshot': None})
            ids.append(str(species.pk))
        ids = sorted(set(ids))
        job = Job.objects.create(source=observation.source, source_version=observation.version,
            expected_sha256=photo.sha256, kind='archive', state='awaiting_executor')
        next_photo = next_review_photo()
        operation = ArchiveOperation.objects.create(photo=photo, job=job, request_id=request_id,
            payload_digest=digest, classification=classification, species_ids_snapshot=ids,
            capture_snapshot=photo.effective_capture(),
            original_basename=observation.source.relative_path.rsplit('/', 1)[-1], source_observation=observation,
            source_version=observation.version, source_fingerprint=observation.fingerprint,
            expected_sha256=photo.sha256, byte_size=photo.byte_size,
            roots_snapshot={'binding_state': 'unbound'},
            retry_context={'cycle_start_attempt': 0, 'cycle_attempts': 0,
                           'next_photo_id': str(next_photo.pk) if next_photo else None})
        PhotoSpecies.objects.bulk_create([PhotoSpecies(photo=photo, species_id=ident) for ident in ids])
    return operation, next_photo
