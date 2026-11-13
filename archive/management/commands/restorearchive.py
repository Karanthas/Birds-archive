"""把完整备份显式恢复到新隔离根；核验和激活是分开的持久步骤。"""
import json
import uuid
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ObjectDoesNotExist
from django.db import connections

from archive.services.deployment import DeploymentError, NAMES, activate_restored_deployment


def _result(session):
    return {
        'session_id': str(session.pk),
        'status': session.status,
        'backup_set_id': str(session.backup_set_id),
        'manifest_sha256': session.backup_manifest_sha256,
    }


class Command(BaseCommand):
    help = '把完整备份准备、核验并显式激活到全新隔离根。'
    requires_system_checks = []

    def add_arguments(self, parser):
        subcommands = parser.add_subparsers(dest='verb', required=True)
        prepare = subcommands.add_parser('prepare')
        prepare.add_argument('--backup-set', '--backup', '--set', dest='backup_set', required=True)
        prepare.add_argument('--session', default=None)
        for name in NAMES:
            prepare.add_argument('--' + name.replace('_', '-') + '-root', required=True)
        verify = subcommands.add_parser('verify')
        verify.add_argument('--session', required=True)
        activate = subcommands.add_parser('activate')
        activate.add_argument('--session', required=True)

    def handle(self, *args, **options):
        from archive.services import restore

        try:
            if options['verb'] == 'prepare':
                target_roots = {name: str(Path(options[name + '_root'])) for name in NAMES}
                configured = {name: str(Path(getattr(settings, 'BIRDS_' + name.upper() + '_ROOT')))
                              for name in NAMES}
                if target_roots != configured:
                    raise DeploymentError('restore_target_roots_not_configured')
                session_id = str(uuid.UUID(options['session'])) if options.get('session') else str(uuid.uuid4())
                prepared = restore.prepare_restore(options['backup_set'], target_roots, session_id=session_id)
                # 复制出的旧库先在仍未发布的目标根内升级；正常web/worker此时没有registry，不能启动。
                connections.close_all()
                call_command('migrate', interactive=False, verbosity=0, stdout=self.stderr)
                session = restore.register_prepared_restore(prepared)
            elif options['verb'] == 'verify':
                session = restore.verify_restore(str(uuid.UUID(options['session'])))
            else:
                session_id = str(uuid.UUID(options['session']))
                result = activate_restored_deployment(session_id)
                self.stdout.write(json.dumps({'session_id': session_id,
                                              'status': 'activated', 'deployment': result},
                                             ensure_ascii=False, sort_keys=True))
                return
            self.stdout.write(json.dumps(_result(session), ensure_ascii=False, sort_keys=True))
        except ObjectDoesNotExist:
            raise CommandError('restore_session_missing') from None
        except (restore.RestoreError, DeploymentError, OSError, ValueError, KeyError, TypeError) as error:
            code = getattr(error, 'code', str(error))
            raise CommandError(code) from None
