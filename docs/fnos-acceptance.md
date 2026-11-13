# fnOS 正式环境验收清单

06-08 在实际 fnOS 的专用根用同一批合成 JPEG 验收。现有实验根和照片库保持原状。每项初始状态为「未验」，实际证据齐全后才填「通过」或「失败」。缺设备、窗口或权限时登记「未验」及具体原因。

## 集中填写目标配置

从 [环境样例](deployment/06-environment.example.json) 复制一份 `docs/deployment/06-environment.json`。每个 `null` 表示未配置。填实际生产和恢复实例的专用根、卷和文件系统、UID/GID 与 ACL、运行材料的来源及摘要、独立备份位置与容量、已有服务和端口、域名及证书能力、四端版本。凭据栏只记录受限文件位置，真实值保存在部署目录。备份频率、保留期、停机窗口和更新策略按实际约定填写。

在目标 Docker 主机上执行下列命令。配置里的 `compose_file` 可以相对于配置文件所在目录填写。升级时同时填写旧部署的 `deploy_env_file` 和固定新镜像的 `upgrade_env_file`。

```sh
python3 deploy/maintain.py check --config docs/deployment/06-environment.json
python3 deploy/maintain.py status --config docs/deployment/06-environment.json
python3 deploy/maintain.py restart --config docs/deployment/06-environment.json
python3 deploy/maintain.py upgrade --config docs/deployment/06-environment.json
```

先完成 `check`，再按下面清单在约定窗口执行 `restart` 和 `upgrade`。启动前核对四根互不重叠，inbox 与 claim 在同一个可执行 rename 的挂载内，data 在本地盘，备份目录独立且可写。核对固定的镜像、Python、SQLite、依赖树及已选模型离线材料。版本和资源证据可复用早期实测，正式环境只补实际差异。见 [部署说明](fnos.md)、[06-01 隔离环境记录](../.planning/phases/06-fnos/06-01-SUMMARY.md)和 [Phase 3 模型验收](../.planning/phases/03-recognition-batch-review/03-VERIFICATION.md)。

## 一次目标验收

| 项目 | 预期与证据 | 状态 |
| --- | --- | --- |
| 实际部署及缺卷／权限 | 在实际卷新建专用根并显式初始化。记录卷、所有者、权限、ACL、镜像 ID 和服务角色。用隔离配置分别指向缺失或权限不符的挂载，启动明确拒绝，未创建空图库。 | 未验 |
| Windows 批投与人工归档 | 在真实修图电脑多选合成 JPEG，记录源文件忙、传输中断后按原请求重试、回执和自动导入。人工确认前无归档。确认后对照电脑原件、NAS 文件、数据库登记和下载 SHA-256。 | 未验 |
| 局域网入口 | 普通未登记 LAN 设备用浏览器访问独立 HTTPS 地址，直接浏览、预览和下载。记录 Host、CSRF、敏感响应 `no-store` 及原图 HEAD、Range 实际响应。 | 未验 |
| 私网 Tunnel 与旁路 | 记录实际组织、路由、精确目标 TCP 443、远端入口和防火墙证据。无证书的已登记设备被拒。外网无法通过 LAN 入口、后端端口、NAS 其他服务或别名 Host／SNI 绕过。 | 未验 |
| 生命周期 | 复用同批照片及任务基线，记录容器重建、约定窗口内宿主重启、固定新镜像升级前后的服务 ID、维护会话、备份集合及迁移结果。文件执行者始终唯一。照片、个人元数据、文件与待续任务可对账。 | 未验 |
| 实际卷文件故障 | 在专用隔离根验证无覆盖发布与目录同步，复用归档、更正、回收、恢复的关键中断点。记录每次进程中断及后续文件／数据库状态。根据实际挂载判断是否存在跨盘路径，未出现时记录未测跨盘。 | 未验 |
| 硬件断电 | 单独记录真实断电范围、风险边界及恢复证据。共享 NAS 无安全条件时，保留未验与真实照片启用门。 | 未验 |
| 一致备份和在途恢复 | 对同一集合完成备份核验、全新隔离根恢复与激活、单监督者续作和管理对账。记录原实例及备份未变化的证据。具体核对项见下文。 | 未验 |

已验基线包括 NAS 接收端、隔离双 HTTPS 和证书撤销。目标 Windows 投递、正式 Tunnel 与四端接入分别登记实测结果。见 [06-02 接收端回执](../.planning/phases/06-fnos/06-02-NAS-RECEIPT.md)、[06-06 双入口记录](../.planning/phases/06-fnos/06-06-SUMMARY.md)和 [远程接入说明](remote-access.md)。

### 四端外网实测

每端记录系统、浏览器和 One Client 版本，首次选证书行为，以及关闭再打开的日常体验。正向检查直接打开、浏览、预览、下载及 SHA-256。负向检查移除证书或使用已登记但无证书的设备、撤销后的旧连接与新连接。记录另一有效设备仍可访问。端侧 TLS 或代理拒绝、连接关闭及下载摘要分别附证据。

| 客户端 | 首次接入与日常浏览／下载 | 无证书 | 撤销旧连接／新连接 |
| --- | --- | --- | --- |
| Windows | 未验 | 未验 | 未验 |
| macOS | 未验 | 未验 | 未验 |
| Android | 未验 | 未验 | 未验 |
| iPhone | 未验 | 未验 | 未验 |

### 同一集合恢复与对账

沿用 [备份与恢复](backup-restore.md) 的维护屏障和准备流程。使用实际受限环境文件和明确项目名，绑定已核验备份集合的宿主绝对路径。下面两条核验命令分别使用源和恢复实例配置。

```sh
docker compose --project-name "$BIRDS_PROJECT" --env-file "$BIRDS_DEPLOY_ENV" -f compose.yaml \
  --profile maintenance run --rm --no-deps \
  --volume "$BIRDS_BACKUP_SET:/backup/$backup_set_id:ro" maintenance \
  backuparchive verify --set "/backup/$backup_set_id"
docker compose --project-name "$BIRDS_RESTORE_PROJECT" --env-file "$BIRDS_RESTORE_ENV" -f compose.yaml \
  --profile maintenance run --rm --no-deps maintenance restorearchive verify --session "$restore_session_id"
```

在恢复会话中对齐集合 ID、摘要和备份基线。确保原根不挂载到恢复实例。核验照片数量、逐原图 SHA-256、鸟种关联、个人元数据、回收站、资料和离线模型依赖、在途导入／归档／管理／识别状态。激活后完成一次新更正、回收、恢复和下载。记录失败会话、缺件或同名冲突，保留原实例和备份。

## 故障记录与启用判定

目标环境的首次请求前准备关联诊断，复查历史工作台／任务 GET 500 与确认 POST 503。若自然复现，暂停该条旅程，保留同一 `request_id` 的响应和时间、异常栈、原生 SQLite 错误码与监督日志，去密后归档。维护期预期的 503 单独记录。若本次没有自然复现，只填「未复现」。见 [Phase 3 延期诊断](../.planning/debug/p3-workbench-get-500.md)。

接入结果写入 `docs/deployment/06-access-results.md`，恢复与持久性结果写入 `docs/deployment/06-recovery-results.md`，并按 OPS-01、OPS-03、OPS-04 逐项引用。真实卷、重启／升级、恢复或断电证据缺失时，真实照片启用保持未验收。四端按实机结果单列，保留未验项。此前隔离恢复和在途续作证据见 [06-04](../.planning/phases/06-fnos/06-04-SUMMARY.md)与 [06-05](../.planning/phases/06-fnos/06-05-SUMMARY.md)。
