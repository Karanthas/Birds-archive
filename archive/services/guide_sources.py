"""Validated local guide content. Reading never creates rows or uses the network."""
import copy
import hashlib
import json
import io
import os
import re
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlsplit

from django.db import transaction
from django.utils import timezone

from archive.models import SpeciesGuideBinding, BirdGuideSnapshot, Species, GuideReferenceAsset

from archive.services.catalog import CatalogError, read_local, text, url

BUNDLE_PATH = Path(__file__).resolve().parent.parent / 'data/bird_guide/bundle.json'
EVIDENCE_PATH = BUNDLE_PATH.with_name('concept_evidence.json')
FIELDS = ('summary', 'appearance', 'habits', 'distribution')
ASSET_MAX_BYTES = 8*1024*1024
ASSET_MAX_PIXELS = 20_000_000


class GuideSourceError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate_date(precision, value):
    if precision == 'unknown' and value is None:
        return
    if precision == 'year' and isinstance(value, str) and re.fullmatch(r'\d{4}', value):
        date(int(value), 1, 1)
        return
    if precision == 'day' and isinstance(value, str):
        date.fromisoformat(value)
        return
    raise GuideSourceError('invalid_date_precision')


def validate_bundle(path=BUNDLE_PATH):
    try:
        data = copy.deepcopy(path) if isinstance(path, dict) else json.loads(read_local(path, 2*1024*1024))
        if type(data['schema_version']) is not int or data['schema_version'] != 1 or not isinstance(data['entries'], dict) or not data['entries']:
            raise GuideSourceError('invalid_schema')
        if not isinstance(data['sources'], dict) or not data['sources']:
            raise GuideSourceError('missing_sources')
        if len(canonical(data)) > 2*1024*1024:
            raise GuideSourceError('bundle_too_large')
        for source_id, source in data['sources'].items():
            text(source_id, 160)
            for key in ('title', 'revision', 'language', 'rights', 'attribution', 'translation_note', 'date_kind'):
                text(source[key], 2000)
            for key in ('source_url', 'permalink', 'rights_url', 'author_history'):
                url(source[key])
            validate_date(source['date_precision'], source['date_value'])
            date.fromisoformat(source['acquired_at'])
        for concept_key, entry in data['entries'].items():
            if not re.fullmatch(r'avibase:[A-F0-9]{16}:avilist-2025:v1', concept_key):
                raise GuideSourceError('invalid_concept')
            if entry['concept_key'] != concept_key:
                raise GuideSourceError('concept_mismatch')
            text(entry['scientific_name'], 300)
            if not isinstance(entry['display_names'], list) or not entry['display_names']:
                raise GuideSourceError('missing_display_names')
            for field in [*entry['display_names'], *(entry[k] for k in FIELDS if k in entry)]:
                text(field['text'], 12000)
                refs = field['source_ids']
                if not isinstance(refs, list) or not refs or any(ref not in data['sources'] for ref in refs):
                    raise GuideSourceError('missing_field_source')
            for record in entry['protection_records']:
                for key in ('system', 'region', 'source_version', 'concept_key'):
                    text(record[key], 500)
                if record['concept_key'] != concept_key or not record['source_ids']:
                    raise GuideSourceError('invalid_protection_concept')
                if any(ref not in data['sources'] for ref in record['source_ids']):
                    raise GuideSourceError('missing_protection_source')
                if record.get('code') is not None:
                    text(record['code'], 100)
                if record.get('assessment_date') is not None:
                    validate_date('day', record['assessment_date'])
                if record.get('assessment_doi'):
                    url(record['assessment_doi'])
                for key in ('assessment', 'citation', 'amendment', 'publication', 'effective'):
                    value = record.get(key, {'precision':'unknown', 'value':None})
                    validate_date(value['precision'], value['value'])
            if not isinstance(entry['similar_species'], list) or not isinstance(entry['reference_assets'], list):
                raise GuideSourceError('invalid_related_content')
            for relation in entry['similar_species']:
                if not re.fullmatch(r'avibase:[A-F0-9]{16}:avilist-2025:v1', relation['target_concept']):
                    raise GuideSourceError('invalid_related_concept')
                if type(relation['editorial_order']) is not int or relation['editorial_order'] < 0:
                    raise GuideSourceError('invalid_related_order')
                for key in ('current_text', 'target_text', 'limitations'):
                    text(relation[key], 6000)
                if not relation['source_ids'] or any(ref not in data['sources'] for ref in relation['source_ids']):
                    raise GuideSourceError('missing_related_source')
                url(relation['fact_check_url'])
            for asset in entry['reference_assets']:
                for key in ('public_file_id', 'author', 'license', 'change_note', 'alt'):
                    text(asset[key], 4000)
                for key in ('license_url', 'source_url', 'original_source_url'):
                    url(asset[key])
                date.fromisoformat(asset['acquired_at'])
                key = PurePosixPath(asset['relative_key'])
                if (key.is_absolute() or '..' in key.parts or '\\' in asset['relative_key']
                        or not re.fullmatch(r'assets/[A-Za-z0-9._-]+', asset['relative_key'])):
                    raise GuideSourceError('invalid_asset_path')
                if not re.fullmatch(r'[a-f0-9]{64}', asset['sha256']):
                    raise GuideSourceError('invalid_asset_hash')
                if asset['mime'] not in ('image/jpeg', 'image/png', 'image/webp') or any(
                        type(asset[k]) is not int or asset[k] <= 0 for k in ('size','width','height')):
                    raise GuideSourceError('invalid_asset_dimensions')
                if asset['size'] > ASSET_MAX_BYTES or asset['width']*asset['height'] > ASSET_MAX_PIXELS:
                    raise GuideSourceError('asset_limit_exceeded')
        return data
    except (CatalogError, KeyError, TypeError, ValueError, OSError, UnicodeError) as error:
        if isinstance(error, GuideSourceError):
            raise
        raise GuideSourceError('invalid_bundle') from None


def open_reference_asset(asset, root=None):
    """Read a reviewed local asset authorized by pinned metadata or a snapshot.

    Check its complete hash before Pillow sees any bytes; HTTP parameters cannot
    supply new image bytes or hashes. The acquisition download deadline belongs
    to material preparation, not this local reader. Runtime decoding is bounded
    by the reviewed asset, byte and pixel limits, not a claimed decoder timeout.
    """
    from PIL import Image
    from archive.services.paths import open_root, open_regular_beneath, UnsafePath
    def value(key):
        return asset[key] if isinstance(asset, dict) else getattr(asset, key)
    try:
        relative_key = value('relative_key')
        if not re.fullmatch(r'assets/[A-Za-z0-9._-]+', relative_key):
            raise GuideSourceError('invalid_asset_path')
        fd = open_root(str(root or BUNDLE_PATH.parent))
        try:
            with open_regular_beneath(fd, relative_key) as stream:
                if os.fstat(stream.fileno()).st_size > ASSET_MAX_BYTES:
                    raise GuideSourceError('asset_limit_exceeded')
                raw = stream.read(ASSET_MAX_BYTES+1)
        finally:
            os.close(fd)
        if (len(raw) > ASSET_MAX_BYTES or len(raw) != value('size')
                or hashlib.sha256(raw).hexdigest() != value('sha256')):
            raise GuideSourceError('asset_bytes_mismatch')
        with Image.open(io.BytesIO(raw), formats=['JPEG', 'PNG', 'WEBP']) as image:
            if image.width*image.height > ASSET_MAX_PIXELS:
                raise GuideSourceError('asset_limit_exceeded')
            if image.size != (value('width'), value('height')) or Image.MIME[image.format] != value('mime'):
                raise GuideSourceError('asset_dimensions_mismatch')
            image.verify()
        with Image.open(io.BytesIO(raw), formats=['JPEG', 'PNG', 'WEBP']) as image:
            image.load()
        return io.BytesIO(raw)
    except (OSError, ValueError, KeyError, TypeError, UnsafePath, Image.DecompressionBombError) as error:
        if isinstance(error, GuideSourceError):
            raise
        raise GuideSourceError('invalid_asset_file') from None


def reference_metadata(guide, asset):
    """The immutable payload, not a mutable asset row alone, grants read scope."""
    fields = ('public_file_id','author','license','license_url','source_url','change_note',
        'sha256','relative_key','mime','size','width','height')
    if guide.get('snapshot_id') != str(asset.snapshot_id):
        return None
    return next((item for item in (guide['entry'] or {}).get('reference_assets', [])
        if all(item.get(key) == getattr(asset, key) for key in fields)), None)


def load_evidence(path=EVIDENCE_PATH):
    try:
        evidence = copy.deepcopy(path) if isinstance(path, dict) else json.loads(read_local(path, 1024*1024))
        if evidence['schema_version'] != 1 or not isinstance(evidence['mappings'], list):
            raise GuideSourceError('invalid_evidence')
        for row in evidence['mappings']:
            if not re.fullmatch(r'[a-f0-9]{64}', row['snapshot_sha256']):
                raise GuideSourceError('invalid_snapshot_hash')
            for key in ('external_id', 'scientific_name', 'scope', 'snapshot_version', 'concept_key', 'identifier_evidence'):
                text(row[key], 4000)
            url(row['crosswalk_url'])
            date.fromisoformat(row['checked_at'])
            if not re.fullmatch(r'avibase:[A-F0-9]{16}:avilist-2025:v1', row['concept_key']):
                raise GuideSourceError('invalid_concept')
            parsed = urlsplit(row['crosswalk_url'])
            if parsed.hostname != 'avibase.bsc-eoc.org' or parse_qs(parsed.query).get('avibaseid') != [row['concept_key'].split(':')[1]]:
                raise GuideSourceError('crosswalk_concept_mismatch')
            for alias in row.get('local_bindings', []):
                import uuid
                uuid.UUID(alias['species_uuid'])
                text(alias['local_name'], 300)
                if alias['evidence_url'] != row['crosswalk_url']:
                    raise GuideSourceError('local_evidence_mismatch')
        return evidence
    except (CatalogError, KeyError, TypeError, ValueError, OSError) as error:
        if isinstance(error, GuideSourceError):
            raise
        raise GuideSourceError('invalid_evidence') from None


def matching_rows(species, evidence):
    """A name alone never establishes identity, even if it is unique today."""
    if species.source_kind == 'local':
        return [row for row in evidence['mappings'] if any(
            alias.get('species_uuid') == str(species.pk)
            and alias.get('local_name') == species.local_name
            and alias.get('evidence_url') == row['crosswalk_url']
            for alias in row.get('local_bindings', []))]
    concepts = list(species.concepts.all())
    return [row for row in evidence['mappings'] if any(
        concept.snapshot_id == species.source_snapshot_id
        and concept.external_id == row['external_id']
        and concept.snapshot.sha256 == row['snapshot_sha256']
        and concept.snapshot.version == row['snapshot_version']
        and species.scientific_name == row['scientific_name']
        for concept in concepts)]


def resolve_binding(species, evidence):
    evidence = load_evidence(evidence)
    candidates = matching_rows(species, evidence)
    with transaction.atomic():
        binding, _ = SpeciesGuideBinding.objects.get_or_create(species=species)
        binding = SpeciesGuideBinding.objects.select_for_update().get(pk=binding.pk)
        if len(candidates) != 1:
            if not binding.current_snapshot_id:
                binding.match_status = 'ambiguous' if candidates else 'pending'
                binding.save(update_fields=['match_status'])
            return binding
        row = candidates[0]
        if binding.source_concept and binding.source_concept != row['concept_key']:
            raise GuideSourceError('existing_concept_conflict')
        binding.match_status = 'matched'
        binding.source_concept = row['concept_key']
        binding.mapping_evidence = row
        binding.save(update_fields=['match_status', 'source_concept', 'mapping_evidence'])
        return binding


def entry_payload(bundle, concept_key):
    entry = bundle['entries'][concept_key]
    # Retain every source referenced by related content, including later plans.
    return {'schema_version':1, 'version':bundle.get('version', 'unknown'),
        'entries':{concept_key:copy.deepcopy(entry)}, 'sources':copy.deepcopy(bundle['sources'])}


def publish_snapshot(binding, payload, expected_generation=None):
    """Validate before the transaction; publish one complete immutable version."""
    payload = validate_bundle(payload)
    if list(payload['entries']) != [binding.source_concept]:
        raise GuideSourceError('binding_payload_mismatch')
    assets = payload['entries'][binding.source_concept]['reference_assets']
    for asset in assets:
        open_reference_asset(asset).close()
    content_hash = digest(payload)
    versions = {key:source['revision'] for key,source in payload['sources'].items()}
    generation = binding.request_generation if expected_generation is None else expected_generation
    with transaction.atomic():
        current = SpeciesGuideBinding.objects.select_for_update().get(pk=binding.pk)
        if current.request_generation != generation or current.source_concept != binding.source_concept:
            raise GuideSourceError('stale_generation')
        if current.match_status != 'matched':
            raise GuideSourceError('binding_not_matched')
        snapshot, created = BirdGuideSnapshot.objects.get_or_create(binding=current,
            content_sha256=content_hash, defaults={'schema_version':1, 'payload':payload,
                'source_versions':versions, 'acquired_at':timezone.now()})
        if created:
            fields = ('public_file_id','author','license','license_url','source_url','change_note',
                'sha256','relative_key','mime','size','width','height')
            for asset in assets:
                GuideReferenceAsset.objects.create(snapshot=snapshot, **{key:asset[key] for key in fields})
        if current.current_snapshot_id != snapshot.pk:
            current.current_snapshot = snapshot
            current.full_clean()
            current.save(update_fields=['current_snapshot'])
        return snapshot


def import_bundle(bundle_path=BUNDLE_PATH, evidence_path=EVIDENCE_PATH):
    bundle = validate_bundle(bundle_path)
    evidence = load_evidence(evidence_path)
    for entry in bundle['entries'].values():
        for asset in entry['reference_assets']:
            open_reference_asset(asset).close()
    # No partial publication from an invalid evidence document or content package.
    if any(row['concept_key'] not in bundle['entries'] for row in evidence['mappings']):
        raise GuideSourceError('evidence_missing_entry')
    results = []
    for species in Species.objects.select_related('source_snapshot').prefetch_related('concepts__snapshot').iterator(chunk_size=200):
        candidates = matching_rows(species, evidence)
        if not candidates:
            results.append({'species_id':str(species.pk), 'status':'pending'})
            continue
        binding = resolve_binding(species, evidence)
        if binding.match_status != 'matched':
            results.append({'species_id':str(species.pk), 'status':binding.match_status})
            continue
        snapshot = publish_snapshot(binding, entry_payload(bundle, binding.source_concept))
        results.append({'species_id':str(species.pk), 'status':'matched', 'snapshot_id':str(snapshot.pk)})
    return results


def read_guide(species):
    binding = SpeciesGuideBinding.objects.filter(species=species).select_related('current_snapshot').first()
    if binding and binding.current_snapshot_id:
        snapshot = binding.current_snapshot
        if snapshot.binding_id != binding.pk or digest(snapshot.payload) != snapshot.content_sha256:
            return {'match_status':'pending', 'origin':'已保存资料校验失败；个人照片仍可浏览',
                'entry':None, 'sources':{}}
        return {'match_status':binding.match_status, 'origin':'本地已保存资料',
            'entry':snapshot.payload['entries'].get(binding.source_concept),
            'sources':snapshot.payload['sources'], 'published_at':snapshot.published_at,
            'acquired_at':snapshot.acquired_at, 'snapshot_id':str(snapshot.pk)}
    try:
        bundle = validate_bundle()
        candidates = matching_rows(species, load_evidence())
    except GuideSourceError:
        return {'match_status':'pending', 'origin':'资料包暂不可用；资料待匹配', 'entry':None, 'sources':{}}
    if len(candidates) != 1:
        return {'match_status': 'pending', 'origin': '资料待匹配', 'entry': None, 'sources': {}}
    entry = bundle['entries'].get(candidates[0]['concept_key'])
    return {'match_status': 'matched' if entry else 'pending', 'origin': '随应用资料包',
        'entry': entry, 'sources': bundle['sources'], 'published_at': None}


def guide_presentation(species):
    guide = read_guide(species)
    entry = guide['entry'] or {}
    def section(key, title):
        value = entry.get(key, {})
        return {'title': title, 'text': value.get('text', '暂缺'),
            'sources': [guide['sources'][ref] for ref in value.get('source_ids', [])]}
    overview = section('summary', '概览')
    records = []
    for record in entry.get('protection_records', []):
        record = copy.deepcopy(record)
        code = record.get('code')
        record['status'] = {('IUCN','LC'): 'LC · 无危',
            ('中国三有名录','CN_THREE_VALUES'): '三有名录收录'}.get(
                (record['system'], code), f'{code} · 待核' if code else '未知')
        record['sources'] = [guide['sources'][ref] for ref in record['source_ids']]
        records.append(record)
    records.sort(key=lambda r: r['source_ids'])
    records.sort(key=lambda r: r.get('publication', r.get('citation', {})).get('value') or '', reverse=True)
    records.sort(key=lambda r: (r['system'] != 'IUCN', r['system'], r['region']))
    if not any(r['system'] == 'IUCN' for r in records):
        records.insert(0, {'system': '全球受威胁等级', 'status': '未知'})
    if not any(r['system'] != 'IUCN' for r in records):
        records.append({'system': '适用地区法定保护', 'status': '未知'})
    return {'guide_data': guide, 'guide_overview': overview['text'], 'overview_section': overview,
        'guide_sections': [section(key, title) for key, title in
            (('appearance', '外形'), ('habits', '习性'), ('distribution', '分布'))],
        'protection_records': records, 'guide_scientific_name': entry.get('scientific_name', ''),
        'guide_names': [{'text':name['text'], 'sources':[guide['sources'][ref] for ref in name['source_ids']]}
            for name in entry.get('display_names', [])]}
