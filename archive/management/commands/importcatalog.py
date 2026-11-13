import json
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from archive.services.catalog import CatalogError, import_catalog


class Command(BaseCommand):
    help = '导入经过核验的本地 CSV 与 manifest；不获取网络数据。'

    def add_arguments(self, parser):
        parser.add_argument('--file', required=True)
        parser.add_argument('--manifest', required=True)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        try:
            result=import_catalog(options['file'],options['manifest'],dry_run=options['dry_run'])
        except CatalogError as exc:
            raise CommandError(f'名录未导入：{exc}。请核对本地快照与差异报告。') from None
        except DatabaseError:
            raise CommandError('名录未导入：database_unavailable。请稍后重试。') from None
        self.stdout.write(json.dumps(result,ensure_ascii=False))
