"""离线公开评测合同。控制、模型 CPU、目标 NAS 证据互不升级。

JSON 摘要是完整性检查而非身份签名。NAS 结论还须审阅实际运行、
资源采集与浏览器原始证据；此工具不会从设备名推断 NAS 或从 TopK 推断动作。
"""
import csv
import hashlib
import importlib
import json
import math
import os
import re
import stat
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from PIL import Image


class EvaluationError(ValueError):
    pass


COHORTS = ("single", "own_candidates", "manual")
ACTIONS = {"accept", "search", "add_species", "remove_species", "confirm", "exclude"}
EVIDENCE_TYPES = {"control", "model_cpu", "nas_cpu"}
HASH = re.compile(r"[a-f0-9]{64}\Z")


def require(condition, message):
    if not condition:
        raise EvaluationError(message)


def digest(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError):
        raise EvaluationError("invalid_json_value") from None
    return hashlib.sha256(raw).hexdigest()


def read_bytes(path, limit=32 * 1024 * 1024):
    """普通文件有界读取，拒绝任何路径组件中的符号链接。"""
    path = Path(path).absolute()
    require(not any(p.is_symlink() for p in (path, *path.parents)), "symlink_path")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_size <= limit, "invalid_regular_file")
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        require(len(raw) <= limit and (before.st_ino, before.st_size, before.st_mtime_ns) ==
                (after.st_ino, after.st_size, after.st_mtime_ns), "file_changed")
        return raw
    except OSError as exc:
        raise EvaluationError(f"file_unavailable:{path.name}:{exc.__class__.__name__}") from None


def load_json(path):
    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    try:
        return json.loads(read_bytes(path), object_pairs_hook=no_duplicates,
                          parse_constant=lambda _: (_ for _ in ()).throw(EvaluationError("nonfinite_json")))
    except (UnicodeError, ValueError) as exc:
        if isinstance(exc, EvaluationError):
            raise
        raise EvaluationError("invalid_json") from None


def text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 8000


def finite_nonnegative(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def string_set(value, allow_empty=False):
    require(isinstance(value, list) and (allow_empty or value) and all(text(x) for x in value)
            and len(set(value)) == len(value), "invalid_species_set")
    return set(value)


def validate_protocol(protocol):
    require(isinstance(protocol, dict) and protocol.get("schema_version") == 1, "invalid_protocol")
    require(text(protocol.get("version")) and protocol.get("evaluation_split") in ("evaluation", "calibration"), "invalid_protocol_split")
    require(isinstance(protocol.get("allowed_licenses"), list) and protocol["allowed_licenses"], "missing_license_policy")
    require(set(protocol.get("required_scenarios", [])) >= {"common", "confusable", "natural_multispecies"}, "missing_scenario_policy")
    require(isinstance(protocol.get("selection"), dict) and isinstance(protocol.get("threshold"), dict), "missing_selection_policy")
    require(protocol["threshold"].get("calibration_split") == "calibration", "invalid_calibration_split")
    digest(protocol)
    return protocol


def validate_manifest(manifest, protocol, data_root=None):
    validate_protocol(protocol)
    require(isinstance(manifest, dict) and manifest.get("schema_version") == 1 and text(manifest.get("dataset_id")), "invalid_manifest")
    require(manifest.get("evidence_type") in ("control", "public"), "invalid_corpus_evidence")
    images = manifest.get("images")
    require(isinstance(images, list) and 0 < len(images) <= 10000, "empty_or_large_manifest")
    if protocol.get("manifest_digest") is not None:
        require(protocol["manifest_digest"] == digest(manifest), "frozen_manifest_mismatch")
    seen, hashes, groups, coverage = set(), {}, {}, set()
    included = []
    for item in images:
        require(isinstance(item, dict), "invalid_image")
        image_id = item.get("photo_id")
        require(text(image_id) and image_id not in seen, "duplicate_image_id")
        seen.add(image_id)
        require(type(item.get("included")) is bool, "missing_inclusion")
        if not item["included"]:
            require(text(item.get("exclusion_reason")), "missing_exclusion_reason")
            continue
        for field in ("source_url", "source_revision", "author", "license", "license_url", "attribution", "acquired_at", "label_evidence", "near_duplicate_group"):
            require(text(item.get(field)), f"missing_{field}")
        for field in ("source_url", "license_url"):
            parsed = urlsplit(item[field])
            require(parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password, "invalid_source_url")
        require(item["license"] in protocol["allowed_licenses"], "unapproved_license")
        try:
            date.fromisoformat(item["acquired_at"])
        except ValueError:
            raise EvaluationError("invalid_acquired_date") from None
        require(HASH.fullmatch(item.get("sha256", "")) and type(item.get("size_bytes")) is int and 0 < item["size_bytes"] <= 64 * 1024 * 1024, "invalid_image_hash_or_size")
        truth = string_set(item.get("truth_set"))
        require(item.get("complete_truth_verified") is True, "incomplete_truth")
        taxonomy = item.get("taxonomy")
        require(isinstance(taxonomy, list) and len(taxonomy) == len(truth)
                and {t.get("id") for t in taxonomy if isinstance(t, dict)} == truth, "missing_taxonomy_mapping")
        require(all(all(text(t.get(f)) for f in ("snapshot", "external_id", "scientific_name", "evidence")) for t in taxonomy), "incomplete_taxonomy_mapping")
        review = item.get("pixel_review")
        require(isinstance(review, dict) and text(review.get("reviewer")) and text(review.get("method")), "missing_pixel_review")
        require(item.get("training_overlap") in ("unknown", "known_overlap", "checked_no_overlap"), "missing_training_overlap")
        if item["training_overlap"] != "unknown":
            require(text(item.get("training_overlap_evidence")), "missing_overlap_evidence")
        require(item.get("split") in ("evaluation", "calibration"), "invalid_split")
        for key, table in ((item["near_duplicate_group"], groups), (item["sha256"], hashes)):
            require(key not in table or table[key] == item["split"], "split_leakage")
            table[key] = item["split"]
        scenarios = string_set(item.get("scenarios"))
        if "natural_multispecies" in scenarios:
            require(len(truth) > 1 and item.get("synthetic", False) is False, "not_natural_multispecies")
            if manifest["evidence_type"] != "control":
                birds = review.get("visible_birds")
                require(isinstance(birds, list) and len(birds) >= 2
                        and all(isinstance(b, dict) and text(b.get("position")) and b.get("species_id") in truth and text(b.get("identification")) for b in birds)
                        and {b["species_id"] for b in birds} == truth, "missing_complete_bird_review")
        path = PurePosixPath(item.get("path", ""))
        require(not path.is_absolute() and bool(path.parts) and ".." not in path.parts and "\\" not in str(path), "invalid_image_path")
        if data_root is not None:
            root = Path(data_root).absolute()
            require(root.is_dir() and not any(p.is_symlink() for p in (root, *root.parents)), "invalid_data_root")
            file = root.joinpath(*path.parts)
            raw = read_bytes(file, 64 * 1024 * 1024)
            require(len(raw) == item["size_bytes"] and hashlib.sha256(raw).hexdigest() == item["sha256"], "jpeg_hash_mismatch")
            try:
                import io
                with Image.open(io.BytesIO(raw)) as image:
                    require(image.format == "JPEG" and image.width * image.height <= 80_000_000, "invalid_jpeg")
                    image.verify()
            except (OSError, ValueError, Image.DecompressionBombError):
                raise EvaluationError("invalid_jpeg") from None
        if item["split"] == protocol["evaluation_split"]:
            included.append(item)
            coverage.update(scenarios)
    require(included, "empty_evaluation_partition")
    included_total = sum(i["included"] for i in images)
    return {"included": len(included), "excluded": len(images) - len(included),
            "included_total": included_total, "excluded_by_rule": len(images) - included_total,
            "other_partition": included_total - len(included),
            "manifest_digest": digest(manifest), "protocol_digest": digest(protocol),
            "scenario_gaps": sorted(set(protocol["required_scenarios"]) - coverage)}


def validate_traces(bundle, protocol, manifest=None):
    validate_protocol(protocol)
    require(isinstance(bundle, dict) and bundle.get("schema_version") == 1 and isinstance(bundle.get("traces"), list), "invalid_traces")
    traces = bundle["traces"]
    require(all(isinstance(t, dict) for t in traces), "invalid_trace_row")
    require({t.get("cohort") for t in traces} == set(COHORTS) and len(traces) == 3, "missing_three_cohorts")
    ids, sessions, operators, all_ids = set(), set(), set(), []
    lookup = {i["photo_id"]: i for i in manifest["images"] if i.get("included") and i["split"] == protocol["evaluation_split"]} if manifest else None
    for trace in traces:
        require(text(trace.get("trace_id")) and trace["trace_id"] not in ids, "duplicate_trace")
        ids.add(trace["trace_id"])
        require(text(trace.get("session_id")) and trace["session_id"] not in sessions, "shared_workspace_session")
        sessions.add(trace["session_id"])
        require(text(trace.get("operator")), "missing_operator")
        operators.add(trace["operator"])
        require(trace.get("partition") == protocol["evaluation_split"] and trace.get("protocol_digest") == digest(protocol), "trace_partition_or_protocol_mismatch")
        require(trace.get("observation_type") in ("simulated_browser", "observed_agent", "observed_human") and text(trace.get("browser_evidence_ref")), "missing_browser_evidence")
        if trace["observation_type"] != "simulated_browser":
            require(trace.get("operator_type") == ("human" if trace["observation_type"] == "observed_human" else "agent"), "operator_type_mismatch")
        require(HASH.fullmatch(trace.get("manifest_digest", "")), "missing_trace_manifest_digest")
        if protocol.get('manifest_digest') is not None:
            require(trace['manifest_digest'] == protocol['manifest_digest'], 'trace_frozen_manifest_mismatch')
        if manifest:
            require(trace["manifest_digest"] == digest(manifest), "trace_manifest_mismatch")
        images = trace.get("images")
        require(isinstance(images, list) and images, "empty_trace_images")
        if trace['observation_type'] != 'simulated_browser' or 'publication' in trace:
            validate_publication_trace(trace, protocol, manifest)
        image_ids = []
        for item in images:
            require(isinstance(item, dict), "invalid_trace_image")
            require(text(item.get("photo_id")) and HASH.fullmatch(item.get("sha256", "")), "invalid_trace_image")
            string_set(item.get("final_set"), allow_empty=True)
            image_ids.append(item["photo_id"])
            if lookup:
                require(item["photo_id"] in lookup and item["sha256"] == lookup[item["photo_id"]]["sha256"], "trace_image_mismatch")
        require(len(image_ids) == len(set(image_ids)), "duplicate_trace_image")
        if lookup:
            require(set(image_ids) == set(lookup), "trace_missing_images")
        all_ids.append(set(image_ids))
        actions = trace.get("actions")
        require(isinstance(actions, list) and actions, "missing_observed_actions")
        previous = -1
        terminal = set()
        for action in actions:
            require(isinstance(action, dict), "invalid_action")
            require(action.get("action") in ACTIONS, "invalid_action")
            photos = string_set(action.get("photo_ids"))
            require(photos <= set(image_ids), "action_image_mismatch")
            moment = action.get("at_seconds")
            require(finite_nonnegative(moment) and moment >= previous, "invalid_action_time")
            previous = moment
            if action["action"] in ("confirm", "exclude"):
                terminal.update(photos)
        require(terminal == set(image_ids), "missing_final_actions")
    require(len(operators) == 1 and all(x == all_ids[0] for x in all_ids)
            and len({t["manifest_digest"] for t in traces}) == 1, "unmatched_operator_or_cohort")
    bound = [t['publication'] for t in traces if 'publication' in t]
    if bound:
        require(len(bound) == 3 and len({p['workspace_digest'] for p in bound}) == 3,
                'three_independent_publications_required')
        assisted = [p for p in bound if p['cohort'] != 'manual']
        require(len({tuple(p[k] for k in PUBLICATION_DIGESTS) for p in assisted}) == 1,
                'assisted_candidate_mismatch')
        require(len({row['photo_id'] for p in bound for row in p['images']}) == sum(len(p['images']) for p in bound),
                'shared_workspace_photo')
    return bundle


PUBLICATION_DIGESTS = ('model_manifest_digest','labels_digest','prompt_digest','threshold_digest','runner_digest',
                       'manifest_digest','protocol_digest')
RUN_FIELDS = ('run_id','generation','artifact_id')


def _uuid(value):
    import uuid
    try:
        return isinstance(value,str) and str(uuid.UUID(value)) == value
    except (ValueError,TypeError,AttributeError):
        return False


def validate_publication_trace(trace, protocol, manifest=None):
    """Validate an exported before-review snapshot, not caller-supplied hit counts.

    Digests detect inconsistent records; authenticity still requires reviewing the
    raw browser/process evidence. Checking today's machine cannot attest a past run.
    """
    publication = trace.get('publication')
    require(isinstance(publication,dict) and publication.get('schema_version') == 1, 'publication_required')
    sealed = {k:v for k,v in publication.items() if k != 'publication_digest'}
    require(publication.get('publication_digest') == digest(sealed)
        and trace.get('publication_digest') == publication['publication_digest'], 'publication_digest_mismatch')
    require(_uuid(publication.get('session_id')) and publication['session_id'] == trace['session_id']
        and publication.get('cohort') == trace['cohort'] and publication.get('current_unchanged') is True,
        'publication_session_mismatch')
    require(all(HASH.fullmatch(publication.get(k,'')) for k in (*PUBLICATION_DIGESTS,'workspace_digest','frozen_digest')),
            'publication_identity_missing')
    require(publication['manifest_digest'] == trace['manifest_digest']
        and publication['protocol_digest'] == trace['protocol_digest'] == digest(protocol), 'publication_protocol_mismatch')
    if manifest:
        require(publication.get('corpus_evidence_type') == manifest['evidence_type'], 'publication_corpus_mismatch')
    evidence = publication.get('evidence_type')
    require(evidence in ('control','model_cpu') and
        (trace['cohort'] == 'manual' or (evidence == 'control') == (publication.get('runner') == 'control'))
        and (evidence == 'control') == (publication.get('corpus_evidence_type') == 'control'), 'publication_runner_mismatch')
    if trace['cohort'] == 'manual':
        require(publication.get('runner') == 'manual', 'manual_has_candidates')
    elif evidence == 'model_cpu':
        require(publication.get('runner') == 'local_model_cpu', 'not_real_cpu_runner')
    rows = publication.get('images')
    require(isinstance(rows,list) and rows and all(isinstance(r,dict) for r in rows), 'publication_images_missing')
    lookup = {r.get('image_id'):r for r in rows}
    require(len(lookup) == len(rows) and set(lookup) == {i['photo_id'] for i in trace['images']}, 'publication_images_mismatch')
    for image in trace['images']:
        row = lookup[image['photo_id']]
        require(row.get('sha256') == image['sha256'] and _uuid(row.get('photo_id'))
            and _uuid(row.get('source_observation_id')) and row.get('detail_url') == '/photos/'+row['photo_id']+'/',
            'publication_image_mismatch')
        require(image.get('workspace_photo_id') == row['photo_id']
            and all(k in image and k in row for k in RUN_FIELDS)
            and image.get('source_observation_id') == row['source_observation_id']
            and all(image.get(k) == row.get(k) for k in RUN_FIELDS), 'trace_run_identity_mismatch')
        if trace['cohort'] == 'manual':
            require(all(row.get(k) is None for k in RUN_FIELDS), 'manual_has_candidates')
        else:
            require(_uuid(row.get('run_id')) and _uuid(row.get('artifact_id'))
                and row['artifact_id'] == publication.get('artifact_id')
                and type(row.get('generation')) is int and row['generation'] > 0, 'published_run_required')
    execution = trace.get('execution_evidence')
    if execution is not None:
        _validate_trace_execution(execution,publication,protocol)
    if trace['observation_type'] != 'simulated_browser':
        require(evidence == 'model_cpu', 'real_model_publication_required')
        if trace['cohort'] != 'manual':
            require(execution is not None and execution.get('evidence_type') == 'nas_cpu',
                    'nas_same_run_execution_evidence_required')
    return publication


def _validate_trace_execution(execution, publication, protocol):
    require(isinstance(execution,dict) and execution.get('schema_version') == 1
        and execution.get('evidence_type') in ('model_cpu','nas_cpu')
        and publication['evidence_type'] == 'model_cpu', 'execution_evidence_mismatch')
    raw, attestation = execution.get('raw_record'), execution.get('attestation_record')
    require(isinstance(raw,dict) and execution.get('raw_record_digest') == digest(raw)
        and raw.get('schema_version') == 1 and raw.get('publication_digest') == publication['publication_digest']
        and raw.get('session_id') == publication['session_id'] and raw.get('cohort') == publication['cohort']
        and all(raw.get(k) == publication[k] for k in PUBLICATION_DIGESTS), 'execution_publication_mismatch')
    require(raw.get('runner') == 'local_model_cpu' and raw.get('offline') is True, 'not_real_cpu_runner')
    expected = {r['image_id']:r for r in publication['images']}
    rows = raw.get('images')
    require(isinstance(rows,list) and len(rows) == len(expected) and all(isinstance(r,dict) for r in rows)
        and {r.get('image_id') for r in rows} == set(expected), 'execution_images_mismatch')
    budget = protocol.get('execution',{}).get('memory_mb')
    require(type(budget) is int and budget > 0, 'execution_budget_required')
    for row in rows:
        image = expected[row['image_id']]
        require(all(row.get(k) == image[k] for k in ('photo_id','sha256',*RUN_FIELDS))
            and row.get('input_hash_before') == row.get('input_hash_after') == image['sha256'], 'execution_run_mismatch')
        pids = [row.get(k) for k in ('model_pid','worker_pid','supervisor_pid')]
        require(all(type(pid) is int and pid > 0 for pid in pids) and len(set(pids)) == 3,
                'execution_process_identity_missing')
        resources = row.get('resources',{})
        require(isinstance(resources,dict) and resources.get('memory_control') == 'enforced'
            and resources.get('soft_bytes') == resources.get('hard_bytes') == budget*1024*1024,
            'execution_budget_not_enforced')
        require(HASH.fullmatch(row.get('raw_evidence_sha256','')) and text(row.get('raw_evidence_ref'))
            and not PurePosixPath(row['raw_evidence_ref']).is_absolute()
            and '..' not in PurePosixPath(row['raw_evidence_ref']).parts, 'execution_raw_evidence_missing')
        attached = row.get('raw_evidence')
        require(isinstance(attached,dict) and digest(attached) == row['raw_evidence_sha256']
            and all(attached.get(k) == row[k] for k in ('image_id','photo_id','sha256',*RUN_FIELDS,
                'input_hash_before','input_hash_after','model_pid','worker_pid','supervisor_pid','resources'))
            and attached.get('network_control') == 'os_enforced'
            and attached.get('outbound_attempts') == 0, 'execution_raw_attachment_mismatch')
    if execution['evidence_type'] == 'nas_cpu':
        host = raw.get('observed_host',{})
        declaration = protocol.get('execution',{}).get('nas_attestation',{})
        attestation_json = execution.get('attestation_json')
        require(isinstance(attestation_json,str) and hashlib.sha256(attestation_json.encode()).hexdigest() ==
            declaration.get('sha256') and json.loads(attestation_json) == attestation,
            'nas_attestation_bytes_mismatch')
        require(isinstance(attestation,dict) and execution.get('attestation_digest') == digest(attestation)
            and execution.get('attestation_sha256') == declaration.get('sha256')
            and HASH.fullmatch(execution.get('attestation_sha256',''))
            and attestation.get('schema_version') == 1 and attestation.get('approved_target') is True
            and text(attestation.get('target_id')) and text(attestation.get('approval_evidence_ref'))
            and attestation.get('host') == host and host.get('machine') == 'x86_64'
            and text(host.get('kernel_release')) and HASH.fullmatch(host.get('cpu_identity_sha256',''))
            and type(host.get('memory_total_bytes')) is int and host['memory_total_bytes'] > 0
            and host.get('network_interfaces') == ['lo'], 'nas_same_run_attestation_required')
    return execution


def validate_resources(resources, evidence_type):
    require(isinstance(resources, dict) and resources.get("evidence_type") == evidence_type, "resource_evidence_mismatch")
    cost = resources.get("electricity")
    require(isinstance(cost, dict) and cost.get("status") in ("unknown", "estimated", "measured"), "missing_cost_status")
    if cost["status"] != "unknown":
        require(text(cost.get("currency")) and text(cost.get("source")), "missing_cost_source")
        if cost["status"] == "estimated":
            require(isinstance(cost.get("parameters"), dict) and cost["parameters"], "missing_estimate_parameters")
            for value in cost["parameters"].values():
                require(isinstance(value, dict) and text(value.get("source")) and isinstance(value.get("range"), list)
                        and len(value["range"]) == 2 and all(finite_nonnegative(v) for v in value["range"])
                        and value["range"][0] <= value["range"][1], "invalid_estimate_range")
            require(isinstance(cost.get("range"), list) and len(cost["range"]) == 2
                    and all(finite_nonnegative(v) for v in cost["range"]) and cost["range"][0] <= cost["range"][1], "invalid_cost_range")
        else:
            require(finite_nonnegative(cost.get("value")) and text(cost.get("meter_evidence_ref")), "missing_meter_evidence")
    if evidence_type != "control":
        for field in ("cpu_seconds", "storage_bytes", "peak_rss_bytes", "peak_address_space_bytes", "process_tree_peak_rss_bytes"):
            require(finite_nonnegative(resources.get(field)), f"missing_resource:{field}")
        require(text(resources.get("collector")) and text(resources.get("raw_evidence_ref")), "missing_resource_collector")


def validate_results(results, manifest, protocol):
    require(isinstance(results, dict) and results.get("schema_version") == 1, "invalid_results")
    evidence = results.get("evidence_type")
    require(evidence in EVIDENCE_TYPES, "invalid_evidence_type")
    require(results.get("manifest_digest") == digest(manifest) and results.get("protocol_digest") == digest(protocol), "result_digest_mismatch")
    environment = results.get("environment")
    require(isinstance(environment, dict) and environment.get("evidence_type") == evidence, "environment_evidence_mismatch")
    require((evidence == "control") == (results.get("runner") == "control"), "runner_evidence_mismatch")
    if evidence != "control":
        require(manifest["evidence_type"] == "public" and results.get("runner") == "local_model_cpu", "not_real_cpu_runner")
        for field in ("model_manifest_digest", "labels_digest", "prompt_digest", "execution_id"):
            require(text(environment.get(field)), f"missing_environment:{field}")
        require(environment.get("offline") is True and environment.get("device") == "cpu"
                and text(environment.get("raw_run_evidence_ref")), "missing_offline_cpu_evidence")
        if evidence == "nas_cpu":
            require(environment.get("target_kind") == "nas" and text(environment.get("target_id"))
                    and text(environment.get("nas_attestation_ref")), "missing_nas_evidence")
    validate_resources(results.get("resources"), evidence)
    timings = results.get("batch_timings")
    require(isinstance(timings, dict) and all(finite_nonnegative(timings.get(k)) for k in
            ("cold_load_seconds", "vocabulary_seconds", "wall_seconds")), "invalid_batch_timings")
    expected = {x["photo_id"]: x for x in manifest["images"] if x.get("included") and x["split"] == protocol["evaluation_split"]}
    if results.get("smoke") is True:
        expected = dict(list(expected.items())[:1])
    rows = results.get("images")
    require(isinstance(rows, list) and len(rows) == len(expected) and rows, "missing_result_rows")
    require(all(isinstance(r, dict) for r in rows), "invalid_result_row")
    require({r.get("photo_id") for r in rows} == set(expected), "duplicate_or_unknown_results")
    for row in rows:
        image = expected[row["photo_id"]]
        require(row.get("input_hash_before") == image["sha256"] == row.get("input_hash_after"), "input_hash_changed")
        require(row.get("failure") is None or text(row.get("failure")), "invalid_failure")
        require(type(row.get("retries")) is int and row["retries"] >= 0, "invalid_retry")
        require(isinstance(row.get("mapping_errors"), list) and all(text(x) for x in row["mapping_errors"]), "invalid_mapping_errors")
        ranked = row.get("ranked_candidates")
        require(isinstance(ranked, list) and len(ranked) <= 3, "invalid_candidates")
        mapped, last_score = set(), float("inf")
        for candidate in ranked:
            require(isinstance(candidate, dict) and text(candidate.get("raw_label")), "missing_raw_label")
            score = candidate.get("score")
            require(type(score) in (float, int) and math.isfinite(score) and score <= last_score, "invalid_rank_score")
            last_score = score
            species = candidate.get("species_id")
            require(species is None or text(species), "invalid_mapped_id")
            require(species is None or species not in mapped, "duplicate_candidate")
            if species:
                mapped.add(species)
            else:
                require(row["mapping_errors"], "unreported_mapping_error")
        require(row.get("failure") or ranked, "empty_successful_prediction")
        require(isinstance(row.get("timings"), dict) and all(finite_nonnegative(row["timings"].get(k)) for k in
                ("preprocess_seconds", "inference_seconds", "mapping_seconds", "queue_seconds")), "invalid_image_timings")
        require(row.get("threshold_outcome") in ("skipped", "proposed", "misselected", "corrected"), "missing_threshold_outcome")
        if protocol["threshold"].get("state") != "calibrated":
            require(row["threshold_outcome"] == "skipped", "uncalibrated_threshold_used")
    return results


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def build_report(manifest, protocol, results, traces=None):
    corpus = validate_manifest(manifest, protocol)
    validate_results(results, manifest, protocol)
    if traces:
        validate_traces(traces, protocol, manifest)
        for trace in traces['traces']:
            if 'publication' in trace and trace['cohort'] != 'manual' and results['evidence_type'] != 'control':
                publication = trace['publication']
                require(all(publication[k] == results['environment'].get(field) for k,field in (
                    ('model_manifest_digest','model_manifest_digest'),('labels_digest','labels_digest'),
                    ('prompt_digest','prompt_digest'))), 'trace_result_model_mismatch')
    lookup = {r["photo_id"]: r for r in results["images"]}
    records, species_counts, strata = [], {}, {}
    single = {"n": 0, "top1_hits": 0, "top3_hits": 0}
    multi = {"n": 0, "candidate_complete": 0, "candidate_partial": 0, "extra_candidates": 0,
             "top1_set_complete": 0, "top1_set_partial": 0, "missing_species_additions": 0,
             "manual_final_exact_by_cohort": {}, "observed_add_species_by_cohort": {}}
    for image in manifest["images"]:
        if not image.get("included") or image["split"] != protocol["evaluation_split"]:
            continue
        if results.get("smoke") is True and image["photo_id"] not in lookup:
            continue
        row = lookup[image["photo_id"]]
        truth = set(image["truth_set"])
        # Unmapped ranks retain their position; do not promote rank 2 to Top1.
        ranked = [] if row["failure"] else [c.get("species_id") for c in row["ranked_candidates"]]
        c3 = set(ranked) - {None}
        top1 = set(ranked[:1]) - {None}
        hit1, hit3 = int(bool(truth & top1)), int(bool(truth & c3))
        record = {**row, "truth_set": image["truth_set"], "complete_truth_verified": True,
                  "top1_set": sorted(top1), "top1_hit": hit1, "top3_hit": hit3,
                  "candidate_complete": int(truth <= c3),
                  "candidate_partial": int(0 < len(truth & c3) < len(truth)),
                  "extra_candidates": len(c3 - truth), "missing_species_additions": len(truth - top1),
                  "scenarios": image["scenarios"]}
        records.append(record)
        if len(truth) == 1:
            single["n"] += 1
            single["top1_hits"] += hit1
            single["top3_hits"] += hit3
            count = species_counts.setdefault(next(iter(truth)), {"n": 0, "top1_hits": 0, "top3_hits": 0})
            count["n"] += 1
            count["top1_hits"] += hit1
            count["top3_hits"] += hit3
        else:
            multi["n"] += 1
            for field in ("candidate_complete", "candidate_partial", "extra_candidates", "missing_species_additions"):
                multi[field] += record[field]
            multi["top1_set_complete"] += int(truth <= top1)
            multi["top1_set_partial"] += int(0 < len(truth & top1) < len(truth))
        for scenario in image["scenarios"]:
            count = strata.setdefault(scenario, {"n": 0, "single_n": 0, "top1_hits": 0, "top3_hits": 0,
                                                  "multispecies_n": 0, "candidate_complete": 0, "candidate_partial": 0})
            count["n"] += 1
            if len(truth) == 1:
                count["single_n"] += 1
                count["top1_hits"] += hit1
                count["top3_hits"] += hit3
            else:
                count["multispecies_n"] += 1
                count["candidate_complete"] += record["candidate_complete"]
                count["candidate_partial"] += record["candidate_partial"]
    for k in (1, 3):
        single[f"top{k}_micro"] = _ratio(single[f"top{k}_hits"], single["n"])
        single[f"top{k}_macro"] = _ratio(sum(c[f"top{k}_hits"] / c["n"] for c in species_counts.values()), len(species_counts))
    single["per_species"] = species_counts
    operations = {"status": "unknown", "cohorts": {}}
    if traces:
        operations["status"] = "recorded"
        for trace in traces["traces"]:
            events = trace["actions"]
            operations["cohorts"][trace["cohort"]] = {
                "n": len(trace["images"]), "events": len(events),
                "photo_action_links": sum(len(e["photo_ids"]) for e in events),
                "events_per_photo": len(events) / len(trace["images"]),
                "action_counts": {a: sum(e["action"] == a for e in events) for a in sorted(ACTIONS)},
                "per_photo": {i["photo_id"]: sum(i["photo_id"] in e["photo_ids"] for e in events) for i in trace["images"]},
                "observation_types": [trace["observation_type"]], "operator": trace["operator"],
                "browser_evidence_ref": trace["browser_evidence_ref"]}
            finals = {i["photo_id"]: set(i["final_set"]) for i in trace["images"]}
            multis = [r for r in records if len(r["truth_set"]) > 1]
            multi_ids = {r["photo_id"] for r in multis}
            multi["observed_add_species_by_cohort"][trace["cohort"]] = sum(
                len(set(event["photo_ids"]) & multi_ids) for event in events if event["action"] == "add_species")
            multi["manual_final_exact_by_cohort"][trace["cohort"]] = {
                "n": len(multis), "exact": sum(finals[r["photo_id"]] == set(r["truth_set"]) for r in multis)}
    threshold_counts = {name: sum(r["threshold_outcome"] == name for r in records) for name in
                        ("skipped", "proposed", "misselected", "corrected")}
    return {"schema_version": 1, "evidence_type": results["evidence_type"], "selection_state": "pending",
            "execution_scope": "smoke" if results.get("smoke") is True else protocol["evaluation_split"],
            "manifest_digest": digest(manifest), "protocol_digest": digest(protocol),
            "results_digest": digest(results), "traces_digest": digest(traces) if traces else None,
            "corpus": corpus, "metrics": {"single": single, "multispecies": multi, "strata": strata,
                "n": len(records), "failures": sum(bool(r["failure"]) for r in records),
                "mapping_failure_images": sum(bool(r["mapping_errors"]) for r in records),
                "retries": sum(r["retries"] for r in records), "threshold_outcomes": threshold_counts},
            "operations": operations, "batch_timings": results["batch_timings"], "per_image": records,
            "limitations": ["公开样本不代表私人照片准确率", "Top3 是候选覆盖，不是多鸟检测集合", "选型须另核原始 NAS 与人工轨迹证据"]}


CSV_FIELDS = ("photo_id", "truth_set", "complete_truth_verified", "ranked_candidates", "top1_set", "top1_hit", "top3_hit",
              "candidate_complete", "candidate_partial", "extra_candidates", "missing_species_additions", "mapping_errors",
              "failure", "retries", "timings", "input_hash_before", "input_hash_after", "operation_trace_ref", "threshold_outcome", "scenarios")


def write_report(output, manifest, protocol, results, traces=None):
    report = build_report(manifest, protocol, results, traces)
    output = Path(output).absolute()
    require(not output.exists() and not any(p.is_symlink() for p in output.parents), "output_exists_or_symlink")
    output.mkdir(mode=0o700, parents=False)
    files = {"manifest.json": manifest, "protocol.json": protocol, "results.json": results,
             "operation-traces.json": traces or {"schema_version": 1, "traces": []},
             "environment.json": results["environment"], "resources.json": results["resources"], "report.json": report}
    for name, value in files.items():
        with (output / name).open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
    with (output / "per-image.csv").open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in report["per_image"]:
            writer.writerow({k: json.dumps(row[k], ensure_ascii=False, sort_keys=True) if isinstance(row[k], (dict, list)) else row[k] for k in CSV_FIELDS})
    return report


def validate_report(directory, require_nas=False, selection=None):
    directory = Path(directory)
    report = load_json(directory / "report.json")
    manifest, protocol, results, traces = (load_json(directory / name) for name in
                                         ("manifest.json", "protocol.json", "results.json", "operation-traces.json"))
    computed = build_report(manifest, protocol, results, traces if traces.get("traces") else None)
    require(report == computed, "report_recomputation_mismatch")
    require(load_json(directory / "environment.json") == results["environment"]
            and load_json(directory / "resources.json") == results["resources"], "report_attachment_mismatch")
    import io
    rows = list(csv.DictReader(io.StringIO(read_bytes(directory / "per-image.csv").decode())))
    require(len(rows) == len(report["per_image"]), "csv_row_mismatch")
    for actual, row in zip(rows, report["per_image"]):
        for key in CSV_FIELDS:
            expected = json.dumps(row[key], ensure_ascii=False, sort_keys=True) if isinstance(row[key], (dict, list)) else str(row[key]) if row[key] is not None else ""
            require(actual.get(key) == expected, "csv_value_mismatch")
    if require_nas or selection is not None:
        require(results["evidence_type"] == "nas_cpu", "nas_evidence_required")
        require(not corpus_gaps(report) and manifest["evidence_type"] == "public", "incomplete_corpus")
        require(all(t["observation_type"] in ("observed_human", "observed_agent") for t in traces.get("traces", []))
                and len(traces.get("traces", [])) == 3, "real_three_cohort_traces_required")
        require(all('publication' in t and (t['cohort'] == 'manual' or
            t.get('execution_evidence',{}).get('evidence_type') == 'nas_cpu') for t in traces['traces']),
            'nas_same_run_trace_evidence_required')
        require(not results.get("smoke", False), "smoke_is_not_complete_evaluation")
        policy = protocol["selection"]
        require(policy.get("state") == "frozen" and isinstance(policy.get("nas_budget"), dict)
                and finite_nonnegative(policy.get("maximum_occupancy_seconds"))
                and text(policy.get("correction_rule")) and text(policy.get("frozen_before_execution")), "unfrozen_selection_policy")
        budget = policy["nas_budget"]
        for name in ("process_tree_peak_rss_bytes", "peak_address_space_bytes", "cpu_seconds"):
            require(finite_nonnegative(budget.get(name)) and results["resources"][name] <= budget[name], "resource_budget_exceeded")
        require(results["batch_timings"]["wall_seconds"] <= policy["maximum_occupancy_seconds"], "occupancy_exceeded")
    if selection is not None:
        require(isinstance(selection, dict) and selection.get("state") == "selected", "invalid_selection_state")
        for key in ("manifest_digest", "protocol_digest", "results_digest", "traces_digest"):
            require(selection.get(key) == report[key], "selection_digest_mismatch")
        require(selection.get("report_digest") == digest(report) and selection.get("environment_digest") == digest(results["environment"])
                and selection.get("resources_digest") == digest(results["resources"]), "selection_attachment_digest_mismatch")
        require(selection.get("checks") == {"license": True, "offline": True, "resource": True, "manual_safety": True,
                "correction_burden": True} and text(selection.get("decision_evidence_ref")), "selection_checks_incomplete")
    return report


def corpus_gaps(report):
    return report["corpus"]["scenario_gaps"]


def run_cpu(model_manifest, manifest, protocol, data_root, output, smoke=False, operation_traces=None):
    """仅同一受监督 CPU 角色可产出真实结果，缺包或受限执行失败明确非零。

    run_cpu_evaluation 接收本地路径与 smoke 标记，返回完整 results envelope。
    03-08 已提供实际 adapter；普通运行按 evaluation_split，smoke只运行冻结分区第一张。
    不在本模块 import torch，不下载或训练，验证通过后才写报告。
    """
    validate_manifest(manifest, protocol, data_root)
    try:
        adapter = importlib.import_module("archive.services.local_model")
        runner = getattr(adapter, "run_cpu_evaluation")
    except (ImportError, AttributeError):
        raise EvaluationError("cpu_adapter_unavailable:尚未安装或实现本地 CPU 评测适配器") from None
    results = runner(model_manifest=model_manifest, manifest=manifest, protocol=protocol,
                     data_root=str(data_root), smoke=smoke)
    require(isinstance(results, dict) and results.get("evidence_type") in ("model_cpu", "nas_cpu"), "cpu_adapter_returned_control")
    results["smoke"] = bool(smoke)
    return write_report(output, manifest, protocol, results, operation_traces)
