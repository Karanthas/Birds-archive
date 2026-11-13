"""Ordinary forms are complete; no POST is automatically replayed by this UI."""
import uuid
from django.core.paginator import Paginator
from django.db import DatabaseError
from django.shortcuts import get_object_or_404, render
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from archive.models import BatchReviewPreview, Species
from archive.forms import BatchSpeciesForm
from archive.services.batch_review import (build_preview, revise_preview, submit_preview,
    checked_preview, preview_token, PreviewConflict)
from archive.services.confirmation import search_confirmable_species

REASONS = {'photo_missing': '照片不可用', 'not_review_pending': '已不在待确认状态',
    'source_unavailable': '来源不可用', 'candidates_unavailable': '候选不可用，请手动补选',
    'candidate_stale': '候选已变化，请重新核对', 'candidates_close': '候选接近，请手动补选',
    'threshold_unknown': '判据未知，请手动补选', 'candidate_unmapped': '候选未映射，请手动补选',
    'photo_changed': '照片或候选已变化，请重新核对', 'excluded': '已排除',
    'empty_set': '未选择鸟种', 'confirmation_conflict': '确认冲突，请重新核对',
    'confirmation_failed': '确认失败，请重新核对', 'database_unavailable': '数据库暂不可用，尚未处理',
    'not_processed': '尚未处理，请重新核对',
    'awaiting_archive': '等待归档'}
STATUS = {'accepted': '已受理', 'skipped': '已跳过', 'failed': '失败', 'unprocessed': '未处理'}


def _redirect(preview):
    response = HttpResponseRedirect(reverse('batch-display', args=[preview.pk]))
    response.status_code = 303
    return response


def _page(request, preview=None, *, error='', status=200, draft=None, results=None):
    rows = []
    supplied_results = results is not None
    durable = list(preview.results.all()) if preview and not supplied_results else []
    results = results if results is not None else [dict(photo_id=str(r.photo_id), status=r.status,
        reason_code=r.reason_code, operation_id=str(r.operation_id) if r.operation_id else None) for r in durable]
    by_id = {r['photo_id']: r for r in results}
    if preview and results:
        for frozen in preview.frozen_rows:
            if frozen['photo_id'] not in by_id:
                pending = dict(photo_id=frozen['photo_id'], status='unprocessed',
                    reason_code='not_processed', operation_id=None)
                results.append(pending)
                by_id[pending['photo_id']] = pending
    for frozen in preview.frozen_rows if preview else []:
        row = dict(frozen)
        row['labels'] = [local or (f'{chinese}（{scientific}）' if chinese and scientific else chinese or scientific or '未知')
            for chinese, scientific, local in row['species_labels']]
        row['final_count'] = len(row['labels']) + len(row['local_names'])
        row['skip_reason'] = REASONS.get('excluded' if row['excluded'] else row['reason_code'], '需要重新核对')
        result = by_id.get(row['photo_id'])
        row['result'] = result
        if result:
            row['result_label'] = STATUS[result['status']]
            row['result_reason'] = REASONS.get(result['reason_code'], '需要重新核对')
        row['selected_ids'], row['draft_names'] = row['final_species_ids'], row['local_names']
        row['q'], row['search_page'], row['editing'] = '', None, False
        if draft and draft.get('photo_id') == row['photo_id']:
            row.update(editing=True, selected_ids=draft.getlist('species_ids'),
                draft_names=draft.getlist('local_names'), q=draft.get('q', '')[:200])
            row['search_page'] = Paginator(search_confirmable_species(row['q']), 24).get_page(draft.get('species_page', '1'))
        row['selected_species'] = (Species.objects.filter(pk__in=[ident for ident in row['selected_ids'] if _valid_uuid(ident)])
            if not supplied_results else [])
        rows.append(row)
    ready = sum(not r['excluded'] and not r['reason_code'] and bool(r['final_species_ids'] or r['local_names']) for r in rows)
    latest = (preview if supplied_results else BatchReviewPreview.objects.filter(series_id=preview.series_id).order_by('-revision').first()) if preview else None
    editable = bool(preview and not results and latest.pk == preview.pk)
    return render(request, 'archive/batch_review.html', {'preview': preview, 'rows': rows,
        'token': preview_token(preview) if preview else '', 'error_message': error,
        'total': len(rows), 'ready_count': ready, 'skip_count': len(rows)-ready, 'editable': editable,
        'latest': latest, 'results': results, 'has_results': bool(results)}, status=status)


def _valid_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


@require_POST
def preview(request):
    try:
        mode = request.GET.get('mode') or request.POST.get('mode')
        if mode in ('uniform', 'uniform_species'):
            from archive.services.batch_review import explicit_ids
            ids = explicit_ids(request.POST.getlist('photo_ids'))
            action = request.POST.get('action') or request.GET.get('action')
            if action in ('search', 'page', 'append') or (action is None
                    and not request.POST.getlist('species_ids') and not any(request.POST.getlist('local_names'))):
                return _uniform_page(request, ids)
            if action not in (None, 'preview'):
                raise ValueError('请选择核对统一鸟种集合。')
            form = BatchSpeciesForm(request.POST)
            if not form.is_valid():
                return _uniform_page(request, ids, error='；'.join(form.non_field_errors()), status=400)
            return _redirect(build_preview(mode='uniform_species', **form.cleaned_data))
        return _redirect(build_preview(request.POST.getlist('photo_ids'), mode=mode))
    except ValueError as error:
        return _page(request, error=str(error), status=400)
    except DatabaseError:
        return _page(request, error='数据库暂不可用，请稍后重新核对。', status=503)


def _uniform_page(request, ids, *, error='', status=200):
    selected = [ident for ident in request.POST.getlist('species_ids') if _valid_uuid(ident)]
    q = request.POST.get('q', '')[:200]
    return render(request, 'archive/batch_review.html', {
        'uniform_setup': True, 'total': len(ids), 'photo_ids': ids,
        'selected_ids': selected, 'selected_species': Species.objects.filter(pk__in=selected),
        'local_names': request.POST.getlist('local_names'), 'q': q,
        'search_page': Paginator(search_confirmable_species(q), 24).get_page(request.POST.get('species_page', '1')),
        'error_message': error}, status=status)


@require_GET
def display(request, preview_id):
    return _page(request, get_object_or_404(BatchReviewPreview, pk=preview_id))


@require_POST
def revise(request, preview_id):
    preview = get_object_or_404(BatchReviewPreview, pk=preview_id)
    try:
        checked_preview(preview_id, request.POST.get('token', ''))
        photo_id = request.POST.get('photo_id', '')
        if photo_id not in preview.selection_ids:
            raise ValueError('修订对象无效。')
        action = request.POST.get('action') or request.GET.get('action')
        if action in ('search', 'page', 'append'):
            # Only drafts are displayed: search never silently changes the final frozen set.
            return _page(request, preview, draft=request.POST)
        if action == 'exclude':
            changes = {photo_id: {'excluded': True}}
        elif action == 'include':
            changes = {photo_id: {'excluded': False}}
        elif action == 'save':
            changes = {photo_id: {'species_ids': request.POST.getlist('species_ids'),
                'local_names': request.POST.getlist('local_names'), 'excluded': False}}
        else:
            raise ValueError('请选择修订操作。')
        return _redirect(revise_preview(preview_id, request.POST.get('token', ''), changes))
    except ValueError as error:
        return _page(request, preview, error=str(error), status=409 if isinstance(error, PreviewConflict) else 400, draft=request.POST)
    except DatabaseError:
        return _page(request, error='数据库暂不可用，请稍后核对；未自动重试。', status=503)


@require_POST
def submit(request, preview_id):
    preview = get_object_or_404(BatchReviewPreview, pk=preview_id)
    try:
        if request.POST.get('confirm') != 'yes':
            raise ValueError('请核对全部照片后明确确认。')
        results = submit_preview(preview_id, request.POST.get('token', ''))
        if any(r['status'] == 'unprocessed' for r in results):
            return _page(request, preview, results=results,
                error='部分照片尚未处理，请核对逐项结果；未自动重试。', status=503)
        return _redirect(preview)
    except ValueError as error:
        return _page(request, preview, error=str(error), status=409 if isinstance(error, PreviewConflict) else 400)
    except DatabaseError:
        return _page(request, error='数据库暂不可用，提交结果未知；请稍后查看预览记录核对。', status=503)
