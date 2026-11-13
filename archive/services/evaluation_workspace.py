"""Explicit disposable review workspaces. No activation, publication or model execution."""
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import uuid
from contextlib import contextmanager

from django.conf import settings
from django.core.management import call_command
from django.db import connection, transaction
from django.db.models import F

from archive.services import model_packages as packages
from archive.services.recognition_evaluation import (COHORTS, EvaluationError, digest, load_json,
    read_bytes, require, validate_manifest)

SESSION_KEYS = {'schema_version','session_id','cohort','workspace_digest','inputs','frozen_digest',
                'phase','artifact_id','current','images','snapshot'}
INPUT_KEYS = {'manifest','protocol','data_root','model_manifest','catalog_file','catalog_manifest'}


def _plain(path):
    path = Path(path).absolute()
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'workspace_symlink')
    return path


def assert_evaluation_workspace():
    """Read-only checks precede every database/file operation, including lock creation."""
    require(os.environ.get('DJANGO_SETTINGS_MODULE') == 'config.settings_test'
            and settings.SETTINGS_MODULE == 'config.settings_test', 'explicit_test_settings_required')
    supplied = os.environ.get('BIRDS_TEST_ROOT')
    require(bool(supplied), 'explicit_test_root_required')
    root = _plain(supplied)
    require(root.is_dir() and root.parent == Path(tempfile.gettempdir()).resolve()
            and root.name.startswith('birds-tests-') and root.stat().st_uid == os.getuid(), 'invalid_workspace_root')
    require(Path(settings.BIRDS_TEST_ROOT).absolute() == root, 'inherited_workspace_root')
    marker = _plain(root/'.birds-test-root')
    require(marker.stat().st_uid == os.getuid() and read_bytes(marker) == b'birds isolated v1', 'invalid_workspace_marker')
    for key, name in (('BIRDS_INBOX_ROOT','inbox'),('BIRDS_DATA_ROOT','data'),
                      ('BIRDS_ARCHIVE_ROOT','archive'),('BIRDS_CLAIM_ROOT','claims'),('STATIC_ROOT','static')):
        path = _plain(getattr(settings,key))
        require(path == root/name and path.is_dir() and path.stat().st_uid == os.getuid(), 'external_workspace_media')
    db = connection.settings_dict
    require(db['ENGINE'] == 'django.db.backends.sqlite3' and _plain(db['NAME']) == root/'data'/'test.sqlite3',
            'external_workspace_database')
    for suffix in ('','-wal','-shm','-journal'):
        file = _plain(str(db['NAME'])+suffix)
        require(not file.exists() or stat.S_ISREG(file.stat().st_mode) and file.stat().st_uid == os.getuid(),
                'invalid_workspace_database')
    return root


@contextmanager
def _workspace_lock(root):
    path = _plain(root/'data'/'.evaluation.lock')
    fd = os.open(path, os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1,
                'invalid_workspace_lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise EvaluationError('workspace_busy') from None
        yield
    finally:
        os.close(fd)


def _session_path(root):
    return _plain(root/'data'/'evaluation-session.json')


def _save(root, session, *, create=False):
    path = _session_path(root)
    if path.exists():
        require(not create and _read_session(root)['session_id'] == session['session_id'], 'session_conflict')
    else:
        require(create, 'session_missing')
    raw = json.dumps(session, ensure_ascii=False, sort_keys=True, separators=(',',':'), allow_nan=False).encode()
    fd, temporary = tempfile.mkstemp(prefix='.evaluation-session-', dir=root/'data')
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            os.link(temporary,path)  # atomic no-clobber; never takes over an unfamiliar file
        else:
            os.replace(temporary,path)
        directory = os.open(root/'data',os.O_RDONLY|os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_session(root):
    path = _session_path(root)
    info = path.stat()
    require(info.st_uid == os.getuid() and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600,
            'session_not_private')
    session = load_json(path)
    require(isinstance(session,dict) and set(session) == SESSION_KEYS and session['schema_version'] == 1
            and session['cohort'] in COHORTS and session['phase'] in ('initializing','ready')
            and isinstance(session['inputs'],dict) and set(session['inputs']) == INPUT_KEYS
            and isinstance(session['images'],list) and session['workspace_digest'] == digest(str(root)),
            'invalid_workspace_session')
    try:
        uuid.UUID(session['session_id'])
    except (TypeError,ValueError,AttributeError):
        raise EvaluationError('invalid_workspace_session') from None
    return session


def _materials(inputs):
    corpus, protocol, model, catalog_meta = (load_json(inputs[name]) for name in
        ('manifest','protocol','model_manifest','catalog_manifest'))
    audit = validate_manifest(corpus,protocol,inputs['data_root'])
    from archive.services.catalog import read_package
    meta, rows = read_package(inputs['catalog_file'],inputs['catalog_manifest'])
    checked = packages.verify_manifest(model)
    snapshot = packages.load_json(checked['paths']['snapshot'])
    require((snapshot['source'],snapshot['version'],snapshot['sha256'],snapshot['species_count']) ==
            (meta['source'],meta['version'],meta['sha256'],meta['species_count']), 'catalog_model_snapshot_mismatch')
    concepts = {r['external_id']:r for r in rows}
    require(len(concepts) == len(checked['labels']) and all(r['external_id'] in concepts
        and r['scientific_name'] == concepts[r['external_id']]['scientific_name'] for r in checked['labels']),
        'catalog_model_concept_mismatch')
    images = [x for x in corpus['images'] if x['included'] and x['split'] == protocol['evaluation_split']]
    require(len({x['sha256'] for x in images}) == len(images), 'duplicate_workspace_pixels')
    for image in images:
        require(all(t['snapshot'] == meta['source']+'-'+meta['version'] and t['external_id'] in concepts
            and t['scientific_name'] == concepts[t['external_id']]['scientific_name'] for t in image['taxonomy']),
            'corpus_catalog_mismatch')
    frozen = {'manifest':digest(corpus),'protocol':digest(protocol),'model':digest(model),
        'catalog_manifest':digest(catalog_meta),'catalog_file':meta['sha256'],'inputs':inputs}
    return dict(corpus=corpus,protocol=protocol,model=model,checked=checked,meta=meta,images=images,
                frozen_digest=digest(frozen),audit=audit)


def _empty_database(root):
    database = root/'data'/'test.sqlite3'
    if database.exists() and database.stat().st_size:
        with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True) as db:
            tables = {x[0] for x in db.execute("select name from sqlite_master where type='table'")}
        require(tables <= {'django_migrations','sqlite_sequence'}, 'unfamiliar_workspace_database')
    for name in ('inbox','archive','claims','static'):
        require(not list((root/name).iterdir()), 'unfamiliar_workspace_files')
    require({p.name for p in (root/'data').iterdir()} <=
            {'test.sqlite3','test.sqlite3-wal','test.sqlite3-shm','test.sqlite3-journal','.evaluation.lock'},
            'unfamiliar_workspace_files')


def _current_refs():
    from archive.models import ModelArtifact
    return sorted(str(x) for x in ModelArtifact.objects.filter(is_current=True).values_list('pk',flat=True))


def _checked_session(root):
    session = _read_session(root)
    materials = _materials(session['inputs'])
    require(materials['frozen_digest'] == session['frozen_digest'], 'frozen_workspace_changed')
    require(session['phase'] == 'ready', 'bootstrap_incomplete')
    require(len(session['images']) == len(materials['images']), 'workspace_images_changed')
    for row, image in zip(session['images'],materials['images']):
        require(isinstance(row,dict) and set(row) == {'image_id','sha256','size_bytes','relative_path',
            'photo_id','source_observation_id','run_id','generation'} and row['image_id'] == image['photo_id']
            and row['sha256'] == image['sha256'] and row['size_bytes'] == image['size_bytes']
            and row['relative_path'] == hashlib.sha256(image['photo_id'].encode()).hexdigest()+'.jpg',
            'workspace_images_changed')
    from archive.models import ModelArtifact, SourceSnapshot
    require(SourceSnapshot.objects.filter(pk=session['snapshot'],source=materials['meta']['source'],
        version=materials['meta']['version'],sha256=materials['meta']['sha256']).exists(), 'workspace_snapshot_changed')
    artifact = ModelArtifact.objects.get(pk=session['artifact_id'])
    require(artifact.manifest_digest == digest(materials['model']) and artifact.manifest == materials['model']
            and artifact.provider == packages.PROVIDER and artifact.selection_state == 'candidate'
            and not artifact.is_current and _current_refs() == session['current'], 'workspace_artifact_changed')
    return session, materials, artifact


def bootstrap_cohort(*, cohort, manifest, protocol, data_root, model_manifest, catalog_file, catalog_manifest):
    root = assert_evaluation_workspace()
    require(cohort in COHORTS, 'invalid_cohort')
    inputs = {key:str(_plain(value)) for key,value in dict(manifest=manifest,protocol=protocol,data_root=data_root,
        model_manifest=model_manifest,catalog_file=catalog_file,catalog_manifest=catalog_manifest).items()}
    materials = _materials(inputs)  # all licenses, JPEG bytes and package validation before writing
    if _session_path(root).exists():
        _read_session(root)  # an unfamiliar session never even gets a lock file
    else:
        _empty_database(root)  # refuse an existing library before the first write
    with _workspace_lock(root):
        if _session_path(root).exists():
            session = _read_session(root)
            require(session['cohort'] == cohort and session['inputs'] == inputs
                    and session['frozen_digest'] == materials['frozen_digest'], 'session_conflict')
            if session['phase'] == 'ready':
                _checked_session(root)
                return _public_session(session)
        else:
            _empty_database(root)
            session = dict(schema_version=1,session_id=str(uuid.uuid4()),cohort=cohort,
                workspace_digest=digest(str(root)),inputs=inputs,frozen_digest=materials['frozen_digest'],
                phase='initializing',artifact_id=None,current=[],images=[],snapshot=None)
            _save(root,session,create=True)
        # A persisted, private same-input session owns interrupted initialization from here.
        call_command('migrate',interactive=False,verbosity=0,stdout=io.StringIO())
        call_command('importcatalog',file=inputs['catalog_file'],manifest=inputs['catalog_manifest'],stdout=io.StringIO())
        out = io.StringIO()
        call_command('preparemodel',register=True,manifest=inputs['model_manifest'],stdout=out)
        artifact = json.loads(out.getvalue())
        require(artifact['state'] == 'candidate' and artifact['is_current'] is False, 'candidate_required')
        from archive.models import SourceSnapshot
        snapshot = SourceSnapshot.objects.get(source=materials['meta']['source'],version=materials['meta']['version'],
            sha256=materials['meta']['sha256'])
        session.update(artifact_id=artifact['artifact'],snapshot=str(snapshot.pk),current=_current_refs())
        _save(root,session)
        images = []
        for image in materials['images']:
            relative = hashlib.sha256(image['photo_id'].encode()).hexdigest()+'.jpg'
            destination = _plain(root/'inbox'/relative)
            raw = read_bytes(Path(inputs['data_root'])/image['path'],64*1024*1024)
            require(hashlib.sha256(raw).hexdigest() == image['sha256'], 'jpeg_hash_mismatch')
            if destination.exists():
                require(read_bytes(destination,64*1024*1024) == raw, 'workspace_image_conflict')
            else:
                fd, temporary = tempfile.mkstemp(prefix='.evaluation-jpeg-',dir=root/'inbox')
                try:
                    with os.fdopen(fd,'wb') as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.link(temporary,destination)
                finally:
                    os.unlink(temporary)
            images.append(dict(image_id=image['photo_id'],sha256=image['sha256'],size_bytes=image['size_bytes'],
                relative_path=relative,photo_id=None,source_observation_id=None,run_id=None,generation=None))
        session.update(phase='ready',images=images)
        _save(root,session)
        return _public_session(session)


def _public_session(session):
    return {key:session[key] for key in ('session_id','cohort','artifact_id','workspace_digest','frozen_digest')}


def is_workspace_import(photo, observation):
    """Only a verified public-session image may suppress ordinary current-model import.

    Called inside finish_job's transaction. A broken evaluation session fails that
    transaction instead of silently turning an unrelated library into manual mode.
    """
    if settings.SETTINGS_MODULE != 'config.settings_test':
        return False
    data = Path(settings.BIRDS_DATA_ROOT)
    if not (data/'evaluation-session.json').exists() and not (data/'evaluation-session.json').is_symlink():
        return False
    root = assert_evaluation_workspace()
    session, materials, artifact = _checked_session(root)
    require(any(row['relative_path'] == observation.source.relative_path and row['sha256'] == photo.sha256
        and row['size_bytes'] == photo.byte_size == observation.byte_size and observation.sha256 == photo.sha256
        and observation.photo_id == photo.pk for row in session['images']), 'unlisted_workspace_import')
    from archive.services.paths import open_root, open_regular_beneath
    from archive.services.images import content_hash
    fd = open_root(root/'inbox')
    try:
        with open_regular_beneath(fd,observation.source.relative_path) as stream:
            require(content_hash(stream) == (photo.sha256,photo.byte_size), 'workspace_import_hash_changed')
    finally:
        os.close(fd)
    return True


def _bind_image(row):
    from archive.models import SourceObservation
    observation = SourceObservation.objects.select_related('source','photo').get(source__relative_path=row['relative_path'],
        available=True,state__in=['ready','duplicate'],sha256=row['sha256'],byte_size=row['size_bytes'],
        source__current_observation_id=F('pk'))
    photo = observation.photo
    source = observation.source
    require(photo is not None and photo.state == 'review_pending' and photo.original_available
            and (photo.sha256,photo.byte_size) == (row['sha256'],row['size_bytes'])
            and source.photo_id == photo.pk and source.observed_version == observation.version
            and source.fingerprint == observation.fingerprint and source.sha256 == photo.sha256
            and source.state in ('ready','duplicate'), 'workspace_photo_changed')
    require(row['photo_id'] in (None,str(photo.pk))
            and row['source_observation_id'] in (None,str(observation.pk)), 'workspace_source_changed')
    row.update(photo_id=str(photo.pk),source_observation_id=str(observation.pk))
    return photo, observation


def enqueue_candidate_runs(artifact_id):
    root = assert_evaluation_workspace()
    with _workspace_lock(root):
        session, materials, artifact = _checked_session(root)
        require(str(artifact_id) == str(artifact.pk), 'workspace_artifact_mismatch')
        require(session['cohort'] != 'manual', 'manual_cohort_cannot_enqueue')
        from archive.models import RecognitionRun, RecognitionState
        from archive.services.recognition import run_payload_digest, _current, validate_source
        from archive.services.paths import open_root
        result = []
        with transaction.atomic():
            for row in session['images']:
                photo, observation = _bind_image(row)
                request_id = uuid.uuid5(uuid.UUID(session['session_id']),row['image_id']+':'+artifact.manifest_digest)
                existing = RecognitionRun.objects.filter(request_id=request_id).select_related('photo','artifact',
                    'source_observation__source').first()
                if existing:
                    require(existing.reason == 'evaluation' and existing.artifact_id == artifact.pk
                            and existing.source_observation_id == observation.pk and _current(existing), 'request_conflict')
                    run = existing
                else:
                    state, _ = RecognitionState.objects.get_or_create(photo=photo)
                    run = RecognitionRun(photo=photo,source_observation=observation,request_id=request_id,
                        source_version=observation.version,source_fingerprint=observation.fingerprint,
                        photo_sha256=photo.sha256,byte_size=photo.byte_size,metadata_version=photo.metadata_version,
                        artifact=artifact,generation=state.requested_generation+1,reason='evaluation',state='queued',
                        queue_age=photo.created_at)
                    run.payload_digest = run_payload_digest(run)
                    fd = open_root(root/'inbox')
                    try:
                        validate_source(run,fd)
                    finally:
                        os.close(fd)
                    run.save()
                    require(RecognitionState.objects.filter(pk=state.pk,requested_generation=state.requested_generation)
                        .update(requested_generation=run.generation) == 1, 'workspace_generation_changed')
                row.update(run_id=str(run.pk),generation=run.generation)
                result.append(str(run.pk))
        # request_id makes a DB-committed/file-interrupted enqueue safe to resume.
        _save(root,session)
        return result


def map_evaluation_candidates(run, candidates):
    """Rebind verified catalog concepts, never reuse UUIDs from another database."""
    root = assert_evaluation_workspace()
    session, materials, artifact = _checked_session(root)
    require(run.reason == 'evaluation' and run.artifact_id == artifact.pk, 'workspace_artifact_mismatch')
    # The DB transaction can commit before the atomic session-file update. The
    # deterministic intent also authorizes that narrow crash/race recovery window.
    require(any(run.request_id == uuid.uuid5(uuid.UUID(session['session_id']),
            row['image_id']+':'+artifact.manifest_digest) and row['sha256'] == run.photo_sha256
        and row['relative_path'] == run.source_observation.source.relative_path
        and row['run_id'] in (None,str(run.pk)) and row['photo_id'] in (None,str(run.photo_id))
        for row in session['images']), 'workspace_run_mismatch')
    from archive.models import SpeciesConcept
    labels = {r['scientific_name']:r for r in materials['checked']['labels']}
    require(isinstance(candidates,list), 'invalid_result')
    mapped = []
    for candidate in candidates:
        require(isinstance(candidate,dict) and candidate.get('raw_label') in labels, 'workspace_label_mismatch')
        label = labels[candidate['raw_label']]
        require(candidate.get('species_id') == label.get('species_id')
            and candidate.get('mapping_status') == ('mapped' if label.get('species_id') else 'unmapped')
            and candidate.get('mapping_evidence','') == label.get('mapping_evidence',''), 'workspace_label_identity_mismatch')
        item = dict(candidate)
        if label.get('species_id') is not None:
            concept = SpeciesConcept.objects.select_related('species').filter(snapshot_id=session['snapshot'],
                external_id=label['external_id'],species__source_kind='catalog').first()
            require(concept is not None and concept.species.scientific_name == label['scientific_name'] and bool(concept.evidence),
                'workspace_concept_mismatch')
            item.update(species_id=str(concept.species_id),mapping_status='mapped',
                mapping_evidence='Frozen catalog '+materials['meta']['sha256']+' concept '+label['external_id'])
        else:
            item.update(species_id=None,mapping_status='unmapped',mapping_evidence='')
        mapped.append(item)
    return mapped


def export_published_runs():
    root = assert_evaluation_workspace()
    session, materials, artifact = _checked_session(root)
    from archive.models import RecognitionRun, RecognitionState
    from archive.services.recognition import _current, validate_source
    from archive.services.paths import open_root
    images = []
    for saved in session['images']:
        row = dict(saved)
        photo, observation = _bind_image(row)
        exported = dict(image_id=row['image_id'],photo_id=str(photo.pk),sha256=row['sha256'],
            source_observation_id=str(observation.pk),detail_url='/photos/'+str(photo.pk)+'/',
            run_id=None,generation=None,artifact_id=None)
        if session['cohort'] == 'manual':
            require(not RecognitionRun.objects.filter(photo=photo,artifact__isnull=False).exists()
                    and not RecognitionState.objects.filter(photo=photo,active_run__isnull=False).exists(), 'manual_has_candidates')
            from archive.services.paths import open_root, open_regular_beneath, fingerprint
            from archive.services.images import content_hash
            fd = open_root(root/'inbox')
            try:
                with open_regular_beneath(fd,row['relative_path']) as stream:
                    before = fingerprint(os.fstat(stream.fileno()))
                    require(before == observation.fingerprint and content_hash(stream) == (row['sha256'],row['size_bytes'])
                        and fingerprint(os.fstat(stream.fileno())) == before, 'workspace_source_changed')
            finally:
                os.close(fd)
        else:
            require(row['run_id'] is not None, 'workspace_not_enqueued')
            run = RecognitionRun.objects.select_related('photo','artifact','source_observation__source').get(pk=row['run_id'])
            state = RecognitionState.objects.get(photo=photo)
            require(run.state == 'succeeded' and run.reason == 'evaluation' and run.artifact_id == artifact.pk
                and run.photo_id == photo.pk and run.source_observation_id == observation.pk
                and run.generation == row['generation'] and state.active_run_id == run.pk
                and _current(run) and run.candidates.exists(), 'workspace_result_not_current')
            fd = open_root(root/'inbox')
            try:
                validate_source(run,fd)
            finally:
                os.close(fd)
            exported.update(run_id=str(run.pk),generation=run.generation,artifact_id=str(artifact.pk))
        images.append(exported)
    evidence = 'control' if materials['corpus']['evidence_type'] == 'control' else 'model_cpu'
    result = {**_public_session(session),'schema_version':1,'corpus_evidence_type':materials['corpus']['evidence_type'],
        'manifest_digest':digest(materials['corpus']),
        'protocol_digest':digest(materials['protocol']),'model_manifest_digest':artifact.manifest_digest,
        'labels_digest':artifact.manifest['label_digest'],'prompt_digest':artifact.manifest['prompt_digest'],
        'threshold_digest':digest({key:artifact.manifest.get(key) for key in
            ('threshold_version','margin_threshold','calibration_evidence','score_kind')}),
        'runner_digest':artifact.manifest['runner_digest'],'evidence_type':evidence,
        'runner':'manual' if session['cohort'] == 'manual' else ('control' if evidence == 'control' else 'local_model_cpu'),
        'current_unchanged':True,'images':images}
    result['publication_digest'] = digest(result)
    return result


def write_publication(output, value):
    root = assert_evaluation_workspace()
    path = _plain(output)
    require(path.parent == root/'data' and path.name != 'evaluation-session.json', 'invalid_publication_output')
    raw = json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    if path.exists():
        require(read_bytes(path) == raw, 'publication_output_exists')
        return
    fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
