"""Explicit offline publication; GET requests never call this command."""
import json

from django.core.management.base import BaseCommand, CommandError

from archive.services.guide_sources import BUNDLE_PATH, EVIDENCE_PATH, GuideSourceError, import_bundle


class Command(BaseCommand):
    help = '完整校验并幂等导入审定鸟种资料；未知或歧义身份保持待匹配。'

    def add_arguments(self, parser):
        parser.add_argument('--bundle', default=str(BUNDLE_PATH))
        parser.add_argument('--concept-evidence', default=str(EVIDENCE_PATH))

    def handle(self, *args, **options):
        try:
            results = import_bundle(options['bundle'], options['concept_evidence'])
        except GuideSourceError as error:
            raise CommandError(str(error)) from None
        self.stdout.write(json.dumps(results, ensure_ascii=False))
