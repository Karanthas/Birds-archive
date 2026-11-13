"""有证据的有向对照；个人照片与外部资料资产始终分开。"""
from django.db.models import Q
from django.urls import reverse

from archive.models import Species
from archive.services.browse_collection import visible_photos
from archive.services.guide_sources import load_evidence, matching_rows, GuideSourceError
from archive.services.guide_online import read_metadata


def species_for_concept(concept):
    try:
        evidence = load_evidence()
    except GuideSourceError:
        return None
    rows = [row for row in evidence['mappings'] if row['concept_key'] == concept]
    if len(rows) != 1:
        return None
    row = rows[0]
    candidates = Species.objects.filter(Q(scientific_name=row['scientific_name']) |
        Q(pk__in=[alias['species_uuid'] for alias in row.get('local_bindings', [])]))
    matches = [species for species in candidates.select_related('source_snapshot').prefetch_related('concepts__snapshot')
        if [item['concept_key'] for item in matching_rows(species, evidence)] == [concept]]
    return matches[0] if len(matches) == 1 else None


def related_species(species):
    guide = read_metadata(species)
    entry = guide['entry'] or {}
    seen = {entry.get('concept_key')}
    result = []
    for relation in sorted(entry.get('similar_species', []),
            key=lambda value: (value['editorial_order'], value['target_concept'])):
        concept = relation['target_concept']
        if concept in seen:
            continue
        seen.add(concept)
        target = species_for_concept(concept)
        if target is not None and target.pk != species.pk:
            result.append((target, relation, guide))
    return result


def reachable_source(species, source_id=None):
    """Unphotographed details require a current edge from a photographed species."""
    roots = Species.objects.filter(confirmed_photos__photo_id__in=visible_photos().values('pk')).distinct()
    if source_id:
        import uuid
        try:
            roots = roots.filter(pk=uuid.UUID(source_id))
        except (ValueError, TypeError, AttributeError):
            return None
    for root in roots:
        if any(target.pk == species.pk for target, _, _ in related_species(root)):
            return root
    return None


def reference_for_species(species, context_species=None):
    from archive.services.gallery import ordered_gallery_queryset
    photo = ordered_gallery_queryset(visible_photos().filter(confirmed_species__species=species)).order_by(
        '-favorite', '-rating', '-_known', '-_sort_key', '-id').first()
    if photo:
        return {'kind':'personal', 'url':reverse('media', args=[photo.pk, 'preview']) if photo.preview_key else '',
            'alt':f'{species.display_name}的个人拍摄记录', 'photo_id':str(photo.pk),
            'note':'个人照片：包含本种的拍摄记录；多种照片未标定具体个体，不作为单种标准图。'}
    guide = read_metadata(species)
    assets = (guide['entry'] or {}).get('reference_assets', [])
    if assets:
        asset = sorted(assets, key=lambda value:value['public_file_id'])[0]
        return {'kind':'external', 'asset':asset, 'url':reverse('guide_detail', args=[(context_species or species).pk])+
            '?reference_asset='+asset['sha256'], 'alt':asset['alt'], 'note':'外部参考图',
            'original_source_url':asset['original_source_url']}
    return None


def similar_cards(species):
    current_reference = reference_for_species(species, species)
    cards = []
    for target, relation, guide in related_species(species):
        target_reference = reference_for_species(target, species)
        if (current_reference and target_reference and current_reference.get('photo_id')
                and current_reference['photo_id'] == target_reference.get('photo_id')):
            target_reference = None
        cards.append({'target':target, 'display_name':target.display_name,
            'url':reverse('guide_detail', args=[target.pk]), 'editorial_order':relation['editorial_order'],
            'sides':[{'species':species, 'text':relation['current_text'], 'reference':current_reference},
                {'species':target, 'text':relation['target_text'], 'reference':target_reference}],
            'limitations':relation['limitations'], 'source_ids':relation['source_ids'],
            'sources':[guide['sources'][key] for key in relation['source_ids']],
            'fact_check_url':relation['fact_check_url']})
    return cards
