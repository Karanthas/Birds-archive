"""Public sample review intentions only; never activate or publish a model."""
import json
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.management.base import BaseCommand, CommandError
from archive.services import evaluation_workspace as workspace


class Command(BaseCommand):
    help = '在显式标记的测试根初始化公开审核样本、入队指定candidate或只读检查发布。'

    def add_arguments(self, parser):
        modes = parser.add_mutually_exclusive_group(required=True)
        for mode in ('bootstrap','enqueue','check-published'):
            modes.add_argument('--'+mode,action='store_true')
        parser.add_argument('--cohort',choices=('single','own_candidates','manual'))
        for key in ('manifest','protocol','data-root','model-manifest','catalog-file','catalog-manifest','artifact','output'):
            parser.add_argument('--'+key)

    def handle(self,*args,**options):
        try:
            if options.get('settings') != 'config.settings_test':
                raise ValueError('explicit_test_settings_required')
            workspace.assert_evaluation_workspace()
            if options['bootstrap']:
                keys = ('cohort','manifest','protocol','data_root','model_manifest','catalog_file','catalog_manifest')
                if any(not options.get(key) for key in keys):
                    raise ValueError('bootstrap_inputs_required')
                result = workspace.bootstrap_cohort(**{key:options[key] for key in keys})
            elif options['enqueue']:
                if not options.get('artifact'):
                    raise ValueError('explicit_candidate_required')
                if any(options.get(k) for k in ('cohort',*workspace.INPUT_KEYS)):
                    raise ValueError('frozen_session_inputs_required')
                result = {'run_ids':workspace.enqueue_candidate_runs(options['artifact'])}
            else:
                if any(options.get(k) for k in ('cohort','artifact',*workspace.INPUT_KEYS)):
                    raise ValueError('frozen_session_inputs_required')
                result = workspace.export_published_runs()
                if options.get('output'):
                    workspace.write_publication(options['output'],result)
            self.stdout.write(json.dumps(result,ensure_ascii=False,sort_keys=True))
        except (OSError,ValueError,KeyError,TypeError,ObjectDoesNotExist,ValidationError) as error:
            # Do not echo supplied private paths or raw OS exceptions.
            code = str(error) if isinstance(error,ValueError) else error.__class__.__name__
            raise CommandError('评测工作区拒绝：'+code) from None
