"""公开评测输入校验、统计、实际 CPU 接缝与报告复验。"""
import json

from django.core.management.base import BaseCommand, CommandError

from archive.services.recognition_evaluation import (
    EvaluationError, load_json, run_cpu, validate_manifest, validate_report,
    validate_traces, write_report,
)


class Command(BaseCommand):
    help = "可复核的本地公开JPEG评测；不会下载模型或访问应用照片数据库。"

    def add_arguments(self, parser):
        modes = parser.add_mutually_exclusive_group(required=True)
        modes.add_argument("--validate-only", action="store_true")
        modes.add_argument("--score", metavar="RESULTS")
        modes.add_argument("--run-cpu", action="store_true")
        modes.add_argument("--validate-traces", metavar="PATH")
        modes.add_argument("--validate-report", metavar="DIR")
        for name in ("manifest", "protocol", "data-root", "output", "model-manifest", "operation-traces", "selection"):
            parser.add_argument(f"--{name}")
        parser.add_argument("--smoke", action="store_true")
        parser.add_argument("--require-nas", action="store_true")

    def handle(self, *args, **options):
        def required(*names):
            for name in names:
                if not options.get(name):
                    raise CommandError(f"缺少 --{name.replace('_', '-')}")
        try:
            if options["require_nas"] or options["selection"]:
                required("validate_report")
            if options["smoke"]:
                required("run_cpu")
            if options["validate_report"]:
                result = validate_report(options["validate_report"], options["require_nas"],
                                         load_json(options["selection"]) if options["selection"] else None)
            elif options["validate_traces"]:
                required("protocol")
                result = validate_traces(load_json(options["validate_traces"]), load_json(options["protocol"]),
                                         load_json(options["manifest"]) if options["manifest"] else None)
            else:
                required("manifest", "protocol", "data_root")
                manifest, protocol = load_json(options["manifest"]), load_json(options["protocol"])
                summary = validate_manifest(manifest, protocol, options["data_root"])
                if options["validate_only"]:
                    result = summary
                else:
                    required("output")
                    traces = load_json(options["operation_traces"]) if options["operation_traces"] else None
                    if options["run_cpu"]:
                        required("model_manifest")
                        result = run_cpu(options["model_manifest"], manifest, protocol, options["data_root"],
                                         options["output"], options["smoke"], traces)
                    else:
                        result = write_report(options["output"], manifest, protocol, load_json(options["score"]), traces)
            self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
        except (EvaluationError, OSError, KeyError, TypeError) as exc:
            raise CommandError(str(exc)) from None
