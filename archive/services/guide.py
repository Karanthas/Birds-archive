"""已拍图鉴只读投影；资料由后续本地快照读取，不在此联网。"""
from dataclasses import dataclass

from django.core.paginator import Paginator
from django.db.models import Prefetch

from archive.models import PhotoSpecies, Species
from archive.services.browse_collection import visible_photos, decode_collection_token
from archive.services.catalog import normalize
from archive.services.confirmation import local_name_key
from archive.services.gallery import ordered_gallery_queryset, query_gallery, InvalidGalleryCursor


@dataclass
class GuideCard:
    species: Species
    photo_count: int
    cover: object
    latest_capture_key: str

    @property
    def display_name(self):
        return self.species.display_name

    @property
    def scientific_name(self):
        return self.species.scientific_name


def clean_query(value):
    query = value.strip()
    if len(query) > 200:
        raise InvalidGalleryCursor('查询不能超过 200 个字符。')
    return query


def guide_context(data, from_photo=False):
    """Only typed values and signed collections travel between local routes."""
    from archive.views import gallery_loaded_pages
    from archive.forms import GalleryFilterForm
    keys = ('q', 'page', 'album_cursor', 'album_loaded', 'cursor', 'loaded', 'species',
        'guide_q', 'guide_page', 'return')
    if hasattr(data, 'getlist') and any(len(data.getlist(key)) > 1 for key in keys):
        raise InvalidGalleryCursor('图鉴参数不能重复。')
    if data.get('return') and (not from_photo or data['return'] != 'guide'):
        raise InvalidGalleryCursor('返回位置无效。')
    query = clean_query(data.get('guide_q' if from_photo else 'q', ''))
    try:
        page = max(1, min(1000000, int(data.get('guide_page' if from_photo else 'page', 1))))
    except (ValueError, TypeError):
        page = 1
    params = {'q': query, 'page': page}
    album_url = '/'
    if data.get('album_cursor'):
        collection, _ = decode_collection_token(data['album_cursor'])
        form = GalleryFilterForm(collection.filters)
        if not form.is_valid() or form.normalized_filters() != collection.filters:
            raise InvalidGalleryCursor('原相册筛选无效。')
        loaded = gallery_loaded_pages({'loaded': data.get('album_loaded', 1)})
        params.update(album_cursor=data['album_cursor'], album_loaded=loaded)
        from urllib.parse import urlencode
        album_url += '?' + urlencode({'cursor': data['album_cursor'], 'loaded': loaded})
    return params, album_url


def query_guide(query='', page=1, page_size=24):
    query = clean_query(query)
    if page_size != 24:
        raise InvalidGalleryCursor('图鉴每页显示24种。')
    cards = {}
    photos = ordered_gallery_queryset(visible_photos()).prefetch_related(Prefetch(
        'confirmed_species', queryset=PhotoSpecies.objects.select_related('species')))
    for photo in photos:
        key = photo._sort_key if photo._known else ''
        cover_key = (photo.favorite, photo.rating is not None, photo.rating or 0,
            photo._known, key, photo.pk)
        for link in photo.confirmed_species.all():
            species = link.species
            if query and not (normalize(query) in normalize(' '.join(filter(None, [
                    species.chinese_name, species.scientific_name]))) or
                    local_name_key(query) in local_name_key(species.local_name or '')):
                continue
            card = cards.get(species.pk)
            if card is None:
                card = GuideCard(species, 0, photo, key)
                card.cover_key = cover_key
                cards[species.pk] = card
            card.photo_count += 1
            if cover_key > card.cover_key:
                card.cover, card.cover_key = photo, cover_key
    # The first photo is the latest capture; the chosen cover is independent.
    ordered = sorted(cards.values(), key=lambda card: card.species.pk)
    ordered.sort(key=lambda card: (bool(card.latest_capture_key), card.latest_capture_key), reverse=True)
    return Paginator(ordered, page_size).get_page(page)


def species_collection(species_id, cursor=None):
    if cursor is not None:
        collection, _ = decode_collection_token(cursor)
        if collection.filters != {'species': [str(species_id)]}:
            raise InvalidGalleryCursor('浏览集合与鸟种不匹配，请重新打开图鉴。')
    page = query_gallery(cursor=cursor, species_id=species_id)
    # Frozen membership preserves position, while current association and recycle
    # gates prevent corrected photos leaking into the former species' guide.
    current = visible_photos().filter(confirmed_species__species_id=species_id)
    allowed = set(current.filter(pk__in=[item.pk for item in page.items]).values_list('pk', flat=True))
    page.items = [item for item in page.items if item.pk in allowed]
    from archive.services.gallery import group_gallery_items
    page.groups = group_gallery_items(page.items)
    page.remaining_count = page.collection.members.filter(photo_id__in=current.values('pk')).count()
    page.dirty = page.dirty or page.created_count != page.remaining_count
    return page


def get_guide_detail(species_id, collection_token=None):
    species = Species.objects.get(pk=species_id)
    if not visible_photos().filter(confirmed_species__species=species).exists():
        raise Species.DoesNotExist
    return {'species': species, 'gallery_page': species_collection(species.pk, collection_token),
        'guide_sections': [{'title': title, 'text': '暂缺'} for title in ('外形', '习性', '分布')],
        'protection_records': [{'system': title, 'status': '未知'} for title in
            ('全球受威胁等级', '适用地区法定保护')], 'similar_cards': [], 'guide_overview': '介绍暂缺。'}
