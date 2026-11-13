# 本地手动归档与恢复

此说明只用于临时目录、合成 JPEG 和隔离 SQLite。本阶段没有接入识别服务，也没有验证真实 NAS、私人照片、LAN 或 Tunnel。应用无账号，打开即可使用；本地示例仅监听 `127.0.0.1`。

## 建立可丢弃的隔离环境

在项目根执行下面的准备命令。沿用已经安装的 `.venv`，不安装依赖。`config.settings_test` 只接受系统临时目录下一层、当前用户拥有、带正确标记且不是符号链接的 `birds-tests-` 根；它强制将数据库、输入、图库及 claim 放到该根中。不要把这个配置用于真实照片或部署。

```sh
export BIRDS_TEST_ROOT="$(.venv/bin/python -c 'import tempfile; from pathlib import Path; p=Path(tempfile.mkdtemp(prefix="birds-tests-")).resolve(); (p/".birds-test-root").write_text("birds isolated v1"); print(p)')"
export DJANGO_SETTINGS_MODULE=config.settings_test
export BIRDS_HANDOFF_MODE=published
export BIRDS_PUBLISHED_CONTRACT=1
.venv/bin/python manage.py migrate --settings=config.settings_test --noinput
.venv/bin/python -c 'from pathlib import Path; from PIL import Image; import os; p=Path(os.environ["BIRDS_TEST_ROOT"])/"inbox"; Image.new("RGB",(240,160),(40,120,80)).save(p/"合成演示.jpg",format="JPEG")'
.venv/bin/python manage.py check --settings=config.settings_test
```

这里的生产者是刚刚结束写入并关闭文件的合成图片命令；`published` 的声明只适用于这个可控示例。目录均为同级独立目录，claim 与输入在同一文件系统，图库根和应用数据根不重叠。后续三个命令必须继承同一 `BIRDS_TEST_ROOT`；测试命令会创建／销毁测试数据库，不要在正在演示的同一根执行 `manage.py test`。

先导入这一张合成图，然后启动网页：

```sh
.venv/bin/python manage.py runworker --once --settings=config.settings_test
.venv/bin/python manage.py runserver 127.0.0.1:8773 --noreload --settings=config.settings_test
```

访问 `http://127.0.0.1:8773/review/`，打开照片，输入并添加本地鸟名，或明确选择“无法确定”，再确认。两种分类都需要明确提交；留空、查看或跳过不会归档。另一个终端先导出准备步骤输出的同一个临时根路径，再执行：

```sh
export BIRDS_TEST_ROOT='/这里替换为准备步骤生成的完整临时根路径'
export BIRDS_HANDOFF_MODE=published BIRDS_PUBLISHED_CONTRACT=1
.venv/bin/python manage.py runworker --once --settings=config.settings_test
```

单次命令最多处理一个任务；更多照片需要继续执行，或在同一环境运行持续 worker：

```sh
.venv/bin/python manage.py runworker --settings=config.settings_test
```

只启动一个持续 worker。第二个会得到 `worker_busy`；不要删除 `worker.lock` 来强行并发。使用 Ctrl-C 停止，等待进程退出后再启动新 worker。连续模式的扫描周期为 `SCAN_INTERVAL_MS`（默认 10000 毫秒）；普通复制稳定窗口为 `STABLE_WINDOW_MS`（默认 30000 毫秒），两项必须为正整数。

## 结果与输入交接

确认成功即进入下一张；后台任务完成之前，相册不展示该照片。相册为默认首页，只含已完成一致性检查的照片，按月份排列，未知日期在末尾。人工名称不捏造学名，多鸟种只有一个受管理原图。目录采用安全化鸟名／年份，未知时间使用“未知年份”；单种原名不冲突时保留，同名冲突分配短后缀，不能覆盖旧文件。多种位于“多鸟种”，明确无法确定位于“无法确定”。下载使用实际归档 basename，原 JPEG 不重新编码。

`copy_compatible` 可用于普通复制导入，但不能证明生产者关闭了写句柄，因此归档会阻塞并保留输入。不要为了消除提示，把尚未遵守契约的生产工具直接声明为 `published`。可靠交接要求同盘临时名写完、关闭所有写句柄后发布最终 JPEG 名，并且发布后不再修改；仅仅 rename 不构成关闭证明。详见 [JPEG 导入交接](input-handoff.md)。

归档日志在首次实际执行前可处于 `unbound`，此时可等待目录配置就绪再绑定一次。进入 `bound` 后固定根路径与设备／inode 身份；换路径、陌生挂载或替换同名目录不会自动重新绑定。已持有 claim 的恢复可在输入目录暂时不可用时继续，仍须验证其他已绑定根和操作持有文件，不能因此放松身份校验。

## 故障与恢复

“已接受确认”不等于文件已归档；任务页和归档状态区区分等待、失败和完成。归档失败时当前下一张的输入、焦点与 URL 不应被状态轮询改写。提交结果未知时保留当前输入、禁止自动重发；先到任务或相册核对。相册加载失败可显式重试，游标失效则刷新相册。

恢复先确认旧 worker 已退出，再以同一根、同一 SQLite 和同一应用版本重启 worker。它根据持久日志对账重放，不依靠心跳超时抢占所有权。中断时 claim／保护副本／发布文件可能暂时共存；保留这些文件和数据库，不手工清理 `.archive-ops`、claim、已发布目标或锁 inode。

| 提示 | 处理方式 |
|---|---|
| 空间不足、权限不足 | 恢复容量或权限后查看任务；重试前仍需通过文件身份、摘要和日志核验。 |
| 目录暂不可用 | 恢复原挂载／原目录身份，再检查任务；不要创建一个同名空目录冒充原根。 |
| 同步失败 | 核查存储和日志；不能因为文件看起来存在就强制完成或盲目重试。 |
| 根身份、来源或文件身份变化 | 停止对相关根的其他写入，保留全部原图与日志，由维护者对账；不能重绑陌生根、替换摘要或删除外来文件。 |
| 交接未就绪 | 核对生产者关闭契约、目录独占权限和配置；没有契约不能启用破坏性归档。 |
| 下载原图不可用 | 保留现存目标和完成记录，核对主文件／绑定根；不把旧预览当原图，不回退任意来源文件。 |

重试入口执行只读校验，不能让不一致的资产重新获得清理权限。已归档照片的改种／改年、回收站和恢复由后续阶段实现，当前不提供手工改数据库的替代操作。

## 本轮验证与部署边界

02-08 完整测试为 284 项通过、0 跳过（49.935 秒），包含 0004 到最新 schema 的真实迁移、匿名 CSRF HTTP、真实 worker、独立 SQLite 与 SHA-256，以及 14 个真实 SIGKILL 窗口及各两次恢复。合成浏览器夹具的桌面／375px 流程有 11 项原记录通过，另有实际预览解码与晚响应导航后不追加卡片两项补验通过。独立 HTTP GET／HEAD 下载的附件名及字节与数据库、清单和磁盘相符。Chrome 扩展环境实际点击下载遭 `ERR_BLOCKED_BY_CLIENT`，尚未证明浏览器保存附件；也不把 HTTP 落盘文件冒称浏览器保存文件。详细证据见阶段 `evidence-08` 与 `02-08-SUMMARY.md`。

本机输入、claim、图库的 `st_dev` 相同，没有两个真实设备的跨盘证据。SIGKILL 不等于硬件断电。Phase 6 仍须验证真实 NAS 双挂载、生产者关闭契约、归档／claim 根独占写权限、目录 fsync／发布原语、容器 UID/GID、Linux 资源限制、LAN／Tunnel 入口和受控断电及备份恢复；这些未验条件不因本地测试通过而解除。原规划保留的 8 个未分类／未解决探针边缘项与 descriptor-less 禁止项继续生效。
