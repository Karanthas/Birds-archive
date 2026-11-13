import os
import math
import unicodedata
import uuid
from urllib.parse import urlencode
from django.utils import timezone
from django.conf import settings
from django.core import signing
from django.db import transaction
from django.db import DatabaseError
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.core.paginator import Paginator
from django.db.models import F, Q, Prefetch
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from archive.models import Photo, ImportSource, SourceObservation, Job, ArchiveCompletion
from archive.forms import BatchLocationForm, MissingCaptureTimeForm, CaptureCorrectionForm, SpeciesCorrectionForm
from archive.forms import PersonalNoteForm, ArchivedLocationForm, PersonalBatchForm, GalleryFilterForm
from archive.services.paths import open_root, open_regular_beneath, UnsafePath, parts_for, read_scan_status, read_scan_code
from archive.services.paths import read_worker_heartbeat
from archive.services.jobs import retry_job, STATE_LABELS, STAGE_LABELS, SAFE_ERRORS, ARCHIVE_STAGES
from archive.services.archiving import MESSAGES as ARCHIVE_ERRORS
from archive.services.archiving import open_completed_original, record_original_unavailable
from archive.services.images import ImageRejected
from archive.services.catalog import search_species, CatalogError
from archive.models import SourceSnapshot, Species
from archive.services.confirmation import (confirm_photo, ConfirmationConflict, ConfirmationValidation,
    search_confirmable_species, confirmed_photos_for_species)
from archive.services.gallery import query_gallery, InvalidGalleryCursor, group_gallery_items
from archive.services.recognition import recognition_summary
from archive.recognition_views import recognition_status_context, recognition_counts


def display_path(value):
    try:
        parts_for(value)
    except UnsafePath:
        return "来源路径不可用"
    return ''.join(' ' if unicodedata.category(char).startswith('C') else char for char in value)


def csrf_failure(request, reason=""):
    # 固定错误提示，不回显请求内容或内部拒绝原因。
    return render(request, "archive/base.html", {"error_message": "页面已过期，请刷新后重试。"}, status=403)


def _management_page(request, preview=None, *, error='', status=200, results=None):
    from archive.models import ManagementOperation
    from archive.services.management import describe_operation
    from archive.services.management_batch import management_token
    data = request.POST if request.method == 'POST' else request.GET
    draft = data if request.method == 'POST' or data else (
        preview.frozen_rows[0]['payload'] if preview and preview.frozen_rows else {})
    draft = draft.copy()
    draft.setdefault('mode', 'replace')
    draft.setdefault('classification', 'identified')
    form = SpeciesCorrectionForm(draft)
    def values(key):
        value = draft.getlist(key) if hasattr(draft, 'getlist') else draft.get(key, [])
        return [value] if isinstance(value, str) else value
    ids = data.getlist('photo_ids') or (preview.selection_ids if preview else [])
    selected = []
    for ident in values('species_ids'):
        try:
            selected.append(uuid.UUID(str(ident)))
        except ValueError:
            pass
    query = data.get('q', '')[:200]
    species = {item.pk:item for item in Species.objects.filter(pk__in=selected)}
    if query:
        species.update({item.pk:item for item in search_confirmable_species(query)[:24]})
    durable = list(preview.results.all()) if preview else []
    by_id = {str(item.photo_id): {'status': item.status, **item.result} for item in durable}
    if results is not None:
        by_id.update({item['photo_id']:item for item in results})
    rows = []
    labels = {'completed':'已完成', 'accepted':'已受理，后台完成前正式信息不变', 'skipped':'已跳过', 'failed':'失败', 'unprocessed':'未处理'}
    personal_initial = {'action': preview.action if preview else 'species'}
    if preview and preview.action not in ('species', 'trash', 'restore') and preview.frozen_rows:
        personal_initial.update(preview.frozen_rows[0]['payload'])
        if 'favorite' in personal_initial:
            personal_initial['favorite'] = 'true' if personal_initial['favorite'] else 'false'
    personal_batch_form = PersonalBatchForm(data if request.method == 'POST' else None,
        initial=personal_initial)
    for frozen in preview.frozen_rows if preview else []:
        row = dict(frozen)
        row['result'] = by_id.get(row['photo_id'])
        if row['result']:
            row['result_label'] = labels.get(row['result']['status'], '未处理')
            operation = ManagementOperation.objects.filter(pk=row['result'].get('operation_id')).first()
            row['operation'] = describe_operation(operation) if operation else None
        rows.append(row)
    return render(request, 'archive/management_preview.html', {'preview':preview, 'rows':rows,
        'photo_ids':ids, 'form':form, 'query':query, 'species_results':species.values(),
        'selected_ids':[str(ident) for ident in selected], 'local_names':values('local_names'),
        'token':management_token(preview) if preview else '', 'error_message':error,
        'has_results':bool(by_id), 'ready':sum(not row['reason'] for row in rows),
        'personal_batch_form':personal_batch_form,
        'personal_preview':bool(preview and preview.action not in ('species', 'trash', 'restore')),
        'restore_preview':bool(preview and preview.action == 'restore'),
        'trash_preview':bool(preview and preview.action == 'trash'), 'total':len(ids)}, status=status)


def _management_payload(data):
    action = data.get('action', 'species')
    if action in ('trash', 'restore'):
        return action, {}
    if action == 'species':
        form = SpeciesCorrectionForm(data)
        if not form.is_valid():
            raise ValueError('请明确选择操作、鸟种或无法确定。')
        return action, form.cleaned_data['payload']
    form = PersonalBatchForm(data)
    if not form.is_valid() or any(name in data for name in ('notes','capture','date','time','year')):
        raise ValueError('请明确选择有效整理目标；备注和时间须逐张修改。')
    return action, form.management_payload()


@require_http_methods(['GET', 'POST'])
def management_preview(request):
    from archive.services.management_batch import build_management_preview
    from archive.services.management import ManagementConflict
    if request.method == 'GET' or (request.POST.get('choose') and request.POST.get('action') not in ('trash', 'restore')) or request.POST.get('draft_search'):
        return _management_page(request)
    try:
        action, payload = _management_payload(request.POST)
        preview = build_management_preview(action, request.POST.getlist('photo_ids'), payload)
        return HttpResponseRedirect(reverse('management-display', args=[preview.pk]), status=303)
    except (ValueError, ManagementConflict) as error:
        return _management_page(request, error=str(error), status=400)


@require_GET
def management_display(request, preview_id):
    from archive.models import ManagementPreview
    return _management_page(request, get_object_or_404(ManagementPreview, pk=preview_id))


@require_POST
def management_revise(request, preview_id):
    from archive.models import ManagementPreview
    from archive.services.management_batch import revise_management_preview
    from archive.services.management import ManagementConflict
    preview = get_object_or_404(ManagementPreview, pk=preview_id)
    try:
        action, payload = _management_payload(request.POST)
        preview = revise_management_preview(preview_id, request.POST.get('token'), action=action,
            ids=request.POST.getlist('photo_ids'), payload=payload)
        return HttpResponseRedirect(reverse('management-display', args=[preview.pk]), status=303)
    except (ValueError, ManagementConflict) as error:
        return _management_page(request, preview, error=str(error), status=409)


@require_POST
def management_submit(request, preview_id):
    from archive.models import ManagementPreview
    from archive.services.management_batch import submit_management_preview
    from archive.services.management import ManagementConflict
    preview = get_object_or_404(ManagementPreview, pk=preview_id)
    try:
        results = submit_management_preview(preview_id, request.POST.get('token'))
        return _management_page(request, preview, results=results)
    except ManagementConflict as error:
        return _management_page(request, preview, error=str(error), status=409)


@require_GET
def gallery(request):
    if request.GET.get('view') == 'trash':
        from archive.services.management import describe_operation
        photos = Photo.objects.filter(Q(trash_requested_at__isnull=False) | Q(trashed_at__isnull=False)
            ).select_related('managed_original').order_by('-trash_requested_at', '-id')
        page = Paginator(photos, 24).get_page(request.GET.get('page', 1))
        for photo in page:
            operation = photo.management_operations.order_by('-created_at', '-id').first()
            photo.trash_operation = describe_operation(operation) if operation else None
        return render(request, 'archive/trash.html', {'trash_page':page})
    fragment = request.resolver_match.url_name == 'gallery-page'
    try:
        if len(request.GET.getlist('cursor')) > 1:
            raise InvalidGalleryCursor('相册参数无效。')
        if request.GET.get('cursor') is not None:
            from archive.services.browse_collection import decode_collection_token
            collection, _ = decode_collection_token(request.GET['cursor'])
            form = GalleryFilterForm(collection.filters)
        else:
            form = GalleryFilterForm(request.GET)
        if not form.is_valid():
            return render(request, 'archive/gallery_error.html', {'filter_form':form,
                'error_message':'筛选条件无效，请检查后重新筛选。'}, status=400)
        page = query_gallery(cursor=request.GET.get('cursor'), filters=form.normalized_filters())
        loaded = gallery_loaded_pages(request.GET)
        pages = [page]
        if not fragment:
            for _ in range(loaded-1):
                if not pages[-1].next_cursor:
                    break
                pages.append(query_gallery(cursor=pages[-1].next_cursor))
            page.items = [item for batch in pages for item in batch.items]
            page.groups = group_gallery_items(page.items)
            page.next_cursor = pages[-1].next_cursor
    except InvalidGalleryCursor as error:
        return render(request, 'archive/gallery_error.html', {'error_message': str(error)}, status=400)
    except Species.DoesNotExist:
        raise Http404 from None
    except DatabaseError:
        return render(request, 'archive/base.html', {'error_message':'暂时无法读取相册，请刷新重试。'}, status=503)
    context = {'gallery_page':page, 'fragment':fragment, 'filter_form':form,
        'filter_summary':form.summary(), 'gallery_loaded':len(pages),
        'gallery_page_tokens':[batch.cursor for batch in pages],
        'gallery_refresh_url':reverse('gallery')+'?'+urlencode(page.collection.filters, doseq=True)}
    for item in page.items:
        item.personal_request_id = str(uuid.uuid4())
    def query_params(cursor):
        return urlencode({'cursor':cursor})
    context['gallery_return_query'] = query_params(page.cursor)
    context['gallery_detail_query'] = urlencode({'return':'gallery', 'cursor':page.cursor, 'loaded':len(pages)})
    context['guide_index_url'] = reverse('guide_index')+'?'+urlencode({
        'album_cursor':page.cursor, 'album_loaded':len(pages)})
    if page.next_cursor:
        context['gallery_next_url'] = reverse('gallery-page')+'?'+query_params(page.next_cursor)
    return render(request, 'archive/gallery.html', context)


@require_GET
def catalog(request):
    query=request.GET.get('q','').strip()
    context={'query':query,'catalog_available':True,'configured':False}
    status=200
    try:
        context['configured']=SourceSnapshot.objects.exists()
        if len(query)>200:
            context['error_message']='查询不能超过 200 个字符。'
            status=400
        elif context['configured'] and query:
            context['page_obj']=Paginator(search_species(query).filter(source_kind='catalog'),24).get_page(request.GET.get('page',1))
            context['page_obj'].object_list=list(context['page_obj'].object_list)
    except (DatabaseError,CatalogError):
        context['error_message']='暂时无法读取数据。请刷新重试；若持续失败，请查看任务诊断。'
        status=503
    return render(request,'archive/catalog.html',context,status=status)


def workbench_page(filter_name='all', page_number=1):
    root_unavailable = read_scan_code() in ("root_missing", "root_permission", "root_identity_changed")
    if filter_name not in ("all", "pending", "running", "review", "failed", "duplicate"):
        filter_name = "all"
    sources = list(ImportSource.objects.select_related("photo", "current_observation").order_by("created_at", "id"))
    duplicates = [source for source in sources if source.state == "duplicate" and source.photo_id]
    waiting = [source for source in sources if not source.photo_id and source.state != "unavailable"]
    current_jobs={(job.source_id,job.source_version):job for job in Job.objects.filter(
        source_version=F('source__observed_version')).order_by('created_at','id')}
    rows = []
    if filter_name in ("all","review"):
        photos = Photo.objects.select_related('managed_original').prefetch_related(Prefetch("observations",
            queryset=SourceObservation.objects.select_related("source").order_by("first_seen", "id")), "sources")
        for photo in photos:
            if filter_name=='review' and photo.state!='review_pending':
                continue
            photo.original_available = photo.original_available and (hasattr(photo, 'managed_original') or not root_unavailable)
            observations = list(photo.observations.all())
            legacy = list(photo.sources.all())
            photo.display_name = display_path(observations[0].source.relative_path) if observations else (
                display_path(legacy[0].relative_path) if legacy else "照片")
            rows.append({"kind": "photo", "photo": photo, "created_at": photo.created_at, "id": str(photo.pk)})
    if filter_name in ("all", "pending", "running", "failed"):
        for source in waiting:
            job=current_jobs.get((source.pk,source.observed_version))
            source.display_state=job.state if job else source.state
            if filter_name=='pending' and source.display_state not in ('pending','queued','waiting','retry_wait'):
                continue
            if filter_name in ('running','failed') and source.display_state!=filter_name:
                continue
            source.display_path = display_path(source.relative_path)
            rows.append({"kind": "source", "source": source, "created_at": source.created_at, "id": str(source.pk)})
    if filter_name in ("all", "duplicate"):
        for source in duplicates:
            source.display_path = display_path(source.relative_path)
            rows.append({"kind": "duplicate", "source": source, "created_at": source.created_at, "id": str(source.pk)})
    rows.sort(key=lambda item: (item["created_at"], item["id"]))
    page = Paginator(rows, 24).get_page(page_number)
    return page, filter_name, duplicates


@require_GET
def workbench(request):
    page, filter_name, duplicates = workbench_page(request.GET.get('filter','all'),request.GET.get('page',1))
    for row in page:
        if row['kind'] == 'photo':
            row['recognition'] = recognition_status_context(row['photo'])
    return render(request, "archive/workbench.html", {
        'recognition_counts': recognition_counts(),
        "page_obj": page, "filter_name": filter_name, "photo_count": Photo.objects.count(),
        # 仅重排本页呈现；分页成员、顺序签名和可编辑选择仍取原page。
        "display_rows": sorted(page, key=lambda row: row['kind'] != 'photo'),
        "duplicate_count": len(duplicates), "scan_diagnostic": read_scan_status(),
        "status_checked_at": timezone.now(),
        "page_snapshot": signing.dumps({str(row['photo'].pk): row['photo'].metadata_version
            for row in page if row['kind']=='photo' and row['photo'].state=='review_pending'},
            salt='workbench-selection', compress=True),
        'saved_message': request.session.pop('metadata_saved', ''),
        "jobs": Job.objects.filter(source_version=F("source__observed_version"))
                   .select_related("source").order_by("created_at", "id"),
    })


TASK_STATES = {'queued', 'running', 'retry_wait', 'succeeded', 'failed', 'stale', 'blocked', 'awaiting_executor'}


def task_navigation(data):
    state = data.get('state', '')
    try:
        page = max(1, int(data.get('page', '1')))
    except (ValueError, TypeError):
        page = 1
    return {'state': state if state in TASK_STATES else '', 'page': page}


def task_context(request):
    from archive.models import ManagementOperation
    from archive.services.management import describe_operation
    selected=task_navigation(request.GET)['state']
    queryset=Job.objects.select_related('source', 'archive_operation').order_by('next_attempt_at','created_at','id')
    if selected:
        queryset=queryset.filter(state__in=['pending','queued'] if selected=='queued' else [selected])
    page=Paginator(queryset,24).get_page(request.GET.get('page',1))
    for job in page:
        archive_job = job.kind == 'archive'
        errors = ARCHIVE_ERRORS if archive_job else SAFE_ERRORS
        fallback = 'sync_failed' if archive_job else 'processing_failed'
        job.kind_label = '归档' if archive_job else '导入'
        job.display_name=display_path(job.archive_operation.original_basename if archive_job else job.source.relative_path)
        job.state_label=STATE_LABELS.get(job.state,'状态未知')
        job.stage_label=(ARCHIVE_STAGES if archive_job else STAGE_LABELS).get(job.stage,'阶段未知')
        job.display_code=job.error_code if job.error_code in errors else fallback
        job.display_error=errors.get(job.error_code,errors[fallback])
        job.can_retry=(job.state in ('failed', 'blocked') if archive_job else
            (job.state=='failed' and job.source_version==job.source.observed_version
             and job.source.current_observation_id is not None))
    heartbeat=read_worker_heartbeat()
    stale=heartbeat is not None and (timezone.now()-heartbeat).total_seconds()>max(30,settings.BIRDS_SCAN_SECONDS*3)
    workbench, _, duplicates = workbench_page(request.GET.get('workbench_filter','all'),
                                            request.GET.get('workbench_page',1))
    task_counts={state:Job.objects.filter(state__in=['pending','queued'] if state=='queued' else [state]).count()
                 for state in ('queued','running','failed')}
    management_rows = ManagementOperation.objects.select_related('photo__managed_original').order_by('-updated_at', '-id')
    if selected:
        management_rows = management_rows.filter(status__in={
            'queued': ['pending'], 'failed': ['failed', 'blocked']}.get(selected, [selected]))
    management_page = Paginator(management_rows, 24).get_page(request.GET.get('management_page', 1))
    for operation in management_page:
        describe_operation(operation)
        operation.retry_request_id = str(uuid.uuid4())
    return {'task_page':page,'state_filter':selected,
            'management_page': management_page,
            'job_count':Job.objects.count(),'photo_count':Photo.objects.count(),'heartbeat':heartbeat,
            'duplicate_count':len(duplicates),
            'task_counts':task_counts,
            'editable_ids':' '.join(str(row['photo'].pk) for row in workbench
                if row['kind']=='photo' and row['photo'].state=='review_pending'),
            'heartbeat_old':stale,'scan_diagnostic':read_scan_status(),
            'status_checked_at':timezone.now(),'input_scope':'已配置的导入目录'}


@require_GET
def tasks(request):
    return render(request,'archive/tasks.html',{**task_context(request),
        'task_message': request.session.pop('task_message', '')})


@require_GET
def task_status(request):
    if request.GET.get('scope') == 'archive':
        return render(request, 'archive/tasks.html', {**archive_status_context(),
            'fragment': True, 'archive_only': True})
    return render(request,'archive/tasks.html',{**task_context(request),'fragment':True})


def archive_status_context():
    jobs = Job.objects.filter(kind='archive')
    failures = []
    for job in jobs.filter(state__in=['failed', 'blocked']).select_related('archive_operation').order_by('updated_at', 'id'):
        code = job.error_code if job.error_code in ARCHIVE_ERRORS else 'sync_failed'
        failures.append({'key': f'{job.archive_operation.pk}:{code}:{job.updated_at.isoformat()}',
                         'message': ARCHIVE_ERRORS[code]})
    return {'archive_total': jobs.count(), 'archive_completed': jobs.filter(state='succeeded').count(),
        'archive_waiting': jobs.exclude(state__in=['failed', 'blocked', 'succeeded']).count(),
        'archive_failure_count': len(failures), 'archive_failures': failures}


@require_POST
def task_retry(request,job_id):
    get_object_or_404(Job,pk=job_id)
    navigation = task_navigation(request.POST)
    try:
        version=int(request.POST.get('version',''))
    except (ValueError,TypeError):
        message = '任务版本无效，请刷新状态。'
    else:
        _,queued,reason=retry_job(job_id,version)
        message=('任务已在队列中。' if reason=='already_queued' else '已加入重试队列。') if queued else (
            '来源已变化，请刷新状态。' if reason=='version_conflict' else
            ARCHIVE_ERRORS.get(reason, '任务状态已更新。'))
    request.session['task_message'] = message
    return redirect(f"/tasks/?state={navigation['state']}&page={navigation['page']}")


class MetadataConflict(Exception):
    pass


def selected_page_photos(form):
    page, _, _ = workbench_page(form.cleaned_data['filter'],form.cleaned_data['page'])
    ids = form.cleaned_data['photo_ids']
    editable = {row['photo'].pk for row in page if row['kind']=='photo' and row['photo'].state=='review_pending'}
    photos = list(Photo.objects.filter(pk__in=ids).order_by('created_at','id'))
    from archive.services.management import editable_lifecycle
    if set(ids) - editable or len(photos)!=len(ids) or any(not editable_lifecycle(photo) for photo in photos):
        raise MetadataConflict('选择已变化，请返回重新选择。')
    return photos


def location_snapshot(form, photos):
    return {'ids':[str(p.pk) for p in photos], 'versions':{str(p.pk):p.metadata_version for p in photos},
            'location':form.cleaned_data['location'], 'filter':form.cleaned_data['filter'],'page':form.cleaned_data['page']}


@require_http_methods(['GET','POST'])
def location(request):
    data = request.POST if request.method=='POST' else request.GET
    form = BatchLocationForm(data)
    photos, snapshot, error, status = [], '', '', 200
    conflicts=[]
    if form.is_valid():
        try:
            if request.method=='POST':
                try:
                    frozen = signing.loads(data.get('snapshot',''),salt='photo-location',max_age=3600)
                except signing.BadSignature:
                    raise MetadataConflict('预览已失效，请重新核对。') from None
                with transaction.atomic():
                    photos = selected_page_photos(form)
                    current = location_snapshot(form,photos)
                    if frozen != current or any(data.get(f'version_{p.pk}')!=str(p.metadata_version) for p in photos):
                        raise MetadataConflict('照片信息已变化，本次未保存。请核对当前值与草稿。')
                    count=0
                    for photo in photos:
                        if photo.user_location==form.cleaned_data['location']:
                            continue
                        changed=Photo.objects.filter(pk=photo.pk,metadata_version=photo.metadata_version,
                                                     state='review_pending',trash_requested_at__isnull=True,
                                                     trashed_at__isnull=True).update(
                            user_location=form.cleaned_data['location'],metadata_version=F('metadata_version')+1)
                        if changed!=1:
                            raise MetadataConflict('照片信息已变化，本次未保存。')
                        count+=1
                request.session['metadata_saved'] = f'已保存 {count} 张地点。' if count else '地点已是当前值。'
                return redirect(reverse('workbench')+'?'+urlencode({
                    'filter': form.cleaned_data['filter'], 'page': form.cleaned_data['page']}))
            photos=selected_page_photos(form)
            if data.get('page_snapshot'):
                try:
                    selected_versions=signing.loads(data['page_snapshot'],salt='workbench-selection',max_age=3600)
                except signing.BadSignature:
                    raise MetadataConflict('选择已过期，请返回重新选择。') from None
                if not isinstance(selected_versions,dict) or any(
                    selected_versions.get(str(p.pk))!=p.metadata_version for p in photos):
                    raise MetadataConflict('照片信息已变化，本次未保存。请核对当前值与草稿。')
            snapshot=signing.dumps(location_snapshot(form,photos),salt='photo-location',compress=True)
        except MetadataConflict as conflict:
            error,status=str(conflict),409
            photos=list(Photo.objects.filter(pk__in=form.cleaned_data['photo_ids']).order_by('created_at','id'))
            versions = frozen.get('versions',{}) if request.method=='POST' and isinstance(locals().get('frozen'),dict) else {}
            actual_ids={p.pk for p in photos}
            conflicts=[str(ident) for ident in form.cleaned_data['photo_ids'] if ident not in actual_ids]
            for photo in photos:
                photo.metadata_conflict=(photo.state!='review_pending' or (versions and versions.get(str(photo.pk))!=photo.metadata_version))
                if photo.metadata_conflict:
                    conflicts.append(str(photo.pk))
    else:
        status=400
        # Validate selection separately so a location error cannot discard the draft IDs.
        if 'photo_ids' in data:
            probe=data.copy();probe['location']='地点草稿'
            selection=BatchLocationForm(probe)
            if selection.is_valid():
                try:
                    photos=selected_page_photos(selection)
                    if request.method=='GET' and 'location' not in data:
                        status=200
                        form=BatchLocationForm(initial={'filter':selection.cleaned_data['filter'],'page':selection.cleaned_data['page']})
                except MetadataConflict as conflict:
                    error,status=str(conflict),409
    form.fields['location'].widget.attrs['aria-describedby'] = 'location-help'
    for photo in photos:
        source=photo.observations.select_related('source').order_by('first_seen','id').first()
        photo.display_name=display_path(source.source.relative_path) if source else '照片'
    photos_by_id = {str(photo.pk): photo for photo in photos}
    conflict_details = []
    for ident in conflicts:
        photo = photos_by_id.get(ident)
        conflict_details.append({'id': ident, 'name': photo.display_name if photo else '照片已不可用',
                                 'available': photo is not None and photo.state == 'review_pending',
                                 'current_location': photo.user_location if photo else ''})
    return render(request,'archive/metadata.html',{'form':form,'photos':photos,'snapshot':snapshot,
                  'page_snapshot':signing.dumps({str(p.pk):p.metadata_version for p in photos},
                      salt='workbench-selection',compress=True),
                  'error_message':error,'conflicts':conflicts,'conflict_details':conflict_details,
                  'mode':'location','new_location':form.cleaned_data.get('location','') if form.is_bound else '',
                  'return_filter':data.get('filter','all'),'return_page':data.get('page','1')},status=status)


def candidates_context(photo):
    """Read only the published snapshot; candidate ranks never imply a species set."""
    summary = recognition_summary(photo)
    run = summary['active_run']
    items = []
    manifest = run.artifact.manifest if run and run.artifact_id else {}
    manifest = manifest if isinstance(manifest, dict) else {}
    if run:
        for candidate in run.candidates.select_related('species__source_snapshot').order_by('rank')[:3]:
            mapped = (candidate.mapping_status == 'mapped' and candidate.species_id
                and candidate.species.source_kind == 'catalog' and candidate.mapping_evidence.strip())
            items.append({'rank': candidate.rank, 'raw_label': candidate.raw_label,
                'mapping_status': candidate.mapping_status, 'species': candidate.species if mapped else None,
                'mapping_evidence': candidate.mapping_evidence, 'score': candidate.score})
    from archive.services.recognition_policy import candidate_ambiguity
    ambiguity = candidate_ambiguity(manifest, [item['score'] for item in items])
    return {'items': items, 'run': run, 'policy': manifest, 'ambiguity': ambiguity,
        'manual_first': ambiguity != 'separated'}


@require_http_methods(['GET', 'POST'])
def detail(request, photo_id):
    photo = get_object_or_404(Photo.objects.select_related('managed_original',
        'archive_completion__operation'), pk=photo_id)
    from archive.services.management import editable_lifecycle
    if request.method == 'POST' and not editable_lifecycle(photo) and 'management_action' not in request.POST:
        return HttpResponse('照片已进入回收流程，只可查看、下载或处理原操作。', status=409)
    try:
        navigation = detail_navigation(request.GET, photo)
    except InvalidGalleryCursor as invalid:
        return render(request, 'archive/base.html', {'error_message': str(invalid)}, status=400)
    error, status = '', 200
    if request.method == 'GET' and request.GET.get('management_action') == 'restore':
        from archive.services.management import restore_confirmation, restore_origin, ManagementConflict
        try:
            return render(request, 'archive/trash.html', {'confirmation_photo':photo,
                'restore_token':restore_confirmation(photo), 'restore_path':restore_origin(photo),
                'restore_confirmation':True})
        except ManagementConflict as conflict:
            error, status = str(conflict), 409
    if request.method == 'GET' and request.GET.get('management_action') == 'trash':
        from archive.services.management import trash_confirmation, require_editable, UNRESOLVED, ManagementConflict
        try:
            require_editable(photo)
            if not photo.current_classification or not hasattr(photo, 'managed_original'):
                raise ManagementConflict('只可回收已归档照片。')
            if photo.management_operations.filter(status__in=UNRESOLVED).exists():
                raise ManagementConflict('已有更正尚未完成，请先处理原操作。')
            return render(request, 'archive/trash.html', {'confirmation_photo':photo,
                'trash_token':trash_confirmation(photo)})
        except ManagementConflict as conflict:
            error, status = str(conflict), 409
    capture_data = request.POST.copy()
    if 'mode' not in capture_data:
        capture_data['mode'] = 'manual'
    capture_form = CaptureCorrectionForm(capture_data if request.method == 'POST' and
        request.POST.get('management_action') == 'capture' else None)
    species_form = SpeciesCorrectionForm(request.POST if request.method == 'POST' and
        request.POST.get('management_action') == 'species' else None)
    personal_field = request.POST.get('personal_field') if request.method == 'POST' else None
    note_form = PersonalNoteForm(request.POST if personal_field == 'notes' else None,
        initial={'notes': photo.notes})
    location_form = ArchivedLocationForm(request.POST if personal_field == 'user_location' else None,
        initial={'user_location': photo.user_location})
    selected_ids = request.POST.getlist('species_ids') if request.method == 'POST' else request.GET.getlist('species_ids')
    draft_data = request.POST if request.method == 'POST' else request.GET
    request_id = draft_data.get('request_id', '')
    if request.method == 'GET' and not request_id:
        request_id = str(uuid.uuid4())
    local_names = [name for name in draft_data.getlist('local_names') if name != '']
    classification = draft_data.get('classification', 'species')
    if request.method == 'POST' and 'management_action' in request.POST:
        from archive.services.management import accept_management, retry_management, ManagementConflict
        try:
            actions = request.POST.getlist('management_action')
            if actions == ['restore']:
                from archive.services.management import accept_confirmed_restore
                accept_confirmed_restore(photo.pk, request.POST.get('restore_token'))
                return HttpResponseRedirect(reverse('detail', args=[photo.pk])+'?return=trash#restore', status=303)
            elif actions == ['trash']:
                from archive.services.management import accept_confirmed_trash
                accept_confirmed_trash(photo.pk, request.POST.get('trash_token'))
                return HttpResponseRedirect(reverse('detail', args=[photo.pk]), status=303)
            elif actions == ['personal']:
                from archive.services.management_metadata import set_personal_metadata
                if personal_field == 'favorite' and request.POST.get('favorite') in ('true', 'false'):
                    changes = {'favorite': request.POST['favorite'] == 'true'}
                elif personal_field == 'rating':
                    value = request.POST.get('rating')
                    if value not in ('', '1', '2', '3', '4', '5'):
                        raise ValueError('请选择 1–5 星或清除评分。')
                    changes = {'rating': int(value) if value else None}
                elif personal_field == 'notes' and note_form.is_valid():
                    changes = {'notes': note_form.cleaned_data['notes']}
                elif personal_field == 'user_location' and location_form.is_valid():
                    changes = {'user_location': location_form.cleaned_data['user_location']}
                else:
                    raise ValueError('整理内容无效，请检查长度与目标值。')
                # A personal form cannot smuggle a second field through a native POST.
                if any(key in request.POST for key in {'favorite','rating','notes','user_location'} - set(changes)):
                    raise ValueError('每次只保存当前整理字段。')
                result = set_personal_metadata(photo.pk, int(request.POST.get('metadata_version', '')),
                    request.POST.get('request_id'), changes)
                photo.refresh_from_db()
                if request.headers.get('Accept') == 'application/json':
                    from archive.services.management_metadata import personal_state
                    return JsonResponse({**personal_state(photo), 'request_id':result['request_id'],
                        'next_request_id': str(uuid.uuid4())})
                request.session['metadata_saved'] = '个人整理已保存。'
                if request.POST.get('personal_return') == 'gallery':
                    return HttpResponseRedirect(reverse('gallery') + ('?'+request.POST['gallery_query']
                        if request.POST.get('gallery_query') else ''), status=303)
            elif actions == ['retry']:
                retry_management(photo.pk, request.POST.get('operation_id'), request.POST.get('request_id'))
                request.session['metadata_saved'] = '已核对原更正，将从原阶段继续；完成前正式信息保持原值。'
            elif actions == ['capture'] and capture_form.is_valid():
                accept_management(photo.pk, int(request.POST.get('metadata_version', '')),
                    request.POST.get('request_id'), 'capture', capture_form.management_payload())
                request.session['metadata_saved'] = '已受理时间更正，后台完成后更新正式时间与年份。'
            elif actions == ['species'] and species_form.is_valid():
                accept_management(photo.pk, int(request.POST.get('metadata_version', '')),
                    request.POST.get('request_id'), 'species', species_form.cleaned_data['payload'])
                request.session['metadata_saved'] = '已受理鸟种更正，后台完成后更新正式鸟种与目录。'
            else:
                raise ValueError('请明确选择有效鸟种与操作。' if actions == ['species'] else '请填写有效的拍摄时间。')
            return HttpResponseRedirect(reverse('detail', args=[photo.pk]) +
                ('?'+request.GET.urlencode() if request.GET else ''), status=303)
        except ManagementConflict as conflict:
            error, status = str(conflict), 409
        except (ValueError, TypeError) as invalid:
            error, status = str(invalid) or '更正信息无效。', 400
        except DatabaseError:
            error, status = '暂时无法保存，请刷新核对后重试。', 503
        photo.refresh_from_db()
        if actions == ['personal'] and request.headers.get('Accept') == 'application/json':
            from archive.services.management_metadata import personal_state
            return JsonResponse({**personal_state(photo), 'error':error,
                'next_request_id': str(uuid.uuid4())}, status=status)
    elif request.method == 'POST' and 'draft_action' in request.POST:
        try:
            actions = request.POST.getlist('draft_action')
            if (len(actions) != 1 or actions[0] not in {'search', 'add', 'remove', 'page', 'append', 'candidate',
                    'recognition_retry', 'recognition_rerun'}
                    or 'classification' in request.POST):
                raise ValueError
            action = actions[0]
            draft_selected = {uuid.UUID(value) for value in selected_ids}
            if action in ('recognition_retry', 'recognition_rerun'):
                from archive.services.recognition import request_rerun, retry_run
                service = retry_run if action == 'recognition_retry' else request_rerun
                try:
                    service(photo.pk, int(request.POST.get('metadata_version', '')),
                        uuid.UUID(request.POST.get('recognition_request_id', request_id)))
                except DatabaseError:
                    raise ValueError from None
            if action == 'candidate':
                if (photo.state != 'review_pending' or len(request.GET.getlist('candidate_run')) != 1
                        or len(request.GET.getlist('candidate_rank')) != 1):
                    raise ValueError
                candidates = candidates_context(photo)
                run_id = uuid.UUID(request.GET['candidate_run'])
                rank = int(request.GET['candidate_rank'])
                candidate = next((item for item in candidates['items'] if item['rank'] == rank), None)
                if not candidates['run'] or candidates['run'].pk != run_id or not candidate or not candidate['species']:
                    raise ValueError
                draft_selected.add(candidate['species'].pk)
            if action in ('add', 'remove'):
                if len(request.GET.getlist('draft_species')) != 1:
                    raise ValueError
                ident = uuid.UUID(request.GET['draft_species'])
                if action == 'add':
                    draft_selected.add(ident)
                else:
                    draft_selected.discard(ident)
            if action == 'page' and len(request.GET.getlist('draft_page')) != 1:
                raise ValueError
            page = 1 if action == 'search' else int(
                request.GET['draft_page'] if action == 'page' else request.POST.get('species_page', '1'))
            if page < 1:
                raise ValueError
            # Explicit output allowlist: never copy POST wholesale into a URL (CSRF lives only in its body).
            draft_query = urlencode({'q': request.POST.get('q', ''), 'species_page': page,
                'species_ids': sorted(str(ident) for ident in draft_selected), 'local_names': local_names,
                'request_id': request_id, 'metadata_version': request.POST.get('metadata_version', '')}, doseq=True)
            return HttpResponseRedirect(reverse('detail', args=[photo.pk])+'?'+draft_query, status=303)
        except (ValueError, TypeError, KeyError):
            error, status = '草稿操作无效，请核对后重试。', 400
    elif request.method == 'POST':
        try:
            operation, next_photo = confirm_photo(photo.pk, int(request.POST.get('metadata_version', '')),
                uuid.UUID(request_id), request.POST.get('classification', ''), selected_ids,
                request.POST.getlist('local_names'))
            next_url = reverse('detail', args=[next_photo.pk]) if next_photo else reverse('workbench')
            if next_photo is None:
                request.session['metadata_saved'] = '审核结束，已确认的照片等待后台归档。'
            if request.headers.get('X-Archive-Confirm') == 'true':
                return JsonResponse({'accepted': True, 'next_url': next_url, 'operation_id': str(operation.pk)})
            response = HttpResponseRedirect(next_url, status=303)
            if request.headers.get('HX-Request') == 'true':
                response['HX-Redirect'] = next_url
                response['X-Next-URL'] = next_url
            return response
        except ConfirmationValidation as invalid:
            error, status = str(invalid), 400
        except (ValueError, TypeError):
            error, status = '请明确选择有效鸟种后确认。', 400
        except ConfirmationConflict as conflict:
            error, status = str(conflict), 409
        photo.refresh_from_db()
    query = request.GET.get('q', request.POST.get('q', '')).strip()
    try:
        selected = {uuid.UUID(value) for value in selected_ids}
    except (ValueError, TypeError):
        selected = set()
    # GET actions update only this submitted draft; final decisions remain POST-only.
    if request.method == 'GET':
        for action in ('add_species', 'remove_species'):
            if request.GET.get(action):
                try:
                    ident = uuid.UUID(request.GET[action])
                except (ValueError, TypeError):
                    error, status = '鸟种标识无效。', 400
                else:
                    if action == 'add_species':
                        selected.add(ident)
                    else:
                        selected.discard(ident)
    selected_species = list(Species.objects.filter(pk__in=selected))
    selected_ids = [str(value) for value in selected]
    results, species_search_page = [], None
    search_links = {}
    if len(query) > 200:
        error, status = '查询不能超过 200 个字符。', 400
    elif query:
        species_search_page = Paginator(search_confirmable_species(query), 24).get_page(request.GET.get('species_page', 1))
        results = list(species_search_page)
        for direction in ('next', 'previous'):
            if getattr(species_search_page, 'has_'+direction)():
                search_links['species_search_'+direction] = getattr(species_search_page, direction+'_page_number')()
    collection_species, species_photos = None, []
    if request.GET.get('species') and request.GET.get('return') != 'gallery':
        try:
            species_id = uuid.UUID(request.GET['species'])
        except (ValueError, TypeError):
            error, status = '鸟种标识无效。', 400
        else:
            collection_species = get_object_or_404(Species, pk=species_id)
            species_photos = confirmed_photos_for_species(species_id)
    # 搜索结果改变不丢失已选集合；取消勾选即可移除，默认不选任何鸟种。
    species_results = {item.pk: item for item in [*selected_species, *results]}
    observations = list(photo.observations.select_related("source").order_by("first_seen", "id"))
    job_pairs = Q(pk__in=[])
    for observation in observations:
        observation.display_path = display_path(observation.source.relative_path)
        observation.is_current = observation.source.current_observation_id == observation.pk
        job_pairs |= Q(source_id=observation.source_id, source_version=observation.version)
    legacy = list(photo.sources.all())
    photo.display_name = observations[0].display_path if observations else (
        display_path(legacy[0].relative_path) if legacy else "照片")
    all_page,_,_=workbench_page()
    location_page=next((index//24+1 for index,row in enumerate(all_page.paginator.object_list)
                        if row['kind']=='photo' and row['photo'].pk==photo.pk),1)
    jobs=Job.objects.filter(job_pairs).order_by('created_at','id')
    for job in jobs:
        errors = ARCHIVE_ERRORS if job.kind == 'archive' else SAFE_ERRORS
        job.display_error=errors.get(job.error_code, errors['sync_failed' if job.kind=='archive' else 'processing_failed'])
        job.state_label=STATE_LABELS.get(job.state, '状态未知')
        job.stage_label=ARCHIVE_STAGES.get(job.stage, '阶段未知') if job.kind=='archive' else STAGE_LABELS.get(job.stage, '阶段未知')
    effective_capture = (photo.current_capture or {}) if hasattr(
        photo, 'archive_completion') else photo.effective_capture()
    download_available = False
    if hasattr(photo, 'archive_completion'):
        try:
            original, _ = open_completed_original(photo)
        except (OSError, UnsafePath, ImageRejected, ValueError, KeyError, TypeError):
            record_original_unavailable(photo)
            photo.original_available = False
        else:
            original.close()
            download_available = True
            photo.original_available = True
    from archive.services.management import describe_operation
    management_operation = photo.management_operations.filter(status__in=['pending','running','failed','blocked']).first()
    latest_management = management_operation or photo.management_operations.order_by('-created_at', '-id').first()
    if latest_management:
        describe_operation(latest_management)
    return render(request, "archive/detail.html", {"photo": photo, "observations": observations,
        "jobs": jobs, **archive_status_context(),
        'recognition': recognition_summary(photo),
        'recognition_status': recognition_status_context(photo),
        'recognition_counts': recognition_counts(), 'status_checked_at': timezone.now(),
        'candidates': candidates_context(photo),
        'download_available': download_available,
        'management_operation': management_operation, 'management_status': latest_management,
        'current_original': photo.managed_original if hasattr(photo, 'managed_original') else None,
        'capture_form': capture_form, 'capture_draft': capture_form.is_bound,
        'species_form': species_form, 'species_draft': species_form.is_bound,
        'note_form': note_form, 'location_form': location_form,
        'personal_request_id': str(uuid.uuid4()), 'note_request_id': str(uuid.uuid4()),
        'location_request_id': str(uuid.uuid4()),
        'management_request_id': request.POST.get('request_id') or str(uuid.uuid4()),
        'retry_request_id': str(uuid.uuid4()),
        "original_available": photo.original_available and (hasattr(photo, 'managed_original') or
            read_scan_code() not in ("root_missing", "root_permission", "root_identity_changed")),
        **return_context(request.GET), **navigation,
        'effective_capture': effective_capture,
        'effective_capture_source': {'manual':'人工补充', 'exif':'原始 EXIF'}.get(effective_capture.get('source'), '未知'),
        'archive_classification': ('undetermined' if photo.current_classification == 'unidentified' else 'species') if hasattr(photo,'archive_completion') else '',
        'saved_message':request.session.pop('metadata_saved',''),
        'location_page':location_page,
        'error_message': error, 'request_id': request_id,
        'recognition_request_id': str(uuid.uuid4()),
        'confirmation_version': draft_data.get('metadata_version', photo.metadata_version),
        'species_results': list(species_results.values()), 'selected_species': selected_species,
        'selected_ids': selected_ids, 'query': query,
        'local_names': local_names, 'classification': classification,
        'confirmed_species': Species.objects.filter(confirmed_photos__photo=photo),
        'collection_species': collection_species, 'species_photos': species_photos,
        'species_search_page': species_search_page, **search_links,
    }, status=status)


MAX_GALLERY_RETURN_PAGES = 20


def gallery_loaded_pages(data):
    try:
        if hasattr(data, "getlist") and len(data.getlist("loaded")) > 1:
            raise ValueError
        loaded = int(data.get("loaded", 1))
        if not 1 <= loaded <= MAX_GALLERY_RETURN_PAGES:
            raise ValueError
        return loaded
    except (ValueError, TypeError):
        raise InvalidGalleryCursor("返回浏览范围无效，请重新筛选。") from None


def detail_navigation(data, photo):
    from archive.services.browse_collection import collection_neighbors, encode_collection_token
    default = "gallery" if hasattr(photo, "archive_completion") and "filter" not in data else "workbench"
    destination = data.get("return", default)
    extra = {"previous_url":None, "next_url":None, "collection_visible":True, "has_collection":False}
    if destination in ("gallery", "guide"):
        guide_params = {}
        guide_species = None
        if destination == 'guide':
            extra['return_label'] = '返回鸟种图鉴'
            from archive.services.guide import guide_context, species_collection
            from archive.services.gallery import species_filter_id
            guide_params, _ = guide_context(data, from_photo=True)
            guide_species = species_filter_id(data.get('species'))
            if not guide_species:
                raise InvalidGalleryCursor('鸟种标识无效。')
        cursor = data.get("cursor")
        if hasattr(data, "getlist") and len(data.getlist("cursor")) > 1:
            raise InvalidGalleryCursor("浏览集合参数无效，请重新筛选。")
        loaded = gallery_loaded_pages(data)
        params = {}
        anchor = ""
        if cursor is not None:
            neighbors = collection_neighbors(cursor, photo.pk)
            if guide_species:
                species_collection(guide_species, cursor)
                from archive.services.browse_collection import visible_photos
                current = visible_photos().filter(confirmed_species__species_id=guide_species)
                members = neighbors['collection'].members.filter(photo_id__in=current.values('pk'))
                neighbors.update(visible=members.filter(photo_id=photo.pk).exists(),
                    previous=members.filter(position__lt=neighbors['position']).order_by('-position').values_list('photo_id', flat=True).first(),
                    next=members.filter(position__gt=neighbors['position']).order_by('position').values_list('photo_id', flat=True).first())
            # Read only the bounded restored window. Crossing its edge starts a new
            # signed window around the current member, never a whole-library fallback.
            tokens, seen, current = [], set(), cursor
            for _ in range(loaded):
                if not current:
                    break
                batch = species_collection(guide_species, current) if guide_species else query_gallery(cursor=current)
                tokens.append(current); seen.update(item.pk for item in batch.items)
                current = batch.next_cursor
            if photo.pk not in seen and neighbors["visible"]:
                cursor = encode_collection_token(neighbors["collection"], max(0, neighbors["position"]-24))
                loaded = 1
            params = {"cursor":cursor, "loaded":loaded}
            if guide_species:
                params.update(species=guide_species, guide_q=guide_params['q'], guide_page=guide_params['page'],
                    **{key:value for key,value in guide_params.items() if key.startswith('album_')})
            anchor = "#photo-"+str(photo.pk)
            extra.update(has_collection=True, collection_visible=neighbors["visible"])
            for key in ("previous", "next"):
                ident = neighbors[key]
                if ident:
                    extra[key+"_url"] = reverse("detail", args=[ident])+"?"+urlencode({"return":destination, **params})
        if guide_species:
            if cursor is None:
                raise InvalidGalleryCursor('图鉴浏览集合无效。')
            url = reverse('guide_detail', args=[guide_species])+'?'+urlencode({**guide_params,
                'cursor':cursor, 'loaded':loaded})+anchor
        else:
            url = reverse("gallery")+("?"+urlencode(params) if params else "")+anchor
        refresh_params = {"return":destination, **params}
    elif destination == "trash":
        url = reverse('gallery')+'?view=trash'
        refresh_params = {'return':'trash'}
    elif destination == "workbench":
        navigation = return_context(data)
        params = {"filter": navigation["return_filter"], "page": navigation["return_page"]}
        url = reverse("workbench")+"?"+urlencode(params)
        refresh_params = params
    else:
        raise InvalidGalleryCursor("返回位置无效。")
    return {"return_url":url, "detail_refresh_url":reverse("detail",args=[photo.pk])+"?"+urlencode(refresh_params), **extra}


def return_context(data):
    filter_name=data.get('filter','all')
    if filter_name not in ('all','pending','running','review','failed','duplicate'):
        filter_name='all'
    try:
        page=max(1,int(data.get('page',1)))
    except (ValueError,TypeError):
        page=1
    return {'return_filter':filter_name,'return_page':page}


@require_http_methods(['GET','POST'])
def capture_time(request, photo_id):
    photo=get_object_or_404(Photo,pk=photo_id)
    data=request.POST if request.method=='POST' else request.GET
    form=MissingCaptureTimeForm(data if request.method=='POST' or 'date' in data else None,
                                initial={'version':photo.metadata_version})
    error,snapshot,status,value='', '',200,None
    from archive.services.management import editable_lifecycle
    if not photo.can_fill_capture or not editable_lifecycle(photo):
        error,status='已有可靠或人工时间，不能从此处修改。',409
    elif form.is_bound:
        if form.is_valid():
            value=form.capture_value()
            frozen={'id':str(photo.pk),'version':photo.metadata_version,'value':value}
            if request.method=='POST':
                try:
                    try:
                        expected=signing.loads(data.get('snapshot',''),salt='photo-capture',max_age=3600)
                    except signing.BadSignature:
                        raise MetadataConflict('预览已失效，请重新核对。') from None
                    with transaction.atomic():
                        current=Photo.objects.get(pk=photo.pk)
                        if not current.can_fill_capture or not editable_lifecycle(current) or expected!=frozen or form.cleaned_data['version']!=current.metadata_version:
                            raise MetadataConflict('照片信息已变化，本次未保存。请核对当前值与草稿。')
                        updated=Photo.objects.filter(pk=photo.pk,metadata_version=expected['version'],
                            state='review_pending',capture_quality__in=['missing','invalid'],manual_capture__isnull=True,
                            trash_requested_at__isnull=True,trashed_at__isnull=True).update(
                                manual_capture=value,capture_override_mode='manual',metadata_version=F('metadata_version')+1)
                        if updated!=1:
                            raise MetadataConflict('照片信息已变化，本次未保存。')
                    request.session['metadata_saved']='已补充时间。'
                    back=return_context(data)
                    return redirect(f"/photos/{photo.pk}/?filter={back['return_filter']}&page={back['return_page']}")
                except MetadataConflict as conflict:
                    photo.refresh_from_db()
                    error,status=str(conflict),409
            else:
                snapshot=signing.dumps(frozen,salt='photo-capture',compress=True)
        else:
            status=400
    return render(request,'archive/metadata.html',{'mode':'capture','photo':photo,'form':form,
        'snapshot':snapshot,'error_message':error,'capture_value':value,**return_context(data)},status=status)


@require_http_methods(["GET", "HEAD"])
def download_original(request, photo_id):
    photo = get_object_or_404(Photo, pk=photo_id)
    try:
        original, filename = open_completed_original(photo)
    except ArchiveCompletion.DoesNotExist:
        return HttpResponse('归档尚未完成，原图当前不可下载。', status=404, content_type='text/plain; charset=utf-8')
    except (OSError, UnsafePath, ImageRejected, ValueError, KeyError, TypeError):
        record_original_unavailable(photo)
        return HttpResponse('原图当前不可用，请核对归档文件。', status=409, content_type='text/plain; charset=utf-8')
    try:
        response = FileResponse(original, content_type='image/jpeg', as_attachment=True, filename=filename)
    except BaseException:
        original.close()
        raise
    if request.method == 'HEAD':
        # 保留GET头部和核验结果；HEAD无需等客户端结束才释放原图FD。
        response.streaming_content = ()
        original.close()
    return response


@require_http_methods(["GET", "HEAD"])
def media(request, photo_id, variant):
    if variant not in ("thumb", "preview"):
        raise Http404
    photo = get_object_or_404(Photo, pk=photo_id)
    key = photo.preview_key if variant == "preview" else photo.preview_key.removesuffix("-preview.jpg") + "-thumb.jpg"
    data_fd = None
    try:
        data_fd = open_root(settings.BIRDS_DATA_ROOT)
        source_file = open_regular_beneath(data_fd, key)
    except (OSError, UnsafePath):
        raise Http404 from None
    finally:
        if data_fd is not None:
            os.close(data_fd)
    return FileResponse(source_file, content_type="image/jpeg")
