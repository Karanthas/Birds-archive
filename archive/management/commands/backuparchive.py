import json

from django.core.management.base import BaseCommand, CommandError

from archive.services import backup, maintenance


class Command(BaseCommand):
    help = "请求、创建、校验或结束受维护屏障保护的一致备份。"
    requires_system_checks = []

    def add_arguments(self, parser):
        subcommands = parser.add_subparsers(dest="verb", required=True)
        subcommands.add_parser("request").add_argument("--owner")
        subcommands.add_parser("status")
        create = subcommands.add_parser("create")
        create.add_argument("--session", required=True)
        create.add_argument("--destination", required=True)
        verify = subcommands.add_parser("verify")
        verify.add_argument("--set", dest="set_path", required=True)
        quiesce = subcommands.add_parser("quiesce")
        quiesce.add_argument("--session", required=True)
        resume = subcommands.add_parser("resume")
        resume.add_argument("--session", required=True)

    def handle(self, *args, **options):
        try:
            if options["verb"] == "request":
                result = maintenance.request(options.get("owner"))
            elif options["verb"] == "status":
                result = maintenance.status()
            elif options["verb"] == "create":
                result = backup.create(options["session"], options["destination"])
            elif options["verb"] == "verify":
                result = backup.verify(options["set_path"])
            elif options["verb"] == "quiesce":
                session = options["session"]
                with maintenance.acquire(session, owner="backuparchive", require_supervisor=True):
                    pass
                result = {"status": "quiescent", "session_id": session}
            else:
                result = maintenance.resume(options["session"], owner="backuparchive")
            self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
        except (backup.BackupError, maintenance.MaintenanceError, OSError, ValueError,
                KeyError, TypeError) as error:
            raise CommandError(str(error)) from None
