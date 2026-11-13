# 本地运行

目前无需登录，打开即可使用；提供只读补扫、来源版本与重复关联、EXIF／人工地点时间、私有预览、单 worker 恢复和任务重试。尚未连接真实 NAS、Tunnel、模型、云 API 或私人照片。

## 已验证环境与依赖

2026-10-14：macOS 26.5.1 arm64，项目 `.venv` 由 Codex 本地运行时的 Python 3.12.14 建立，实际 sqlite3 3.53.1。

从 Git 获取源码后，请从各自的存储位置提供 Docker 离线材料和识别模型。历史 NAS 识别报告复算及安装清单核验还需本地原始证据，验收记录中的内部路径仅供原工作区对账。

实际 `SELECT sqlite_source_id()` 为 `2026-05-05 10:34:17 c88b22011a54b4f6fbd149e9f8e4de77658ce58143a1af0e3785e4e6475127e9`，与 [SQLite 3.53.1 官方发行标识](https://sqlite.org/releaselog/3_53_1.html) 完整一致。该发行说明继承 WAL-reset 修复；[官方 WAL 说明](https://sqlite.org/wal.html#walreset) 明确 3.51.3 及以后已修复。不能以系统 Python 3.9.6／SQLite 3.51.0 代替这个运行环境。

四项基础依赖为 Django 5.2.18、Pillow 12.3.0、Gunicorn 26.2.0、WhiteNoise 6.12.0；必要传递依赖 asgiref 3.12.1、sqlparse 0.6.0。安装前从各版本的 `https://pypi.org/pypi/<包>/<版本>/json` 核对官方仓库和 wheel SHA-256：Django→django/django，Pillow→python-pillow/Pillow，Gunicorn→benoitc/gunicorn，WhiteNoise→evansd/whitenoise，asgiref→django/asgiref，sqlparse→andialbrecht/sqlparse。官方版本元数据与实际哈希安装均已核对；未以未取得的下载量编造审计结论。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip --isolated install --require-hashes -r requirements.lock
.venv/bin/python -m pip check
.venv/bin/python -c 'import sqlite3; print(sqlite3.connect(":memory:").execute("select sqlite_version(),sqlite_source_id()").fetchone())'
```

lock 只允许经核验的 CPython 3.12 macOS／Linux wheels；其他 Python 小版本或操作系统需先核对新增发行哈希。没有安装识别、测试、lint 或类型工具。版本文档依据：[Django SQLite](https://docs.djangoproject.com/en/5.2/ref/databases/)、[Pillow Image](https://pillow.readthedocs.io/en/stable/reference/Image.html)、[WhiteNoise](https://whitenoise.readthedocs.io/en/stable/django.html)。Context7 MCP 与 ctx7 CLI 本环境不可用，已使用官方文档回退。

## 隔离本地演练

以下路径只含合成 JPEG；切勿填真实图库。HTTP 开发只允许 loopback Host；生产默认要求 HTTPS 和 secure cookies。

```sh
export BIRDS_DEMO_ROOT="$(mktemp -d /tmp/birds-local-XXXXXX)"
mkdir "$BIRDS_DEMO_ROOT/inbox" "$BIRDS_DEMO_ROOT/data"
export BIRDS_INBOX_ROOT="$BIRDS_DEMO_ROOT/inbox"
export BIRDS_DATA_ROOT="$BIRDS_DEMO_ROOT/data"
export BIRDS_SECRET_KEY="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export BIRDS_LOCAL_HTTP=1
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py runserver 127.0.0.1:8000 --noreload
```

另一个终端设置同一组根目录与 secret；启动唯一后台执行者：

导入根与数据根必须为互相独立的绝对目录：不能相同，也不能把一根放在另一根内。程序核对规范路径与实际目录 FD 身份；预检、worker 和直接补扫均拒绝无法证明隔离的配置，返回 `roots_not_isolated`，不自动移动或改写用户目录。递归扫描也检查实际子目录 FD，拒绝应用数据根的目录别名，避免把派生 JPEG 再次导入。

```sh
.venv/bin/python manage.py runworker --once
# 常驻本地演练：
.venv/bin/python manage.py runworker
```

默认 `copy_compatible` 以至少两次观察、10秒扫描和30秒稳定窗口发现普通复制输入；长暂停不能证明已写完。可靠交接需显式设置 `BIRDS_HANDOFF_MODE=published` 和 `BIRDS_PUBLISHED_CONTRACT=1`，并遵守关闭全部写句柄后发布且不再改写的约定。worker 不移动、不删除、不写入源文件；来源变化产生版本，旧任务不能提交。

每张照片在解码前按配置检查字节和像素，拒绝截断、非 JPEG、炸弹、链接与特殊文件。缓存仅生成最长边480px／1600px的派生JPEG，路径绑定Job UUID、SHA-256和配方v2。SQLite使用短IMMEDIATE事务、5秒busy timeout、WAL、FULL、外键；所有进程共享数据目录的同一 `worker.lock` inode，运行中禁止删锁。获锁后才恢复遗留任务；心跳变旧从不允许接管。SIGTERM停止领取，完成当前工作后释放锁。

## 资源与诊断

以下环境参数严格接受正整数；小于1毫秒、负数、NaN及格式错误拒绝启动。`WORKER_MEMORY_MB` 实际单位是MiB。旧的秒单位环境参数由下表替代。

| 参数 | 默认值 | 单位／边界 |
|---|---:|---|
| WORKER_CONCURRENCY | 1 | 仅允许1 |
| MAX_FILE_BYTES | 104857600 | 字节 |
| MAX_IMAGE_PIXELS | 40000000 | 像素 |
| SCAN_INTERVAL_MS | 10000 | 毫秒 |
| STABLE_WINDOW_MS | 30000 | 毫秒 |
| WORKER_MEMORY_MB | 1024 | MiB，转换为1024²字节 |
| WORKER_NICE | 10 | 1–19，降低调度优先级 |
| WORKER_JOB_SLEEP_MS | 250 | 毫秒，串行任务间休眠 |
| RETRY_LIMIT | 3 | 自动尝试总数 |
| RETRY_BASE_MS | 1000 | 毫秒，1／2／4指数退避基数 |

迁移后运行 `.venv/bin/python manage.py checkruntime`。它检查实际Python／SQLite、已核查发行source-ID、WAL／FULL／外键、根可用性与隔离关系（`roots_isolated`）、已登记导入根及数据根的内核挂载类型；输出不含绝对路径、环境、秘密或原始异常。未知挂载类型不会标为本地，预检失败返回非零。诊断不创建照片或任务，也不代替实际NAS挂载验证。

Linux worker启动后用标准库 `resource.RLIMIT_AS` 限制地址空间，并读取实际限额；配合nice和串行任务间休眠。地址空间限额不是RSS预算，也不是CPU百分比限制；实际容器部署还应设置内存／CPU上限（部署阶段配置）。macOS明确报告 `unsupported_unverified`，不套用Linux内存语义；本次受限运行环境若nice调用被拒绝，报告 `permission_denied_unverified`。RSS以字节报告当前进程峰值和测量条件，不用它推断NAS能力。[Python资源文档](https://docs.python.org/3.12/library/resource.html)、[os.nice](https://docs.python.org/3.12/library/os.html#os.nice)。

2026-10-14的macOS隔离测试实际通过进程互斥、四边界强杀恢复及字节／像素限制。未发现Docker、Podman、nerdctl或合格Linux运行器；没有Linux地址空间拒绝行为／NAS峰值的实测证据，没有安装容器／VM工具。真实fnOS资源限制仍待部署阶段验收。

瞬时I/O或SQLite busy只做有限自动重试；损坏、权限、丢源和空间不足持久失败并给出修复条件。任务成功后照片仍待确认。人工重试复用同一Job、保留累计尝试编号，不自动移动原图。成功、重复、旧版本或无效版本重试均重定向到可 GET 的任务页，保留有效筛选／页码；单次提示不在刷新时重放提交。

Web 查询数据库并通过受控媒体视图读取派生缓存，无账号、密码或登录步骤；CSRF、Host和no-store仍生效。`/media/<UUID>/<thumb|preview>/` 不是公开静态路由；没有原图、注册、归档或回收站端点。`config.wsgi:application` 为部署钩子，后续实际 Linux／fnOS／挂载与 TLS 仍需验证。生产须配置 `BIRDS_SECRET_KEY`、`BIRDS_ALLOWED_HOSTS` 和隔离的绝对根；不能将 `BIRDS_LOCAL_HTTP=1` 用于公网或 NAS LAN。能到达配置入口的请求可直接使用应用；真实LAN／Tunnel的访问范围、反向代理和TLS由OPS-03部署阶段验证，不以本地结果代替，也不代表公网私有入口已完成。

## 测试

```sh
.venv/bin/python manage.py test archive.tests.test_walkthrough --settings=config.settings_test --noinput
.venv/bin/python manage.py test archive.tests.test_auth --settings=config.settings_test --noinput
.venv/bin/python manage.py test archive.tests --settings=config.settings_test --noinput
.venv/bin/python manage.py check --settings=config.settings_test
.venv/bin/python manage.py makemigrations --check --dry-run --settings=config.settings_test
.venv/bin/python manage.py migrate --settings=config.settings_test --noinput
.venv/bin/python -m compileall -q config archive
```

`settings_test` 忽略真实路径与 secret，只使用 `TemporaryDirectory` 磁盘库。测试子进程显式共享 `BIRDS_TEST_ROOT`；只接受系统临时目录内带受控标记且属于当前 uid 的 `birds-tests-*` 根。NonEmptyDiscoverRunner 拒绝零测试，并使意外跳过返回非零；子进程有超时，无 NAS 访问。compileall 只验证语法，不代表 lint／类型检查。本次无登录收集数、耗时与证据见 `.planning/quick/261015-isf-remove-application-login-for-personal-us/261015-isf-SUMMARY.md`；原Phase 1验证见 `01-REVIEW-FIX.md`；前序计划摘要保留当时证据。
