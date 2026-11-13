"""Standard library input boundary and lazy offline OpenCLIP CPU adapter.

No Django, original path, database or file-moving code is available in this role.
The caller owns the FD; this module only duplicates a verified read-only stream.
"""
import fcntl
import hashlib
import os
import stat
import json
from pathlib import Path
import tempfile
import time
import uuid

from archive.services import model_packages as packages

_ADAPTER = None
_THREADS_CONFIGURED = False


def _deny_network():
    """Model-role guard; deployment still needs the 03-10 OS egress test."""
    import socket
    def denied(*args, **kwargs):
        raise OSError('model_network_forbidden')
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.socket.sendto = denied
    socket.getaddrinfo = denied
    socket.create_connection = denied


class OfflineCPUAdapter:
    """Created only inside the independently limited model role (or prepare role).

    Constructor injection is used by unit controls only; no CLI/setting/manifest
    selects a test backend. Heavy libraries are never imported by Web/fileworker.
    """
    def __init__(self, checked, *, _modules=None, prepare=False):
        global _THREADS_CONFIGURED
        real_modules = _modules is None
        os.environ.update(HF_HUB_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
                          TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false')
        if _modules is None:
            _deny_network()
        if _modules is None:
            import importlib.metadata
            import re
            # Check the complete authorized lock before executing optional library code.
            local_lock = packages.read_local(checked['paths']['dependency_lock'])
            canonical_lock = packages.read_local(Path(__file__).resolve().parents[2]/'requirements-recognition.lock')
            packages.require(local_lock == canonical_lock,'package_dependency_lock_mismatch')
            pins = re.findall(r'^# ([a-z0-9-]+)==([0-9.]+(?:\+cpu)?);',local_lock.decode(),re.MULTILINE)
            packages.require(len(pins) == 31 and all(importlib.metadata.version(name)==version for name,version in pins),
                             'package_runtime_version_mismatch')
            import torch
            import open_clip
            from open_clip.tokenizer import SimpleTokenizer
            from safetensors.torch import load_file, save_file
            _modules = (torch, open_clip, SimpleTokenizer, load_file, save_file)
        self.torch, clip, tokenizer, load, self.save = _modules
        self.checked = checked
        paths = checked['paths']
        # Thread settings precede model creation/encoding; eager CPU only.
        if real_modules:
            packages.require(self.torch.version.cuda is None, 'package_runtime_version_mismatch')
        if not real_modules or not _THREADS_CONFIGURED:
            self.torch.set_num_threads(checked['runner']['intra_threads'])
            self.torch.set_num_interop_threads(checked['runner']['inter_threads'])
            if real_modules:
                _THREADS_CONFIGURED = True
        self.model = clip.create_model('local-dir:' + str(paths['config'].parent),
            device='cpu', precision='fp32', jit=False, pretrained_image=False,
            pretrained_text=False, require_pretrained=True, weights_only=True)
        self.model.eval()
        # Use the exact frozen inference transform, never training augmentation.
        from_config = packages.load_json(paths['config'])['preprocess_cfg']
        self.preprocess = clip.image_transform(224, is_train=False, **from_config)
        self.tokenizer = tokenizer(bpe_path=str(paths['tokenizer']), context_length=77)
        self.features = None
        self.timings = {}
        if not prepare:
            tensors = load(str(paths['text_features']), device='cpu')
            self.features = tensors['text_features']
            expected = (len(checked['labels']),768)
            packages.require(tuple(self.features.shape) == expected and self.features.dtype == self.torch.float32
                             and bool(self.torch.isfinite(self.features).all()), 'package_embedding_invalid')
            norms = self.features.norm(dim=-1)
            packages.require(bool(self.torch.allclose(norms, self.torch.ones_like(norms), atol=1e-4)), 'package_embedding_invalid')

    def prepare_features(self):
        rows = self.checked['labels']
        chunks = []
        with self.torch.inference_mode():
            for start in range(0, len(rows), self.checked['runner']['embedding_chunk_size']):
                tokens = self.tokenizer([packages.PROMPT.format(scientific_name=r['scientific_name'])
                                         for r in rows[start:start+32]])
                encoded = self.model.encode_text(tokens.to('cpu'), normalize=True).float().cpu()
                packages.require(bool(self.torch.isfinite(encoded).all()), 'package_embedding_invalid')
                chunks.append(encoded)
        return self.torch.cat(chunks, dim=0).contiguous()

    def infer(self, stream):
        from PIL import Image, ImageOps
        started = time.monotonic()
        # Decode a bounded, verified anonymous JPEG; EXIF changes only this copy.
        with Image.open(stream) as image:
            packages.require(image.format == 'JPEG' and image.width*image.height <= 50_000_000, 'input_invalid')
            image.load()
            oriented = ImageOps.exif_transpose(image)
            try:
                rgb = oriented.convert('RGB')
                try:
                    pixels = self.preprocess(rgb).unsqueeze(0).to(device='cpu', dtype=self.torch.float32)
                finally:
                    rgb.close()
            finally:
                oriented.close()
        prepared = time.monotonic()
        with self.torch.inference_mode():
            image_features = self.model.encode_image(pixels, normalize=True)
            scores = (image_features @ self.features.T).squeeze(0)
            packages.require(bool(self.torch.isfinite(scores).all()), 'model_result_invalid')
            # Stable ties preserve the frozen vocabulary ordering; no softmax probability.
            indices = self.torch.argsort(scores, descending=True, stable=True)[:3].tolist()
        inferred = time.monotonic()
        result = []
        for rank, index in enumerate(indices, 1):
            row = self.checked['labels'][index]
            mapped = row.get('species_id') is not None
            result.append({'rank':rank, 'raw_label':row['scientific_name'], 'species_id':row.get('species_id'),
                'score':float(scores[index].item()), 'mapping_status':'mapped' if mapped else 'unmapped',
                'mapping_evidence':row.get('mapping_evidence','')})
        self.timings = {'preprocess_seconds':prepared-started, 'inference_seconds':inferred-prepared,
                        'mapping_seconds':time.monotonic()-inferred, 'queue_seconds':0}
        return result


def _get_adapter(manifest):
    global _ADAPTER
    identity = packages.digest(manifest)
    # Verify bytes on every request, also on cache hits; model dirs must be read-only
    # at OS deployment boundary. Caching alone cannot detect changed local files.
    checked = packages.verify_manifest(manifest)
    if _ADAPTER is None or _ADAPTER.checked['manifest_digest'] != identity:
        if _ADAPTER is not None:
            # Only one model lives here; never hold old/new weights simultaneously.
            import gc
            _ADAPTER = None
            gc.collect()
        _ADAPTER = OfflineCPUAdapter(checked)
    return _ADAPTER


def infer_readonly(input_fd, *, input_sha256, input_size, artifact, test_provider=None):
    try:
        info = os.fstat(input_fd)
        if (fcntl.fcntl(input_fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY
                or not stat.S_ISREG(info.st_mode) or info.st_size != input_size
                or type(input_size) is not int or not 0 < input_size <= 100*1024*1024):
            return {'error_code': 'input_invalid'}
        with os.fdopen(os.dup(input_fd), 'rb') as stream:
            stream.seek(0)
            digest = hashlib.sha256()
            head = stream.read(2)
            digest.update(head)
            while block := stream.read(1024*1024):
                digest.update(block)
            if head != b'\xff\xd8' or digest.hexdigest() != input_sha256:
                return {'error_code': 'input_invalid'}
        # Constructor-only injection from tests, never a setting or management flag.
        if test_provider is not None:
            time.sleep(test_provider.get('delay', 0))
            return {'candidates': test_provider['candidates']}
        if isinstance(artifact,dict) and artifact.get('provider') != packages.PROVIDER:
            # Unavailable providers (including synthetic names) are never selectable.
            return {'error_code':'model_package_missing'}
        if not isinstance(artifact,dict) or not isinstance(artifact.get('manifest'),dict):
            return {'error_code': 'model_package_invalid'}
        try:
            loading = time.monotonic()
            adapter = _get_adapter(artifact['manifest'])
            cold_load = time.monotonic()-loading
            with os.fdopen(os.dup(input_fd), 'rb') as stream:
                stream.seek(0)
                candidates = adapter.infer(stream)
                return {'candidates':candidates,'timings':adapter.timings,'cold_load_seconds':cold_load}
        except (FileNotFoundError, ImportError):
            return {'error_code':'model_package_missing'}
        except MemoryError:
            return {'error_code':'model_memory_exceeded'}
        except packages.ModelPackageError as error:
            return {'error_code':'input_invalid' if str(error)=='input_invalid' else 'model_package_invalid'}
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            return {'error_code':'model_package_invalid'}
    except (OSError, ValueError, TypeError):
        return {'error_code': 'input_invalid'}


def _anonymous_input(raw):
    fd, name = tempfile.mkstemp(prefix='birds-evaluation-input-')
    readonly = None
    try:
        readonly = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        os.unlink(name)
        with os.fdopen(fd,'wb') as output:
            fd = None
            output.write(raw)
        return readonly
    except BaseException:
        if readonly is not None:
            os.close(readonly)
        raise
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(name):
            os.unlink(name)


def _linux_sample(pid):
    """Actual procfs counters, not fabricated zeros on unsupported hosts."""
    values = {}
    for line in Path(f'/proc/{pid}/status').read_text().splitlines():
        key, _, value = line.partition(':')
        if key in ('VmRSS','VmHWM','VmPeak'):
            values[key] = int(value.strip().split()[0])*1024
    return values


CPU_IDENTITY_FIELDS = ('vendor_id','cpu family','model','model name','stepping','flags')


def _cpu_identity(cpuinfo):
    """Stable x86 identity v1; ignore frequency, bogomips, CPU ordering and whitespace.

    Keep logical CPU count and the unique vendor/family/model/name/stepping/flags
    combinations. Flags are a sorted set. This is a target-comparison record,
    not an authenticity proof or a CPU speed measurement.
    """
    records, current = [], {}
    for line in cpuinfo.splitlines()+['']:
        if not line.strip():
            if current:
                packages.require(all(current.get(k) for k in CPU_IDENTITY_FIELDS), 'nas_cpu_identity_incomplete')
                records.append({k:current[k] for k in CPU_IDENTITY_FIELDS})
                current = {}
            continue
        key, separator, value = line.partition(':')
        key = key.strip()
        if separator and key in CPU_IDENTITY_FIELDS:
            packages.require(key not in current, 'nas_cpu_identity_duplicate_field')
            value = ' '.join(value.split())
            current[key] = ' '.join(sorted(set(value.split()))) if key=='flags' else value
    packages.require(bool(records), 'nas_cpu_identity_incomplete')
    unique = {packages.digest(row):row for row in records}
    return {'schema_version':1,'logical_processors':len(records),
            'models':sorted(unique.values(),key=lambda row:tuple(row[k] for k in CPU_IDENTITY_FIELDS))}


def _nas_identity(execution):
    """Optional pre-approved target record + live host/egress checks, never a device-name guess.

    The record itself is auditable authorization material, not a signature. 03-11
    still independently reviews its provenance and the attached raw run evidence.
    """
    declaration = execution.get('nas_attestation')
    if declaration is None:
        return {'evidence_type':'model_cpu','target_kind':'unattested_linux'}
    packages.require(isinstance(declaration,dict) and declaration.get('path')
                     and packages.HASH.fullmatch(declaration.get('sha256','')), 'nas_attestation_invalid')
    raw = packages.read_local(declaration['path'],maximum=1024*1024)
    packages.require(hashlib.sha256(raw).hexdigest()==declaration['sha256'],'nas_attestation_hash_mismatch')
    record = packages.load_json(declaration['path'])
    cpu = Path('/proc/cpuinfo').read_text()
    memory = next(int(x.split()[1])*1024 for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemTotal:'))
    interfaces = sorted(x.split(':')[0].strip() for x in Path('/proc/net/dev').read_text().splitlines() if ':' in x)
    host = {'machine':os.uname().machine,'kernel_release':os.uname().release,
            'cpu_identity_sha256':packages.digest(_cpu_identity(cpu)),'memory_total_bytes':memory,
            'network_interfaces':interfaces}
    packages.require(record.get('schema_version')==1 and record.get('approved_target') is True
        and isinstance(record.get('target_id'),str) and record['target_id']
        and isinstance(record.get('approval_evidence_ref'),str) and record['approval_evidence_ref']
        and record.get('host')==host and host['machine']=='x86_64' and interfaces==['lo'],
        'nas_target_or_offline_evidence_mismatch')
    return {'evidence_type':'nas_cpu','target_kind':'nas','target_id':record['target_id'],
            'nas_attestation_ref':declaration['path'],'nas_attestation_sha256':declaration['sha256'],
            'observed_host':host,'approval_evidence_ref':record['approval_evidence_ref']}


def evaluation_concept_candidates(checked, manifest, candidates):
    """Evaluation IDs belong to the corpus taxonomy, never a package's source DB.

    The model's frozen vocabulary proves external concept identity. A matching
    display name alone, an arbitrary UUID, or a rank cannot establish that link.
    Production infer_readonly continues to return the unmodified label identity.
    """
    snapshot = packages.load_json(checked['paths']['snapshot'])
    snapshot_name = snapshot['source']+'-'+snapshot['version']
    labels = {row['scientific_name']:row for row in checked['labels']}
    concepts, ambiguous = {}, set()
    for image in manifest['images']:
        if not image.get('included'):
            continue
        for row in image.get('taxonomy',[]):
            key = (row.get('snapshot'),row.get('external_id'))
            value = (row.get('id'),row.get('scientific_name'))
            if key in concepts and concepts[key] != value:
                ambiguous.add(key)
            concepts[key] = value
    result = []
    for candidate in candidates:
        item = dict(candidate)
        label = labels.get(item.get('raw_label'))
        key = (snapshot_name,label['external_id']) if label else None
        concept = concepts.get(key)
        valid = (label and label.get('species_id') is not None
            and item.get('species_id') == label['species_id']
            and item.get('mapping_status') == 'mapped'
            and item.get('mapping_evidence') == label.get('mapping_evidence')
            and concept and key not in ambiguous and concept[1] == label['scientific_name'])
        item.update(species_id=concept[0] if valid else None,
            mapping_status='mapped' if valid else 'unmapped',
            mapping_evidence=('Frozen '+snapshot_name+' concept '+label['external_id']) if valid else '')
        result.append(item)
    return result


def run_cpu_evaluation(*, model_manifest, manifest, protocol, data_root, smoke=False):
    """Return results only. 03-07 validates/scores/writes the report.

    This light controller is the budget-preserving supervisor, not the capped
    file worker. It uses the identical InferenceChannel/FD/nonce/timeout/lock
    and infer_readonly adapter used by Supervisor. No heavy import occurs here.
    Actual NAS attribution is deliberately left to independently verified 03-10.
    """
    import select
    import sys
    import resource
    from archive.services.recognition_evaluation import EvaluationError, read_bytes, validate_manifest
    from archive.services.recognition_process import InferenceChannel, InferenceLimits, ProcessBoundaryError
    started = time.monotonic()
    try:
        model = packages.load_json(model_manifest) if not isinstance(model_manifest,dict) else model_manifest
        checked = packages.verify_manifest(model)
    except (OSError, ValueError, TypeError):
        raise EvaluationError('cpu_adapter_unavailable:model_package_missing_or_invalid') from None
    if not sys.platform.startswith('linux'):
        raise EvaluationError('cpu_adapter_unavailable:linux_procfs_required')
    validate_manifest(manifest, protocol, data_root)
    execution_id = str(uuid.uuid4())
    samples, images = [], []
    # Frozen protocol resource budgets must be supplied before any execution.
    budget = protocol.get('execution',{})
    if not (type(budget.get('memory_mb')) is int and budget['memory_mb'] > 0
            and type(budget.get('timeout_seconds')) is int and budget['timeout_seconds'] > 0
            and budget.get('python') and budget.get('model_lock')):
        raise EvaluationError('cpu_adapter_unavailable:frozen_execution_budget_required')
    try:
        target = _nas_identity(budget)
    except (OSError, ValueError, StopIteration) as error:
        raise EvaluationError('cpu_adapter_unavailable:nas_attestation_invalid') from error
    channel = InferenceChannel(python=budget['python'], lock_path=budget['model_lock'],
        limits=InferenceLimits(memory_mb=budget['memory_mb'],timeout_seconds=budget['timeout_seconds']))
    before_cpu = resource.getrusage(resource.RUSAGE_CHILDREN)
    cold = 0
    try:
        channel.start().wait_ready()
        ready = channel.ready
        controlled = ready.get('resources',{})
        expected_limit = budget['memory_mb']*1024*1024
        if (controlled.get('memory_control') != 'enforced' or controlled.get('soft_bytes') != expected_limit
                or controlled.get('hard_bytes') != expected_limit):
            raise EvaluationError('cpu_adapter_unavailable:child_budget_not_enforced')
        cohort = [x for x in manifest['images'] if x.get('included') and x['split']==protocol['evaluation_split']]
        for image in cohort[:1] if smoke else cohort:
            path = Path(data_root)/image['path']
            raw = read_bytes(path)
            before = hashlib.sha256(raw).hexdigest()
            if before != image['sha256']:
                raise EvaluationError('input_hash_changed')
            fd = _anonymous_input(raw)
            moment = time.monotonic()
            try:
                channel.submit(run_id=str(uuid.uuid4()),generation=1,artifact_digest=checked['manifest_digest'],
                    input_fd=fd,input_sha256=before,input_size=len(raw),artifact={'provider':packages.PROVIDER,'manifest':model})
            finally:
                os.close(fd)
            while True:
                sample = _linux_sample(channel.process.pid)
                sample['controller_rss'] = _linux_sample(os.getpid())['VmRSS']
                sample['elapsed_seconds'] = time.monotonic()-started
                samples.append(sample)
                reply = channel.poll()
                if reply is not None:
                    break
                select.select([channel.socket],[],[],.02)
            failure = reply.get('error_code') or None
            ranked = evaluation_concept_candidates(checked,manifest,reply.get('candidates',[])) if not failure else []
            elapsed = time.monotonic()-moment
            cold += reply.get('cold_load_seconds',0)
            after = hashlib.sha256(read_bytes(path)).hexdigest()
            if before != after:
                raise EvaluationError('input_hash_changed')
            images.append({'photo_id':image['photo_id'],'ranked_candidates':ranked,
                'mapping_errors':[r['raw_label'] for r in ranked if r.get('species_id') is None],
                'failure':failure,'retries':0,'input_hash_before':before,'input_hash_after':after,
                'timings':reply.get('timings',{'preprocess_seconds':0,'inference_seconds':elapsed,'mapping_seconds':0,'queue_seconds':0}),
                'threshold_outcome':'skipped','operation_trace_ref':None})
    except (ProcessBoundaryError, OSError, ValueError) as error:
        raise EvaluationError('cpu_adapter_unavailable:supervised_execution_failed') from error
    finally:
        channel.close()
    after_cpu = resource.getrusage(resource.RUSAGE_CHILDREN)
    if not samples or not any(not x['failure'] for x in images):
        raise EvaluationError('cpu_adapter_unavailable:model_forward_failed')
    raw_ref = 'results.json#/raw_execution'
    evidence = target['evidence_type']
    return {'schema_version':1,'evidence_type':evidence,'runner':'local_model_cpu',
        'manifest_digest':packages.digest(manifest),'protocol_digest':packages.digest(protocol),'images':images,
        'environment':{**target,'model_manifest_digest':checked['manifest_digest'],
            'labels_digest':model['label_digest'],'prompt_digest':model['prompt_digest'],'execution_id':execution_id,
            'offline':True,'device':'cpu','raw_run_evidence_ref':raw_ref,
            'network_evidence':'adapter has no network API; OS egress attestation required separately in 03-10'},
        'resources':{'evidence_type':evidence,'cpu_seconds':(after_cpu.ru_utime-before_cpu.ru_utime)+(after_cpu.ru_stime-before_cpu.ru_stime),
            'storage_bytes':sum(x['size_bytes'] for x in model['files']),
            'peak_rss_bytes':max(x['VmHWM'] for x in samples),
            'peak_address_space_bytes':max(x['VmPeak'] for x in samples),
            'process_tree_peak_rss_bytes':max(x['VmRSS']+x['controller_rss'] for x in samples),
            'collector':'Linux procfs sampled 20ms plus wait4 child CPU; tree RSS is sampled, not cgroup peak',
            'raw_evidence_ref':raw_ref,'electricity':{'status':'unknown'}},
        'batch_timings':{'cold_load_seconds':cold,'vocabulary_seconds':0,'wall_seconds':time.monotonic()-started},
        'raw_execution':{'execution_id':execution_id,'ready':ready,'samples':samples,
                         'timing_scope':'cold_load_seconds sums per-request package verification and model load; vocabulary_seconds=0 means precomputed frozen matrix; per-image stages measured in child'}}


def _prepare_vocabulary(manifest_path):
    """Explicit one-time local derived artifact, never reachable from inference."""
    from archive.services.recognition_process import address_space
    # Separate process, before heavy imports; fileworker hard limit is untouched.
    address_space(6144)
    os.nice(10)
    started = time.monotonic()
    manifest = packages.load_json(manifest_path)
    checked = packages.verify_manifest(manifest,require_features=False)
    adapter = OfflineCPUAdapter(checked,prepare=True)
    loaded = time.monotonic()
    features = adapter.prepare_features()
    encoded = time.monotonic()
    output = Path(manifest['local_root'])/'avilist-text-features.safetensors'
    if output.exists() or output.is_symlink():
        raise packages.ModelPackageError('output_exists_or_symlink')
    # safetensors serializer, no pickle/torch.save. Final manifest is a new path.
    adapter.save({'text_features':features},str(output),metadata={
        'labels_digest':manifest['label_digest'],'prompt_digest':manifest['prompt_digest'],
        'model_revision':packages.REVISION,'dtype':'float32','device':'cpu'})
    sha = hashlib.sha256(packages.read_local(output)).hexdigest()
    manifest['files'].append({'role':'text_features','path':output.name,'sha256':sha,'size_bytes':output.stat().st_size})
    manifest['text_features_digest'] = sha
    manifest['preparation'] = {'evidence_type':'model_cpu','cold_load_seconds':loaded-started,
        'vocabulary_seconds':encoded-loaded,'labels':len(checked['labels']),
        'scope':'explicit offline vocabulary maintenance process; not current image batch'}
    destination = Path(manifest_path).with_name('prepared-manifest.json')
    with destination.open('x',encoding='utf-8') as stream:
        json.dump(manifest,stream,ensure_ascii=False,sort_keys=True,indent=2)
    packages.verify_manifest(manifest)
    print(json.dumps({'manifest':str(destination),'manifest_digest':packages.digest(manifest),'labels':len(checked['labels'])}))


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 3 or sys.argv[1] != '--prepare-vocabulary':
        raise SystemExit(2)
    try:
        _prepare_vocabulary(sys.argv[2])
    except (ImportError, OSError, ValueError, RuntimeError, MemoryError):
        raise SystemExit(3)
