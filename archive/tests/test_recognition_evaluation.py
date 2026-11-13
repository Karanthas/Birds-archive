"""只使用隔离 JPEG 的可手算评测合同；不证明模型或 NAS 效果。"""
import copy
import csv
import hashlib
import io
import json
import tempfile
import uuid
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from django.core.management import call_command, get_commands
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from archive.tests.fixtures import jpeg


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def protocol():
    return {
        "schema_version": 1, "version": "control-v1", "evaluation_split": "evaluation",
        "required_scenarios": ["common", "confusable", "natural_multispecies"],
        "allowed_licenses": ["CC0-1.0", "CC-BY-3.0", "CC-BY-SA-4.0"],
        "selection": {"state": "baseline_only", "nas_budget": None,
                      "maximum_occupancy_seconds": None, "correction_rule": None},
        "threshold": {"state": "uncalibrated", "calibration_split": "calibration"},
    }


class EvaluationFixture(SimpleTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="birds-evaluation-control-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.data = self.root / "public"
        self.data.mkdir()
        self.protocol = protocol()
        self.manifest = {"schema_version": 1, "dataset_id": "control", "evidence_type": "control",
                         "images": []}
        # A occurs twice, B once: micro Top1=1/3, macro=(1/2+0)/2=1/4.
        for index, truth in enumerate([["A"], ["A"], ["B"], ["A", "B"]]):
            path = self.data / f"{index}.jpg"
            sha = jpeg(path, color=(index * 40, 90, 50))
            self.manifest["images"].append({
                "photo_id": f"p{index}", "path": path.name, "sha256": sha,
                "size_bytes": path.stat().st_size, "split": "evaluation",
                "near_duplicate_group": f"g{index}", "training_overlap": "unknown",
                "truth_set": truth, "complete_truth_verified": True, "included": True,
                "scenarios": ["common", "confusable"] if index < 3 else ["natural_multispecies"],
                "source_url": "https://example.org/photo", "source_revision": "123",
                "author": "control author", "license": "CC0-1.0",
                "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                "attribution": "Synthetic test fixture", "acquired_at": "2026-10-17",
                "label_evidence": "Synthetic control truth, not actual bird labels",
                "pixel_review": {"reviewer": "test", "method": "control", "visible_birds": []},
                "taxonomy": [{"id": name, "snapshot": "control", "external_id": name,
                              "scientific_name": name, "evidence": "control"} for name in truth],
            })
        self.results = {"schema_version": 1, "evidence_type": "control", "runner": "control",
                        "manifest_digest": digest(self.manifest), "protocol_digest": digest(self.protocol),
                        "images": [], "environment": {"evidence_type": "control"},
                        "resources": {"evidence_type": "control", "electricity": {"status": "unknown"}},
                        "batch_timings": {"cold_load_seconds": 1, "vocabulary_seconds": 2,
                                          "wall_seconds": 10}}
        for i, ranked in enumerate([["A", "B"], ["B", "A"], [], ["A", "C", "B"]]):
            sha = self.manifest["images"][i]["sha256"]
            self.results["images"].append({
                "photo_id": f"p{i}", "ranked_candidates": [{"species_id": x, "raw_label": x,
                    "score": 1 - rank / 10} for rank, x in enumerate(ranked)],
                "mapping_errors": ["unresolved B"] if i == 2 else [],
                "failure": "mapping_failed" if i == 2 else None, "retries": 1 if i == 2 else 0,
                "input_hash_before": sha, "input_hash_after": sha,
                "timings": {"preprocess_seconds": .1, "inference_seconds": .2,
                            "mapping_seconds": .01, "queue_seconds": 0},
                "threshold_outcome": "skipped", "operation_trace_ref": None})

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def score(self, output="report", traces=None):
        args = {"score": str(self.write("results.json", self.results)),
                "manifest": str(self.write("manifest.json", self.manifest)),
                "protocol": str(self.write("protocol.json", self.protocol)),
                "data_root": str(self.data), "output": str(self.root / output), "stdout": io.StringIO()}
        if traces:
            args["operation_traces"] = str(self.write("traces.json", traces))
        call_command("evaluaterecognition", **args)
        return self.root / output

    def traces(self):
        rows = []
        for cohort in ("single", "own_candidates", "manual"):
            rows.append({"trace_id": cohort, "session_id": cohort, "cohort": cohort,
                         "partition": "evaluation", "operator": "script",
                         "observation_type": "simulated_browser", "browser_evidence_ref": "control-log",
                         "protocol_digest": digest(self.protocol), "manifest_digest": digest(self.manifest),
                         "images": [{"photo_id": f"p{i}", "sha256": self.manifest["images"][i]["sha256"],
                                     "final_set": self.manifest["images"][i]["truth_set"]}
                                    for i in range(4)],
                         "actions": [{"action": "confirm", "photo_ids": [f"p{i}"], "at_seconds": i}
                                     for i in range(4)]})
        return {"schema_version": 1, "traces": rows}


class EvaluationRedGateTests(SimpleTestCase):
    def test_report_command_is_available(self):
        self.assertIn("evaluaterecognition", get_commands(),
                      "可复核统计必须经实际管理命令提供")


class SelectedReportContractTests(SimpleTestCase):
    """真实报告的本地只读复算合同；不执行模型或重复科学留出。"""
    def setUp(self):
        self.directory = Path(__file__).resolve().parents[2] / 'docs/recognition/nas-baseline'
        self.selection = json.loads((self.directory.parent / 'selection.json').read_text())

    def validate(self, selection=None, directory=None):
        from archive.services.recognition_evaluation import validate_report
        return validate_report(directory or self.directory, require_nas=True,
            selection=self.selection if selection is None else selection)

    def test_selected_decision_recomputes_all_seven_bound_digests(self):
        report = self.validate()
        self.assertEqual(report['metrics']['n'], 7)
        self.assertEqual(report['metrics']['mapping_failure_images'], 7)
        self.assertEqual(report['metrics']['threshold_outcomes']['skipped'], 7)
        self.assertEqual(report['selection_state'], 'pending', '原报告不是后来选型决定，原字节不回填')

    def test_missing_or_wrong_digest_and_incomplete_checks_cannot_select(self):
        from archive.services.recognition_evaluation import EvaluationError
        for key in ('manifest_digest', 'protocol_digest', 'results_digest', 'traces_digest',
                    'report_digest', 'environment_digest', 'resources_digest'):
            for replacement in (None, '0'*64):
                invalid = copy.deepcopy(self.selection)
                if replacement is None:
                    invalid.pop(key)
                else:
                    invalid[key] = replacement
                with self.subTest(key=key, value=replacement), self.assertRaises(EvaluationError):
                    self.validate(invalid)
        for key in self.selection['checks']:
            invalid = copy.deepcopy(self.selection)
            invalid['checks'][key] = False
            with self.subTest(check=key), self.assertRaises(EvaluationError):
                self.validate(invalid)
        for patch_values in ({'state': 'pending'}, {'decision_evidence_ref': ''}):
            with self.subTest(values=patch_values), self.assertRaises(EvaluationError):
                self.validate({**self.selection, **patch_values})

    def test_development_result_unfrozen_policy_or_missing_natural_cannot_be_relabelled_selected(self):
        import shutil
        from archive.services.recognition_evaluation import EvaluationError
        # Every mutation alters only its named source fact. The normal validator
        # must reject rather than trusting a selected flag or stale report hash.
        mutations = (
            ('results.json', lambda value: value.update(evidence_type='model_cpu')),
            ('protocol.json', lambda value: value['selection'].update(state='baseline_only')),
            ('manifest.json', lambda value: [row.update(scenarios=[s for s in row['scenarios']
                if s != 'natural_multispecies']) for row in value['images']]),
        )
        for name, mutate in mutations:
            with self.subTest(source=name), tempfile.TemporaryDirectory(prefix='birds-selection-contract-') as temp:
                target = Path(temp)
                for source in self.directory.iterdir():
                    if source.name in ('manifest.json', 'protocol.json', 'results.json', 'operation-traces.json',
                            'environment.json', 'resources.json', 'report.json', 'per-image.csv'):
                        shutil.copyfile(source, target/source.name)
                value = json.loads((target/name).read_text())
                mutate(value)
                (target/name).write_text(json.dumps(value))
                with self.assertRaises(EvaluationError):
                    self.validate(directory=target)


class EvaluationMetricsTests(EvaluationFixture):
    def test_cli_report_recomputable_denominators_topk_macro_and_csv(self):
        out = self.score()
        report = json.loads((out / "report.json").read_text())
        single = report["metrics"]["single"]
        self.assertEqual(single["n"], 3)
        self.assertEqual(single["top1_hits"], 1)
        self.assertEqual(single["top3_hits"], 2)
        self.assertAlmostEqual(single["top1_micro"], 1 / 3)
        self.assertAlmostEqual(single["top3_micro"], 2 / 3)
        self.assertEqual(single["top1_macro"], .25)
        self.assertEqual(single["top3_macro"], .5)
        self.assertEqual(report["metrics"]["failures"], 1)
        self.assertEqual(report["metrics"]["retries"], 1)
        self.assertEqual(report["metrics"]["strata"]["confusable"]["n"], 3)
        rows = list(csv.DictReader((out / "per-image.csv").open()))
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(int(r["top1_hit"]) for r in rows[:3]), 1)
        self.assertEqual(report["selection_state"], "pending")
        self.assertEqual(json.loads((out / "resources.json").read_text())["electricity"]["status"], "unknown")
        self.assertEqual(report["batch_timings"]["cold_load_seconds"], 1)

    def test_multispecies_coverage_is_not_top1_set_or_manual_exact_match(self):
        out = self.score(traces=self.traces())
        multi = json.loads((out / "report.json").read_text())["metrics"]["multispecies"]
        self.assertEqual(multi["n"], 1)
        self.assertEqual(multi["candidate_complete"], 1)
        self.assertEqual(multi["candidate_partial"], 0)
        self.assertEqual(multi["extra_candidates"], 1)
        self.assertEqual(multi["top1_set_complete"], 0)
        self.assertEqual(multi["top1_set_partial"], 1)
        self.assertEqual(multi["missing_species_additions"], 1)
        self.assertEqual(multi["manual_final_exact_by_cohort"]["single"], {"n": 1, "exact": 1})

    def test_absent_actions_are_unknown_not_inferred_from_candidates(self):
        report = json.loads((self.score() / "report.json").read_text())
        self.assertEqual(report["operations"], {"status": "unknown", "cohorts": {}})

    def test_actions_count_actual_events_and_keep_simulation_label(self):
        traces = self.traces()
        traces["traces"][1]["actions"] = [{"action": "confirm", "photo_ids": ["p0", "p1", "p2", "p3"], "at_seconds": 1}]
        report = json.loads((self.score(traces=traces) / "report.json").read_text())
        cohort = report["operations"]["cohorts"]["own_candidates"]
        self.assertEqual(cohort["events"], 1)
        self.assertEqual(cohort["photo_action_links"], 4)
        self.assertEqual(cohort["events_per_photo"], .25)
        self.assertEqual(cohort["observation_types"], ["simulated_browser"])

    def test_failures_cannot_keep_successful_candidates(self):
        self.results["images"][0]["failure"] = "timeout"
        report = json.loads((self.score() / "report.json").read_text())
        self.assertEqual(report["metrics"]["single"]["top1_hits"], 0)


class EvaluationActionRedTests(EvaluationFixture):
    def test_observed_actions_require_matching_published_run_identity(self):
        from archive.services.recognition_evaluation import validate_traces, EvaluationError
        traces = self.traces()
        for trace in traces['traces']:
            trace.update(observation_type='observed_agent',operator_type='agent')
            trace['publication'] = {'schema_version':1, 'session_id':'wrong', 'images':[]}
        with self.assertRaises(EvaluationError, msg='真实浏览器动作必须绑定实际发布run，不能只比较图片hash'):
            validate_traces(traces,self.protocol,self.manifest)

    def test_actual_multispecies_additions_are_counted_from_trace_only(self):
        traces = self.traces()
        traces["traces"][0]["actions"].insert(0, {"action": "add_species", "photo_ids": ["p3"], "at_seconds": 0})
        report = json.loads((self.score(traces=traces) / "report.json").read_text())
        self.assertEqual(report["metrics"]["multispecies"].get("observed_add_species_by_cohort"),
                         {"single": 1, "own_candidates": 0, "manual": 0},
                         "实际补选必须来自对应多鸟图动作，不能用G减Top1的机会数冒充")


class PublishedTraceTests(EvaluationFixture):
    """Schema fixtures only. These records are never written as actual NAS evidence."""
    def bound_traces(self):
        from archive.services.recognition_evaluation import PUBLICATION_DIGESTS
        host=dict(machine='x86_64',kernel_release='CONTROL-KERNEL',cpu_identity_sha256='a'*64,
            memory_total_bytes=16*1024**3,network_interfaces=['lo'])
        attestation=dict(schema_version=1,approved_target=True,target_id='CONTROL ONLY',
            approval_evidence_ref='Synthetic schema contract; not NAS authorization',host=host)
        attestation_json=json.dumps(attestation)
        attestation_sha=hashlib.sha256(attestation_json.encode()).hexdigest()
        self.protocol['execution']=dict(memory_mb=6144,nas_attestation=dict(path='approved-target.json',sha256=attestation_sha))
        self.manifest['evidence_type']='public'
        bundle=self.traces()
        for trace in bundle['traces']:
            trace.update(session_id=str(uuid.uuid4()),observation_type='observed_agent',operator_type='agent')
            p=dict(schema_version=1,session_id=trace['session_id'],cohort=trace['cohort'],
                artifact_id=str(uuid.uuid4()),workspace_digest=digest(trace['cohort']),frozen_digest='b'*64,
                evidence_type='model_cpu',runner='manual' if trace['cohort']=='manual' else 'local_model_cpu',
                corpus_evidence_type='public',current_unchanged=True,images=[])
            for key in PUBLICATION_DIGESTS:
                p[key]={'manifest_digest':digest(self.manifest),'protocol_digest':digest(self.protocol)}.get(key,'c'*64)
            for image in trace['images']:
                row=dict(image_id=image['photo_id'],photo_id=str(uuid.uuid4()),sha256=image['sha256'],
                    source_observation_id=str(uuid.uuid4()),run_id=None,generation=None,artifact_id=None)
                row['detail_url']='/photos/'+row['photo_id']+'/'
                if trace['cohort']!='manual':
                    row.update(run_id=str(uuid.uuid4()),generation=2,artifact_id=p['artifact_id'])
                p['images'].append(row)
                image.update(workspace_photo_id=row['photo_id'],source_observation_id=row['source_observation_id'],
                    **{key:row[key] for key in ('run_id','generation','artifact_id')})
            p['publication_digest']=digest(p)
            trace.update(publication=p,publication_digest=p['publication_digest'])
            if trace['cohort']=='manual':
                continue
            raw=dict(schema_version=1,publication_digest=p['publication_digest'],session_id=p['session_id'],
                cohort=p['cohort'],runner='local_model_cpu',offline=True,observed_host=host,images=[])
            raw.update({key:p[key] for key in PUBLICATION_DIGESTS})
            for image in p['images']:
                row={key:image[key] for key in ('image_id','photo_id','sha256','run_id','generation','artifact_id')}
                row.update(input_hash_before=image['sha256'],input_hash_after=image['sha256'],
                    model_pid=11,worker_pid=12,supervisor_pid=13,
                    resources=dict(memory_control='enforced',soft_bytes=6144*1024**2,hard_bytes=6144*1024**2))
                attached={**row,'network_control':'os_enforced','outbound_attempts':0}
                row.update(raw_evidence=attached,raw_evidence_sha256=digest(attached),raw_evidence_ref='raw/process.json')
                raw['images'].append(row)
            trace['execution_evidence']=dict(schema_version=1,evidence_type='nas_cpu',raw_record=raw,
                raw_record_digest=digest(raw),attestation_record=attestation,attestation_digest=digest(attestation),
                attestation_json=attestation_json,attestation_sha256=attestation_sha)
        return bundle

    def test_bound_three_cohort_schema_accepts_complete_records_and_rejects_identity_edits(self):
        from archive.services.recognition_evaluation import validate_traces, EvaluationError
        bundle=self.bound_traces()
        validate_traces(bundle,self.protocol,self.manifest)
        mutations=[lambda b:b['traces'].pop(),
            lambda b:b['traces'][0]['images'][0].update(run_id=str(uuid.uuid4())),
            lambda b:b['traces'][0]['images'][0].update(generation=99),
            lambda b:b['traces'][0]['images'][0].update(sha256='d'*64),
            lambda b:b['traces'][0]['publication'].update(protocol_digest='e'*64),
            lambda b:b['traces'][0]['execution_evidence'].update(evidence_type='control'),
            lambda b:b['traces'][0]['execution_evidence'].update(attestation_sha256='f'*64),
            lambda b:b['traces'][0]['execution_evidence'].pop('raw_record'),
            lambda b:b['traces'][0].pop('execution_evidence'),
            lambda b:b['traces'][2]['images'][0].update(run_id=str(uuid.uuid4()))]
        for mutate in mutations:
            broken=copy.deepcopy(bundle)
            mutate(broken)
            with self.subTest(mutate=mutate),self.assertRaises(EvaluationError):
                validate_traces(broken,self.protocol,self.manifest)

    def test_resealed_bad_process_budget_target_raw_attachment_or_candidate_cannot_pass(self):
        from archive.services.recognition_evaluation import validate_traces, EvaluationError
        bundle=self.bound_traces()
        for key,value in [('model_pid',12),('input_hash_before','e'*64),('run_id',str(uuid.uuid4()))]:
            broken=copy.deepcopy(bundle)
            evidence=broken['traces'][0]['execution_evidence']
            evidence['raw_record']['images'][0][key]=value
            evidence['raw_record_digest']=digest(evidence['raw_record'])
            with self.assertRaises(EvaluationError):
                validate_traces(broken,self.protocol,self.manifest)
        for mutate in [lambda e:e['raw_record']['images'][0]['resources'].update(memory_control='unsupported_unverified'),
            lambda e:e['raw_record']['images'][0]['raw_evidence'].update(outbound_attempts=1),
            lambda e:e['raw_record']['observed_host'].update(cpu_identity_sha256='d'*64),
            lambda e:e.update(attestation_json='{}')]:
            broken=copy.deepcopy(bundle)
            evidence=broken['traces'][0]['execution_evidence']
            mutate(evidence)
            evidence['raw_record_digest']=digest(evidence['raw_record'])
            with self.assertRaises(EvaluationError):
                validate_traces(broken,self.protocol,self.manifest)
        broken=copy.deepcopy(bundle)
        p=broken['traces'][0]['publication']
        p.update(evidence_type='control',runner='control',corpus_evidence_type='control')
        p['publication_digest']=digest({k:v for k,v in p.items() if k!='publication_digest'})
        broken['traces'][0]['publication_digest']=p['publication_digest']
        with self.assertRaises(EvaluationError):
            validate_traces(broken,self.protocol,self.manifest)


class EvaluationIntegrityTests(EvaluationFixture):
    def test_empty_predictions_missing_files_and_empty_manifest_fail(self):
        self.results["images"] = []
        with self.assertRaises(CommandError):
            self.score()
        with self.assertRaises(CommandError):
            call_command("evaluaterecognition", validate_report=str(self.root / "absent"))
        self.manifest["images"] = []
        with self.assertRaises(CommandError):
            self.score()

    def test_manifest_rejects_missing_permission_hash_truth_and_duplicate_split_leakage(self):
        from archive.services.recognition_evaluation import validate_manifest, EvaluationError
        for field in ("license", "sha256", "truth_set", "source_revision", "pixel_review", "taxonomy"):
            broken = copy.deepcopy(self.manifest)
            del broken["images"][0][field]
            with self.subTest(field=field), self.assertRaises(EvaluationError):
                validate_manifest(broken, self.protocol, self.data)
        broken = copy.deepcopy(self.manifest)
        broken["images"][1]["near_duplicate_group"] = "g0"
        broken["images"][1]["split"] = "calibration"
        with self.assertRaises(EvaluationError):
            validate_manifest(broken, self.protocol, self.data)

    def test_paths_escape_symlink_and_tampered_jpeg_rejected(self):
        from archive.services.recognition_evaluation import validate_manifest, EvaluationError
        for name in ("../0.jpg", "/tmp/0.jpg"):
            broken = copy.deepcopy(self.manifest)
            broken["images"][0]["path"] = name
            with self.assertRaises(EvaluationError):
                validate_manifest(broken, self.protocol, self.data)
        original = self.data / "0.jpg"
        original.rename(self.data / "real.jpg")
        original.symlink_to(self.data / "real.jpg")
        with self.assertRaises(EvaluationError):
            validate_manifest(self.manifest, self.protocol, self.data)

    def test_changed_hash_mapping_rank_nan_and_missing_rows_rejected(self):
        for key, value in (("input_hash_after", "0" * 64), ("ranked_candidates", [{"species_id": "A", "score": float("nan")}])):
            original = copy.deepcopy(self.results)
            self.results["images"][0][key] = value
            with self.subTest(key=key), self.assertRaises(CommandError):
                self.score()
            self.results = original
        self.results["images"].pop()
        with self.assertRaises(CommandError):
            self.score()

    def test_report_recompute_detects_edits_and_control_cannot_be_nas(self):
        out = self.score()
        call_command("evaluaterecognition", validate_report=str(out), stdout=io.StringIO())
        with self.assertRaises(CommandError):
            call_command("evaluaterecognition", validate_report=str(out), require_nas=True)
        report = json.loads((out / "report.json").read_text())
        report["metrics"]["single"]["top1_hits"] = 3
        (out / "report.json").write_text(json.dumps(report))
        with self.assertRaises(CommandError):
            call_command("evaluaterecognition", validate_report=str(out))

    def test_relabelled_control_and_unfrozen_selection_fail(self):
        self.results["evidence_type"] = "nas_cpu"
        with self.assertRaises(CommandError):
            self.score()
        self.results["evidence_type"] = "control"
        out = self.score("baseline")
        selected = self.write("selection.json", {"state": "selected"})
        with self.assertRaises(CommandError):
            call_command("evaluaterecognition", validate_report=str(out), selection=str(selected))

    def test_trace_modes_final_sets_partitions_and_operators_validated(self):
        from archive.services.recognition_evaluation import validate_traces, EvaluationError
        validate_traces(self.traces(), self.protocol, self.manifest)
        for mutate in (lambda x: x["traces"].pop(),
                       lambda x: x["traces"][0].update(operator=""),
                       lambda x: x["traces"][0].update(partition="calibration"),
                       lambda x: x["traces"][0]["images"][0].update(final_set=["A", "A"]),
                       lambda x: x["traces"][0]["images"][0].update(sha256="0"*64)):
            broken = self.traces()
            mutate(broken)
            with self.assertRaises(EvaluationError):
                validate_traces(broken, self.protocol, self.manifest)

    def test_validate_only_and_validate_traces_flags_really_execute(self):
        out = io.StringIO()
        call_command("evaluaterecognition", validate_only=True,
                     manifest=str(self.write("manifest.json", self.manifest)),
                     protocol=str(self.write("protocol.json", self.protocol)), data_root=str(self.data), stdout=out)
        self.assertEqual(json.loads(out.getvalue())["included"], 4)
        call_command("evaluaterecognition", validate_traces=str(self.write("traces.json", self.traces())),
                     protocol=str(self.write("protocol.json", self.protocol)), stdout=io.StringIO())

    def test_run_cpu_missing_adapter_fails_explicitly_without_report(self):
        with self.assertRaisesRegex(CommandError, "cpu_adapter_unavailable"):
            call_command("evaluaterecognition", run_cpu=True, smoke=True,
                         model_manifest=str(self.write("model.json", {"schema_version": 1})),
                         manifest=str(self.write("manifest.json", self.manifest)),
                         protocol=str(self.write("protocol.json", self.protocol)), data_root=str(self.data),
                         output=str(self.root / "cpu"))
        self.assertFalse((self.root / "cpu" / "report.json").exists())

    def test_unknown_cost_is_not_free_and_estimates_need_sources_ranges(self):
        self.results["resources"]["electricity"] = {"status": "estimated", "value": 0}
        with self.assertRaises(CommandError):
            self.score()

    def test_resource_contract_keeps_cpu_storage_rss_address_space_and_tree_distinct(self):
        from archive.services.recognition_evaluation import validate_resources, EvaluationError
        resources = {"evidence_type": "model_cpu", "cpu_seconds": 2.5, "storage_bytes": 1024,
                     "peak_rss_bytes": 2048, "peak_address_space_bytes": 16384,
                     "process_tree_peak_rss_bytes": 4096, "collector": "controlled schema example",
                     "raw_evidence_ref": "control resource log", "electricity": {"status": "unknown"}}
        validate_resources(resources, "model_cpu")
        self.assertEqual([resources[k] for k in ("cpu_seconds", "storage_bytes", "peak_rss_bytes",
                         "peak_address_space_bytes", "process_tree_peak_rss_bytes")], [2.5, 1024, 2048, 16384, 4096])
        for missing in ("cpu_seconds", "storage_bytes", "peak_rss_bytes", "peak_address_space_bytes", "process_tree_peak_rss_bytes"):
            broken = dict(resources)
            del broken[missing]
            with self.subTest(missing=missing), self.assertRaises(EvaluationError):
                validate_resources(broken, "model_cpu")
        resources["process_tree_peak_rss_bytes"] = -1
        with self.assertRaises(EvaluationError):
            validate_resources(resources, "model_cpu")

    def test_output_never_overwrites_existing_evidence(self):
        self.score()
        with self.assertRaises(CommandError):
            self.score()

    def test_csv_and_raw_result_tampering_each_fail_revalidation(self):
        for attachment in ("per-image.csv", "results.json", "environment.json", "resources.json"):
            with self.subTest(attachment=attachment):
                out = self.score(output=attachment.replace(".", "-"))
                (out / attachment).write_text("{}")
                with self.assertRaises(CommandError):
                    call_command("evaluaterecognition", validate_report=str(out))

    def test_missing_natural_multispecies_stays_gap_not_data_validation_success_claim(self):
        self.manifest["images"][3]["scenarios"] = ["overseas"]
        self.results["manifest_digest"] = digest(self.manifest)
        report = json.loads((self.score() / "report.json").read_text())
        self.assertEqual(report["corpus"]["scenario_gaps"], ["natural_multispecies"])
        self.assertEqual(report["selection_state"], "pending")

    def test_unmapped_rank_one_does_not_promote_second_candidate_to_top1(self):
        self.results["images"][0]["ranked_candidates"][0]["species_id"] = None
        self.results["images"][0]["mapping_errors"] = ["A unresolved"]
        self.results["images"][0]["ranked_candidates"][1]["species_id"] = "A"
        report = json.loads((self.score() / "report.json").read_text())
        self.assertEqual(report["metrics"]["single"]["top1_hits"], 0)
        self.assertEqual(report["metrics"]["single"]["top3_hits"], 2)

    def test_synthetic_multispecies_and_false_complete_truth_cannot_enter_main_metrics(self):
        for updates in ({"synthetic": True}, {"complete_truth_verified": False}):
            original = copy.deepcopy(self.manifest)
            self.manifest["images"][3].update(updates)
            self.results["manifest_digest"] = digest(self.manifest)
            with self.assertRaises(CommandError):
                self.score()
            self.manifest = original

    def test_estimated_cost_keeps_sourced_ranges_separate_from_measurement(self):
        self.results["resources"]["electricity"] = {
            "status": "estimated", "currency": "CNY", "source": "control only",
            "range": [0.01, 0.02], "parameters": {"watts": {"range": [50, 100], "source": "test assumption"}}}
        out = self.score()
        cost = json.loads((out / "resources.json").read_text())["electricity"]
        self.assertEqual(cost["status"], "estimated")
        self.assertEqual(cost["parameters"]["watts"]["range"], [50, 100])

    def test_bad_numeric_timing_negative_resources_and_retries_fail(self):
        for mutate in (lambda r: r["batch_timings"].update(wall_seconds=-1),
                       lambda r: r["images"][0].update(retries=-1),
                       lambda r: r["images"][0]["timings"].update(inference_seconds=True)):
            original = copy.deepcopy(self.results)
            mutate(self.results)
            with self.assertRaises(CommandError):
                self.score()
            self.results = original

    def test_subprocess_missing_report_is_nonzero_and_reports_reason(self):
        from archive.tests.fixtures import command
        result = command("evaluaterecognition", "--validate-report", str(self.root / "missing"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("file_unavailable", result.stderr)

    def test_cpu_adapter_cannot_return_control_or_implicitly_write_fake_model_report(self):
        from archive.services.recognition_evaluation import run_cpu, EvaluationError
        fake = SimpleNamespace(run_cpu_evaluation=lambda **kwargs: self.results)
        with patch("archive.services.recognition_evaluation.importlib.import_module", return_value=fake):
            with self.assertRaisesRegex(EvaluationError, "cpu_adapter_returned_control"):
                run_cpu("model.json", self.manifest, self.protocol, self.data, self.root / "cpu")

    def test_duplicate_json_keys_and_nonfinite_values_fail(self):
        from archive.services.recognition_evaluation import load_json, EvaluationError
        for value in ('{"schema_version":1,"schema_version":2}', '{"score":NaN}'):
            p = self.root / "bad.json"
            p.write_text(value)
            with self.assertRaises(EvaluationError):
                load_json(p)

    def test_frozen_protocol_rejects_manifest_changes_even_with_updated_results_digest(self):
        self.protocol["manifest_digest"] = digest(self.manifest)
        self.manifest["images"][0]["source_revision"] = "another revision"
        self.results["manifest_digest"] = digest(self.manifest)
        self.results["protocol_digest"] = digest(self.protocol)
        with self.assertRaisesRegex(CommandError, "frozen_manifest_mismatch"):
            self.score()

    def test_smoke_keeps_frozen_digest_but_scores_one_image_and_never_selected(self):
        self.protocol["manifest_digest"] = digest(self.manifest)
        self.results["protocol_digest"] = digest(self.protocol)
        self.results["smoke"] = True
        self.results["images"] = self.results["images"][:1]
        out = self.score()
        report = json.loads((out / "report.json").read_text())
        self.assertEqual(report["metrics"]["n"], 1)
        self.assertEqual(report["manifest_digest"], digest(self.manifest))
        self.assertEqual(report["execution_scope"], "smoke")
        self.assertEqual(report["selection_state"], "pending")

    def test_observed_agent_is_distinct_from_simulation_and_human(self):
        from archive.services.recognition_evaluation import validate_traces, EvaluationError
        traces = self.traces()
        for trace in traces["traces"]:
            trace.update(observation_type="observed_agent", operator_type="agent")
        # An operator label alone no longer upgrades a simulated fixture: actual
        # observations additionally need the exported publication and process records.
        with self.assertRaisesRegex(EvaluationError, 'publication_required'):
            validate_traces(traces, self.protocol, self.manifest)
        traces["traces"][0]["operator_type"] = "human"
        with self.assertRaisesRegex(EvaluationError, "operator_type_mismatch"):
            validate_traces(traces, self.protocol, self.manifest)
