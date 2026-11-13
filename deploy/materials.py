"""在导入第三方包前，用标准库核对离线材料的精确字节集合。"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat


def verify_materials(root, manifest_path, expected):
    try:
        root, manifest_path = Path(root), Path(manifest_path)
        if (not re.fullmatch('[a-f0-9]{64}', expected) or root.is_symlink()
                or root.resolve(strict=True) != root or manifest_path.is_symlink()):
            raise ValueError
        rows = json.loads(manifest_path.read_text())
        if not isinstance(rows, list) or not rows:
            raise ValueError
        paths = set()
        directories = set()
        for row in rows:
            if set(row) != {'path', 'bytes', 'sha256'}:
                raise ValueError
            path = PurePosixPath(row['path'])
            if (path.is_absolute() or '..' in path.parts or '.' in path.parts or not path.parts
                    or str(path) != row['path'] or row['path'] in paths
                    or type(row['bytes']) is not int or row['bytes'] < 0
                    or not re.fullmatch('[a-f0-9]{64}', row['sha256'])):
                raise ValueError
            paths.add(row['path'])
            directories.update(str(parent) for parent in path.parents if str(parent) != '.')
        if [row['path'] for row in rows] != sorted(paths, key=PurePosixPath):
            raise ValueError
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if digest != expected:
            raise ValueError
        actual_files, actual_directories = set(), set()
        for path in root.rglob('*'):
            mode = path.lstat().st_mode
            relative = path.relative_to(root).as_posix()
            if stat.S_ISDIR(mode):
                actual_directories.add(relative)
            elif stat.S_ISREG(mode):
                actual_files.add(relative)
            else:
                raise ValueError
        if actual_files != paths or actual_directories != directories:
            raise ValueError
        for row in rows:
            path = root / row['path']
            if path.stat().st_size != row['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
                raise ValueError
        return digest
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError('deployment_python_materials_invalid') from None


if __name__ == '__main__':
    try:
        verify_materials('/opt/birds-python', '/opt/birds-python-manifest.json',
                         os.environ.get('BIRDS_PYTHON_TREE_SHA', ''))
    except ValueError as error:
        print(str(error))
        raise SystemExit(2) from None
    else:
        print('python_materials_verified')
