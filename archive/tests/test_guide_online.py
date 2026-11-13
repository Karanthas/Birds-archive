"""Controlled online fixtures; never send synthetic/private data to a public service."""
import json
import re
import subprocess
from unittest import mock

from django.urls import reverse

from archive.tests.test_guide_sources import GuideSimilarTracerTests
from archive.tests.test_download import DownloadFixture


class GuideOnlineTracerTests(DownloadFixture):
    pair = GuideSimilarTracerTests.pair

    def test_django_configuration_runs_production_javascript_to_safe_dom(self):
        _, cardinal, _ = self.pair()
        with mock.patch('urllib.request.urlopen', side_effect=AssertionError('local GET only')):
            response = self.client.get(reverse('guide_detail', args=[cardinal.pk]))
        self.assertContains(response, 'data-guide-online')
        raw = re.search(r'<script id="guide-online-config" type="application/json">(.*?)</script>',
            response.content.decode(), re.S).group(1)
        config = json.loads(raw)
        self.assertEqual(config['qid'], 'Q726389')
        self.assertTrue(config['scope'])
        result = subprocess.run(['node', 'archive/tests/test_guide_online_browser.js', '--tracer'],
            input=raw, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn('TRACER PASS', result.stdout)


class GuideOnlinePresentationTests(DownloadFixture):
    pair = GuideSimilarTracerTests.pair

    def test_candidate_and_confirmed_entrypoints(self):
        from archive.models import PhotoSpecies, ArchiveOperation
        from archive.tests.test_recognition_views import RecognitionCandidateViewTests
        from archive.tests.test_recognition_state import RecognitionFixture
        # Reuse the synthetic importer; no model inference is run.
        self.imported = lambda *args, **kwargs: RecognitionFixture.imported(self, *args, **kwargs)
        photo, path, run, species = RecognitionCandidateViewTests.candidates(self, 2)
        run.candidates.filter(rank=2).update(mapping_evidence='')
        before = (path.read_bytes(), PhotoSpecies.objects.count(), ArchiveOperation.objects.count())
        response = self.client.get(reverse('detail', args=[photo.pk]))
        entry = reverse('guide_information', args=[species[0].pk])
        self.assertContains(response, entry+'?candidate=1')
        self.assertNotContains(response, reverse('guide_information', args=[species[1].pk]))
        self.assertContains(response, '尚未确认')
        self.assertContains(response, 'target="_blank"')
        page = self.client.get(entry, {'candidate':'1', 'return':reverse('detail', args=[photo.pk])})
        self.assertContains(page, '候选尚未确认')
        self.assertContains(page, '返回原照片')
        self.assertNotContains(page, '/media/')
        config = json.loads(re.search(r'id="guide-online-config" type="application/json">(.*?)</script>',
            page.content.decode(), re.S).group(1))
        self.assertNotIn(str(photo.pk), json.dumps(config))
        self.assertNotIn(str(run.pk), json.dumps(config))
        for bad in ('https://evil.example/', '//evil.example/', '//[bad/', '/photos/../secret/', '/tasks/?secret=x'):
            rejected = self.client.get(entry, {'return':bad})
            self.assertNotContains(rejected, '返回原照片')
        self.assertEqual((path.read_bytes(), PhotoSpecies.objects.count(), ArchiveOperation.objects.count()), before)
        import uuid
        response = self.client.post(reverse('detail', args=[photo.pk]), {
            'metadata_version':photo.metadata_version, 'request_id':str(uuid.uuid4()),
            'classification':'species', 'species_ids':[str(species[0].pk)]})
        self.assertEqual(response.status_code, 303)
        self.run_worker()
        response = self.client.get(reverse('detail', args=[photo.pk]))
        self.assertContains(response, entry+'?return=')

    def test_connect_policy_and_public_payload(self):
        from django.http import HttpResponse
        from archive.guide_views import online_response
        _, cardinal, _ = self.pair()
        response = self.client.get(reverse('guide_detail', args=[cardinal.pk]))
        expected = "connect-src 'self' https://www.wikidata.org/w/api.php https://zh.wikipedia.org/w/api.php https://en.wikipedia.org/w/api.php"
        self.assertEqual(response['Content-Security-Policy'], expected)
        self.assertEqual(self.client.get(reverse('guide_information', args=[cardinal.pk]))['Content-Security-Policy'],expected)
        prior = HttpResponse(); prior['Content-Security-Policy'] = "script-src 'self'; style-src 'self'; connect-src 'none'"
        self.assertEqual(online_response(prior)['Content-Security-Policy'], "script-src 'self'; style-src 'self'; "+expected)
        raw = re.search(r'id="guide-online-config" type="application/json">(.*?)</script>',response.content.decode(),re.S).group(1)
        payload = json.loads(raw)
        self.assertEqual(set(payload), {'species_id','name','scientific_name','qid','scope','concept_key','mapping_url'})
        self.assertNotIn('Content-Security-Policy',self.client.get(reverse('gallery')))
        from django.test import Client
        self.assertEqual(Client(enforce_csrf_checks=True).post(reverse('detail',args=[response.context['gallery_page'].items[0].pk])).status_code,403)

    def test_slow_browser_lookup_does_not_block_archive(self):
        import hashlib, uuid, threading, urllib.request, urllib.parse
        from wsgiref.simple_server import make_server
        from django.core.wsgi import get_wsgi_application
        from django.conf import settings
        from archive.tests.test_recognition_state import RecognitionFixture
        from archive.models import Photo, PhotoSpecies, ManagedOriginal, ArchiveCompletion
        photo, path = RecognitionFixture.imported(self, 'slow-browser.jpg')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        server = make_server('127.0.0.1',0,get_wsgi_application())  # WSGIServer is single threaded.
        node = subprocess.Popen(['node','archive/tests/test_guide_online_browser.js','--slow'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            self.assertEqual(node.stdout.readline().strip(),'BROWSER FETCH PENDING')
            origin = 'http://127.0.0.1:'+str(server.server_port)
            with urllib.request.urlopen(origin+reverse('detail',args=[photo.pk]),timeout=3) as response:
                self.assertEqual(response.status,200)
            data = urllib.parse.urlencode({'metadata_version':photo.metadata_version,
                'request_id':str(uuid.uuid4()),'classification':'species','species_ids':str(self.species.pk)}).encode()
            # Real CSRF-aware HTTP POST, without following the response redirect.
            import http.cookiejar
            jar = http.cookiejar.CookieJar(); opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            page = opener.open(origin+reverse('detail',args=[photo.pk]),timeout=3).read().decode()
            token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"',page).group(1)
            data += b'&csrfmiddlewaretoken='+token.encode()
            result = opener.open(urllib.request.Request(origin+reverse('detail',args=[photo.pk]),data=data),timeout=3)
            self.assertEqual(result.status,200); result.close()
            self.assertIsNone(node.poll(), 'controlled browser fetch remains pending')
            self.run_worker()
            photo.refresh_from_db(); managed = ManagedOriginal.objects.get(photo=photo)
            self.assertEqual(photo.state,'archived'); self.assertEqual(PhotoSpecies.objects.filter(photo=photo).count(),1)
            self.assertEqual(ArchiveCompletion.objects.filter(photo=photo).count(),1)
            self.assertEqual(hashlib.sha256((self.archive/managed.relative_path).read_bytes()).hexdigest(),digest)
            self.assertEqual(len(list(self.archive.rglob('*.jpg'))),1)
            print(json.dumps({'proof':'slow controlled browser fetch + actual single-thread HTTP and worker',
                'root':str(self.root),'photo_state':photo.state,'associations':1,'managed_files':1,'sha256':digest}))
            out, err = node.communicate('cancel',timeout=5)
            self.assertEqual(node.returncode,0,out+err); self.assertIn('CANCELLED SAFELY',out)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)
            if node.poll() is None: node.terminate(); node.communicate(timeout=5)

    def test_public_information_never_queries_personal_collection(self):
        from archive.models import Photo, PhotoSpecies
        from archive.services.guide_sources import read_guide
        before = (Photo.objects.count(), PhotoSpecies.objects.count())
        with mock.patch('archive.services.guide.species_collection', side_effect=AssertionError('no photos')),\
                mock.patch('archive.services.guide_sources.read_guide', side_effect=AssertionError('no biography')):
            response = self.client.get(reverse('guide_information', args=[self.species.pk]))
        self.assertContains(response, 'data-guide-online')
        self.assertNotContains(response, '我的照片')
        self.assertNotContains(response, '/media/')
        self.assertEqual(response.content.decode().count('<html'), 1)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual((Photo.objects.count(), PhotoSpecies.objects.count()), before)
        self.assertEqual(self.client.get(reverse('guide_detail', args=[self.species.pk])).status_code, 404)

    def test_local_photos_and_nojs_fallback_stay_on_the_same_page(self):
        photo, cardinal, _ = self.pair()
        with mock.patch('archive.services.guide_sources.read_guide', side_effect=AssertionError('no biography')):
            response = self.client.get(reverse('guide_detail', args=[cardinal.pk]))
        self.assertContains(response, str(photo.pk))
        self.assertContains(response, '我的照片')
        self.assertContains(response, '<noscript>')
        self.assertContains(response, '原站检索')
        self.assertEqual(response.content.decode().count('<html'), 1)
        self.assertLess(response.content.index(b'data-guide-online'),response.content.index(b'data-guide-photos'))

    def test_public_page_is_get_only_and_host_validation_remains(self):
        url = reverse('guide_information', args=[self.species.pk])
        self.assertEqual(self.client.post(url).status_code, 405)
        self.assertEqual(self.client.get(url, HTTP_HOST='untrusted.example').status_code, 400)
        import uuid
        self.assertEqual(self.client.get(reverse('guide_information', args=[uuid.uuid4()])).status_code, 404)
