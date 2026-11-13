"""Only synthetic photos and the isolated settings_test database are used."""
from unittest import mock

from django.urls import reverse

from archive.tests.test_download import DownloadFixture


class GuideSimilarTracerTests(DownloadFixture):
    def pair(self):
        from archive.models import Species, SpeciesConcept, PhotoSpecies
        from archive.services.guide_sources import load_evidence
        photo, _, _ = self.completed()
        catalog_fixture(self.species)
        pair = []
        for row, name in zip(load_evidence()['mappings'][2:], ('北美红雀', '灰额主红雀')):
            species = Species.objects.create(scientific_name=row['scientific_name'],
                scientific_key=row['scientific_name'].lower(), chinese_name=name,
                search_key=name, source_snapshot=self.species.source_snapshot)
            SpeciesConcept.objects.create(species=species, snapshot=species.source_snapshot,
                external_id=row['external_id'], evidence='Isolated fixture for verified mapping')
            pair.append(species)
        PhotoSpecies.objects.filter(photo=photo).update(species=pair[0])
        return photo, *pair

    def test_unphotographed_related_species_is_readable_but_not_in_personal_index(self):
        from archive.models import Photo, PhotoSpecies
        photo, cardinal, pyrrhuloxia = self.pair()
        before = (Photo.objects.count(), PhotoSpecies.objects.count())
        response = self.client.get(reverse('guide_detail', args=[cardinal.pk]))
        self.assertContains(response, '灰额主红雀')
        self.assertContains(response, '成年雄鸟')
        self.assertContains(response, '成年雌鸟')
        self.assertContains(response, '幼鸟')
        self.assertContains(response, '不能套用')
        self.assertContains(response, 'CC BY-SA 4.0')
        response = self.client.get(reverse('guide_detail', args=[pyrrhuloxia.pk]))
        self.assertContains(response, '尚无照片')
        self.assertContains(response, str(cardinal.pk))
        response = self.client.get(reverse('guide_detail', args=[pyrrhuloxia.pk]),
            {'from_species':str(cardinal.pk), 'q':'北美', 'page':2})
        self.assertIn('q=%E5%8C%97%E7%BE%8E', response.context['related_return_url'])
        self.assertIn('page=2', response.context['related_return_url'])
        self.assertEqual(self.client.get(reverse('guide_detail', args=[pyrrhuloxia.pk]),
            {'from_species':str(self.species.pk)}).status_code, 404)
        index = self.client.get(reverse('guide_index'))
        self.assertNotContains(index, '灰额主红雀')
        self.assertEqual((Photo.objects.count(), PhotoSpecies.objects.count()), before)

    def test_unrelated_unphotographed_species_stays_outside_detail(self):
        self.pair()
        self.assertEqual(self.client.get(reverse('guide_detail', args=[self.species.pk])).status_code, 404)


class GuideOnlineMetadataTests(DownloadFixture):
    pair = GuideSimilarTracerTests.pair

    def test_default_protection_relations_and_assets_do_not_read_biographies_or_snapshots(self):
        from archive.models import BirdGuideSnapshot
        _, cardinal, target = self.pair()
        with mock.patch('archive.services.guide_sources.read_guide', side_effect=AssertionError('no biography')),\
                mock.patch('archive.services.guide_sources.validate_bundle', side_effect=AssertionError('no bundle')):
            response = self.client.get(reverse('guide_detail', args=[cardinal.pk]))
            self.assertContains(response, '成年雄鸟')
            self.assertContains(response, 'LC · 无危')
            from archive.services.guide_similar import reference_for_species
            reference = reference_for_species(target, cardinal)
            self.assertEqual(reference['kind'], 'external')
            asset_response = self.client.get(reference['url'])
            self.assertEqual(asset_response.status_code, 200)
            self.assertTrue(b''.join(asset_response.streaming_content))
            asset_response.close()
        self.assertEqual(BirdGuideSnapshot.objects.count(), 0)

    def test_metadata_has_no_biography_and_unknown_names_are_searchable_without_binding(self):
        from archive.services.guide_online import load_metadata, online_config
        data = load_metadata()
        for entry in data['entries'].values():
            self.assertFalse(set(entry) & {'summary','appearance','habits','distribution'})
        config = online_config(self.species)
        self.assertTrue(config['name'])
        self.assertFalse(config['qid'])
        self.assertEqual(set(config), {'species_id','name','scientific_name','qid','scope','concept_key','mapping_url'})

    def test_asset_hash_scope_cannot_read_unrelated_or_client_paths(self):
        _, cardinal, _ = self.pair()
        for value in ('../secret', 'https://example.org', '0'*64):
            self.assertEqual(self.client.get(reverse('guide_detail', args=[cardinal.pk]),
                {'reference_asset':value}).status_code, 404)


class GuideReferenceAssetTests(DownloadFixture):
    pair = GuideSimilarTracerTests.pair

    def test_real_package_contains_two_decodable_licensed_images(self):
        import hashlib
        from PIL import Image
        from archive.services.guide_sources import validate_bundle, BUNDLE_PATH
        assets = [asset for entry in validate_bundle()['entries'].values() for asset in entry['reference_assets']]
        self.assertEqual(len(assets), 2, 'Both real public reference images must be delivered')
        for asset in assets:
            raw = (BUNDLE_PATH.parent/asset['relative_key']).read_bytes()
            self.assertEqual(len(raw), asset['size'])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), asset['sha256'])
            with Image.open(BUNDLE_PATH.parent/asset['relative_key']) as image:
                image.load()
                self.assertEqual(image.size, (asset['width'], asset['height']))
            for key in ('public_file_id','author','license','license_url','source_url',
                    'original_source_url','acquired_at','change_note','alt'):
                self.assertTrue(asset[key])

    def test_import_is_idempotent_and_reference_response_is_offline(self):
        from archive.models import GuideReferenceAsset, Photo, PhotoSpecies
        from archive.services.guide_sources import import_bundle
        _, cardinal, target = self.pair()
        before = (Photo.objects.count(), PhotoSpecies.objects.count())
        import_bundle(); import_bundle()
        self.assertEqual(GuideReferenceAsset.objects.count(), 2)
        asset = GuideReferenceAsset.objects.get(snapshot__binding__species=target)
        with mock.patch('urllib.request.urlopen', side_effect=AssertionError('offline read')):
            response = self.client.get(reverse('guide_detail', args=[cardinal.pk]), {'reference_asset':str(asset.pk)})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(b''.join(response.streaming_content)), asset.size)
            response.close()
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual((Photo.objects.count(), PhotoSpecies.objects.count()), before)
        response = self.client.get(reverse('guide_detail', args=[cardinal.pk]))
        self.assertContains(response, '外部参考图')
        self.assertContains(response, 'SearchNetMedia')
        self.assertContains(response, 'CC BY 2.0')

    def test_asset_metadata_limits_and_required_rights(self):
        import copy
        from archive.services.guide_sources import validate_bundle, GuideSourceError
        original = validate_bundle()
        for key, value in [('author',''),('license',''),('acquired_at','invalid'),
                ('width',20_000_001),('size',8*1024*1024+1),('relative_key','../secret'),
                ('relative_key','assets//secret'),('source_url','http://example.org')]:
            data = copy.deepcopy(original)
            next(e for e in data['entries'].values() if e['reference_assets'])['reference_assets'][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(GuideSourceError):
                validate_bundle(data)

    def test_corruption_and_symlink_are_rejected_before_publication(self):
        import copy, hashlib
        from pathlib import Path
        from archive.services.guide_sources import validate_bundle, open_reference_asset, GuideSourceError
        asset = copy.deepcopy(next(e for e in validate_bundle()['entries'].values() if e['reference_assets'])['reference_assets'][0])
        root = self.data/'guide'; (root/'assets').mkdir(parents=True)
        asset['relative_key'] = 'assets/bad.jpg'
        (root/asset['relative_key']).write_bytes(b'not an image')
        asset.update(size=len(b'not an image'), sha256=hashlib.sha256(b'not an image').hexdigest())
        with self.assertRaises(GuideSourceError):
            open_reference_asset(asset, root=root)
        (root/asset['relative_key']).unlink()
        (root/asset['relative_key']).symlink_to(Path('/etc/hosts'))
        with self.assertRaises(GuideSourceError):
            open_reference_asset(asset, root=root)

    def test_actual_byte_and_decoded_pixel_limits(self):
        import copy, hashlib, io
        from PIL import Image
        from archive.services.guide_sources import validate_bundle, open_reference_asset, GuideSourceError
        asset = copy.deepcopy(next(e for e in validate_bundle()['entries'].values() if e['reference_assets'])['reference_assets'][0])
        root = self.data/'guide'; (root/'assets').mkdir(parents=True)
        asset['relative_key'] = 'assets/large.jpg'
        path = root/asset['relative_key']
        path.write_bytes(b'x'*(8*1024*1024+1))
        with self.assertRaises(GuideSourceError):
            open_reference_asset(asset, root=root)
        output = io.BytesIO()
        Image.new('1', (5000,4001)).convert('RGB').save(output, format='JPEG')
        raw = output.getvalue(); path.write_bytes(raw)
        # Plausible small metadata must not bypass the actual header dimensions.
        asset.update(sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))
        with self.assertRaises(GuideSourceError):
            open_reference_asset(asset, root=root)

    def test_failure_preserves_current_snapshot_and_assets(self):
        import copy
        from archive.models import SpeciesGuideBinding, GuideReferenceAsset
        from archive.services.guide_sources import import_bundle, validate_bundle, entry_payload, publish_snapshot, GuideSourceError
        _, cardinal, _ = self.pair(); import_bundle()
        binding = SpeciesGuideBinding.objects.get(species=cardinal)
        old, count = binding.current_snapshot_id, GuideReferenceAsset.objects.count()
        payload = entry_payload(validate_bundle(), binding.source_concept)
        payload['entries'][binding.source_concept]['reference_assets'][0]['sha256'] = '0'*64
        with self.assertRaises(GuideSourceError):
            publish_snapshot(binding, payload)
        binding.refresh_from_db()
        self.assertEqual(binding.current_snapshot_id, old)
        self.assertEqual(GuideReferenceAsset.objects.count(), count)

    def test_reference_id_cannot_read_unrelated_asset_or_client_paths(self):
        from archive.models import SpeciesGuideBinding, GuideReferenceAsset
        from archive.services.guide_sources import import_bundle, publish_snapshot, entry_payload, validate_bundle
        _, cardinal, _ = self.pair(); import_bundle()
        binding = SpeciesGuideBinding.objects.get(species=self.species)
        payload = entry_payload(validate_bundle(), binding.source_concept)
        payload['entries'][binding.source_concept]['reference_assets'] = next(
            e for e in validate_bundle()['entries'].values() if e['reference_assets'])['reference_assets']
        publish_snapshot(binding, payload)
        foreign = GuideReferenceAsset.objects.get(snapshot__binding=binding)
        for value in ('../../etc/hosts','https://example.org/private',str(foreign.pk)):
            self.assertEqual(self.client.get(reverse('guide_detail', args=[cardinal.pk]),
                {'reference_asset':value}).status_code, 404)


class GuideSimilarBoundaryTests(DownloadFixture):
    pair = GuideSimilarTracerTests.pair

    def test_shared_multi_species_photo_does_not_pose_as_two_standard_images(self):
        from archive.models import PhotoSpecies
        from archive.services.guide_similar import similar_cards
        photo, cardinal, target = self.pair()
        PhotoSpecies.objects.create(photo=photo, species=target)
        card = similar_cards(cardinal)[0]
        references = [side['reference'] for side in card['sides'] if side['reference']]
        self.assertEqual(len(references), 1, 'One shared photo must not impersonate separate species examples')
        self.assertIn('未标定具体个体', references[0]['note'])

    def test_zero_one_three_four_cards_keep_native_expand_and_real_remaining_count(self):
        from archive.services.guide_similar import similar_cards
        _, cardinal, _ = self.pair()
        card = similar_cards(cardinal)[0]
        for count in (0,1,3,4):
            with self.subTest(count=count), mock.patch('archive.services.guide_similar.similar_cards', return_value=[card]*count):
                response = self.client.get(reverse('guide_detail', args=[cardinal.pk]))
                if count == 0:
                    self.assertContains(response, '暂无有来源相似种资料')
                else:
                    self.assertContains(response, f'{count} 个有来源的相似种')
                self.assertContains(response, '<details class="guide-more">', count=1 if count == 4 else 0, html=False)
                if count == 4:
                    self.assertContains(response, '更多相似种（1）')

    def test_relations_deduplicate_concepts_exclude_self_and_sort_editorially(self):
        import copy
        from archive.services.guide_online import read_metadata
        from archive.services.guide_similar import related_species
        _, cardinal, target = self.pair()
        guide = read_metadata(cardinal); relation = guide['entry']['similar_species'][0]
        own = copy.deepcopy(relation); own['target_concept'] = guide['entry']['concept_key']
        duplicate = copy.deepcopy(relation); duplicate['editorial_order'] = 9
        guide['entry']['similar_species'] = [duplicate, own, relation]
        with mock.patch('archive.services.guide_similar.read_metadata', return_value=guide):
            rows = related_species(cardinal)
        self.assertEqual([item[0].pk for item in rows], [target.pk])
        self.assertEqual(rows[0][1]['editorial_order'], 1)

    def test_homonymous_unproven_species_does_not_replace_proven_target(self):
        from archive.models import Species
        from archive.services.guide_similar import related_species
        _, cardinal, target = self.pair()
        Species.objects.create(scientific_name=target.scientific_name, scientific_key='unproven',
            chinese_name=target.chinese_name, search_key='unproven', source_snapshot=target.source_snapshot)
        self.assertEqual([row[0].pk for row in related_species(cardinal)], [target.pk])

    def test_recycling_or_reassociation_selects_licensed_fallback_on_next_read(self):
        from django.utils import timezone
        from archive.models import PhotoSpecies
        from archive.services.guide_sources import import_bundle
        from archive.services.guide_similar import reference_for_species
        photo, cardinal, target = self.pair(); import_bundle()
        self.assertEqual(reference_for_species(cardinal)['kind'], 'personal')
        photo.trash_requested_at = timezone.now(); photo.save(update_fields=['trash_requested_at'])
        self.assertEqual(reference_for_species(cardinal)['kind'], 'external')
        photo.trash_requested_at = None; photo.save(update_fields=['trash_requested_at'])
        PhotoSpecies.objects.filter(photo=photo).update(species=target)
        self.assertEqual(reference_for_species(cardinal)['kind'], 'external')

    def test_single_sided_image_and_failed_external_image_keep_text_and_credit(self):
        from archive.services.guide_similar import reference_for_species
        photo, cardinal, target = self.pair()
        # A missing approved reference affects only that side, never its text.
        actual = reference_for_species
        with mock.patch('archive.services.guide_similar.reference_for_species',
                side_effect=lambda species, context_species=None: None if species.pk == target.pk else actual(species, context_species)):
            response = self.client.get(reverse('guide_detail', args=[cardinal.pk]))
        section = response.content.decode().split('<section class="guide-similar">')[1].split('</article>')[0]
        self.assertEqual(section.count('<img '), 1)
        self.assertIn('灰额主红雀', section)
        self.assertIn('成年雌鸟', section)
        response = self.client.get(reverse('guide_detail', args=[cardinal.pk]))
        section = response.content.decode().split('<section class="guide-similar">')[1].split('</article>')[0]
        for text in ('参考图暂不可用','SearchNetMedia','CC BY 2.0','成年雌鸟','幼鸟','loading="lazy"'):
            self.assertIn(text, section)
        for control in ('download/','data-favorite','data-rating','data-photo-id'):
            self.assertNotIn(control, section)

    def test_asset_publish_keeps_original_bytes_identity_paths_and_counts(self):
        from archive.models import Photo, PhotoSpecies, ManagedOriginal
        from archive.services.guide_sources import import_bundle
        photo, _, _ = self.pair()
        managed = ManagedOriginal.objects.get(photo=photo)
        path = self.archive/managed.relative_path
        before = (path.read_bytes(), managed.relative_path, list(Photo.objects.values()), list(PhotoSpecies.objects.values()))
        import_bundle()
        managed.refresh_from_db()
        self.assertEqual((path.read_bytes(), managed.relative_path, list(Photo.objects.values()), list(PhotoSpecies.objects.values())), before)

    def test_noncurrent_or_tampered_asset_record_is_not_a_file_capability(self):
        from archive.models import GuideReferenceAsset
        from archive.services.guide_sources import import_bundle, validate_bundle, publish_snapshot, entry_payload
        _, cardinal, target = self.pair(); import_bundle()
        asset = GuideReferenceAsset.objects.get(snapshot__binding__species=target)
        GuideReferenceAsset.objects.filter(pk=asset.pk).update(public_file_id='File:unpublished')
        response = self.client.get(reverse('guide_detail', args=[cardinal.pk]), {'reference_asset':str(asset.pk)})
        self.assertEqual(response.status_code, 404)
        binding = asset.snapshot.binding
        payload = entry_payload(validate_bundle(), binding.source_concept)
        payload['entries'][binding.source_concept]['summary']['text'] += '（隔离新版本）'
        publish_snapshot(binding, payload)
        self.assertEqual(self.client.get(reverse('guide_detail', args=[cardinal.pk]),
            {'reference_asset':str(asset.pk)}).status_code, 404)
        current_asset = GuideReferenceAsset.objects.get(snapshot__current_for=binding)
        response = self.client.get(reverse('guide_detail', args=[cardinal.pk]), {'reference_asset':str(current_asset.pk)})
        self.assertEqual(response.status_code, 200); response.close()


def catalog_fixture(species):
    from archive.models import SpeciesConcept
    snapshot = species.source_snapshot
    snapshot.version = 'v2025b'
    snapshot.sha256 = 'c03d5fbcfe18d75ce08f08d3bd4c03f056ced508cbc1b27a6c6d25a5220e2250'
    snapshot.save()
    SpeciesConcept.objects.create(species=species, snapshot=snapshot,
        external_id='avibase-49D9148A', evidence='Synthetic DB row for verified public mapping')


class GuideContentTracerTests(DownloadFixture):
    def test_default_page_uses_online_entry_without_biography(self):
        self.completed()
        catalog_fixture(self.species)
        with mock.patch("archive.services.guide_sources.read_guide", side_effect=AssertionError("no biography")):
            response = self.client.get(reverse("guide_detail", args=[self.species.pk]))
        self.assertContains(response, "data-guide-online")
        self.assertContains(response, "原站检索")
        self.assertContains(response, "需要JavaScript")
        from archive.services.guide_sources import read_guide
        for key in ("summary", "appearance", "habits", "distribution"):
            self.assertNotContains(response, read_guide(self.species)["entry"][key]["text"])

    def test_source_text_is_escaped_and_unknown_fields_remain_visible(self):
        self.completed()
        self.species.chinese_name = "<script>alert(1)</script>"
        self.species.save(update_fields=["chinese_name"])
        response = self.client.get(reverse("guide_detail", args=[self.species.pk]))
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert(")
        self.assertContains(response, "未知")
        self.assertNotContains(response, "\"name\": \"<script>")

    def test_invalid_source_links_and_licenses_are_rejected(self):
        from archive.services.guide_sources import validate_bundle, GuideSourceError
        data = validate_bundle()
        source = next(iter(data['sources'].values()))
        for value in ('javascript:alert(1)', 'https://user:secret@example.org', 'http://example.org'):
            source['permalink'] = value
            with self.assertRaises(GuideSourceError):
                validate_bundle(data)

    def test_missing_source_map_is_a_validation_error(self):
        from archive.services.guide_sources import validate_bundle, GuideSourceError
        data = validate_bundle()
        data['sources'] = []
        with self.assertRaises(GuideSourceError):
            validate_bundle(data)


class GuideSnapshotMigrationTests(DownloadFixture):
    def test_snapshot_binding_structure_exists(self):
        from archive import models
        self.assertIsNotNone(getattr(models, 'SpeciesGuideBinding', None),
            'A guide needs a persistent binding separate from Species identity')

    def prepared(self):
        from archive.services.guide_sources import load_evidence, resolve_binding, validate_bundle, entry_payload
        catalog_fixture(self.species)
        binding = resolve_binding(self.species, load_evidence())
        return binding, entry_payload(validate_bundle(), binding.source_concept)

    def test_explicit_command_is_idempotent_and_get_does_not_write(self):
        import io
        from django.core.management import call_command
        from archive.models import BirdGuideSnapshot
        from archive.services.guide_sources import read_guide
        self.completed()
        catalog_fixture(self.species)
        self.assertEqual(BirdGuideSnapshot.objects.count(), 0)
        self.assertEqual(read_guide(self.species)['origin'], '随应用资料包')
        self.assertEqual(BirdGuideSnapshot.objects.count(), 0)
        call_command('loadguidebundle', stdout=io.StringIO())
        before = list(BirdGuideSnapshot.objects.values())
        call_command('loadguidebundle', stdout=io.StringIO())
        self.assertEqual(list(BirdGuideSnapshot.objects.values()), before)
        with mock.patch('archive.services.guide_sources.validate_bundle', side_effect=AssertionError('saved snapshot first')):
            self.assertEqual(read_guide(self.species)['origin'], '本地已保存资料')

    def test_invalid_or_interrupted_publication_preserves_old_snapshot(self):
        import copy
        from archive.models import SpeciesGuideBinding, BirdGuideSnapshot
        from archive.services.guide_sources import publish_snapshot, read_guide, GuideSourceError
        binding, payload = self.prepared()
        old = publish_snapshot(binding, payload)
        broken = copy.deepcopy(payload)
        next(iter(broken['sources'].values()))['rights'] = ''
        with self.assertRaises(GuideSourceError):
            publish_snapshot(binding, broken)
        changed = copy.deepcopy(payload)
        changed['entries'][binding.source_concept]['summary']['text'] += '（测试新版本）'
        with mock.patch.object(SpeciesGuideBinding, 'save', side_effect=RuntimeError('simulated publish interruption')):
            with self.assertRaises(RuntimeError):
                publish_snapshot(binding, changed)
        binding.refresh_from_db()
        self.assertEqual(binding.current_snapshot_id, old.pk)
        self.assertEqual(BirdGuideSnapshot.objects.count(), 1)
        self.assertEqual(read_guide(self.species)['entry'], payload['entries'][binding.source_concept])
        new = publish_snapshot(binding, changed)
        self.assertNotEqual(new.pk, old.pk)
        old.refresh_from_db()
        self.assertEqual(old.payload, payload)

    def test_cross_binding_and_inplace_updates_are_rejected_even_by_queryset(self):
        from django.db import IntegrityError, transaction
        from archive.models import SpeciesGuideBinding, BirdGuideSnapshot, Species
        from archive.services.guide_sources import publish_snapshot
        binding, payload = self.prepared()
        snapshot = publish_snapshot(binding, payload)
        local = Species.objects.create(source_kind='local', local_name='测试名', local_key='测试名', search_key='测试名')
        other = SpeciesGuideBinding.objects.create(species=local)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SpeciesGuideBinding.objects.filter(pk=other.pk).update(current_snapshot=snapshot)
        with self.assertRaises(IntegrityError), transaction.atomic():
            BirdGuideSnapshot.objects.filter(pk=snapshot.pk).update(payload={})

    def test_stale_generation_cannot_publish(self):
        from archive.models import SpeciesGuideBinding, BirdGuideSnapshot
        from archive.services.guide_sources import publish_snapshot, GuideSourceError
        binding, payload = self.prepared()
        SpeciesGuideBinding.objects.filter(pk=binding.pk).update(request_generation=1)
        with self.assertRaisesRegex(GuideSourceError, 'stale_generation'):
            publish_snapshot(binding, payload, expected_generation=0)
        self.assertEqual(BirdGuideSnapshot.objects.count(), 0)

    def test_concurrent_reader_sees_old_then_complete_new_publication(self):
        import copy
        import threading
        from django.db import close_old_connections
        from archive.models import SpeciesGuideBinding
        from archive.services.guide_sources import publish_snapshot, read_guide
        binding, payload = self.prepared()
        old = publish_snapshot(binding, payload)
        changed = copy.deepcopy(payload)
        changed['entries'][binding.source_concept]['summary']['text'] += '（并发测试版本）'
        ready, release = threading.Event(), threading.Event()
        failures = []
        original_save = SpeciesGuideBinding.save
        def paused_save(instance, *args, **kwargs):
            ready.set()
            if not release.wait(5):
                raise RuntimeError('reader did not release writer')
            return original_save(instance, *args, **kwargs)
        def writer():
            close_old_connections()
            try:
                publish_snapshot(binding, changed)
            except Exception as error:
                failures.append(error)
            finally:
                close_old_connections()
        with mock.patch.object(SpeciesGuideBinding, 'save', paused_save):
            thread = threading.Thread(target=writer)
            thread.start()
            try:
                self.assertTrue(ready.wait(5))
                self.assertEqual(read_guide(self.species)['snapshot_id'], str(old.pk))
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(read_guide(self.species)['entry'], changed['entries'][binding.source_concept])

    def test_real_0010_0011_roundtrip_preserves_photo_identity_paths_and_bytes(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        from archive.models import Photo, PhotoSpecies, ManagedOriginal, Species
        photo, target, managed = self.completed()
        before = (list(Photo.objects.values()), list(PhotoSpecies.objects.values()),
            list(ManagedOriginal.objects.values()), list(Species.objects.values()), target.read_bytes())
        leafs = MigrationExecutor(connection).loader.graph.leaf_nodes()
        try:
            with mock.patch('archive.services.paths.open_root', side_effect=AssertionError('migration photo IO')):
                MigrationExecutor(connection).migrate([('archive', '0010_gallery_collections')])
                MigrationExecutor(connection).migrate([('archive', '0011_bird_guide')])
                MigrationExecutor(connection).migrate([('archive', '0010_gallery_collections')])
                MigrationExecutor(connection).migrate([('archive', '0011_bird_guide')])
        finally:
            MigrationExecutor(connection).migrate(leafs)
        self.assertEqual(before, (list(Photo.objects.values()), list(PhotoSpecies.objects.values()),
            list(ManagedOriginal.objects.values()), list(Species.objects.values()), target.read_bytes()))


class GuideBaselineTests(DownloadFixture):
    def test_four_substantive_entries_and_separate_protection_records(self):
        from archive.services.guide_sources import validate_bundle, load_evidence
        bundle = validate_bundle()
        self.assertEqual(len(bundle['entries']), 4)
        self.assertEqual(len(load_evidence()['mappings']), 4)
        legal = []
        for entry in bundle['entries'].values():
            for field in ('summary', 'appearance', 'habits', 'distribution'):
                self.assertGreater(len(entry[field]['text']), 35)
                for ref in entry[field]['source_ids']:
                    source = bundle['sources'][ref]
                    self.assertEqual(source['rights'], 'CC BY-SA 4.0')
                    self.assertIn('oldid=', source['permalink'])
                    self.assertIn('同按CC BY-SA', source['translation_note'])
            records = entry['protection_records']
            global_record = next(r for r in records if r['system'] == 'IUCN')
            self.assertEqual(global_record['code'], 'LC')
            self.assertIsNone(global_record['assessment_date'])
            self.assertIn('据Wikipedia转述', global_record['note'])
            self.assertEqual(global_record['citation']['precision'], 'year')
            legal += [r for r in records if r['system'] == '中国三有名录']
        self.assertEqual(len(legal), 2)
        self.assertEqual({r['code'] for r in legal}, {'CN_THREE_VALUES'})
        self.assertEqual({r['publication']['value'] for r in legal}, {'2023-06-26'})

    def test_full_evidence_binds_all_four_without_uuid_or_core_field_changes(self):
        from archive.models import Species, SpeciesConcept
        from archive.services.guide_sources import load_evidence, resolve_binding, read_guide
        catalog_fixture(self.species)
        snapshot = self.species.source_snapshot
        for row in load_evidence()['mappings']:
            species = self.species if row['scientific_name'] == 'Ardea alba' else Species.objects.create(
                source_snapshot=snapshot, scientific_name=row['scientific_name'],
                scientific_key=row['scientific_name'].lower(), search_key=row['scientific_name'])
            SpeciesConcept.objects.get_or_create(species=species, snapshot=snapshot,
                defaults={'external_id':row['external_id'], 'evidence':'synthetic catalog fixture'})
            before = Species.objects.filter(pk=species.pk).values().get()
            binding = resolve_binding(species, load_evidence())
            self.assertEqual(binding.source_concept, row['concept_key'])
            self.assertEqual(binding.match_status, 'matched')
            self.assertIsNotNone(read_guide(species)['entry'])
            self.assertEqual(Species.objects.filter(pk=species.pk).values().get(), before)

    def test_name_alone_short_id_guess_scope_and_snapshot_mismatch_are_pending(self):
        import copy
        from archive.models import SpeciesConcept
        from archive.services.guide_sources import load_evidence, resolve_binding, GuideSourceError
        evidence = load_evidence()
        self.assertEqual(resolve_binding(self.species, evidence).match_status, 'pending')
        catalog_fixture(self.species)
        for field, value in [('external_id','49D9148A'), ('snapshot_sha256','a'*64),
                ('snapshot_version','2026'), ('scientific_name','Ardea modesta')]:
            changed = copy.deepcopy(evidence)
            changed['mappings'][0][field] = value
            self.assertEqual(resolve_binding(self.species, changed).match_status, 'pending')
        changed = copy.deepcopy(evidence)
        changed['mappings'][0]['concept_key'] = 'avibase:49D9148A:avilist-2025:v1'
        with self.assertRaises(GuideSourceError):
            resolve_binding(self.species, changed)
        ambiguous = copy.deepcopy(evidence)
        ambiguous['mappings'].append(copy.deepcopy(ambiguous['mappings'][0]))
        self.assertEqual(resolve_binding(self.species, ambiguous).match_status, 'ambiguous')

    def test_custom_name_needs_explicit_uuid_and_unique_evidence(self):
        import copy
        from archive.models import Species
        from archive.services.guide_sources import load_evidence, resolve_binding, publish_snapshot, entry_payload, validate_bundle, read_guide
        local = Species.objects.create(source_kind='local', local_name='大白鹭', local_key='大白鹭', search_key='大白鹭')
        before = Species.objects.filter(pk=local.pk).values().get()
        evidence = load_evidence()
        self.assertEqual(resolve_binding(local, evidence).match_status, 'pending')
        evidence['mappings'][0]['local_bindings'] = [{'species_uuid':str(local.pk),
            'local_name':local.local_name, 'evidence_url':evidence['mappings'][0]['crosswalk_url']}]
        binding = resolve_binding(local, evidence)
        publish_snapshot(binding, entry_payload(validate_bundle(), binding.source_concept))
        self.assertEqual(read_guide(local)['entry']['scientific_name'], 'Ardea alba')
        self.assertEqual(Species.objects.filter(pk=local.pk).values().get(), before)
        ambiguous = copy.deepcopy(evidence)
        ambiguous['mappings'][1]['local_bindings'] = evidence['mappings'][0]['local_bindings']
        ambiguous['mappings'][1]['local_bindings'][0]['evidence_url'] = ambiguous['mappings'][1]['crosswalk_url']
        # Existing valid publication is preserved if newly supplied evidence conflicts.
        self.assertEqual(resolve_binding(local, ambiguous).source_concept, binding.source_concept)

    def test_dates_unknown_codes_and_rendered_systems_remain_distinct(self):
        import copy
        from archive.services.guide_sources import validate_bundle, guide_presentation, validate_date, GuideSourceError
        data = validate_bundle()
        entry = next(iter(data['entries'].values()))
        record = copy.deepcopy(entry['protection_records'][0])
        record['system'] = '地区未知体系'
        record['code'] = 'LC'
        entry['protection_records'].append(record)
        with mock.patch('archive.services.guide_sources.read_guide', return_value={
                'origin':'测试快照', 'entry':entry, 'sources':data['sources']}):
            presentation = guide_presentation(self.species)
        self.assertEqual(presentation['protection_records'][0]['system'], 'IUCN')
        unknown = next(r for r in presentation['protection_records'] if r['system'] == '地区未知体系')
        self.assertEqual(unknown['status'], 'LC · 待核')
        with self.assertRaises(GuideSourceError):
            validate_date('year', '2018-01-01')
        self.completed()
        catalog_fixture(self.species)
        response = self.client.get(reverse('guide_detail', args=[self.species.pk]))
        self.assertContains(response, '据Wikipedia转述')
        self.assertContains(response, '三有名录收录')
        self.assertContains(response, '精确评估日期：未知')
        self.assertContains(response, '国家一级／二级保护：未知')
