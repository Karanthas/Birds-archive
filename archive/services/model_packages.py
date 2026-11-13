"""Offline, stdlib-only package validation. No registry/Hub access or model imports."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid
import ast
import email
import unicodedata
from urllib.parse import urlsplit

PROVIDER = 'open_clip_cpu_bioclip2'
REPOSITORY = 'imageomics/bioclip-2'
REVISION = '2957b322090f9cb17ae72c71981c7218a28d81e0'
WEIGHTS_SHA256 = 'b7b2bf6fbc95799e42630e394cf95803892ab447c1a8ab629dbc82fbeaf7dfef'
CONFIG_SHA256 = '1bf947e96e943fe50efd5c3e26c37f843a2fa3c358967719a68c8a6d17ce68c8'
BPE_SHA256 = '924691ac288e54409236115652ad4aa250f48203de50a9e4722a6ecd48d6804a'
GLOBAL_SPECIES_COUNT = 11131
HASH = re.compile(r'^[0-9a-f]{64}$')
PROMPT = 'a photo of {scientific_name}.'
ROLES = frozenset({'weights', 'config', 'tokenizer', 'labels', 'prompts', 'text_features',
                   'snapshot', 'preprocess', 'runner', 'dependency_lock', 'license'})


class ModelPackageError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise ModelPackageError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def load_json(path):
    try:
        return json.loads(read_local(path, maximum=16*1024*1024), object_pairs_hook=_pairs,
                          parse_constant=lambda value: require(False, 'nonfinite_json'))
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise ModelPackageError('package_json_invalid') from error


def read_local(path, maximum=None):
    """Refuse symlinks in every component, including maintenance input paths."""
    path = Path(path).absolute()
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'package_path_invalid')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and (maximum is None or info.st_size <= maximum), 'package_file_invalid')
        return stream.read() if maximum is None else stream.read(maximum+1)


def checked_path(root, relative):
    require(isinstance(relative, str) and relative and '\\' not in relative, 'package_path_invalid')
    p = Path(relative)
    require(not p.is_absolute() and all(x not in ('..', '.') for x in p.parts), 'package_path_invalid')
    require(len(p.parts) == 1, 'package_path_invalid')
    return root / p


def verify_manifest(manifest, *, require_features=True):
    """Validate bytes before optional imports. A manifest never supplies executable code."""
    require(isinstance(manifest, dict) and manifest.get('schema_version') == 1, 'package_manifest_invalid')
    require(manifest.get('provider') == PROVIDER and manifest.get('repository') == REPOSITORY
            and manifest.get('revision') == REVISION, 'package_identity_invalid')
    require(manifest.get('license') == 'MIT' and isinstance(manifest.get('attribution'), str)
            and manifest['attribution'].strip(), 'package_license_missing')
    root = Path(manifest.get('local_root', '')).absolute()
    require(isinstance(manifest.get('local_root'), str) and Path(manifest['local_root']).is_absolute()
            and root.is_dir() and not any(p.is_symlink() for p in (root, *root.parents)), 'package_path_invalid')
    rows = manifest.get('files')
    require(isinstance(rows, list) and all(isinstance(x, dict) for x in rows), 'package_files_invalid')
    roles = {x.get('role') for x in rows}
    expected_roles = ROLES if require_features else ROLES-{'text_features'}
    calibrated = manifest.get('margin_threshold') is not None
    if calibrated:
        expected_roles = expected_roles | {'calibration'}
    require(len(roles) == len(rows) and roles == expected_roles, 'package_files_incomplete')
    paths, names = {}, set()
    for row in rows:
        path = checked_path(root, row.get('path'))
        require(path.name not in names and isinstance(row.get('sha256'), str) and HASH.fullmatch(row['sha256'])
                and type(row.get('size_bytes')) is int and row['size_bytes'] > 0, 'package_file_invalid')
        names.add(path.name)
        # Stream large weights, never retain another 1.7GB copy in Python.
        require(not path.is_symlink() and path.is_file(), 'package_file_missing')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            h = hashlib.file_digest(stream, 'sha256').hexdigest()
            after = os.fstat(stream.fileno())
        identity = lambda s: (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        require(stat.S_ISREG(before.st_mode) and identity(before) == identity(after) and before.st_size == row['size_bytes']
                and h == row['sha256'], 'package_hash_mismatch')
        paths[row['role']] = path
    require(paths['weights'].name == 'open_clip_model.safetensors'
            and paths['config'].name == 'open_clip_config.json', 'package_format_invalid')
    by_role = {x['role']: x for x in rows}
    require(by_role['weights']['sha256'] == WEIGHTS_SHA256 and by_role['config']['sha256'] == CONFIG_SHA256
            and by_role['tokenizer']['sha256'] == BPE_SHA256,
            'package_upstream_hash_mismatch')
    require(not any(p.suffix in ('.bin','.pt','.pth','.py','.pkl') for p in root.iterdir()), 'package_executable_or_pickle')
    require({p.name for p in root.glob('*.safetensors')} ==
            {paths[k].name for k in ('weights','text_features') if k in paths}, 'package_unlisted_weights')
    for role, field in (('dependency_lock','dependency_lock_digest'), ('config','config_digest'),
                        ('tokenizer','tokenizer_digest'), ('labels','label_digest'), ('snapshot','snapshot_digest'),
                        ('preprocess','preprocess_digest'), ('prompts','prompt_digest'), ('runner','runner_digest')):
        require(manifest.get(field) == by_role[role]['sha256'], 'package_digest_mismatch')
    if require_features:
        require(manifest.get('text_features_digest') == by_role['text_features']['sha256'], 'package_digest_mismatch')
    require(b'MIT' in read_local(paths['license'], maximum=100000), 'package_license_missing')
    config = load_json(paths['config'])
    require(set(config) == {'model_cfg','preprocess_cfg'} and config['model_cfg'] == {
        'embed_dim':768, 'vision_cfg':{'image_size':224,'layers':24,'width':1024,'patch_size':14},
        'text_cfg':{'context_length':77,'vocab_size':49408,'width':768,'heads':12,'layers':12}}, 'package_config_invalid')
    preprocess = load_json(paths['preprocess'])
    require(preprocess == {**config['preprocess_cfg'], 'size':224, 'mode':'RGB', 'exif_transpose':True}, 'package_preprocess_invalid')
    runner = load_json(paths['runner'])
    require(runner.get('version') == 'openclip-cpu-eager-v1' and runner.get('device') == 'cpu'
            and runner.get('dtype') == 'float32' and runner.get('batch_size') == 1
            and runner.get('intra_threads') == 2 and runner.get('inter_threads') == 1
            and runner.get('embedding_chunk_size') == 32, 'package_runner_invalid')
    labels, snapshot, prompts = (load_json(paths[k]) for k in ('labels','snapshot','prompts'))
    validate_labels(labels, snapshot)
    require(prompts == {'version':'avilist-scientific-v1', 'template':PROMPT,
                        'items':[PROMPT.format(scientific_name=x['scientific_name']) for x in labels]}, 'package_prompt_invalid')
    require(manifest.get('score_kind') == 'cosine_similarity', 'package_score_invalid')
    if not calibrated:
        require(manifest.get('threshold_version') == 'uncalibrated-v1'
                and manifest.get('calibration_evidence') is None, 'package_threshold_uncalibrated_required')
    else:
        threshold = manifest['margin_threshold']
        evidence = load_json(paths['calibration'])
        require(type(threshold) in (int,float) and math.isfinite(threshold) and 0 <= threshold <= 2
                and manifest.get('calibration_evidence') == 'sha256:'+by_role['calibration']['sha256']
                and isinstance(manifest.get('threshold_version'),str) and manifest['threshold_version'] != 'uncalibrated-v1'
                and evidence.get('partition') == 'calibration' and evidence.get('revision') == REVISION
                and HASH.fullmatch(evidence.get('report_digest',''))
                and evidence.get('margin_threshold') == threshold
                and all(evidence.get(k) == manifest.get(k) for k in
                        ('label_digest','prompt_digest','preprocess_digest','score_kind','threshold_version')),
                'package_calibration_invalid')
    return {'manifest':manifest, 'paths':paths, 'labels':labels, 'runner':runner,
            'manifest_digest':digest(manifest), 'preprocess':preprocess}


def validate_labels(labels, snapshot):
    require(isinstance(labels,list) and len(labels) == GLOBAL_SPECIES_COUNT
            and isinstance(snapshot,dict) and snapshot.get('source') == 'AviList'
            and snapshot.get('version') == '2025b' and snapshot.get('species_count') == GLOBAL_SPECIES_COUNT
            and HASH.fullmatch(snapshot.get('sha256','')) and snapshot.get('concepts') == labels,
            'package_global_vocabulary_invalid')
    keys = []
    for row in labels:
        require(isinstance(row,dict) and row.get('identity_kind') == 'catalog'
                and isinstance(row.get('scientific_name'),str) and 0 < len(row['scientific_name']) <= 300
                and row['scientific_name'] == row['scientific_name'].strip()
                and not any(unicodedata.category(c).startswith('C') for c in row['scientific_name'])
                and isinstance(row.get('external_id'),str) and row['external_id'], 'package_label_invalid')
        keys.append((row['scientific_name'], row['external_id']))
        if row.get('species_id') is not None:
            try:
                require(str(uuid.UUID(row['species_id'])) == row['species_id'], 'package_mapping_invalid')
            except (ValueError, TypeError, AttributeError):
                raise ModelPackageError('package_mapping_invalid') from None
            require(isinstance(row.get('mapping_evidence'),str) and 0 < len(row['mapping_evidence']) <= 1000,
                    'package_mapping_invalid')
        else:
            require(not row.get('mapping_evidence'), 'package_mapping_invalid')
    require(keys == sorted(keys) and len(set(x[1] for x in keys)) == len(keys)
            and len(set(x[0] for x in keys)) == len(keys), 'package_label_order_or_ambiguity')
    mapped = [x['species_id'] for x in labels if x.get('species_id') is not None]
    require(len(set(mapped)) == len(mapped), 'package_mapping_ambiguous')


def verify_inventory(inventory):
    """Review completeness is separate from installation authority and runtime compatibility."""
    require(isinstance(inventory,dict) and inventory.get('schema_version') == 1
            and inventory.get('target') == {'python':'3.12','os':'linux','arch':'x86_64','glibc_min':'2.28','device':'cpu'},
            'inventory_platform_invalid')
    require(inventory.get('unresolved') == [] and inventory.get('installation_authorized') is False,
            'inventory_unresolved_or_wrong_gate')
    packages = inventory.get('packages')
    require(isinstance(packages,list) and packages, 'inventory_packages_empty')
    names = set()
    versions = {p.get('name'):p.get('version') for p in packages if isinstance(p,dict)}
    marker_env = inventory.get('dependency_closure',{}).get('target_marker_environment',{})
    require(marker_env.get('python_version') == '3.12' and marker_env.get('sys_platform') == 'linux'
            and marker_env.get('platform_machine') == 'x86_64' and marker_env.get('extra') == '', 'inventory_marker_target_invalid')
    metadata_root = Path(inventory.get('dependency_closure',{}).get('raw_metadata_directory',''))
    for p in packages:
        require(isinstance(p,dict) and isinstance(p.get('name'),str) and p['name'] not in names
                and re.fullmatch(r'[0-9]+(?:\.[0-9]+)+(?:\+cpu)?',p.get('version','')), 'inventory_version_invalid')
        names.add(p['name'])
        require(not p['name'].startswith(('nvidia','cuda','triton')) and '.whl' == Path(p.get('filename','')).suffix
                and ('manylinux' in p['filename'] and 'x86_64' in p['filename'] or p['filename'].endswith('-none-any.whl'))
                and not any(s in p['filename'] for s in ('macos','win_','aarch64')), 'inventory_wheel_platform_invalid')
        filename = p['filename']
        tags = filename[:-4].rsplit('-',3)
        require(len(tags) == 4 and (tags[1] in ('py3','py2.py3','cp312') and tags[2] in ('none','cp312')
                or tags[2] == 'abi3' and re.fullmatch(r'cp3[0-9]+',tags[1]) and int(tags[1][3:]) <= 12),
                'inventory_python_tag_invalid')
        require(tags[3] == 'any' or any(int(v) <= 28 for v in re.findall(r'manylinux_2_([0-9]+)_x86_64',tags[3]))
                or 'manylinux2014_x86_64' in tags[3], 'inventory_glibc_tag_invalid')
        require(HASH.fullmatch(p.get('sha256','')) and p.get('license') and p['license'] != 'UNKNOWN'
                and p.get('purpose') and p.get('install_environments') and p.get('metadata_url')
                and p.get('review_status') == 'metadata_verified_pending_authorization', 'inventory_package_incomplete')
        require(set(p['install_environments']) <= {'.venv-evaluation','.venv-recognition'}
                and 'installed_size_bytes' in p and p.get('installed_size_status'), 'inventory_scope_incomplete')
        require(urlsplit(p.get('url','')).hostname in ('files.pythonhosted.org','download.pytorch.org','download-r2.pytorch.org')
                and urlsplit(p['url']).scheme == 'https', 'inventory_source_invalid')
        if p['name'] in ('torch','torchvision'):
            require(p['version'] == ('2.13.0+cpu' if p['name']=='torch' else '0.28.0+cpu')
                    and '/cpu/' in p['url'], 'inventory_cpu_pair_invalid')
            raw = read_local(metadata_root/(p['name']+'-cpu.metadata'),maximum=1024*1024)
            metadata = email.message_from_bytes(raw)
            requirements = metadata.get_all('Requires-Dist') or []
            require(metadata.get('Version') == p['version'], 'inventory_metadata_mismatch')
            require(p['sha256'] in read_local(metadata_root/(p['name']+'-cpu-index.html'),maximum=8*1024*1024).decode(),
                    'inventory_index_hash_mismatch')
        else:
            raw = read_local(metadata_root/(p['name']+'-'+p['version']+'.json'),maximum=16*1024*1024)
            metadata = json.loads(raw)
            requirements = metadata['info'].get('requires_dist') or []
            require(metadata['info']['version'] == p['version'] and any(f['filename'] == p['filename']
                    and f['digests']['sha256'] == p['sha256'] and f['url'] == p['url']
                    and f['packagetype'] == 'bdist_wheel' and not f['yanked'] for f in metadata['urls']),
                    'inventory_metadata_mismatch')
        require(hashlib.sha256(raw).hexdigest() == p.get('metadata_sha256') and requirements == p.get('requires_dist'),
                'inventory_metadata_mismatch')
        expected_dependencies = []
        for requirement in requirements:
            dependency, _, marker = requirement.partition(';')
            if marker and not _marker(marker.strip(),marker_env):
                continue
            match = re.fullmatch(r'\s*([A-Za-z0-9_.-]+)\s*(?:\(([^)]*)\)|([^;]*))\s*',dependency)
            require(bool(match), 'inventory_requirement_unsupported')
            name = re.sub('[-_.]+','-',match[1]).lower()
            specifier = (match[2] or match[3] or '').strip()
            require(name in versions and _version_satisfies(versions[name],specifier), 'inventory_dependency_unsatisfied')
            expected_dependencies.append((name,versions[name]))
        require(expected_dependencies == [(d.get('name'),d.get('version')) for d in p.get('required_dependencies',[])],
                'inventory_closure_mismatch')
    require({'open-clip-torch','torch','torchvision','django','pillow','gunicorn','whitenoise','asgiref','sqlparse'} <= names,
            'inventory_base_or_model_missing')
    require(inventory.get('dependency_closure',{}).get('verified') is True
            and inventory['dependency_closure'].get('package_count') == len(packages), 'inventory_closure_invalid')
    for name,env in inventory.get('environments',{}).items():
        require(name in ('.venv-evaluation','.venv-recognition') and env.get('packages')
                and set(env['packages']) == {p['name'] for p in packages if name in p['install_environments']},
                'inventory_environment_incomplete')
        lock = read_local(env['lock_path']).decode() if env.get('lock_path') else env.get('lock_content','')
        require(hashlib.sha256(lock.encode()).hexdigest() == env.get('lock_sha256')
                and '--only-binary=:all:' in lock and '--require-hashes' in lock
                and all(p['sha256'] in lock for p in packages if name in p['install_environments']),
                'inventory_lock_mismatch')
    files = inventory.get('model',{}).get('files',[])
    require(inventory.get('model',{}).get('revision') == REVISION and files
            and all(HASH.fullmatch(f.get('sha256','')) and type(f.get('size_bytes')) is int
                    and f['size_bytes'] > 0 and f.get('license') and f.get('url') for f in files), 'inventory_model_incomplete')
    return {'schema_version':1,'review':'complete_pending_03_09_authorization', 'candidate_packages':len(packages),
            'model_files':len(files),'installation_authorized':False,'inventory_digest':digest(inventory)}


def _version_satisfies(version, specifier):
    """Restricted numeric PEP440 subset in the frozen inventory; fail closed otherwise."""
    public = version.split('+',1)[0]
    def key(value):
        require(bool(re.fullmatch(r'[0-9]+(?:\.[0-9]+)*',value)), 'inventory_version_unsupported')
        values = tuple(int(v) for v in value.split('.'))
        return values + (0,)*(4-len(values))
    for item in filter(None,(s.strip() for s in specifier.split(','))):
        wildcard = re.fullmatch(r'(==|!=)\s*([0-9]+(?:\.[0-9]+)*)\.\*',item)
        if wildcard:
            equal = public == wildcard[2] or public.startswith(wildcard[2]+'.')
            if equal != (wildcard[1]=='=='):
                return False
            continue
        match = re.fullmatch(r'(===|==|!=|>=|<=|>|<|~=)\s*([0-9]+(?:\.[0-9]+)*(?:\+cpu)?)',item)
        require(bool(match), 'inventory_specifier_unsupported')
        op, expected = match.groups()
        left,right = key(public),key(expected.split('+',1)[0])
        passed = {'==':left==right,'===':version==expected,'!=':left!=right,'>=':left>=right,
                  '<=':left<=right,'>':left>right,'<':left<right,
                  '~=':left>=right and public.split('.')[:-1]==expected.split('.')[:-1]}[op]
        if not passed:
            return False
    return True


def _marker(expression, environment):
    """Evaluate marker AST data without eval, calls, attributes or executable input."""
    try:
        def visit(node):
            if isinstance(node,ast.Expression):
                return visit(node.body)
            if isinstance(node,ast.Constant) and isinstance(node.value,str):
                return node.value
            if isinstance(node,ast.Name) and node.id in environment:
                return environment[node.id]
            if isinstance(node,ast.BoolOp) and isinstance(node.op,(ast.And,ast.Or)):
                values = [visit(x) for x in node.values]
                return all(values) if isinstance(node.op,ast.And) else any(values)
            if isinstance(node,ast.Compare) and len(node.ops)==1:
                left,right,op = visit(node.left),visit(node.comparators[0]),node.ops[0]
                if isinstance(op,ast.Eq): return left==right
                if isinstance(op,ast.NotEq): return left!=right
                if isinstance(op,ast.In): return left in right
                if isinstance(op,ast.NotIn): return left not in right
                symbols = {ast.Lt:'<',ast.LtE:'<=',ast.Gt:'>',ast.GtE:'>='}
                if type(op) in symbols:
                    return _version_satisfies(left,symbols[type(op)]+right)
            raise ModelPackageError('inventory_marker_unsupported')
        return visit(ast.parse(expression,mode='eval'))
    except (SyntaxError, TypeError):
        raise ModelPackageError('inventory_marker_unsupported') from None
