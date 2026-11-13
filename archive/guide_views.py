"""图鉴 GET 阅读入口。"""
from urllib.parse import urlencode, urlsplit

from django.db import DatabaseError
from django.http import Http404, FileResponse
from django.shortcuts import render
from django.urls import reverse, resolve, Resolver404
from django.views.decorators.http import require_GET

from archive.models import Species
from archive.services.guide import query_guide, get_guide_detail, guide_context, species_collection
from archive.services.gallery import InvalidGalleryCursor


def online_response(response):
    """Limit only online data connections; leave existing script/style policy intact."""
    directive = "connect-src 'self' https://www.wikidata.org/w/api.php https://zh.wikipedia.org/w/api.php https://en.wikipedia.org/w/api.php"
    existing = response.get('Content-Security-Policy', '')
    parts = [part.strip() for part in existing.split(';') if part.strip() and not part.strip().startswith('connect-src ')]
    response['Content-Security-Policy'] = '; '.join([*parts, directive])
    return response


@require_GET
def information(request, species_id):
    """Public species information only; no personal collection or photo query."""
    from archive.services.guide_online import online_presentation
    try:
        species = Species.objects.select_related('source_snapshot').prefetch_related('concepts__snapshot').get(pk=species_id)
    except Species.DoesNotExist:
        raise Http404 from None
    return_url = ''
    target = request.GET.get('return', '')
    try:
        parsed = urlsplit(target)
        if target.startswith('/') and not parsed.netloc and not parsed.scheme and not parsed.query and not parsed.fragment:
            if resolve(parsed.path).url_name in {'detail', 'guide_detail', 'guide_index', 'gallery'}:
                return_url = parsed.path
    except (Resolver404, ValueError):
        pass
    return online_response(render(request, 'archive/guide_information.html', {
        'species':species, 'information_only':True, 'candidate_information':request.GET.get('candidate') == '1',
        'information_return_url':return_url, **online_presentation(species)}))


@require_GET
def index(request):
    query = request.GET.get('q', '').strip()
    context = {'query': query}
    try:
        params, album_url = guide_context(request.GET)
        context.update(guide_params=params, album_return_url=album_url)
        page = query_guide(query, request.GET.get('page', 1))
        params['page'] = page.number
        context['page_obj'] = page
        context['clear_url'] = reverse('guide_index')+'?'+urlencode({key:value for key,value in params.items() if key.startswith('album_')})
        for key, number in (('previous_url', page.previous_page_number() if page.has_previous() else None),
                ('next_url', page.next_page_number() if page.has_next() else None)):
            if number:
                context[key] = reverse('guide_index')+'?'+urlencode({**params, 'page':number})
        for card in page:
            card.url = reverse('guide_detail', args=[card.species.pk])+'?'+urlencode(params)
    except InvalidGalleryCursor as error:
        context['error_message'] = str(error)
        return render(request, 'archive/guide.html', context, status=400)
    except DatabaseError:
        context['error_message'] = '暂时无法读取图鉴，请重试。'
        return render(request, 'archive/guide.html', context, status=503)
    return render(request, 'archive/guide.html', context)


@require_GET
def detail(request, species_id):
    try:
        from archive.views import gallery_loaded_pages
        from archive.services.gallery import group_gallery_items
        params, album_url = guide_context(request.GET)
        from archive.services.guide_similar import similar_cards, related_species, reachable_source
        try:
            context = get_guide_detail(species_id, request.GET.get('cursor'))
        except Species.DoesNotExist:
            species = Species.objects.get(pk=species_id)
            source = reachable_source(species, request.GET.get('from_species'))
            if source is None:
                raise Species.DoesNotExist
            context = {'species':species, 'gallery_page':species_collection(species_id, request.GET.get('cursor')),
                'related_return_url':reverse('guide_detail', args=[source.pk])+'?'+urlencode(params),
                'related_return_name':source.display_name}
        if 'reference_asset' in request.GET:
            from archive.services.guide_sources import open_reference_asset, GuideSourceError, read_guide, reference_metadata
            from archive.services.guide_online import authorized_asset
            import re
            key = request.GET['reference_asset']
            if re.fullmatch(r'[a-f0-9]{64}', key):
                asset = authorized_asset(context['species'], key)
                if not asset or len(request.GET.getlist('reference_asset')) != 1:
                    raise Http404
                try:
                    stream = open_reference_asset(asset)
                except (GuideSourceError, OSError):
                    raise Http404 from None
                return FileResponse(stream, content_type=asset['mime'])
            allowed_species = [context['species'], *(target for target, _, _ in related_species(context['species']))]
            snapshot_ids = [read_guide(item).get('snapshot_id') for item in allowed_species]
            from archive.models import GuideReferenceAsset
            import uuid
            try:
                if len(request.GET.getlist('reference_asset')) != 1:
                    raise ValueError
                asset_id = uuid.UUID(request.GET['reference_asset'])
                asset = GuideReferenceAsset.objects.get(pk=asset_id, snapshot_id__in=[key for key in snapshot_ids if key])
                if not any(reference_metadata(read_guide(item), asset) for item in allowed_species):
                    raise GuideSourceError('unpublished_reference_asset')
                stream = open_reference_asset(asset)
            except (ValueError, GuideReferenceAsset.DoesNotExist, GuideSourceError, OSError):
                raise Http404 from None
            return FileResponse(stream, content_type=asset.mime)
        from archive.services.guide_online import online_presentation
        context.update(online_presentation(context['species']))
        context['similar_cards'] = similar_cards(context['species'])
        for card in context['similar_cards']:
            card['url'] += '?'+urlencode({**params, 'from_species':str(species_id)})
        page = context['gallery_page']
        fragment = request.headers.get('HX-Request') == 'true'
        pages = [page]
        if not fragment:
            for _ in range(gallery_loaded_pages(request.GET)-1):
                if not pages[-1].next_cursor:
                    break
                pages.append(species_collection(species_id, pages[-1].next_cursor))
            page.items = [item for batch in pages for item in batch.items]
            page.groups = group_gallery_items(page.items)
            page.next_cursor = pages[-1].next_cursor
        url = reverse('guide_detail', args=[species_id])
        photo_params = {'return': 'guide', 'species': str(species_id), 'cursor': page.cursor,
            'loaded': len(pages), 'guide_q': params['q'], 'guide_page': params['page'],
            **{key: value for key, value in params.items() if key.startswith('album_')}}
        context.update(fragment=fragment, guide_params=params,
            guide_return_url=reverse('guide_index')+'?'+urlencode(params)+'#species-'+str(species_id),
            album_return_url=album_url, gallery_detail_query=urlencode(photo_params),
            gallery_page_tokens=[batch.cursor for batch in pages],
            gallery_refresh_url=url+'?'+urlencode(params),
            gallery_next_url=url+'?'+urlencode({**params, 'cursor':page.next_cursor, 'species':str(species_id)})
                if page.next_cursor else None)
    except Species.DoesNotExist:
        raise Http404 from None
    except InvalidGalleryCursor as error:
        return render(request, 'archive/base.html', {'error_message':str(error)}, status=400)
    except DatabaseError:
        return render(request, 'archive/base.html', {'error_message':'暂时无法读取图鉴，请重试。'}, status=503)
    return online_response(render(request, 'archive/guide_detail.html', context))
