"""显式本地快照：全包校验、保守概念映射、一次提交。没有出站请求。"""
import csv
import hashlib
import io
import json
import os
import re
import stat
import unicodedata
import uuid
from datetime import date
from urllib.parse import urlsplit
from django.db import transaction
from django.db.models import Q
from archive.models import Alias, SourceSnapshot, Species, SpeciesConcept


class CatalogError(ValueError):
    pass


def normalize(value):
    return unicodedata.normalize('NFKC', value.strip()).casefold()


def text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(
            unicodedata.category(c).startswith('C') for c in value):
        raise CatalogError('invalid_text')
    return value.strip()


def url(value):
    value = text(value, 1000)
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise CatalogError('invalid_source_url')
    return value


def read_package(file, manifest):
    try:
        raw = read_local(file,64 * 1024 * 1024)
        meta = json.loads(read_local(manifest,4 * 1024 * 1024).decode('utf-8-sig'))
        if not isinstance(meta, dict):
            raise CatalogError('invalid_manifest')
        for field, limit in [('source',160), ('version',160), ('attribution',4000), ('concept_evidence',4000)]:
            text(meta.get(field), limit)
        for field in ('download_page_url', 'license_url'):
            url(meta.get(field))
        date.fromisoformat(meta['acquired_at'])
        if not re.fullmatch('[a-f0-9]{64}', meta.get('sha256','')) or hashlib.sha256(raw).hexdigest() != meta['sha256']:
            raise CatalogError('checksum_mismatch')
        for field in ('row_count', 'species_count'):
            if type(meta.get(field)) is not int or not 0 < meta[field] <= 100000:
                raise CatalogError('invalid_row_count')
        columns = meta['columns']
        if not isinstance(columns, dict) or not all(columns.get(k) for k in ('scientific_name','external_id')):
            raise CatalogError('missing_columns')
        if len(set(columns.values())) != len(columns):
            raise CatalogError('duplicate_column_mapping')
        reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig'), newline=''), strict=True)
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames) or set(columns.values()) - set(reader.fieldnames):
            raise CatalogError('missing_columns')
        rows, ids, count = [], set(), 0
        for row in reader:
            count += 1
            if count > 100000 or None in row or any(value is None for value in row.values()):
                raise CatalogError('invalid_row')
            if columns.get('rank') and row[columns['rank']] != meta.get('species_rank','species'):
                continue
            external = text(row[columns['external_id']],160)
            scientific = text(row[columns['scientific_name']],300)
            chinese = row[columns['chinese_name']].strip() if columns.get('chinese_name') else ''
            if chinese:
                text(chinese,300)
                text(meta.get('chinese_name_evidence'),4000)
                url(meta.get('chinese_name_license_url'))
                if not re.search(r'[\u3400-\u9fff]',chinese):
                    raise CatalogError('not_chinese_name')
            if len(normalize(scientific))>600 or len(normalize(scientific+' '+chinese))>1000:
                raise CatalogError('normalized_text_too_long')
            if external in ids:
                raise CatalogError('duplicate_concept')
            ids.add(external)
            rows.append(dict(external_id=external,scientific_name=scientific,chinese_name=chinese))
        if count != meta['row_count'] or len(rows) != meta['species_count']:
            raise CatalogError('row_count_mismatch')
        return meta, rows
    except CatalogError:
        raise
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, csv.Error):
        raise CatalogError('invalid_package') from None


def read_local(path,limit):
    """固定打开的普通文件；有界读取，拒绝链接、FIFO和读取期间变更。"""
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        before=os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise CatalogError('input_not_regular')
        if before.st_size>limit:
            raise CatalogError('input_too_large')
        raw=stream.read(limit+1)
        after=os.fstat(stream.fileno())
        if len(raw)>limit:
            raise CatalogError('input_too_large')
        if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            raise CatalogError('input_changed')
        return raw


def differences(meta, rows):
    existing = SourceSnapshot.objects.filter(source=meta['source'],version=meta['version']).first()
    if existing:
        if existing.sha256 != meta['sha256'] or existing.manifest != meta:
            raise CatalogError('version_conflict')
        return {'status':'unchanged','species_count':existing.species_count,'changes':[], 'unresolved':[]}, {}
    previous = list(SpeciesConcept.objects.filter(snapshot__source=meta['source']).select_related('species'))
    previous_ids={item.species_id for item in previous}
    by_id = {}
    for item in previous:
        by_id.setdefault(item.external_id,set()).add(item.species_id)
    by_name = {}
    for item in previous:
        by_name.setdefault(item.species.scientific_key,set()).add(item.species_id)
    mapping = meta.get('concept_mapping',{})
    if not isinstance(mapping,dict) or set(mapping)-{r['external_id'] for r in rows}:
        raise CatalogError('invalid_concept_mapping')
    resolved, changes, unresolved, used = {}, [], [], set()
    for row in rows:
        ident = row['external_id']
        if ident in mapping:
            entry = mapping[ident]
            if (not isinstance(entry, dict)
                    or not isinstance(entry.get('species_id'), str)
                    or not entry['species_id'].strip()):
                raise CatalogError('invalid_concept_mapping')
            try:
                text(entry.get('evidence'),4000)
                species_id=uuid.UUID(entry['species_id'])
                species=Species.objects.get(pk=species_id)
            except (KeyError,TypeError,ValueError,Species.DoesNotExist):
                raise CatalogError('invalid_concept_mapping') from None
            if species_id not in previous_ids or species_id in used:
                unresolved.append({'external_id':ident,'reason':'split_merge_or_foreign_concept'})
                continue
            used.add(species_id)
            resolved[ident]=species
            if (species.scientific_name,species.chinese_name)!=(row['scientific_name'],row['chinese_name']):
                changes.append({'external_id':ident,'species_id':str(species_id),'kind':'name_change'})
        elif ident in by_id or normalize(row['scientific_name']) in by_name:
            unresolved.append({'external_id':ident,'reason':'concept_evidence_required'})
        else:
            changes.append({'external_id':ident,'kind':'new_concept'})
    incoming={row['external_id'] for row in rows}
    for ident in sorted(set(by_id)-incoming):
        changes.append({'external_id':ident,'kind':'absent_retained_no_reassignment'})
    return {'status':'review_required' if unresolved else 'ready','species_count':len(rows),
            'changes':changes,'unresolved':unresolved},resolved


def import_catalog(file, manifest, *, dry_run=False):
    meta, rows = read_package(file,manifest)
    report, _ = differences(meta,rows)
    if dry_run or report['status']=='unchanged':
        return report
    # Recheck against concurrent imports under the SQLite IMMEDIATE transaction.
    # Outside it a snapshot and its concepts can be read across another commit.
    with transaction.atomic():
        report, resolved = differences(meta,rows)
        if report['status']=='unchanged':
            return report
        if report['unresolved']:
            raise CatalogError('concept_review_required')
        snapshot=SourceSnapshot.objects.create(**{k:meta[k] for k in ('source','version','sha256','acquired_at',
            'download_page_url','license_url','attribution','row_count','species_count')},manifest=meta)
        new_species, changed, concepts, aliases = [], [], [], []
        for row in rows:
            species=resolved.get(row['external_id'])
            if species:
                for name,language in ((species.scientific_name,'la'),(species.chinese_name,'zh')):
                    if name:
                        aliases.append(Alias(species=species,name=name,language=language,search_key=normalize(name),source_snapshot=species.source_snapshot))
            else:
                species=Species()
                new_species.append(species)
            species.scientific_name=row['scientific_name']
            species.scientific_key=normalize(row['scientific_name'])
            species.chinese_name=row['chinese_name']
            species.search_key=normalize(row['scientific_name']+' '+row['chinese_name'])
            species.source_snapshot=snapshot
            if row['external_id'] in resolved:
                changed.append(species)
            concepts.append(SpeciesConcept(species=species,snapshot=snapshot,external_id=row['external_id'],
                evidence=meta.get('concept_mapping',{}).get(row['external_id'],{}).get('evidence',meta['concept_evidence'])))
        Species.objects.bulk_create(new_species,batch_size=500)
        Species.objects.bulk_update(changed,['scientific_name','scientific_key','chinese_name','search_key','source_snapshot'],batch_size=500)
        SpeciesConcept.objects.bulk_create(concepts,batch_size=500)
        Alias.objects.bulk_create(aliases,batch_size=500)
    report['status']='imported'
    return report


def search_species(query):
    key=normalize(query)
    if len(query.strip())>200:
        raise CatalogError('query_too_long')
    if not key:
        return Species.objects.none()
    return Species.objects.filter(Q(search_key__contains=key)|Q(aliases__search_key__contains=key)).distinct().select_related('source_snapshot').order_by('scientific_key','id')
