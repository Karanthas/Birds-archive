"""Explicit bounded recognition actions; GET is observation only."""
import json
import uuid

from django.db import OperationalError
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from archive.models import Photo, RecognitionRun
from archive.services.recognition import request_rerun, retry_run, recognition_summary, RETRYABLE


def recognition_status_context(photo):
    summary = recognition_summary(photo)
    latest = photo.recognition_runs.order_by('-generation').first()
    active = summary['active_run']
    editable = photo.state == 'review_pending' and photo.original_available
    message = summary['message']
    if latest and (latest.state == 'stale' or latest.safe_error == 'source_changed'):
        message = '来源或照片信息已变化，请核对后重跑。'
    return dict(photo_id=str(photo.pk), state=summary['state'], message=message,
        metadata_version=photo.metadata_version, run_id=str(latest.pk) if latest else None,
        generation=latest.generation if latest else 0,
        active_run_id=str(active.pk) if active else None,
        active_generation=active.generation if active else 0,
        retry_allowed=bool(editable and latest and latest.state == 'failed' and latest.safe_error in RETRYABLE),
        rerun_allowed=editable)


def recognition_counts():
    return dict(pending_count=RecognitionRun.objects.filter(state__in=['queued', 'running', 'retry_wait']).count(),
        review_count=Photo.objects.filter(state='review_pending').count())


def _body(request):
    if len(request.body) > 65536:
        raise ValueError('invalid_request')
    value = json.loads(request.body)
    if not isinstance(value, dict):
        raise ValueError('invalid_request')
    return value


def _version(value):
    if type(value) is not int or value < 1:
        raise ValueError('invalid_request')
    return value


def _result(photo_id, version, request_id, action):
    try:
        run = action(photo_id, version, request_id)
        return dict(photo_id=str(photo_id), accepted=True, run_id=str(run.pk), generation=run.generation)
    except Photo.DoesNotExist:
        code = 'photo_unavailable'
    except OperationalError:
        code = 'database_busy'
    except ValueError as error:
        code = str(error) if str(error) in {'photo_changed', 'source_changed', 'retry_not_allowed',
            'request_conflict'} else 'invalid_request'
    return dict(photo_id=str(photo_id), accepted=False, reason=code)


@require_GET
def status(request):
    ids = request.GET.getlist('photo_id')
    if len(ids) > 200:
        return JsonResponse({'error': 'invalid_request'}, status=400)
    try:
        ids = [uuid.UUID(value) for value in ids]
    except (ValueError, TypeError, AttributeError):
        return JsonResponse({'error': 'invalid_request'}, status=400)
    photos = Photo.objects.filter(pk__in=ids).order_by('created_at', 'id')
    rows = []
    for photo in photos:
        rows.append(recognition_status_context(photo))
    return JsonResponse({'photos': rows, **recognition_counts(), 'checked_at': timezone.now().isoformat()})


@require_POST
def rerun(request):
    try:
        body = _body(request)
        request_id = uuid.UUID(body['request_id'])
        items = body['photos']
        if not isinstance(items, list) or not 1 <= len(items) <= 200:
            raise ValueError('invalid_request')
        # Validate the entire set before any mutation; never truncate or infer from a filter.
        parsed = [(uuid.UUID(item['photo_id']), _version(item['metadata_version'])) for item in items]
        if len({ident for ident, _ in parsed}) != len(parsed):
            raise ValueError('invalid_request')
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return JsonResponse({'error': 'invalid_request'}, status=400)
    return JsonResponse({'results': [_result(ident, version, uuid.uuid5(request_id, str(ident)), request_rerun)
        for ident, version in parsed]})


@require_POST
def retry(request, photo_id):
    try:
        body = _body(request)
        version, request_id = _version(body['metadata_version']), uuid.UUID(body['request_id'])
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return JsonResponse({'error': 'invalid_request'}, status=400)
    return JsonResponse(_result(photo_id, version, request_id, retry_run))
