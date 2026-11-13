"""仅由部署LAN入口开放；沿用全局Host、CSRF与no-store。"""
from urllib.parse import unquote

from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.http import require_GET, require_POST

from archive.services import upload


def _error(error):
    return JsonResponse(getattr(error, 'receipt', {
        'status': 'failed', 'code': error.code, 'message': upload.MESSAGES[error.code]}), status=error.status)


@require_GET
def token(request):
    return JsonResponse({'csrf_token': get_token(request), 'protocol': 'birds-delivery-v1'})


@require_POST
def files(request):
    try:
        if request.content_type != 'application/octet-stream':
            raise upload.UploadError('metadata_invalid', 400)
        try:
            filename = unquote(request.headers.get('X-Birds-Filename', ''), errors='strict')
        except UnicodeError:
            raise upload.UploadError('metadata_invalid', 400) from None
        result, status = upload.receive(request,
            request_id=request.headers.get('X-Birds-Request-ID', ''), filename=filename,
            length=request.headers.get('X-Birds-Length', ''),
            sha256=request.headers.get('X-Birds-SHA256', ''))
        return JsonResponse(result, status=status)
    except upload.UploadError as error:
        return _error(error)


@require_GET
def receipts(request, request_id):
    try:
        result, status = upload.receipt(request_id)
        return JsonResponse(result, status=status)
    except upload.UploadError as error:
        return _error(error)
