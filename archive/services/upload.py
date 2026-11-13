"""NAS受控生产者：关闭写句柄后发布；JSON意图与inode共同证明归属。"""
import fcntl
import hashlib
import json
import os
import re
import stat
import unicodedata
import uuid
from contextlib import ExitStack, contextmanager

from django.conf import settings

from archive.models import SourceObservation
from archive.services.archiving import _directory
from archive.services.images import ImageRejected, content_hash, inspect_open_jpeg
from archive.services.paths import (UnsafePath, fingerprint, open_regular_beneath,
                                    open_root, root_identity, validate_root_isolation,
                                    verify_root_identity)

BLOCK_BYTES = 64 * 1024
MESSAGES = {
    'metadata_invalid': '投递信息无效，请核对JPEG名称、长度与摘要。',
    'bytes_exceeded': '文件超过允许的字节上限。',
    'length_mismatch': '接收长度与声明不符，文件未发布。',
    'digest_mismatch': '接收摘要不一致，文件未发布。',
    'image_invalid': '文件不是可完整解码的JPEG，或超过像素上限。',
    'stream_interrupted': '传输中断，文件未发布，请查询回执。',
    'request_conflict': '同一请求编号已用于另一文件，请保留原回执。',
    'request_busy': '此请求仍在接收，请稍后查询原回执。',
    'ownership_conflict': '文件归属存在冲突，现有文件均已保留，请核对。',
    'publishing_uncertain': '上次发布是否已被处理无法确认，暂存证据已保留，请核对；不会再次发布。',
    'interrupted_unprepared': '上次传输未完成，暂存证据已保留；请核对后另行投递。',
    'storage_unavailable': '投递存储暂不可用，请恢复原目录后查询原回执。',
    'receipt_missing': '没有找到此请求回执。',
}


class UploadError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def metadata(request_id, filename, length, sha256):
    try:
        canonical = str(uuid.UUID(str(request_id)))
        if canonical != str(request_id).lower():
            raise ValueError()
        if (not isinstance(filename, str) or not filename or filename in ('.', '..')
                or '/' in filename or '\\' in filename
                or any(unicodedata.category(c).startswith('C') for c in filename)
                or not filename.lower().endswith(('.jpg', '.jpeg'))
                or len(filename.encode('utf-8')) > 180):
            raise ValueError()
        if isinstance(length, bool) or not re.fullmatch(r'[1-9][0-9]{0,14}', str(length)):
            raise ValueError()
        if not isinstance(sha256, str) or not re.fullmatch('[0-9a-f]{64}', sha256):
            raise ValueError()
        result = {'request_id': canonical, 'filename': filename, 'length': int(length), 'sha256': sha256}
    except (ValueError, TypeError, UnicodeError):
        raise UploadError('metadata_invalid', 400) from None
    if result['length'] > settings.MAX_FILE_BYTES:
        raise UploadError('bytes_exceeded', 413)
    return result


def fault_hook(stage):
    """测试可在持久边界中断；生产无副作用。"""


def _identity_matches(first, second):
    # link/unlink改变ctime；归属依赖inode、长度、mtime及独立摘要。
    return all(first.get(key) == second.get(key) for key in ('dev', 'ino', 'size', 'mtime_ns'))


@contextmanager
def _storage():
    with ExitStack() as stack:
        roots = {}
        for name in ('inbox', 'data', 'archive', 'claim'):
            fd = open_root(getattr(settings, 'BIRDS_'+name.upper()+'_ROOT'))
            stack.callback(os.close, fd)
            roots[name] = fd
        for index, name in enumerate(roots):
            for other in list(roots)[index+1:]:
                validate_root_isolation(roots[name], roots[other],
                    getattr(settings, 'BIRDS_'+name.upper()+'_ROOT'),
                    getattr(settings, 'BIRDS_'+other.upper()+'_ROOT'))
        verify_root_identity(roots['inbox'], settings.BIRDS_INBOX_ROOT, roots['data'])
        for name, parent, child in (('records', 'data', 'uploads'), ('staging', 'inbox', '.delivery')):
            fd = _directory(roots[parent], child)
            stack.callback(os.close, fd)
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise UploadError('ownership_conflict')
            roots[name] = fd
        yield roots


def _recheck(roots, intent=None):
    for name in ('inbox', 'data', 'archive', 'claim'):
        current = open_root(getattr(settings, 'BIRDS_'+name.upper()+'_ROOT'))
        try:
            if root_identity(current) != root_identity(roots[name]):
                raise UploadError('ownership_conflict')
        finally:
            os.close(current)
    for name, parent, child in (('records', 'data', 'uploads'), ('staging', 'inbox', '.delivery')):
        current = os.open(child, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                          dir_fd=roots[parent])
        try:
            if root_identity(current) != root_identity(roots[name]):
                raise UploadError('ownership_conflict')
        finally:
            os.close(current)
    identities = {name: root_identity(fd) for name, fd in roots.items()}
    if intent is not None and identities != intent['roots'] and not _restored_terminal_receipt(roots, intent):
        raise UploadError('ownership_conflict')
    return identities


def _restored_terminal_receipt(roots, intent):
    # Only an already terminal, exactly bound receipt can use its historical roots.
    # Pending publications still require their original recovery protocol.
    if intent['state'] not in ('complete', 'failed'):
        return False
    from archive.services.restore import RestoreError, effective_path_binding
    name = intent['request']['request_id'] + '.json'
    relative = 'uploads/' + name
    try:
        binding = effective_path_binding('data', relative)
    except RestoreError:
        return False
    if binding is None or binding.target_relative_path != relative:
        return False
    with open_regular_beneath(roots['records'], name) as stream:
        before = fingerprint(os.fstat(stream.fileno()))
        if before != binding.target_fingerprint or before['size'] != binding.byte_size:
            return False
        digest, size = content_hash(stream)
        return ((digest, size) == (binding.sha256, binding.byte_size)
                and before == fingerprint(os.fstat(stream.fileno())))


@contextmanager
def _lock(records, request_id):
    fd = os.open(request_id+'.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=records)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UploadError('ownership_conflict')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UploadError('request_busy') from None
        yield
    finally:
        os.close(fd)


def _load(records, request_id):
    try:
        with open_regular_beneath(records, request_id+'.json') as stream:
            intent = json.loads(stream.read(65537))
    except FileNotFoundError:
        return None
    if (not isinstance(intent, dict) or intent.get('version') != 1
            or intent.get('request', {}).get('request_id') != request_id
            or intent.get('state') not in ('receiving', 'prepared', 'publishing', 'complete', 'failed')):
        raise UploadError('ownership_conflict')
    return intent


def _save(records, intent):
    temporary = '.receipt-'+uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=records)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(json.dumps(intent, ensure_ascii=False).encode('utf-8'))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, intent['request']['request_id']+'.json', src_dir_fd=records, dst_dir_fd=records)
    os.fsync(records)


def _copy_checked(stream, request, destination=None):
    digest, total = hashlib.sha256(), 0
    while True:
        try:
            block = stream.read(min(BLOCK_BYTES, request['length'] - total + 1))
        except OSError:
            raise UploadError('stream_interrupted', 422) from None
        if not block:
            break
        total += len(block)
        if total > settings.MAX_FILE_BYTES:
            raise UploadError('bytes_exceeded', 413)
        if total > request['length']:
            raise UploadError('length_mismatch', 422)
        digest.update(block)
        if destination is not None:
            destination.write(block)
    if total != request['length']:
        raise UploadError('length_mismatch', 422)
    if digest.hexdigest() != request['sha256']:
        raise UploadError('digest_mismatch', 422)


def _owned(roots, directory, name, intent):
    with open_regular_beneath(roots[directory], name) as stream:
        before = fingerprint(os.fstat(stream.fileno()))
        if not _identity_matches(before, intent['fingerprint']):
            raise UploadError('ownership_conflict')
        digest, size = content_hash(stream)
        if (digest != intent['request']['sha256'] or size != intent['request']['length']
                or before != fingerprint(os.fstat(stream.fileno()))):
            raise UploadError('ownership_conflict')


def _receipt(intent, code=''):
    request = intent['request']
    return {'version': 1, **request, 'status': 'failed' if code else 'complete',
            'code': code or 'received',
            'message': MESSAGES[code] if code else 'NAS已完整接收，等待后台处理与人工确认。',
            'receipt_url': '/delivery/receipts/'+request['request_id']+'/',
            **({'target': intent['target']} if not code else {})}


def _cleanup_anchor(roots, intent):
    try:
        _owned(roots, 'staging', intent['temporary'], intent)
    except FileNotFoundError:
        return
    os.unlink(intent['temporary'], dir_fd=roots['staging'])
    os.fsync(roots['staging'])


def _cleanup_failed(roots, intent):
    """终态失败只删除本次O_EXCL写句柄最后核验的对象，未知证据保留。"""
    expected = intent.get('temporary_fingerprint')
    if expected is None:
        return
    try:
        with open_regular_beneath(roots['staging'], intent['temporary']) as stream:
            if fingerprint(os.fstat(stream.fileno())) != expected:
                raise UploadError('ownership_conflict')
    except FileNotFoundError:
        return
    os.unlink(intent['temporary'], dir_fd=roots['staging'])
    os.fsync(roots['staging'])


@contextmanager
def scan_guard(inbox_fd, data_fd, relative):
    """仅接收器UUID命名空间受此门保护；持锁到observe_file完成。"""
    match = re.match(r'^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})--[^/]+$', relative)
    if match is None:
        yield True
        return
    with ExitStack() as stack:
        try:
            roots = stack.enter_context(_storage())
            if (root_identity(roots['inbox']) != root_identity(inbox_fd)
                    or root_identity(roots['data']) != root_identity(data_fd)):
                raise UploadError('ownership_conflict')
            stack.enter_context(_lock(roots['records'], match[1]))
            intent = _load(roots['records'], match[1])
            if intent is None or intent['target'] != relative:
                ready = True  # 旧直接输入，没有本接收器意图。
            elif intent['state'] == 'complete':
                _recheck(roots, intent)
                try:
                    os.stat(intent['temporary'], dir_fd=roots['staging'], follow_symlinks=False)
                except FileNotFoundError:
                    ready = True
                else:
                    ready = False  # 回执已落盘但anchor未清，仍不得登记ctime。
            else:
                ready = False
        except (UploadError, OSError, UnsafePath, ValueError, KeyError, TypeError):
            ready = False
        # 不捕获调用方observe_file的异常；其诊断由既有补扫处理。
        yield ready


def _finish(roots, intent):
    intent.update(state='complete', receipt=_receipt(intent), http_status=201)
    _save(roots['records'], intent)
    fault_hook('receipt_saved')
    # 只清理本请求已核属的anchor，不清理未知暂存物。
    _cleanup_anchor(roots, intent)


def _recover_publishing(roots, intent):
    """发布后的输入可被唯一worker消费；不从anchor重新发布。"""
    _recheck(roots, intent)
    try:
        _owned(roots, 'inbox', intent['target'], intent)
    except FileNotFoundError:
        observations = SourceObservation.objects.filter(source__root_key='inbox',
            source__relative_path=intent['target'], sha256=intent['request']['sha256'],
            byte_size=intent['request']['length'])
        if not any(_identity_matches(observation.fingerprint, intent['fingerprint'])
                   for observation in observations):
            raise UploadError('publishing_uncertain') from None
        # 固定请求路径和实际扫描inode共同证明此前已发布，SHA单独不足以认领。
    else:
        os.fsync(roots['inbox'])
    _finish(roots, intent)


def _publish(roots, intent):
    _recheck(roots, intent)
    _owned(roots, 'staging', intent['temporary'], intent)
    # 此边界之后任何恢复都不得再次link：target可能已被下游扫描／归档移走。
    intent['state'] = 'publishing'
    _save(roots['records'], intent)
    fault_hook('publishing')
    try:
        os.link(intent['temporary'], intent['target'], src_dir_fd=roots['staging'],
                dst_dir_fd=roots['inbox'], follow_symlinks=False)
    except FileExistsError:
        _owned(roots, 'inbox', intent['target'], intent)
    os.fsync(roots['inbox'])
    fault_hook('published')
    _recover_publishing(roots, intent)


def receive(stream, *, request_id, filename, length, sha256):
    request = metadata(request_id, filename, length, sha256)
    try:
        with _storage() as roots, _lock(roots['records'], request['request_id']):
            intent = _load(roots['records'], request['request_id'])
            if intent is not None:
                if intent['request'] != request:
                    raise UploadError('request_conflict')
                _copy_checked(stream, request)
                _recheck(roots, intent)
                if intent['state'] in ('complete', 'failed'):
                    if intent['state'] == 'complete':
                        _cleanup_anchor(roots, intent)
                    else:
                        _cleanup_failed(roots, intent)
                    return intent['receipt'], 200 if intent['state'] == 'complete' else intent['http_status']
                if intent['state'] == 'receiving':
                    raise UploadError('interrupted_unprepared')
                if intent['state'] == 'publishing':
                    _recover_publishing(roots, intent)
                else:
                    _publish(roots, intent)
                return intent['receipt'], 200
            intent = {'version': 1, 'state': 'receiving', 'request': request,
                      'target': request['request_id']+'--'+filename,
                      'temporary': request['request_id']+'.partial', 'roots': _recheck(roots)}
            _save(roots['records'], intent)
            try:
                fd = os.open(intent['temporary'], os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=roots['staging'])
                with os.fdopen(fd, 'wb') as destination:
                    try:
                        _copy_checked(stream, request, destination)
                        destination.flush()
                        os.fsync(destination.fileno())
                    finally:
                        destination.flush()
                        intent['temporary_fingerprint'] = fingerprint(os.fstat(destination.fileno()))
                fault_hook('received')
                # 此时全部写句柄已关闭；只读同一暂存对象进行独立解码与摘要复核。
                with open_regular_beneath(roots['staging'], intent['temporary']) as source:
                    before = fingerprint(os.fstat(source.fileno()))
                    image, _ = inspect_open_jpeg(source)
                    image.close()
                    digest, size = content_hash(source)
                    if (digest != request['sha256'] or size != request['length']
                            or fingerprint(os.fstat(source.fileno())) != before):
                        raise UploadError('ownership_conflict')
                    intent['fingerprint'] = before
                os.fsync(roots['staging'])
                intent['state'] = 'prepared'
                _save(roots['records'], intent)
                fault_hook('prepared')
                _publish(roots, intent)
                return intent['receipt'], 201
            except ImageRejected:
                error = UploadError('image_invalid', 422)
            except UploadError as caught:
                error = caught
            # 发布／存储故障保留prepared或publishing意图；验证失败才持久失败回执。
            if intent['state'] == 'receiving':
                intent.update(state='failed', receipt=_receipt(intent, error.code), http_status=error.status)
                _save(roots['records'], intent)
                error.receipt = intent['receipt']
                fault_hook('failed_saved')
                _cleanup_failed(roots, intent)
            raise error
    except UploadError:
        raise
    except (OSError, UnsafePath, ValueError, KeyError, TypeError):
        raise UploadError('storage_unavailable', 409) from None


def receipt(request_id):
    """不发布JPEG、不归档；成功查询可清理已核属anchor，未完成需原请求对账。"""
    try:
        with _storage() as roots, _lock(roots['records'], str(request_id)):
            intent = _load(roots['records'], str(request_id))
            if intent is None:
                raise UploadError('receipt_missing', 404)
            _recheck(roots, intent)
            if intent['state'] in ('complete', 'failed'):
                if intent['state'] == 'complete':
                    _cleanup_anchor(roots, intent)
                else:
                    _cleanup_failed(roots, intent)
                return intent['receipt'], 200
            return {'request_id': str(request_id), 'status': 'pending',
                    'code': 'reconciliation_required', 'message': '投递尚未完成，请沿用原请求编号重试。'}, 202
    except UploadError:
        raise
    except (OSError, UnsafePath, ValueError, KeyError, TypeError):
        raise UploadError('storage_unavailable', 409) from None
