"""固定成员、顺序与分组；每次读取仍执行实时回收门。"""
import uuid
from datetime import timedelta

from django.core import signing
from django.db import transaction
from django.db.models import Exists, OuterRef, F, Q, Prefetch
from django.utils import timezone

from archive.models import GalleryCollection, GalleryMember, Photo, PhotoSpecies

PAGE_SIZE = 24
MAX_AGE = 7 * 24 * 60 * 60
TOKEN_SALT = 'archive.gallery.collection.v1'


class InvalidCollection(ValueError):
    pass


def encode_collection_token(collection, position=0):
    return signing.dumps({'version':1, 'collection':str(collection.pk), 'position':position},
        salt=TOKEN_SALT, compress=True)


def decode_collection_token(token):
    try:
        if not isinstance(token, str) or not 1 <= len(token) <= 4096:
            raise ValueError
        data = signing.loads(token, salt=TOKEN_SALT, max_age=MAX_AGE)
        if (not isinstance(data, dict) or set(data) != {'version','collection','position'}
                or type(data['version']) is not int or data['version'] != 1
                or type(data['position']) is not int or not 0 <= data['position'] <= 2**63-1
                or str(uuid.UUID(data['collection'])) != data['collection']):
            raise ValueError
        collection = GalleryCollection.objects.get(pk=data['collection'], expires_at__gt=timezone.now())
        if data['position'] > collection.members.count():
            raise ValueError
        return collection, data['position']
    except (ValueError, TypeError, AttributeError, signing.BadSignature, GalleryCollection.DoesNotExist):
        raise InvalidCollection('浏览集合已失效，请重新筛选。') from None


def visible_photos():
    return Photo.objects.filter(archive_completion__isnull=False, current_classification__in=['identified','unidentified'],
        trash_requested_at__isnull=True, trashed_at__isnull=True)


def filtered_photos(filters):
    photos = visible_photos()
    if filters.get('species'):
        photos = photos.filter(Exists(PhotoSpecies.objects.filter(photo_id=OuterRef('pk'),
            species_id__in=filters['species'])))
    if filters.get('favorite') in ('yes', 'no'):
        photos = photos.filter(favorite=filters['favorite'] == 'yes')
    if filters.get('unrated'):
        photos = photos.filter(rating__isnull=True)
    elif filters.get('rating_min'):
        photos = photos.filter(rating__gte=filters['rating_min'])
    if filters.get('time_unknown'):
        photos = photos.filter(Q(current_capture__local__isnull=True) | Q(current_capture__local=None))
    else:
        # ISO local dates compare lexically; upper bound includes the entire final day.
        if filters.get('date_from') or filters.get('date_to'):
            photos = photos.filter(current_capture__local__isnull=False).exclude(current_capture__local=None)
        if filters.get('date_from'):
            photos = photos.filter(current_capture__local__gte=filters['date_from'])
        if filters.get('date_to'):
            photos = photos.filter(current_capture__local__lt=filters['date_to']+'~')
    if filters.get('location_unknown'):
        photos = photos.filter(user_location='')
    elif filters.get('location'):
        photos = photos.filter(user_location__icontains=filters['location'])
    return photos


@transaction.atomic
def create_collection(normalized_filters):
    from archive.services.gallery import ordered_gallery_queryset
    collection = GalleryCollection.objects.create(filters=normalized_filters,
        expires_at=timezone.now()+timedelta(seconds=MAX_AGE))
    batch = []
    rows = ordered_gallery_queryset(filtered_photos(normalized_filters)).values_list(
        'id', 'current_capture', 'metadata_version', 'management_generation').iterator(chunk_size=500)
    for index, (photo_id, capture, metadata, generation) in enumerate(rows, 1):
        batch.append(GalleryMember(collection=collection, photo_id=photo_id, position=index,
            group_key=(capture or {}).get('month') or 'unknown', metadata_version=metadata,
            management_generation=generation))
        if len(batch) == 500:
            GalleryMember.objects.bulk_create(batch); batch = []
    if batch:
        GalleryMember.objects.bulk_create(batch)
    return collection


def visible_members(collection):
    return collection.members.filter(photo_id__in=visible_photos().values('pk'))


def collection_page(token):
    collection, position = decode_collection_token(token)
    members = visible_members(collection)
    rows = list(members.filter(position__gt=position).select_related('photo__archive_completion__operation')
        .prefetch_related(Prefetch('photo__confirmed_species', queryset=PhotoSpecies.objects.select_related('species')))
        .order_by('position')[:PAGE_SIZE+1])
    items = []
    for member in rows[:PAGE_SIZE]:
        photo = member.photo
        photo.gallery_capture = photo.current_capture or {}
        photo.gallery_group_key = member.group_key
        photo.gallery_species = [link.species for link in photo.confirmed_species.all()]
        photo.gallery_position = member.position
        items.append(photo)
    created, remaining = collection.members.count(), members.count()
    dirty = created != remaining or members.exclude(metadata_version=F('photo__metadata_version'),
        management_generation=F('photo__management_generation')).exists()
    previous = collection.members.filter(position__lte=position).order_by('-position').values_list('group_key', flat=True).first()
    return {'collection':collection, 'items':items, 'cursor':token,
        'next_cursor':encode_collection_token(collection, rows[PAGE_SIZE-1].position) if len(rows)>PAGE_SIZE else None,
        'last_month':previous, 'created_count':created, 'remaining_count':remaining, 'dirty':dirty}


def collection_neighbors(token, photo_id):
    collection, _ = decode_collection_token(token)
    member = collection.members.filter(photo_id=photo_id).first()
    if not member:
        raise InvalidCollection('照片不属于当前集合，请重新筛选。')
    members = visible_members(collection)
    return {'collection':collection, 'position':member.position,
        'visible':members.filter(pk=member.pk).exists(),
        'previous':members.filter(position__lt=member.position).order_by('-position').values_list('photo_id', flat=True).first(),
        'next':members.filter(position__gt=member.position).order_by('position').values_list('photo_id', flat=True).first()}
