"""已完成归档的稳定相册；仅合成数据库记录，不触碰真实照片。"""
import uuid
import re
from html.parser import HTMLParser
from unittest import mock

from django.core import signing
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from archive.models import (Photo, Species, PhotoSpecies, ImportSource, SourceObservation,
    Job, ArchiveOperation, ArchiveCompletion, ManagedOriginal)


class GalleryFixture(TestCase):
    def species(self, name='本地鸟'):
        return Species.objects.create(source_kind='local', local_name=name,
            local_key=name.casefold(), search_key=name)

    def completed(self, number=1, *, species=(), complete=True, **fields):
        photo = Photo.objects.create(id=uuid.UUID(int=number), sha256=f'{number:064x}',
            byte_size=100, preview_key=f'synthetic/{number}', state='archived' if complete else 'confirmed',
            **fields)
        source = ImportSource.objects.create(relative_path=f'{number}.jpg', photo=photo)
        observation = SourceObservation.objects.create(source=source, version=1, photo=photo)
        job = Job.objects.create(source=source, source_version=1, kind='archive', state='succeeded')
        operation = ArchiveOperation.objects.create(photo=photo, job=job, request_id=uuid.uuid4(),
            payload_digest='a'*64, classification='species' if species else 'undetermined',
            species_ids_snapshot=[str(item.pk) for item in species], capture_snapshot=photo.effective_capture(),
            original_basename=f'{number}.jpg', source_observation=observation, source_version=1,
            expected_sha256=photo.sha256, byte_size=100)
        for item in species:
            PhotoSpecies.objects.create(photo=photo, species=item)
        if complete:
            ArchiveCompletion.objects.create(photo=photo, operation=operation)
            photo.current_capture = operation.capture_snapshot
            photo.current_classification = 'identified' if species else 'unidentified'
            photo.save(update_fields=['current_capture', 'current_classification'])
        return photo

    def query(self, **kwargs):
        from archive.services.gallery import query_gallery
        return query_gallery(**kwargs)


class GalleryQueryTests(GalleryFixture):
    def test_seven_digit_descending_across_cursor_boundary(self):
        for number in range(1, 28):
            self.completed(number, captured_local=f'2026-10-01 12:00:00.{1000000+28-number}',
                           capture_quality='reliable', capture_precision='subsecond')
        first = self.query(); second = self.query(cursor=first.next_cursor)
        self.assertEqual([p.pk.int for p in first.items+second.items], list(range(1, 28)))
        self.assertEqual(len(first.items), 24)
        self.assertEqual(len(second.items), 3)

    def test_old_microsecond_keys_compare_equally_without_changing_snapshots(self):
        legacy = []
        for number in range(1, 28):
            photo = self.completed(number, captured_local='2026-10-01 12:00:00.12',
                                   capture_quality='reliable', capture_precision='subsecond')
            if number % 2:
                operation = photo.archive_operation
                snapshot = {**operation.capture_snapshot, 'sort_key': '2026-10-01 12:00:00.120000'}
                ArchiveOperation.objects.filter(pk=operation.pk).update(capture_snapshot=snapshot)
                legacy.append((operation.pk, snapshot))
        first = self.query(); second = self.query(cursor=first.next_cursor)
        self.assertEqual([p.pk.int for p in first.items+second.items], list(range(27, 0, -1)))
        for operation_id, snapshot in legacy:
            self.assertEqual(ArchiveOperation.objects.get(pk=operation_id).capture_snapshot, snapshot)

    def test_second_and_zero_fraction_share_sort_key_and_old_cursor(self):
        from archive.services.gallery import decode_gallery_cursor, encode_gallery_cursor
        for number in range(1, 28):
            photo = self.completed(number, captured_local='2026-10-01 12:00:00'+('.0000000' if number % 2 else ''),
                capture_quality='reliable', capture_precision='subsecond' if number % 2 else 'second')
            if number % 3 == 0:
                operation = photo.archive_operation
                ArchiveOperation.objects.filter(pk=operation.pk).update(capture_snapshot={
                    **operation.capture_snapshot, 'sort_key': '2026-10-01 12:00:00'})
        first = self.query(); second = self.query(cursor=first.next_cursor)
        self.assertEqual([p.pk.int for p in first.items+second.items], list(range(27, 0, -1)))
        legacy_payload = decode_gallery_cursor(first.next_cursor)
        legacy_payload['last_sort_key'] = '2026-10-01 12:00:00'
        legacy_page = self.query(cursor=encode_gallery_cursor(legacy_payload))
        self.assertEqual([p.pk for p in legacy_page.items], [p.pk for p in second.items])

    def test_full_precision_descending_across_cursor_boundary(self):
        fractions = [str(10**31+index) for index in range(1, 28)]
        fractions[12:14] = ['1234567', '1234568']
        for number, fraction in enumerate(reversed(fractions), 1):
            self.completed(number, captured_local='2026-10-01 12:00:00.'+fraction,
                           capture_quality='reliable', capture_precision='subsecond')
        expected = sorted(range(1, 28), key=lambda number: fractions[-number].ljust(32, '0'), reverse=True)
        first = self.query()
        second = self.query(cursor=first.next_cursor)
        self.assertEqual([p.pk.int for p in first.items+second.items], expected)
        self.assertEqual(len(first.items), 24)
        self.assertEqual(len(second.items), 3)
        self.assertEqual(len(first.items[-1]._sort_key), 52)
        self.assertIsNone(second.next_cursor)
        self.assertEqual([p.pk for p in second.items], [p.pk for p in self.query(cursor=first.next_cursor).items])

    def test_fraction_trailing_zeros_use_uuid_only_for_equal_times(self):
        for number in range(1, 28):
            fraction = ('12', '1200000', '12000000000000000000000000000000')[number % 3]
            self.completed(number, captured_local='2026-10-01 12:00:00.'+fraction,
                           capture_quality='reliable', capture_precision='subsecond')
        first = self.query(); second = self.query(cursor=first.next_cursor)
        self.assertEqual([p.pk.int for p in first.items+second.items], list(range(27, 0, -1)))

    def test_full_precision_cursor_rejects_malformed_time(self):
        from archive.services.gallery import decode_gallery_cursor, encode_gallery_cursor, InvalidGalleryCursor
        payload = decode_gallery_cursor(self.query().cursor)
        payload.update(last_known=1, last_photo_id=str(uuid.UUID(int=1)), last_month='2026-10')
        valid = '2026-10-01 12:00:00.'+'1'*32
        self.assertEqual(decode_gallery_cursor(encode_gallery_cursor({**payload, 'last_sort_key': valid}))[
            'last_sort_key'], valid)
        for invalid in ('2026-10-01 12:00:00.'+'1'*33, '2026-10-01 12:00:00.',
                '2026-10-01T12:00:00.'+'1'*32, '2026-10-01 12:00:00.'+'１'*32,
                '2026-10-01 25:00:00.'+'1'*32, valid[:-1]+'Z', '2026-10-01 12:00:00.1+00:00'):
            with self.subTest(invalid=invalid), self.assertRaises(InvalidGalleryCursor):
                self.query(cursor=encode_gallery_cursor({**payload, 'last_sort_key': invalid}))

    def test_manual_exif_unknown_order(self):
        manual = self.completed(1, manual_capture={'local':'2026-10-02','precision':'date','offset':None},
            captured_local='2030-01-01 12:00:00', capture_quality='reliable', capture_precision='second')
        exif = self.completed(2, captured_local='2026-09-30 23:59:59', capture_quality='reliable',
            capture_precision='second', capture_offset=480)
        unknown = self.completed(3, captured_local='2031-01-01 00:00:00', capture_quality='invalid')
        page = self.query()
        self.assertEqual([p.pk for p in page.items], [manual.pk, exif.pk, unknown.pk])
        self.assertEqual([g['title'] for g in page.groups], ['2026 年 10 月','2026 年 9 月','时间未知'])
        self.assertEqual(page.items[0].gallery_capture['local'], '2026-10-02')
        self.assertEqual(page.items[0].gallery_capture['precision'], 'date')
        self.assertIsNone(page.items[0].gallery_capture['offset'])
        # 人工元数据之后即使被外部代码改变，也不能漂移已冻结的相册排序。
        Photo.objects.filter(pk=manual.pk).update(manual_capture={'local':'2000-01-01','precision':'date','offset':None})
        self.assertEqual([p.pk for p in self.query().items], [manual.pk, exif.pk, unknown.pk])

    def test_same_time_id_tiebreak(self):
        for number in range(1, 51):
            self.completed(number, manual_capture={'local':'2026-10-01','precision':'date','offset':None})
        ids, cursor = [], None
        with CaptureQueriesContext(connection) as queries:
            while True:
                page = self.query(cursor=cursor)
                ids.extend(p.pk.int for p in page.items)
                if not page.next_cursor:
                    break
                cursor = page.next_cursor
        self.assertEqual(ids, list(range(50, 0, -1)))
        self.assertFalse(any(re.search(r'\bOFFSET\s+\d', query['sql'], re.I) for query in queries))

    def test_month_boundary_cursor(self):
        for number in range(1, 27):
            self.completed(number, manual_capture={'local':'2026-10-01','precision':'date','offset':None})
        self.completed(27, manual_capture={'local':'2026-09-01','precision':'date','offset':None})
        self.completed(28)
        page = self.query()
        self.assertEqual(len(page.items),24)
        self.assertEqual([g['title'] for g in page.groups], ['2026 年 10 月'])
        second = self.query(cursor=page.next_cursor)
        self.assertEqual([g['title'] for g in second.groups], ['', '2026 年 9 月', '时间未知'])
        self.assertEqual(len(second.items),4)
        self.assertIsNone(second.next_cursor)

    def test_completion_upper_bound(self):
        for number in range(1, 27):
            self.completed(number)
        pending = self.completed(90, complete=False)
        first = self.query()
        self.completed(99, manual_capture={'local':'2030-01-01','precision':'date','offset':None})
        ArchiveCompletion.objects.create(photo=pending, operation=pending.archive_operation)
        second = self.query(cursor=first.next_cursor)
        self.assertEqual([p.pk.int for p in second.items], [2, 1])
        self.assertEqual(len(self.query().items),24)
        self.assertEqual(self.query().items[0].pk.int,99)

    def test_duplicate_cursor_same_batch(self):
        for number in range(1, 52):
            self.completed(number)
        cursor = self.query().next_cursor
        first, second = self.query(cursor=cursor), self.query(cursor=cursor)
        self.assertEqual([p.pk for p in first.items], [p.pk for p in second.items])
        self.assertEqual(first.groups[0]['title'], '')
        self.assertEqual([p.pk.int for p in first.items], list(range(27,3,-1)))

    def test_invalid_cursor_rejected(self):
        from archive.services.gallery import InvalidGalleryCursor, decode_gallery_cursor, encode_gallery_cursor
        for number in range(1,26):
            self.completed(number)
        token = self.query().next_cursor
        payload = decode_gallery_cursor(token)
        for invalid in ('bad',token+'x','a'*4097):
            with self.subTest(invalid=invalid[:20]), self.assertRaises(InvalidGalleryCursor):
                self.query(cursor=invalid)
        for key, value in [('version',2),('upper_sequence',-1),('page_size',1000),('last_known',True),
                ('last_photo_id','bad'),('last_sort_key','2099-13-50'),('last_month','bad')]:
            with self.subTest(key=key), self.assertRaises(InvalidGalleryCursor):
                self.query(cursor=encode_gallery_cursor({**payload,key:value}))
        with mock.patch('django.core.signing.time.time', return_value=1):
            expired = encode_gallery_cursor(payload)
        with self.assertRaises(InvalidGalleryCursor):
            self.query(cursor=expired)
        with self.assertRaises(InvalidGalleryCursor):
            self.query(page_size=1000)

    def test_multispecies_collection_unique(self):
        one,two = self.species('鸟一'),self.species('鸟二')
        photo = self.completed(species=[one,two])
        self.assertEqual([p.pk for p in self.query().items],[photo.pk])
        for species in (one,two):
            self.assertEqual([p.pk for p in self.query(species_id=species.pk).items],[photo.pk])
        from archive.services.gallery import InvalidGalleryCursor
        cursor=self.query(species_id=one.pk).cursor
        with self.assertRaises(InvalidGalleryCursor):
            self.query(cursor=cursor,species_id=two.pk)

    def test_home_reads_completed_gallery(self):
        photo = self.completed()
        response = self.client.get('/')
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'摄影相册')
        self.assertEqual([p.pk for p in response.context['gallery_page'].items],[photo.pk])


class GalleryRouteTests(GalleryFixture):
    def test_home_is_gallery_review_named(self):
        self.assertEqual(reverse('gallery'),'/')
        self.assertEqual(reverse('workbench'),'/review/')
        response = self.client.get('/')
        self.assertContains(response,'摄影相册')
        self.assertContains(response,'class="brand" href="/"')
        self.assertContains(response,'href="/review/"')
        self.assertEqual(self.client.get(reverse('workbench')).status_code,200)

    def test_detail_species_links_reachable(self):
        one,two=self.species('鸟一'),self.species('鸟二')
        photo=self.completed(species=[one,two])
        detail=self.client.get(reverse('detail',args=[photo.pk]))
        for species in (one,two):
            link=reverse('gallery')+'?species='+str(species.pk)
            self.assertContains(detail, f'href="{link}"')
            page=self.client.get(link)
            self.assertEqual([p.pk for p in page.context['gallery_page'].items],[photo.pk])
            self.assertContains(page,species.display_name)
        for control in ('确认并归档','补充时间','填写地点','name="species_ids"'):
            self.assertNotContains(detail,control)
        stale=self.client.post(reverse('detail',args=[photo.pk]),{'metadata_version':1,
            'request_id':str(uuid.uuid4()),'classification':'undetermined'})
        self.assertEqual(stale.status_code,409)
        self.assertNotContains(stale,'确认并归档',status_code=409)

    def test_metadata_returns_review(self):
        from archive.models import Photo
        photo=Photo.objects.create(sha256='f'*64,byte_size=10,preview_key='')
        preview=self.client.get(reverse('location'),{'photo_ids':[str(photo.pk)],'location':'广东'})
        data=preview.context['form'].data.dict()
        data.update(snapshot=preview.context['snapshot'],**{f'version_{photo.pk}':1})
        saved=self.client.post(reverse('location'),data)
        self.assertEqual(saved.status_code,302)
        self.assertEqual(saved.url,'/review/?filter=all&page=1')
        self.assertContains(self.client.get(saved.url),'已保存 1 张地点。')

    def test_unconfirmed_not_in_gallery(self):
        pending=self.completed(1,complete=False)
        Photo.objects.filter(pk=pending.pk).update(state='archived')
        complete=self.completed(2)
        response=self.client.get('/')
        self.assertEqual([p.pk for p in response.context['gallery_page'].items],[complete.pk])
        self.assertNotContains(response,f'/photos/{pending.pk}/')
        self.assertContains(self.client.get('/review/'),f'/photos/{pending.pk}/')

    def test_preview_only_and_fragment(self):
        for number in range(1,27):
            self.completed(number)
        first=self.client.get('/')
        self.assertContains(first,'loading="lazy"',count=24)
        self.assertContains(first,'/preview/',count=24)
        self.assertNotContains(first,'/original/')
        token=first.context['gallery_page'].next_cursor
        fragment=self.client.get(reverse('gallery-page'),{'cursor':token})
        self.assertEqual(fragment.status_code,200)
        self.assertNotContains(fragment,'<!doctype')
        self.assertNotContains(fragment,'<header')
        self.assertContains(fragment,'loading="lazy"',count=2)
        self.assertNotContains(fragment,'<h2>时间未知</h2>')

    def test_cursor_filter_host_and_cache_boundaries(self):
        one,two=self.species('鸟一'),self.species('鸟二')
        page=self.client.get('/',{'species':str(one.pk)})
        cursor=page.context['gallery_page'].cursor
        for params in ({'cursor':'invalid'}, {'species':''}, {'species':'bad'},
                {'cursor':cursor,'species':str(two.pk)}, {'cursor':['one','two']}):
            self.assertEqual(self.client.get('/',params).status_code,400)
        self.assertEqual(self.client.get('/',{'species':str(uuid.uuid4())}).status_code,404)
        self.assertEqual(self.client.get('/',HTTP_HOST='evil.invalid').status_code,400)
        for url in ('/', '/review/', '/gallery/page/'):
            response=self.client.get(url)
            self.assertIn('no-store',response['Cache-Control'])
            self.assertEqual(self.client.post(url).status_code,405)
        self.assertEqual(self.client.get('/',{'species':one.pk}).status_code,200)
        self.assertContains(self.client.get('/',{'species':one.pk}),'暂无该鸟种的已归档照片')

    def test_detail_safe_gallery_return_and_unknown_classification(self):
        photo=self.completed(manual_capture={'local':'2026-10-01','precision':'date','offset':None})
        page=self.query()
        detail=self.client.get(reverse('detail',args=[photo.pk]),{'return':'gallery','cursor':page.cursor})
        self.assertEqual(detail.status_code,200)
        self.assertTrue(detail.context['return_url'].startswith('/?cursor='))
        self.assertContains(detail,'无法确定')
        self.assertContains(detail,'2026-10-01 · 人工补充')
        self.assertNotContains(detail,'00:00')
        returned=self.client.get(detail.context['return_url'])
        self.assertEqual([p.pk for p in returned.context['gallery_page'].items],[photo.pk])
        for params in ({'return':'https://evil.invalid/'}, {'return':'gallery','cursor':'bad'}):
            invalid=self.client.get(reverse('detail',args=[photo.pk]),params)
            self.assertEqual(invalid.status_code,400)
            self.assertNotIn('Location',invalid)
        safe=self.client.get(reverse('detail',args=[photo.pk]),{'next':'https://evil.invalid/'})
        self.assertEqual(safe.context['return_url'],'/')
        self.assertNotContains(safe,'evil.invalid')

    def test_registered_original_not_masked_by_inbox_diagnostic(self):
        from io import BytesIO
        photo=self.completed(original_available=True)
        ManagedOriginal.objects.create(photo=photo,operation=photo.archive_operation,
            relative_path='无法确定/未知年份/1.jpg',actual_basename='1.jpg',sha256=photo.sha256,byte_size=100)
        original = BytesIO(b'verified synthetic master')
        # 此测试只隔离inbox诊断；真实FD／根／hash由下载集成夹具验证。
        with mock.patch('archive.views.read_scan_code',return_value='root_missing'), \
             mock.patch('archive.views.open_completed_original',return_value=(original,'1.jpg')):
            response=self.client.get(reverse('detail',args=[photo.pk]))
            self.assertTrue(response.context['original_available'])
            self.assertNotContains(response,'原图当前不可用')
        self.assertTrue(original.closed)

    def test_review_cards_show_real_archive_states(self):
        confirmed=self.completed(1,complete=False)
        failed=self.completed(2,complete=False)
        Photo.objects.filter(pk=failed.pk).update(state='archive_failed')
        self.completed(3)
        ImportSource.objects.filter(photo=confirmed).update(relative_path='<鸟名>.jpg')
        response=self.client.get(reverse('workbench'))
        for state in ('等待归档','归档失败，待恢复','已归档'):
            self.assertContains(response,f'<p>{state}</p>')
        self.assertNotContains(response,'<p>待确认</p>')
        self.assertContains(response,'&lt;鸟名&gt;.jpg')
        self.assertNotContains(response,'<鸟名>.jpg')


class GalleryMarkup(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.nodes = []
        self.feed(response.content.decode())

    def handle_starttag(self, tag, attributes):
        self.nodes.append((tag, dict(attributes)))

    def marked(self, attribute):
        return [attrs for _, attrs in self.nodes if attribute in attrs]


class GalleryDeliveryTests(GalleryFixture):
    def populate(self, count=51):
        for number in range(1, count + 1):
            self.completed(number)

    def test_page_fragment_preview_only(self):
        self.populate(26)
        first = self.client.get('/')
        token = first.context['gallery_page'].next_cursor
        response = self.client.get(reverse('gallery-page'), {'cursor':token})
        markup = GalleryMarkup(response)
        self.assertEqual(len(markup.marked('data-photo-id')), 2)
        self.assertEqual(markup.marked('data-gallery-batch')[0]['data-gallery-cursor'], token)
        self.assertEqual(markup.marked('data-gallery-batch')[0]['data-gallery-species'], '')
        for tag, attrs in markup.nodes:
            if tag == 'img':
                self.assertEqual(attrs['loading'], 'lazy')
                self.assertEqual((attrs['width'], attrs['height']), ('480', '360'))
                self.assertIn('/preview/', attrs['src'])
        self.assertNotContains(response, '/original/')

    def test_next_cursor_and_end_state(self):
        self.populate()
        response = self.client.get('/')
        while response.context['gallery_page'].next_cursor:
            page = response.context['gallery_page']
            markup = GalleryMarkup(response)
            link = markup.marked('data-gallery-next')[0]
            self.assertTrue(link['href'].startswith('/?cursor='))
            self.assertTrue(link['data-gallery-url'].startswith('/gallery/page/?cursor='))
            self.assertEqual(link['data-gallery-next'], page.next_cursor)
            self.assertEqual(len(markup.marked('data-gallery-sentinel')), 1)
            response = self.client.get(link['href'])
            self.assertContains(response, '<!doctype')
        self.assertNotContains(response, 'data-gallery-sentinel')
        self.assertContains(response, 'data-gallery-end')

    def test_replayed_batch_stable(self):
        self.populate()
        token = self.query().next_cursor
        responses = [self.client.get(reverse('gallery-page'), {'cursor':token}) for _ in range(2)]
        batches = [GalleryMarkup(response) for response in responses]
        self.assertEqual(batches[0].marked('data-photo-id'), batches[1].marked('data-photo-id'))
        self.assertEqual(len(batches[0].marked('data-photo-id')), 24)
        for markup in batches:
            self.assertEqual(markup.marked('data-gallery-batch')[0]['data-gallery-cursor'], token)
            self.assertEqual(markup.marked('data-gallery-month')[0]['data-gallery-month'], 'unknown')

    def test_cursor_error_preserves_existing_contract(self):
        self.populate(26)
        first = self.client.get('/')
        markup = GalleryMarkup(first)
        status = markup.marked('data-gallery-message')
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]['aria-live'], 'polite')
        self.assertIn('hidden', markup.marked('data-gallery-retry')[0])
        self.assertEqual(markup.marked('data-gallery-refresh')[0]['href'], '/')
        invalid = self.client.get(reverse('gallery-page'), {'cursor':'invalid'})
        self.assertEqual(invalid.status_code, 400)
        self.assertFalse(GalleryMarkup(invalid).marked('data-gallery-batch'))
        self.assertEqual(len(markup.marked('data-photo-id')), 24)
        token = first.context['gallery_page'].next_cursor
        self.assertEqual(self.client.get(reverse('gallery-page'), {'cursor':token}).status_code, 200)

    def test_empty_gallery_review_link(self):
        response = self.client.get('/')
        self.assertContains(response, '暂无已归档照片')
        self.assertContains(response, 'href="/review/"')
        self.assertNotContains(response, 'data-gallery-sentinel')
        self.assertFalse(GalleryMarkup(response).marked('data-photo-id'))

    def test_long_names_escaped(self):
        name = '<长中文鸟名>' + '多鸟种' * 45
        bird = self.species(name)
        self.completed(species=[bird])
        self.assertContains(self.client.get('/'), '&lt;长中文鸟名&gt;')
        self.assertNotContains(self.client.get('/'), '<长中文鸟名>')
        self.populate_for_species(bird)
        response = self.client.get('/', {'species':bird.pk})
        markup = GalleryMarkup(response)
        self.assertEqual(markup.marked('data-gallery-refresh')[0]['href'], f'/?species={bird.pk}')
        self.assertIn(f'&amp;species={bird.pk}', response.content.decode())

    def populate_for_species(self, bird):
        for number in range(2, 27):
            self.completed(number, species=[bird])
