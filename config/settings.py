"""生产默认私有；HTTP 开发只允许显式启用的 loopback。"""
import os
import re
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured


def positive_integer(name, default):
    value = os.environ.get(name, str(default))
    if not re.fullmatch(r'[0-9]+',value) or int(value) <= 0:
        raise ImproperlyConfigured(f'{name} 必须为正整数；请按标明单位填写。')
    return int(value)


WORKER_CONCURRENCY = positive_integer('WORKER_CONCURRENCY',1)
if WORKER_CONCURRENCY != 1:
    raise ImproperlyConfigured('WORKER_CONCURRENCY 仅允许 1。')
MAX_FILE_BYTES = positive_integer('MAX_FILE_BYTES',100*1024*1024)
MAX_IMAGE_PIXELS = positive_integer('MAX_IMAGE_PIXELS',40_000_000)
SCAN_INTERVAL_MS = positive_integer('SCAN_INTERVAL_MS',10000)
STABLE_WINDOW_MS = positive_integer('STABLE_WINDOW_MS',30000)
WORKER_MEMORY_MB = positive_integer('WORKER_MEMORY_MB',1024)  # MiB: 1024**2字节
RETRY_LIMIT = positive_integer('RETRY_LIMIT',3)
RETRY_BASE_MS = positive_integer('RETRY_BASE_MS',1000)
WORKER_JOB_SLEEP_MS = positive_integer('WORKER_JOB_SLEEP_MS',250)
WORKER_NICE = positive_integer('WORKER_NICE',10)
if WORKER_NICE > 19:
    raise ImproperlyConfigured('WORKER_NICE 必须在 1 至 19 之间。')

# 模型预算没有安全的默认值；缺配置保留人工入口，不自动下载模型。
BIRDS_RECOGNITION_ENABLED = os.environ.get('BIRDS_RECOGNITION_ENABLED', '0') == '1'
BIRDS_RECOGNITION_PYTHON = os.environ.get('BIRDS_RECOGNITION_PYTHON', '')
BIRDS_RECOGNITION_MODEL_DIR = os.environ.get('BIRDS_RECOGNITION_MODEL_DIR', '')
RECOGNITION_MEMORY_MB = positive_integer('RECOGNITION_MEMORY_MB', 0) if 'RECOGNITION_MEMORY_MB' in os.environ else None
RECOGNITION_TIMEOUT_SECONDS = positive_integer('RECOGNITION_TIMEOUT_SECONDS', 0) if 'RECOGNITION_TIMEOUT_SECONDS' in os.environ else None
RECOGNITION_INTRA_THREADS = positive_integer('RECOGNITION_INTRA_THREADS', 2)
RECOGNITION_INTER_THREADS = positive_integer('RECOGNITION_INTER_THREADS', 1)
RECOGNITION_NICE = positive_integer('RECOGNITION_NICE', 10)
RECOGNITION_RETRY_LIMIT = positive_integer('RECOGNITION_RETRY_LIMIT', 3)
RECOGNITION_RETRY_BASE_MS = positive_integer('RECOGNITION_RETRY_BASE_MS', 1000)
if RECOGNITION_NICE > 19:
    raise ImproperlyConfigured('RECOGNITION_NICE 必须在 1 至 19 之间。')
if BIRDS_RECOGNITION_ENABLED and not all((BIRDS_RECOGNITION_PYTHON, BIRDS_RECOGNITION_MODEL_DIR,
                                        RECOGNITION_MEMORY_MB, RECOGNITION_TIMEOUT_SECONDS)):
    raise ImproperlyConfigured('BIRDS_RECOGNITION_ENABLED 需要明确解释器、模型目录、内存和超时预算。')

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get("BIRDS_SECRET_KEY", "")
DEBUG = False
BIRDS_LOCAL_HTTP = os.environ.get("BIRDS_LOCAL_HTTP") == "1"
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"] if BIRDS_LOCAL_HTTP else [
    host.strip() for host in os.environ.get("BIRDS_ALLOWED_HOSTS", "").split(",") if host.strip()
]
# 此开关只用于Compose的封闭代理网络；不得给直接暴露的WSGI服务启用。
BIRDS_PROXY_ONLY = os.environ.get('BIRDS_PROXY_ONLY', '0')
if BIRDS_PROXY_ONLY not in ('0', '1'):
    raise ImproperlyConfigured('BIRDS_PROXY_ONLY 仅允许0或1。')
CSRF_TRUSTED_ORIGINS = []
SECURE_PROXY_SSL_HEADER = None
USE_X_FORWARDED_HOST = False
USE_X_FORWARDED_PORT = False
if BIRDS_PROXY_ONLY == '1':
    if BIRDS_LOCAL_HTTP:
        raise ImproperlyConfigured('代理模式禁止本地HTTP。')
    proxy_hosts = [os.environ.get('BIRDS_LAN_HOST', ''), os.environ.get('BIRDS_REMOTE_HOST', '')]
    # 只接受精确DNS名称；不接受通配符、IP、端口、空名称或配置注入。
    dns_name = r'(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}'
    if (len(set(proxy_hosts)) != 2
            or any(not re.fullmatch(dns_name, host) for host in proxy_hosts)
            or len(ALLOWED_HOSTS) != 2 or set(ALLOWED_HOSTS) != set(proxy_hosts)):
        raise ImproperlyConfigured('代理模式需要两个不同精确DNS Host，ALLOWED_HOSTS必须与其一致。')
    CSRF_TRUSTED_ORIGINS = ['https://' + host for host in proxy_hosts]
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
BIRDS_INBOX_ROOT = os.environ.get("BIRDS_INBOX_ROOT", "")
BIRDS_DATA_ROOT = os.environ.get("BIRDS_DATA_ROOT", "")
BIRDS_ARCHIVE_ROOT = os.environ.get("BIRDS_ARCHIVE_ROOT", "")
BIRDS_CLAIM_ROOT = os.environ.get("BIRDS_CLAIM_ROOT", "")
BIRDS_HANDOFF_MODE = os.environ.get("BIRDS_HANDOFF_MODE", "copy_compatible")
BIRDS_PUBLISHED_CONTRACT = os.environ.get("BIRDS_PUBLISHED_CONTRACT") == "1"
BIRDS_SCAN_SECONDS = SCAN_INTERVAL_MS / 1000
BIRDS_STABILITY_SECONDS = STABLE_WINDOW_MS / 1000
INSTALLED_APPS = [
    "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions",
    "django.contrib.staticfiles", "archive.apps.ArchiveConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    # The maintenance lock must cover SessionMiddleware's response-side database save.
    "archive.access.PrivateResponseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True,
              "OPTIONS": {"context_processors": ["django.template.context_processors.request"]}}]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {"default": {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": Path(BIRDS_DATA_ROOT) / "archive.sqlite3" if BIRDS_DATA_ROOT else "",
    "OPTIONS": {"timeout": 5, "transaction_mode": "IMMEDIATE",
                "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;"},
}}
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# WhiteNoise 只读取应用 CSS/JS，绝不读取照片缓存。
WHITENOISE_USE_FINDERS = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
CSRF_FAILURE_VIEW = "archive.views.csrf_failure"
SESSION_COOKIE_SECURE = not BIRDS_LOCAL_HTTP
CSRF_COOKIE_SECURE = not BIRDS_LOCAL_HTTP
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_SSL_REDIRECT = not BIRDS_LOCAL_HTTP
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
TEST_RUNNER = "archive.tests.runner.NonEmptyDiscoverRunner"
LOGGING = {'version':1,'disable_existing_loggers':False,
           'handlers':{'job_console':{'class':'logging.StreamHandler','formatter':'job_message'}},
           'formatters':{'job_message':{'format':'%(message)s'}},
           'loggers':{'archive.jobs':{'handlers':['job_console'],'level':'INFO','propagate':False}}}
