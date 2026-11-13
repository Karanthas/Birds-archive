"""固定浏览集合的隔离数据库夹具，不读取真实原图。"""
from unittest import mock

from django.core import signing
from django.utils import timezone
from django.urls import reverse

from archive.models import Photo
from archive.tests.test_gallery import GalleryFixture


class CollectionTests(GalleryFixture):
    def test_http_two_pages_freeze_order_and_live_visibility(self):
        for number in range(1, 28):
            self.completed(number, manual_capture={'local':'2026-10-01','precision':'date','offset':None})
        response = self.client.get(reverse('gallery'))
        self.assertEqual(response.status_code, 200)
        first = response.context['gallery_page']
        self.assertEqual(len(first.items), 24)
        Photo.objects.filter(pk=first.items[0].pk).update(metadata_version=2, rating=5)
        Photo.objects.filter(pk=Photo.objects.get(id__in=[1]).pk).update(trash_requested_at=timezone.now())
        self.completed(99)
        second = self.client.get(reverse('gallery-page'), {'cursor':first.next_cursor})
        self.assertEqual(second.status_code, 200)
        page = second.context['gallery_page']
        self.assertEqual([item.pk.int for item in page.items], [3, 2])
        self.assertTrue(page.dirty)
        self.assertEqual((page.created_count, page.remaining_count), (27, 26))
        self.assertEqual(len({p.pk for p in first.items + page.items}), 26)

    def test_changed_capture_keeps_frozen_group_and_new_match_stays_out(self):
        photo = self.completed(1, rating=4, manual_capture={'local':'2026-10-01','precision':'date','offset':None})
        first = self.query(filters={'rating_min':4})
        Photo.objects.filter(pk=photo.pk).update(current_capture={'local':'2030-01-01','month':'2030-01','sort_key':'2030-01-01'}, metadata_version=2)
        self.completed(2, rating=5)
        page = self.query(cursor=first.cursor)
        self.assertEqual([p.pk for p in page.items], [photo.pk])
        self.assertEqual(page.groups[0]['month'], '2026-10')
        self.assertTrue(page.dirty)

    def test_invalid_expired_and_legacy_tokens_do_not_fall_back(self):
        self.completed(1)
        token = self.query().cursor
        from archive.services.browse_collection import encode_collection_token
        from archive.models import GalleryCollection
        with mock.patch('django.core.signing.time.time', return_value=1):
            expired = encode_collection_token(GalleryCollection.objects.first(), 0)
        for value in ('bad', token+'x', expired, signing.dumps({'version':1}, salt='archive.gallery.cursor.v1')):
            response = self.client.get(reverse('gallery'), {'cursor':value})
            self.assertEqual(response.status_code, 400)
            self.assertContains(response, '重新筛选', status_code=400)


class GalleryFilterTests(GalleryFixture):
    def test_five_dimensions_and_species_or_deduplicate(self):
        a, b = self.species('甲'), self.species('乙')
        for number, stars, favorite, place, species in (
                (1,4,True,'广州公园',(a,b)), (2,5,True,'广州',(b,)),
                (3,3,True,'广州',(a,)), (4,5,False,'广州',(a,)), (5,5,True,'南京',(a,))):
            self.completed(number, species=species, rating=stars, favorite=favorite, user_location=place,
                manual_capture={'local':'2026-10-01','precision':'date','offset':None})
        response = self.client.get(reverse('gallery'), {'species':[str(a.pk),str(b.pk),str(a.pk)],
            'favorite':'yes','rating_min':'4','location':' 广州 ','date_from':'2026-10-01','date_to':'2026-10-01'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([p.pk.int for p in response.context['gallery_page'].items], [2,1])
        self.assertContains(response, '匹配 2 张')
        self.assertContains(response, '清除条件')

    def test_unknowns_dates_unrated_clear_and_empty(self):
        self.completed(1, manual_capture={'local':'2026-10-01','precision':'date','offset':None})
        self.completed(2, manual_capture={'local':'2026-10-02 23:59:59','precision':'second','offset':None}, rating=5, user_location='南京')
        self.completed(3)
        self.completed(4, manual_capture={'local':'2026-10-03','precision':'date','offset':None})
        page = lambda data: self.client.get(reverse('gallery'), data).context['gallery_page']
        self.assertEqual([p.pk.int for p in page({'date_from':'2026-10-01','date_to':'2026-10-02'}).items], [2,1])
        self.assertEqual([p.pk.int for p in page({'date_from':'2026-10-01'}).items], [4,2,1])
        unknown = page({'time_unknown':'on','location_unknown':'on','unrated':'on'})
        self.assertEqual([p.pk.int for p in unknown.items], [3])
        self.assertEqual(page({}).created_count, 4)
        empty = self.client.get(reverse('gallery'), {'favorite':'yes'})
        self.assertContains(empty, '没有符合条件的照片')
        for data in ({'species':'bad'}, {'rating_min':'4','unrated':'on'},
                {'time_unknown':'on','date_from':'2026-10-01'}, {'date_from':'2026-10-02','date_to':'2026-10-01'},
                {'location':'南京','location_unknown':'on'}, {'favorite':'maybe'}):
            self.assertEqual(self.client.get(reverse('gallery'), data).status_code, 400)


class CollectionNavigationTests(GalleryFixture):
    def test_neighbors_do_not_wrap_and_skip_tombstone(self):
        for number in range(1, 5):
            self.completed(number)
        token = self.query().cursor
        from archive.views import detail_navigation
        nav = lambda number: detail_navigation({'return':'gallery','cursor':token,'loaded':'1'}, Photo.objects.get(pk=number))
        self.assertIsNone(nav(4)['previous_url'])
        self.assertIsNone(nav(1)['next_url'])
        Photo.objects.filter(pk=3).update(trash_requested_at=timezone.now())
        self.assertIn(str(Photo.objects.get(pk=2).pk), nav(4)['next_url'])
        self.assertFalse(nav(3)['collection_visible'])

    def test_return_loaded_range_anchor_and_untrusted_inputs(self):
        for number in range(1, 28):
            self.completed(number)
        page = self.query()
        response = self.client.get(reverse('detail',args=[Photo.objects.get(pk=1).pk]),
            {'return':'gallery','cursor':page.cursor,'loaded':'2','return_url':'https://evil.test'})
        self.assertEqual(response.status_code, 200)
        url = response.context['return_url']
        self.assertTrue(url.startswith(reverse('gallery')+'?'))
        self.assertIn('loaded=2', url)
        self.assertTrue(url.endswith('#photo-'+str(Photo.objects.get(pk=1).pk)))
        self.assertNotIn('evil.test', url)
        restored = self.client.get(url)
        self.assertEqual(len(restored.context['gallery_page'].items), 27)
        for data in ({'return':'gallery','cursor':'bad'}, {'return':'https://evil.test'},
                {'return':'gallery','cursor':page.cursor,'loaded':'999999'}):
            self.assertEqual(self.client.get(reverse('detail',args=[Photo.objects.get(pk=1).pk]), data).status_code, 400)
        outside = self.completed(99)
        self.assertEqual(self.client.get(reverse('detail',args=[outside.pk]),
            {'return':'gallery','cursor':page.cursor}).status_code, 400)

    def test_direct_detail_has_no_implicit_collection_and_trash_state_is_explicit(self):
        photo = self.completed(1)
        direct = self.client.get(reverse('detail',args=[photo.pk]))
        self.assertContains(direct, '未从浏览集合打开')
        token = self.query().cursor
        Photo.objects.filter(pk=photo.pk).update(trash_requested_at=timezone.now())
        trashed = self.client.get(reverse('detail',args=[photo.pk]), {'return':'gallery','cursor':token})
        self.assertContains(trashed, '照片已移入回收流程')
