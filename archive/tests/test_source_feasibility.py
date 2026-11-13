"""离线结构契约；不把通过称为链接、许可或生物学事实的外部验证。"""
import json
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit
from django.conf import settings
from django.test import SimpleTestCase


class SourceFeasibilityTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.matrix=json.loads((settings.BASE_DIR/'docs/source-feasibility.json').read_text())
        cls.markdown=(settings.BASE_DIR/'docs/source-feasibility.md').read_text()

    def test_every_record_has_reproducible_fields_and_documentation_id(self):
        entries=self.matrix['entries']
        self.assertGreater(len(entries),40)
        self.assertEqual(len(entries),len({r['id'] for r in entries}))
        fields={'id','species','scenario','field','source_url','checked_at','status','evidence_summary',
                'license_url','terms','cache_permission','gap','alternative'}
        for row in entries:
            with self.subTest(id=row['id']):
                self.assertFalse(fields-set(row))
                for key in fields-{'species'}: self.assertTrue(row[key],key)
                date.fromisoformat(row['checked_at'])
                self.assertIn(row['status'],('verified','pending','unavailable'))
                self.assertEqual(urlsplit(row['source_url']).scheme,'https')
                self.assertEqual(urlsplit(row['license_url']).scheme,'https')
                self.assertIn(row['id'],self.markdown)
                self.assertIn(row['source_url'],self.markdown)

    def test_each_representative_and_required_scenario_has_all_fields(self):
        representatives=self.matrix['representatives']
        self.assertEqual(len(representatives),6)
        scenarios={s for r in representatives for s in r['scenarios']}
        self.assertTrue({'common_guangdong','common_jiangsu','similar_species','multi_bird','overseas'}<=scenarios)
        fields={'name','appearance','habits','distribution','similar_species','global_conservation','china_conservation'}
        for representative in representatives:
            records=[r for r in self.matrix['entries'] if r['species']==representative['scientific_name']]
            self.assertEqual({r['field'] for r in records},fields)
            self.assertTrue(all(r['concept_id']==representative['concept_id'] for r in records))

    def test_unknown_licenses_are_not_marked_cacheable_or_runtime_proven(self):
        rows={r['id']:r for r in self.matrix['entries']}
        for row in rows.values():
            if row['field'] in ('appearance','habits','distribution','similar_species','china_conservation','name'):
                self.assertEqual(row['cache_permission'],'permission_required')
            if row['cache_permission']=='allowed_with_attribution':
                self.assertIn(row['id'],('model_weights','catalog_fixed',*[r['id'] for r in rows.values() if r['field']=='global_conservation']))
        for key in ('model_weights','model_cpu','model_accuracy','cloud_entry','cloud_price'):
            self.assertFalse(rows[key]['executed'])
        weights=rows['model_weights']
        self.assertEqual(len(weights['revision']),40)
        self.assertIn(weights['revision'],weights['weight_url'])
        self.assertNotIn('/main/',weights['weight_url'])
        self.assertEqual(rows['chinese_ebird']['cache_permission'],'permission_required')
        self.assertEqual(rows['iucn_cache']['cache_permission'],'permission_required')

    def test_fixed_catalog_counts_and_protection_are_separate_from_live_status(self):
        rows={r['id']:r for r in self.matrix['entries']}
        catalog=rows['catalog_fixed']
        self.assertEqual(catalog['species_count'],11131)
        self.assertEqual(catalog['row_count'],33684)
        self.assertEqual(len(catalog['csv_sha256']),64)
        little=rows['little_egret_global_conservation']
        self.assertIn('=NE',little['evidence_summary'])
        self.assertIn('未评估',little['evidence_summary'])
        self.assertEqual(rows['mixed_egret_pair']['status'],'pending')
        self.assertIn('不能证明外部事实',self.markdown)
