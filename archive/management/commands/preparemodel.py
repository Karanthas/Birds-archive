"""Maintenance only. No downloads/installation or optional imports in this role."""
import json
from pathlib import Path
import subprocess

from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from archive.services import model_packages as packages


def export_labels(snapshot_id, output):
    from archive.models import SourceSnapshot
    snapshot = SourceSnapshot.objects.get(pk=snapshot_id)
    if snapshot.source != 'AviList' or snapshot.version != '2025b' or snapshot.species_count != packages.GLOBAL_SPECIES_COUNT:
        raise packages.ModelPackageError('catalog_snapshot_invalid')
    labels = []
    for concept in snapshot.concepts.select_related('species').order_by('species__scientific_name','external_id'):
        s = concept.species
        if s.source_kind != 'catalog' or s.source_snapshot_id != snapshot.pk:
            raise packages.ModelPackageError('local_name_in_catalog')
        labels.append({'identity_kind':'catalog','external_id':concept.external_id,'scientific_name':s.scientific_name,
                       'species_id':str(s.pk),'mapping_evidence':concept.evidence[:1000]})
    frozen = {'source':snapshot.source,'version':snapshot.version,'sha256':snapshot.sha256,
              'species_count':snapshot.species_count,'concepts':labels}
    packages.validate_labels(labels,frozen)
    root = Path(output).absolute()
    if root.exists() or any(p.is_symlink() for p in root.parents):
        raise packages.ModelPackageError('output_exists_or_symlink')
    root.mkdir(mode=0o700)
    for name, value in [('labels.json',labels),('snapshot.json',frozen),('prompts.json',{
            'version':'avilist-scientific-v1','template':packages.PROMPT,
            'items':[packages.PROMPT.format(scientific_name=r['scientific_name']) for r in labels]})]:
        with (root/name).open('x',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    return {'labels':len(labels),'output':str(root),'snapshot':str(snapshot.pk)}


class Command(BaseCommand):
    help = '离线候选维护；不会安装、下载或自动选择生产模型。'

    def add_arguments(self, parser):
        modes = parser.add_mutually_exclusive_group(required=True)
        for name in ('check-review','verify','register','activate','export-labels','prepare-vocabulary'):
            modes.add_argument('--'+name,action='store_true')
        for name in ('inventory','manifest','artifact','selection','snapshot','output','python'):
            parser.add_argument('--'+name)

    def handle(self, *args, **options):
        def need(*names):
            for name in names:
                if not options.get(name):
                    raise CommandError('缺少 --'+name)
        try:
            if options['check_review']:
                need('inventory')
                inventory = packages.load_json(options['inventory'])
                result = packages.verify_inventory(inventory)
            elif options['export_labels']:
                need('snapshot','output')
                result = export_labels(options['snapshot'], options['output'])
            elif options['prepare_vocabulary']:
                need('manifest','python')
                # Heavy imports happen only in a fresh independently budgeted role.
                package = packages.load_json(options['manifest'])
                packages.verify_manifest(package,require_features=False)
                completed = subprocess.run([options['python'],'-m','archive.services.local_model',
                    '--prepare-vocabulary',str(Path(options['manifest']).absolute())],
                    check=False,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=3600)
                if completed.returncode:
                    raise CommandError('vocabulary_prepare_failed')
                result = json.loads(completed.stdout)
            elif options['activate']:
                need('artifact','selection')
                from archive.models import ModelArtifact
                from archive.services.recognition_evaluation import validate_report
                from archive.services.recognition import set_current_model
                selection = packages.load_json(options['selection'])
                directory = selection.get('report_directory')
                if not directory:
                    raise packages.ModelPackageError('selection_report_required')
                # Full report recomputation, NAS budget, real cohort traces and frozen selection gates.
                report = validate_report(directory,require_nas=True,selection=selection)
                environment = packages.load_json(Path(directory)/'environment.json')
                with transaction.atomic():
                    artifact = ModelArtifact.objects.get(pk=options['artifact'])
                    packages.verify_manifest(artifact.manifest)
                    if (environment.get('model_manifest_digest') != artifact.manifest_digest
                            or environment.get('labels_digest') != artifact.manifest.get('label_digest')
                            or environment.get('prompt_digest') != artifact.manifest.get('prompt_digest')):
                        raise packages.ModelPackageError('selection_model_mismatch')
                    ModelArtifact.objects.filter(pk=artifact.pk).update(selection_state='selected')
                    set_current_model(artifact.pk)
                result = {'artifact':str(artifact.pk),'state':'selected','report_digest':packages.digest(report)}
            else:
                need('manifest')
                manifest = packages.load_json(options['manifest'])
                checked = packages.verify_manifest(manifest)
                result = {'manifest_digest':checked['manifest_digest'],'state':'verified','labels':len(checked['labels'])}
                if options['register']:
                    from archive.models import ModelArtifact
                    artifact, created = ModelArtifact.objects.get_or_create(manifest_digest=checked['manifest_digest'],
                        defaults={'provider':packages.PROVIDER,'version':'BioCLIP2-openclip-cpu-v1',
                                  'revision':packages.REVISION,'manifest':manifest,'selection_state':'candidate','is_current':False})
                    result.update(artifact=str(artifact.pk),state=artifact.selection_state,is_current=artifact.is_current,created=created)
            self.stdout.write(json.dumps(result,ensure_ascii=False,sort_keys=True))
        except (packages.ModelPackageError, OSError, ValueError, KeyError, TypeError, ObjectDoesNotExist, ValidationError,
                subprocess.TimeoutExpired) as error:
            raise CommandError(str(error)) from None
