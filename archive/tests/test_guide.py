"""图鉴只读取当前正常归档投影；夹具均为隔离合成 JPEG。"""
import hashlib
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from archive.models import Photo, PhotoSpecies, Species
from archive.tests.test_download import DownloadFixture
from urllib.parse import parse_qs, urlsplit
from archive.services.gallery import query_gallery


class GuideNavigationTests(DownloadFixture):
    def test_neighbors_do_not_cross_species_and_hx_is_only_a_batch(self):
        import uuid
        first, _, _ = self.completed()
        second, _, _ = self.completed('second.jpg', 'blue')
        foreign, _, _ = self.completed('other.jpg', 'green')
        other = Species.objects.create(source_kind='local', local_name='其他鸟', local_key='其他鸟', search_key='其他鸟')
        PhotoSpecies.objects.filter(photo=foreign).update(species=other)
        response = self.client.get(reverse('guide_detail', args=[self.species.pk]))
        params = {key:values[0] for key,values in parse_qs(response.context['gallery_detail_query']).items()}
        opened = self.client.get(reverse('detail', args=[first.pk]), params)
        links = (opened.context['previous_url'] or '')+(opened.context['next_url'] or '')
        self.assertIn(str(second.pk), links)
        self.assertNotIn(str(foreign.pk), links)
        fragment = self.client.get(reverse('guide_detail', args=[self.species.pk]),
            {'cursor':params['cursor'], 'species':str(self.species.pk)}, HTTP_HX_REQUEST='true')
        self.assertContains(fragment, 'data-gallery-batch')
        self.assertNotContains(fragment, '<html')
        self.assertNotContains(fragment, '<script')
        self.assertNotContains(fragment, 'data-gallery data-guide-photos')

    def test_detail_photos_and_signed_round_trip_preserve_origin(self):
        first, _, _ = self.completed()
        second, _, _ = self.completed('second.jpg', 'blue')
        album = query_gallery(filters={'favorite': 'yes', 'location': '广州'})
        origin = {'q': '白', 'page': '2', 'album_cursor': album.cursor, 'album_loaded': '1'}
        response = self.client.get(reverse('guide_detail', args=[self.species.pk]), origin)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '我的照片')
        self.assertContains(response, 'data-guide-online')
        self.assertContains(response, '维基百科原站检索')
        self.assertContains(response, '资料待匹配')
        self.assertEqual(len(response.context['gallery_page'].items), 2)
        context = parse_qs(response.context['gallery_detail_query'])
        params = {key: values[0] for key, values in context.items()}
        opened = self.client.get(reverse('detail', args=[first.pk]), params)
        self.assertEqual(opened.status_code, 200)
        back = urlsplit(opened.context['return_url'])
        self.assertEqual(back.path, reverse('guide_detail', args=[self.species.pk]))
        self.assertEqual(parse_qs(back.query)['album_cursor'], [album.cursor])
        self.assertEqual(back.fragment, f'photo-{first.pk}')
        neighbor = opened.context['previous_url'] or opened.context['next_url']
        self.assertIsNotNone(neighbor)
        self.assertIn(str(second.pk), neighbor)
        returned = self.client.get(opened.context['return_url'])
        self.assertEqual(returned.status_code, 200)
        self.assertIn('favorite', album.collection.filters)
        self.assertEqual(parse_qs(urlsplit(returned.context['album_return_url']).query)['cursor'], [album.cursor])

    def test_rejects_foreign_species_collection_forged_origin_and_external_return(self):
        photo, _, _ = self.completed()
        generic = query_gallery()
        url = reverse('guide_detail', args=[self.species.pk])
        for params in ({'cursor': generic.cursor}, {'cursor': 'forged'},
                {'album_cursor': 'forged'}, {'return': 'https://evil.invalid/'}, {'q': 'x'*201}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get(url, params).status_code, 400)
        self.assertEqual(self.client.get(reverse('detail', args=[photo.pk]),
            {'return': 'guide', 'species': str(self.species.pk), 'cursor': generic.cursor}).status_code, 400)

    def test_unphotographed_species_not_open_and_get_does_not_fetch_sources(self):
        self.assertEqual(self.client.get(reverse('guide_detail', args=[self.species.pk])).status_code, 404)
        self.completed()
        with mock.patch('urllib.request.urlopen', side_effect=AssertionError('no network on reading')):
            response = self.client.get(reverse('guide_detail', args=[self.species.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '全球受威胁等级')
        self.assertContains(response, '适用地区法定保护')
        self.assertContains(response, '相似种资料暂缺：暂无有来源相似种资料')

    def test_species_collection_excludes_recycled_and_corrected_members(self):
        photo, _, _ = self.completed()
        page = query_gallery(species_id=self.species.pk)
        from archive.services.guide import species_collection
        PhotoSpecies.objects.filter(photo=photo).delete()
        current = species_collection(self.species.pk, page.cursor)
        self.assertEqual(current.items, [])
        self.assertEqual(current.remaining_count, 0)
        self.assertTrue(current.dirty)

    def test_native_next_page_and_loaded_window_are_reachable(self):
        self.completed()
        for index in range(25):
            self.completed(f'page-{index}.jpg', (index*7, 90, 200))
        response = self.client.get(reverse('guide_detail', args=[self.species.pk]))
        self.assertEqual(len(response.context['gallery_page'].items), 24)
        next_url = response.context['gallery_next_url']
        following = self.client.get(next_url)
        self.assertEqual(following.status_code, 200)
        self.assertEqual(len(following.context['gallery_page'].items), 2)
        restored = self.client.get(reverse('guide_detail', args=[self.species.pk]),
            {'cursor': response.context['gallery_page'].cursor, 'loaded': 2})
        self.assertEqual(len(restored.context['gallery_page'].items), 26)


class GuideTracerTests(DownloadFixture):
    def test_archived_card_reaches_species_without_changing_assets(self):
        photo, path, _ = self.completed()
        before = (hashlib.sha256(path.read_bytes()).hexdigest(),
            list(Species.objects.values()), list(PhotoSpecies.objects.values()))
        response = self.client.get('/guide/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '1 张照片')
        self.assertContains(response, reverse('guide_detail', args=[self.species.pk]))
        detail = self.client.get(reverse('guide_detail', args=[self.species.pk]), follow=True)
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, f'/photos/{photo.pk}/')
        self.assertEqual(before, (hashlib.sha256(path.read_bytes()).hexdigest(),
            list(Species.objects.values()), list(PhotoSpecies.objects.values())))

    def test_pending_and_recycle_requested_do_not_make_cards(self):
        photo, _, _ = self.confirmed()
        self.assertNotContains(self.client.get('/guide/'), '1 张照片')
        self.run_worker()
        Photo.objects.filter(pk=photo.pk).update(trash_requested_at=timezone.now())
        self.assertContains(self.client.get('/guide/'), '还没有已拍鸟种')


class GuideProjectionTests(DownloadFixture):
    def local_species(self, name, key=None):
        return Species.objects.create(source_kind='local', local_name=name, local_key=key or name,
            search_key=key or name)

    def test_cover_priority_is_independent_from_recent_species_order(self):
        from archive.services.guide import query_guide
        old, _, _ = self.completed()
        latest, _, _ = self.completed('latest.jpg', 'blue')
        middle, _, _ = self.completed('middle.jpg', 'green')
        other = self.local_species('另一鸟种')
        PhotoSpecies.objects.filter(photo=middle).update(species=other)
        Photo.objects.filter(pk=old.pk).update(favorite=True, rating=1,
            current_capture={'sort_key':'2020-01-01', 'month':'2020-01', 'local':'2020-01-01'})
        Photo.objects.filter(pk=latest.pk).update(rating=5,
            current_capture={'sort_key':'2026-10-25', 'month':'2026-10', 'local':'2026-10-25'})
        Photo.objects.filter(pk=middle.pk).update(
            current_capture={'sort_key':'2024-01-01', 'month':'2024-01', 'local':'2024-01-01'})
        cards = list(query_guide())
        self.assertEqual([card.species.pk for card in cards], [self.species.pk, other.pk])
        self.assertEqual(cards[0].cover.pk, old.pk)
        self.assertEqual(cards[0].photo_count, 2)
        Photo.objects.filter(pk=old.pk).update(favorite=False, rating=None)
        self.assertEqual(query_guide()[0].cover.pk, latest.pk)

    def test_unknown_last_uuid_ties_and_multi_species_distinct_count(self):
        from archive.services.guide import query_guide
        photo, _, _ = self.completed()
        same_name = self.local_species('白鹭')
        PhotoSpecies.objects.create(photo=photo, species=same_name)
        Photo.objects.filter(pk=photo.pk).update(current_capture={})
        cards = list(query_guide())
        self.assertEqual([card.species.pk for card in cards], sorted([self.species.pk, same_name.pk]))
        self.assertEqual([card.photo_count for card in cards], [1, 1])
        self.assertEqual(len({card.cover.pk for card in cards}), 1)
        response = self.client.get('/guide/')
        self.assertContains(response, str(same_name.pk)[:8])

    def test_search_normalizes_without_rewriting_names_and_escapes_markup(self):
        photo, _, _ = self.completed()
        local = self.local_species('<script>alert(1)</script> 很长鸟名'+'鸟'*100)
        PhotoSpecies.objects.create(photo=photo, species=local)
        for query in (' 白鹭 ', ' ARDEA ALBA ', 'Ａｒｄｅａ'):
            response = self.client.get('/guide/', {'q':query})
            self.assertEqual(response.context['page_obj'].paginator.count, 1)
        response = self.client.get('/guide/', {'q':'<script>'})
        self.assertContains(response, '&lt;script&gt;')
        self.assertNotContains(response, '<script>alert(1)</script>')
        local.refresh_from_db()
        self.assertIsNone(local.scientific_name)
        self.assertEqual(local.local_name, '<script>alert(1)</script> 很长鸟名'+'鸟'*100)
        self.assertEqual(self.client.get('/guide/', {'q':'x'*201}).status_code, 400)

    def test_native_pagination_empty_search_and_album_context(self):
        photo, _, _ = self.completed()
        for index in range(25):
            PhotoSpecies.objects.create(photo=photo, species=self.local_species(f'本地鸟{index}'))
        response = self.client.get('/guide/', {'page':999, 'q':'本地鸟'})
        self.assertEqual(response.context['page_obj'].number, 2)
        self.assertEqual(len(response.context['page_obj']), 1)
        self.assertContains(response, '上一页')
        self.assertContains(response, '鸟种查询')
        empty = self.client.get('/guide/', {'q':'未拍物种'})
        self.assertContains(empty, '没有找到已拍鸟种')
        self.assertContains(empty, '清除搜索')
        self.assertEqual(self.client.get('/guide/', {'favorite':'yes'}).context['page_obj'].paginator.count, 26)
        album = self.client.get('/', {'favorite':'yes', 'location':'广州'})
        target = album.context['guide_index_url']
        response = self.client.get(target)
        self.assertEqual(response.context['page_obj'].paginator.count, 26)
        self.assertEqual(parse_qs(urlsplit(response.context['album_return_url']).query)['cursor'],
            [album.context['gallery_page'].cursor])

    def test_database_failure_is_error_not_empty_result(self):
        from django.db import DatabaseError
        with mock.patch('archive.guide_views.query_guide', side_effect=DatabaseError('private path')):
            response = self.client.get('/guide/', {'q':'白鹭'})
        self.assertEqual(response.status_code, 503)
        self.assertContains(response, '重试', status_code=503)
        self.assertNotContains(response, '还没有已拍鸟种', status_code=503)
        self.assertNotContains(response, 'private path', status_code=503)

    def test_real_worker_correction_trash_restore_boundaries(self):
        import uuid
        from archive.services.guide import query_guide
        from archive.services.management import accept_management, restore_confirmation, accept_confirmed_restore
        photo, path, _ = self.completed()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        other = self.local_species('更正鸟种')
        photo.refresh_from_db()
        operation = accept_management(photo.pk, photo.metadata_version, uuid.uuid4(), 'species',
            {'mode':'replace', 'classification':'identified', 'species_ids':[str(other.pk)], 'local_names':[]})
        self.assertEqual(query_guide()[0].species.pk, self.species.pk)
        self.run_worker()
        operation.refresh_from_db()
        self.assertEqual(operation.status, 'succeeded')
        self.assertEqual(query_guide()[0].species.pk, other.pk)
        photo.refresh_from_db()
        accept_management(photo.pk, photo.metadata_version, uuid.uuid4(), 'trash', {})
        self.assertEqual(query_guide().paginator.count, 0)
        self.run_worker()
        photo.refresh_from_db()
        accept_confirmed_restore(photo.pk, restore_confirmation(photo))
        self.assertEqual(query_guide().paginator.count, 0)
        self.run_worker()
        photo.refresh_from_db()
        self.assertEqual(query_guide()[0].species.pk, other.pk)
        self.assertEqual(hashlib.sha256((self.archive/photo.managed_original.relative_path).read_bytes()).hexdigest(), before)

    def test_known_capture_before_unknown_and_cover_uuid_tie(self):
        from archive.services.guide import query_guide
        first, _, _ = self.completed()
        second, _, _ = self.completed('second.jpg', 'blue')
        unknown, _, _ = self.completed('unknown.jpg', 'green')
        other = self.local_species('时间未知鸟种')
        PhotoSpecies.objects.filter(photo=unknown).update(species=other)
        Photo.objects.filter(pk__in=[first.pk, second.pk]).update(rating=2,
            current_capture={'sort_key':'2000-01-01 00:00:00', 'month':'2000-01', 'local':'2000-01-01 00:00:00'})
        Photo.objects.filter(pk=unknown.pk).update(current_capture={})
        cards = list(query_guide())
        self.assertEqual([card.species.pk for card in cards], [self.species.pk, other.pk])
        self.assertEqual(cards[0].cover.pk, max(first.pk, second.pk))

    def test_access_method_host_and_no_store(self):
        self.completed()
        self.assertIn('no-store', self.client.get('/guide/')['Cache-Control'])
        self.assertEqual(self.client.post('/guide/').status_code, 405)
        self.assertEqual(self.client.get('/guide/', HTTP_HOST='evil.invalid').status_code, 400)
        self.assertEqual(self.client.get('/guide/?q=a&q=b').status_code, 400)
