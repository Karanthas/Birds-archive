"""Small explicit personal edits, independent of file-operation generations."""
import uuid

from django.db import transaction
from django.db.models import F

from archive.models import Photo, MetadataReceipt
from archive.services.batch_review import digest
from archive.services.management import ManagementConflict, require_editable

FIELDS = {'favorite', 'rating', 'notes', 'user_location'}


def validate_changes(changes):
    if not isinstance(changes, dict) or not changes or set(changes) - FIELDS:
        raise ValueError('个人整理字段无效。')
    changes = dict(changes)
    if 'favorite' in changes and type(changes['favorite']) is not bool:
        raise ValueError('收藏必须明确设置或取消。')
    if 'rating' in changes and changes['rating'] is not None and (
            type(changes['rating']) is not int or not 1 <= changes['rating'] <= 5):
        raise ValueError('评分须为 1–5 星，或明确清除。')
    for name, limit in [('notes', 10000), ('user_location', 200)]:
        if name in changes:
            value = changes[name]
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(f'内容不能超过 {limit} 个字。')
            if name == 'user_location':
                changes[name] = value.strip()
    return changes


def personal_state(photo):
    return {'photo_id': str(photo.pk), 'metadata_version': photo.metadata_version,
        **{name: getattr(photo, name) for name in sorted(FIELDS)}}


def set_personal_metadata(photo_id, expected_version, request_id, changes):
    changes = validate_changes(changes)
    try:
        request_id = uuid.UUID(str(request_id))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('请求标识无效，请刷新后重试。') from None
    payload_digest = digest({'kind': 'personal_metadata', 'photo': str(photo_id), 'changes': changes})
    with transaction.atomic():
        photo = Photo.objects.get(pk=photo_id)
        require_editable(photo)
        previous = MetadataReceipt.objects.filter(request_id=request_id).first()
        if previous:
            if str(previous.photo_id) != str(photo_id) or previous.payload_digest != payload_digest:
                raise ManagementConflict('请求标识已用于不同内容，请重新核对。')
            return previous.result
        if type(expected_version) is not int or photo.metadata_version != expected_version:
            raise ManagementConflict('照片信息已变化，请核对当前值后重新保存。')
        updated = Photo.objects.filter(pk=photo.pk, metadata_version=expected_version,
            trash_requested_at__isnull=True, trashed_at__isnull=True).update(
                **changes, metadata_version=F('metadata_version') + 1)
        if not updated:
            raise ManagementConflict('照片信息已变化，请重新核对。')
        photo.refresh_from_db()
        result = {**personal_state(photo), 'request_id': str(request_id)}
        MetadataReceipt.objects.create(request_id=request_id, photo=photo,
            payload_digest=payload_digest, result=result)
        return result
