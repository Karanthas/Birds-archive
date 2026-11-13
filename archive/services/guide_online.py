"""Public identity/provenance only. No HTTP, biography, snapshot or write path."""
import copy
import json
from pathlib import Path

from archive.services.catalog import read_local
from archive.services.guide_sources import load_evidence, matching_rows, GuideSourceError

METADATA_PATH = Path(__file__).resolve().parent.parent / 'data/bird_guide/online_metadata.json'


def load_metadata():
    return json.loads(read_local(METADATA_PATH, 1024*1024))


def matched_row(species):
    try:
        rows = matching_rows(species, load_evidence())
    except GuideSourceError:
        rows = []
    return rows[0] if len(rows) == 1 else {}


def online_config(species):
    row = matched_row(species)
    return {'species_id':str(species.pk), 'name':species.display_name,
        'scientific_name':species.scientific_name or row.get('scientific_name', ''), 'qid':row.get('qid'),
        'scope':row.get('scope', ''), 'concept_key':row.get('concept_key', ''),
        'mapping_url':row.get('crosswalk_url', '')}


def read_metadata(species):
    data = load_metadata()
    row = matched_row(species)
    return {'entry':copy.deepcopy(data['entries'].get(row.get('concept_key'))),
        'sources':data['sources'], 'origin':'已核对轻量资料；并非实时保护／法律状态'}


def online_presentation(species):
    guide = read_metadata(species)
    records = []
    for raw in (guide['entry'] or {}).get('protection_records', []):
        record = copy.deepcopy(raw)
        code = record.get('code')
        record['status'] = {('IUCN','LC'):'LC · 无危',
            ('中国三有名录','CN_THREE_VALUES'):'三有名录收录'}.get(
            (record['system'],code), f'{code} · 待核' if code else '未知')
        record['sources'] = [guide['sources'][key] for key in record['source_ids']]
        records.append(record)
    records.sort(key=lambda row:(row['system'] != 'IUCN',row['system'],row.get('region','')))
    if not any(row['system'] == 'IUCN' for row in records):
        records.insert(0, {'system':'IUCN','status':'未知'})
    if not any(row['system'] != 'IUCN' for row in records):
        records.append({'system':'适用地区法定保护','status':'未知'})
    return {'guide_online_config':online_config(species), 'guide_data':guide,
        'protection_records':records}


def authorized_asset(species, key):
    """Full immutable hash in the current approved concept/edge is the capability."""
    from archive.services.guide_similar import related_species
    allowed = [species, *(target for target, _, _ in related_species(species))]
    for item in allowed:
        for asset in (read_metadata(item)['entry'] or {}).get('reference_assets', []):
            if asset['sha256'] == key:
                return asset
    return None
