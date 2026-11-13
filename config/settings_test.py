"""测试永远使用受验证的临时根，不继承真实路径或凭据。"""
import os
import tempfile
from pathlib import Path

from .settings import *  # noqa: F403

_shared = os.environ.get("BIRDS_TEST_ROOT")
if _shared:
    _root = Path(_shared)
    _temp_parent = Path(tempfile.gettempdir()).resolve()
    if (_root.is_symlink() or _root.resolve().parent != _temp_parent
            or not _root.name.startswith("birds-tests-")
            or _root.stat().st_uid != os.getuid()
            or (_root / ".birds-test-root").is_symlink()
            or (_root / ".birds-test-root").read_text() != "birds isolated v1"):
        raise RuntimeError("拒绝未经验证的测试根目录")
else:
    _temporary = tempfile.TemporaryDirectory(prefix="birds-tests-")
    _root = Path(_temporary.name).resolve()
    (_root / ".birds-test-root").write_text("birds isolated v1")
BIRDS_TEST_ROOT = _root
BIRDS_TEST_FAULT_STAGE = os.environ.get('BIRDS_TEST_FAULT_STAGE', '')
# 前序隔离复制夹具的0秒快测别名仅在testsettings兼容；生产仍严格正整数毫秒。
if os.environ.get('BIRDS_STABILITY_SECONDS') == '0':
    BIRDS_STABILITY_SECONDS = 0
BIRDS_INBOX_ROOT = _root / "inbox"
BIRDS_DATA_ROOT = _root / "data"
BIRDS_ARCHIVE_ROOT = _root / "archive"
BIRDS_CLAIM_ROOT = _root / "claims"
for _directory in (BIRDS_INBOX_ROOT, BIRDS_DATA_ROOT, BIRDS_ARCHIVE_ROOT, BIRDS_CLAIM_ROOT):
    if _directory.is_symlink():
        raise RuntimeError("拒绝测试子目录链接")
    _directory.mkdir(exist_ok=True)
SECRET_KEY = "isolated-test-key-only-never-production-9g8h1f"
BIRDS_OWNER_USERNAME = "摄影者"
# 旧骨架夹具由测试生产者关闭后发布；复制兼容测试显式覆盖这些参数。
BIRDS_HANDOFF_MODE = os.environ.get("BIRDS_HANDOFF_MODE", "published")
BIRDS_PUBLISHED_CONTRACT = os.environ.get('BIRDS_PUBLISHED_CONTRACT', '1') == '1'
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1", "other.test"]
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_SSL_REDIRECT = False
STATIC_ROOT = _root / "static"
STATIC_ROOT.mkdir(exist_ok=True)
DATABASES = {"default": {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": BIRDS_DATA_ROOT / "test.sqlite3",
    "TEST": {"NAME": BIRDS_DATA_ROOT / "test.sqlite3"},
    "OPTIONS": {"timeout": 5, "transaction_mode": "IMMEDIATE",
                "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;"},
}}
