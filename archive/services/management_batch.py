"""Explicit, signed management intentions; each row accepts independently."""
import uuid
from datetime import timedelta

from django.core import signing
from django.db import transaction, DatabaseError
from django.utils import timezone

from archive.models import Photo, ManagementPreview, ManagementResult
from archive.services.batch_review import explicit_ids, digest
from archive.services.management import (species_payload, species_target, accept_management,
    ManagementConflict, UNRESOLVED, editable_lifecycle, restore_origin)

SALT = 'birds-management-preview-v1'
MAX_AGE = 7 * 24 * 3600
PERSONAL_ACTIONS = {'set_favorite':'favorite', 'set_rating':'rating', 'set_location':'user_location'}


def personal_payload(action, payload):
    from archive.services.management_metadata import validate_changes
    if action not in PERSONAL_ACTIONS or not isinstance(payload, dict) or set(payload) != {PERSONAL_ACTIONS[action]}:
        raise ValueError('批量整理只允许显式收藏、评分或地点；备注和时间须逐张修改。')
    return validate_changes(payload)


def personal_label(payload):
    if 'favorite' in payload:
        return '收藏' if payload['favorite'] else '取消收藏'
    if 'rating' in payload:
        return f"{payload['rating']} 星" if payload['rating'] else '未评分（清除）'
    return payload['user_location'] or '未知（清除地点）'


def _digest(action, ids, rows):
    return digest({'action': action, 'ids': ids, 'rows': rows})


def _freeze(action, ids, payload):
    if action in ('trash', 'restore'):
        if payload != {}:
            raise ValueError('回收不接受自定义路径或其他字段。')
        rows = []
        for ident in ids:
            photo = Photo.objects.filter(pk=ident).first()
            row = {'photo_id':ident, 'request_id':str(uuid.uuid4()), 'payload':{},
                'payload_digest':digest({'action':action,'photo':ident,'payload':{}}),
                'metadata_version':photo.metadata_version if photo else None,
                'generation':photo.management_generation if photo else None, 'reason':'',
                'trashed_at':photo.trashed_at.isoformat() if photo and photo.trashed_at else None,
                'source_path':'', 'target_path':'', 'target':{}}
            managed = getattr(photo, 'managed_original', None) if photo else None
            if not photo or not photo.current_classification or managed is None or (not photo.trashed_at if action == 'restore' else not editable_lifecycle(photo)):
                row['reason'] = '照片当前不可恢复。' if action == 'restore' else '照片当前不可回收。'
            elif photo.management_operations.filter(status__in=UNRESOLVED).exists():
                row['reason'] = '已有更正尚未完成。'
            else:
                row.update(source_path=managed.relative_path, location_revision=managed.location_revision,
                    target_path=f'.archive-trash/{photo.pk}/{managed.actual_basename}')
                if action == 'restore':
                    try:
                        row['target_path'] = restore_origin(photo)
                    except ManagementConflict as error:
                        row['reason'] = str(error)
            rows.append(row)
        return rows
    if action in PERSONAL_ACTIONS:
        payload = personal_payload(action, payload)
        rows = []
        for ident in ids:
            photo = Photo.objects.filter(pk=ident).first()
            reason = ''
            if photo is None:
                reason = '照片不存在。'
            elif not editable_lifecycle(photo):
                reason = '照片已进入回收流程，恢复后再修改。'
            elif action == 'set_location' and not photo.current_classification:
                reason = '批量地点更正只用于已归档照片。'
            old = {PERSONAL_ACTIONS[action]:getattr(photo, PERSONAL_ACTIONS[action])} if photo else payload
            rows.append({'photo_id':ident, 'request_id':str(uuid.uuid4()), 'payload':payload,
                'payload_digest':digest({'action':action,'photo':ident,'payload':payload}),
                'metadata_version':photo.metadata_version if photo else None,
                'reason':reason, 'old_value':old, 'target':payload,
                'old_display':personal_label(old), 'target_display':personal_label(payload)})
        return rows
    if action != 'species':
        raise ValueError('管理动作无效。')
    payload = species_payload(payload)
    rows = []
    for ident in ids:
        photo = Photo.objects.filter(pk=ident).first()
        row = {'photo_id': ident, 'request_id': str(uuid.uuid4()), 'payload': payload,
            'payload_digest': digest({'action': action, 'photo': ident, 'payload': payload}),
            'reason': '', 'metadata_version': None, 'generation': None, 'old_labels': [],
            'source_path': '', 'target_path': '', 'target': None}
        if photo is None:
            row['reason'] = '照片不存在。'
        else:
            row.update(metadata_version=photo.metadata_version, generation=photo.management_generation,
                old_species_ids=sorted(str(link.species_id) for link in photo.confirmed_species.all()),
                old_classification=photo.current_classification,
                old_labels=[link.species.display_name for link in photo.confirmed_species.select_related('species')])
            managed = getattr(photo, 'managed_original', None)
            if not photo.current_classification or managed is None or not editable_lifecycle(photo):
                row['reason'] = '照片当前不可更正。'
            elif photo.management_operations.filter(status__in=UNRESOLVED).exists():
                row['reason'] = '已有更正尚未完成。'
            else:
                row.update(source_path=managed.relative_path, location_revision=managed.location_revision)
                try:
                    target = species_target(photo, payload)
                    row.update(target=target, target_path=f"{target['directory']}/{photo.current_capture.get('year') or '未知年份'}/{managed.actual_basename}")
                except ValueError as error:
                    row['reason'] = str(error)
        rows.append(row)
    return rows


def build_management_preview(action, ids, payload):
    ids = explicit_ids(ids)
    with transaction.atomic():
        rows = _freeze(action, ids, payload)
        return ManagementPreview.objects.create(action=action, selection_ids=ids, frozen_rows=rows,
            selection_digest=_digest(action, ids, rows), expires_at=timezone.now()+timedelta(seconds=MAX_AGE))


def management_token(preview):
    return signing.dumps({'preview': str(preview.pk), 'revision': preview.revision,
        'digest': preview.selection_digest}, salt=SALT)


def checked_management_preview(preview_id, token):
    try:
        value = signing.loads(token, salt=SALT, max_age=MAX_AGE)
        preview = ManagementPreview.objects.get(pk=preview_id)
        if (value != {'preview': str(preview.pk), 'revision': preview.revision, 'digest': preview.selection_digest}
                or preview.expires_at <= timezone.now() or preview.selection_digest !=
                _digest(preview.action, preview.selection_ids, preview.frozen_rows)):
            raise ValueError
    except (signing.BadSignature, ValueError, TypeError, ManagementPreview.DoesNotExist):
        raise ManagementConflict('核对已过期或发生变化，请重新预览。') from None
    return preview


def revise_management_preview(preview_id, token, *, action, ids, payload):
    with transaction.atomic():
        preview = checked_management_preview(preview_id, token)
        if preview.results.exists():
            raise ManagementConflict('已有提交结果，请从相册重新选择。')
        ids = explicit_ids(ids)
        rows = _freeze(action, ids, payload)
        preview.action, preview.selection_ids, preview.frozen_rows = action, ids, rows
        preview.revision += 1
        preview.selection_digest = _digest(action, ids, rows)
        preview.save(update_fields=['action', 'selection_ids', 'frozen_rows', 'revision', 'selection_digest'])
        return preview


def _row_result(preview, row):
    """Receipt first, then frozen CAS. Never expand from a current filter."""
    with transaction.atomic():
        current = ManagementPreview.objects.get(pk=preview.pk)
        if current.revision != preview.revision or current.selection_digest != preview.selection_digest:
            raise ManagementConflict('核对修订已变化，请重新预览。')
        previous = ManagementResult.objects.filter(request_id=row['request_id']).first()
        if previous:
            if previous.preview_id != preview.pk or previous.payload_digest != row['payload_digest']:
                raise ManagementConflict('请求标识已用于不同内容。')
            return {'photo_id': row['photo_id'], 'status': previous.status, **previous.result}
        photo = Photo.objects.filter(pk=row['photo_id']).first()
        reason, status, operation = row['reason'], 'skipped', None
        if photo is None:
            return {'photo_id': row['photo_id'], 'status': 'skipped', 'reason': '照片不存在。'}
        if preview.action in PERSONAL_ACTIONS:
            from archive.services.management_metadata import set_personal_metadata
            result = {}
            if not reason:
                try:
                    payload = personal_payload(preview.action, row['payload'])
                    if preview.action == 'set_location' and not photo.current_classification:
                        raise ManagementConflict('照片尚未归档，请重新核对。')
                    result = set_personal_metadata(photo.pk, row['metadata_version'], row['request_id'], payload)
                    status = 'completed'
                except ManagementConflict as error:
                    reason = str(error)
                except ValueError as error:
                    status, reason = 'failed', str(error)
            result = {'reason':reason, 'metadata':result}
            ManagementResult.objects.create(request_id=row['request_id'], preview=preview, photo=photo,
                payload_digest=row['payload_digest'], status=status, result=result)
            return {'photo_id':row['photo_id'], 'status':status, **result}
        if preview.action in ('trash', 'restore'):
            if not reason:
                managed = getattr(photo, 'managed_original', None)
                if (photo.metadata_version != row['metadata_version'] or photo.management_generation != row['generation']
                        or (not photo.trashed_at or photo.trashed_at.isoformat() != row['trashed_at']
                            if preview.action == 'restore' else not editable_lifecycle(photo)) or managed is None
                        or managed.relative_path != row['source_path'] or managed.location_revision != row['location_revision']):
                    reason = '照片已变化，已跳过；请重新核对。'
                else:
                    try:
                        if preview.action == 'restore' and restore_origin(photo) != row['target_path']:
                            raise ManagementConflict('恢复原位置已变化，请重新核对。')
                        operation = accept_management(photo.pk, row['metadata_version'], row['request_id'], preview.action, {})
                        status = 'accepted'
                    except ManagementConflict as error:
                        reason = str(error)
            result = {'reason':reason, 'operation_id':str(operation.pk) if operation else None}
            ManagementResult.objects.create(request_id=row['request_id'], preview=preview, photo=photo,
                payload_digest=row['payload_digest'], status=status, result=result)
            return {'photo_id':row['photo_id'], 'status':status, **result}
        if preview.action != 'species':
            raise ValueError('管理动作无效。')
        if not reason:
            managed = getattr(photo, 'managed_original', None)
            if (photo.metadata_version != row['metadata_version'] or photo.management_generation != row['generation']
                    or not editable_lifecycle(photo) or managed is None
                    or managed.relative_path != row['source_path'] or managed.location_revision != row['location_revision']
                    or photo.current_classification != row['old_classification']
                    or sorted(str(link.species_id) for link in photo.confirmed_species.all()) != row['old_species_ids']):
                reason = '照片已变化，已跳过；请重新核对。'
            else:
                try:
                    with transaction.atomic():
                        if species_target(photo, row['payload']) != row['target']:
                            raise ManagementConflict('目标鸟种或目录已变化，请重新核对。')
                        operation = accept_management(photo.pk, row['metadata_version'], row['request_id'],
                            preview.action, row['payload'])
                        if operation.target_snapshot != row['target']:
                            raise ManagementConflict('目标目录已变化，请重新核对。')
                    status = 'accepted'
                except ManagementConflict as error:
                    reason = str(error)
                except ValueError as error:
                    status, reason = 'failed', str(error)
        result = {'reason': reason, 'operation_id': str(operation.pk) if status == 'accepted' else None}
        ManagementResult.objects.create(request_id=row['request_id'], preview=preview, photo=photo,
            payload_digest=row['payload_digest'], status=status, result=result)
        return {'photo_id': row['photo_id'], 'status': status, **result}


def submit_management_preview(preview_id, token):
    preview = checked_management_preview(preview_id, token)
    results = []
    for row in preview.frozen_rows:
        try:
            results.append(_row_result(preview, row))
        except DatabaseError:
            results.append({'photo_id': row['photo_id'], 'status': 'unprocessed', 'reason': '数据库暂不可用，尚未处理。'})
    return results
