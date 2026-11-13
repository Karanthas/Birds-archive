"""从完成凭据读取相册；固定批次、签名上界与无 OFFSET 的稳定查询。"""
import uuid
from dataclasses import dataclass

from django.core import signing
from django.db.models import Case, When, Value, IntegerField, CharField, Max, Q, Prefetch
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Concat, Length, Substr

from archive.models import Photo, PhotoSpecies, ArchiveCompletion, Species, capture_sort_key

PAGE_SIZE = 24
CURSOR_SALT = 'archive.gallery.cursor.v1'
CURSOR_MAX_AGE = 7 * 24 * 60 * 60
CURSOR_KEYS = {'version', 'upper_sequence', 'last_known', 'last_sort_key', 'last_photo_id',
               'last_month', 'species_filter', 'page_size'}


from archive.services.browse_collection import InvalidCollection as InvalidGalleryCursor


def species_filter_id(value):
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise InvalidGalleryCursor('鸟种标识无效。') from None


def encode_gallery_cursor(payload):
    return signing.dumps(payload, salt=CURSOR_SALT, compress=True)


def decode_gallery_cursor(cursor, species_id=None, page_size=PAGE_SIZE):
    """签名正确仍需验证结构；不能让错页、错过滤或超大批次扩大查询。"""
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096 or page_size != PAGE_SIZE:
            raise ValueError
        payload = signing.loads(cursor, salt=CURSOR_SALT, max_age=CURSOR_MAX_AGE)
        if not isinstance(payload, dict) or set(payload) != CURSOR_KEYS:
            raise ValueError
        if type(payload['version']) is not int or payload['version'] != 1:
            raise ValueError
        if type(payload['upper_sequence']) is not int or not 0 <= payload['upper_sequence'] <= 2**63-1:
            raise ValueError
        if type(payload['page_size']) is not int or payload['page_size'] != PAGE_SIZE:
            raise ValueError
        if payload['species_filter'] != species_filter_id(species_id):
            raise ValueError
        known, sort_key, photo_id, month = (payload[key] for key in (
            'last_known', 'last_sort_key', 'last_photo_id', 'last_month'))
        if known is None:
            if (sort_key, photo_id, month) != (None, None, None):
                raise ValueError
        else:
            if type(known) is not int or known not in (0,1):
                raise ValueError
            if not isinstance(photo_id, str) or str(uuid.UUID(photo_id)) != photo_id:
                raise ValueError
            if known:
                payload['last_sort_key'] = capture_sort_key(sort_key)
                if month != sort_key[:7]:
                    raise ValueError
            elif sort_key != '' or month != 'unknown':
                raise ValueError
        return payload
    except (signing.BadSignature, ValueError, TypeError, KeyError, AttributeError):
        raise InvalidGalleryCursor('相册分页已失效，请重新打开相册。') from None


def group_gallery_items(items, last_month=None):
    groups = []
    previous = last_month
    for photo in items:
        month = getattr(photo, "gallery_group_key", None) or photo.gallery_capture.get('month') or 'unknown'
        if not groups or groups[-1]['month'] != month:
            title = '' if month == previous else ('时间未知' if month == 'unknown' else
                f'{int(month[:4])} 年 {int(month[5:])} 月')
            groups.append({'month': month, 'title': title, 'items': []})
        groups[-1]['items'].append(photo)
        previous = month
    return groups


@dataclass
class GalleryPage:
    items: list
    groups: list
    cursor: str
    next_cursor: str | None
    upper_sequence: int
    species: Species | None
    collection: object
    created_count: int
    remaining_count: int
    dirty: bool

    @property
    def has_more(self):
        return self.next_cursor is not None


def ordered_gallery_queryset(queryset):
    snapshot_key = 'current_capture__sort_key'
    queryset = queryset.annotate(_known=Case(
        When(Q(**{snapshot_key: None}) | Q(**{snapshot_key+'__isnull': True}), then=Value(0)),
        default=Value(1), output_field=IntegerField())).annotate(
        _raw_sort_key=KeyTextTransform('sort_key', 'current_capture')
    ).annotate(_key_length=Length('_raw_sort_key')).annotate(
        # Normalize older frozen second/microsecond keys in the query only.
        # Date-only values keep their precision; historical snapshots never change.
        _sort_key=Case(When(_known=0, then=Value('')),
            When(_key_length=10, then='_raw_sort_key'),
            When(_key_length=19, then=Concat('_raw_sort_key', Value('.'+'0'*32), output_field=CharField())),
            default=Substr(Concat('_raw_sort_key', Value('0'*32), output_field=CharField()), 1, 52),
            output_field=CharField()))
    return queryset.order_by("-_known", "-_sort_key", "-id")


def query_gallery(cursor=None, species_id=None, page_size=PAGE_SIZE, filters=None):
    from archive.services.browse_collection import create_collection, collection_page, encode_collection_token
    if type(page_size) is not int or page_size != PAGE_SIZE:
        raise InvalidGalleryCursor("相册每批显示24张。")
    species_id = species_filter_id(species_id)
    species = Species.objects.get(pk=species_id) if species_id else None
    if cursor is None:
        normalized = dict(filters or {})
        if species_id:
            normalized["species"] = [species_id]
        cursor = encode_collection_token(create_collection(normalized))
    result = collection_page(cursor)
    return GalleryPage(result["items"], group_gallery_items(result["items"], result["last_month"]),
        cursor, result["next_cursor"], 0, species, result["collection"],
        result["created_count"], result["remaining_count"], result["dirty"])
