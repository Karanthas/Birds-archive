import hashlib
import os
import subprocess
import sys
from pathlib import Path
from PIL import Image
from django.conf import settings

def jpeg(path, color=(80, 120, 70), size=(96, 64)):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="JPEG")
    return hashlib.sha256(path.read_bytes()).hexdigest()

def command_options(*args):
    env = os.environ.copy()
    env["BIRDS_TEST_ROOT"] = str(settings.BIRDS_TEST_ROOT)
    env["DJANGO_SETTINGS_MODULE"] = "config.settings_test"
    return {"args": [sys.executable, str(settings.BASE_DIR / "manage.py"), *args,
                     "--settings=config.settings_test"], "env": env, "cwd": settings.BASE_DIR}

def command(*args, timeout=20):
    return subprocess.run(**command_options(*args), capture_output=True, text=True, timeout=timeout)


def disk_hashes(root):
    """Independent byte evidence; do not follow directory or file symlinks."""
    result = {}
    for directory, directories, files in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if not (Path(directory)/name).is_symlink()]
        for name in files:
            path = Path(directory)/name
            if not path.is_symlink():
                result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result
