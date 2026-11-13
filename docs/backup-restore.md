# 备份与恢复

本应用的可恢复备份必须在维护屏障内创建。备份集合以最后落盘的 `COMPLETE.json` 为唯一完成标志；没有该文件的目录是失败证据，不得用于恢复，也不要原地续写。

## 创建和校验

先在宿主创建一个独立于四个源根的专用目录，并确认它已存在、不是符号链接且权限只授予部署 UID/GID。沿用 `docs/fnos.md` 中已核验的 `birds_compose` 包装器，不覆盖 entrypoint：

```sh
test -d "$BIRDS_BACKUP_HOST" && test ! -L "$BIRDS_BACKUP_HOST"
birds_compose run --rm --no-deps --volume "$BIRDS_BACKUP_HOST:/backup" web backuparchive request
birds_compose run --rm --no-deps --volume "$BIRDS_BACKUP_HOST:/backup" web backuparchive status
birds_compose run --rm --no-deps --volume "$BIRDS_BACKUP_HOST:/backup" web backuparchive create --session "$maintenance_session_id" --destination /backup
birds_compose run --rm --no-deps --volume "$BIRDS_BACKUP_HOST:/backup:ro" web backuparchive verify --set "/backup/$backup_set_id"
birds_compose run --rm --no-deps --volume "$BIRDS_BACKUP_HOST:/backup:ro" web backuparchive resume --session "$maintenance_session_id"
```

`create` 要求 worker 监督器已为同一 session 写入 `drained` ACK；整个 SQLite backup、四个存储根复制、模型材料验证和完成标志发布过程都持有维护锁及 worker 锁。目的目录必须已存在，且不能是 inbox、data、archive、claim 或它们的父目录、子目录、容器 bind 别名。失败集合保留但没有 `COMPLETE.json`；排除故障后在同一维护 session 中重新执行 `create`，它会使用新的集合 UUID。

部署入口只允许显式的 `web`、`worker`、`init`、`offline`、`backuparchive`、`restorearchive` 模式。`offline` 内仅允许 `importcatalog`、`loadguidebundle`、`preparemodel` 和升级 `migrate`；其中准备词表的文件子进程也属于写者。禁止用 `docker exec`、覆盖 entrypoint 或裸 `python manage.py ...` 绕过同一维护锁。若包装器崩溃并留下未闭合 action，只能查看 `status` 和诊断；不得手工清标记、强制备份或恢复。具有 Docker 管理权限的人仍可绕过这一信任边界；一旦发生，当前备份证据作废，必须重新停写并创建新集合。

集合包含 SQLite 一致快照、四根的文件与目录清单、硬链接关系、当前模型和在途识别引用的离线材料，以及非敏感运行身份。锁、维护状态和运行中的 SQLite WAL/SHM 不进入集合。manifest 对数据库私密字段只保存规范化逻辑摘要与行数，不复制备注正文。运行身份不保存环境变量值、密钥或下载 URL。

只有 `create` 返回 `{"status":"complete","set":"/backup/<id>"}` 且独立 `verify` 返回 `{"status":"verified",...}` 后，才执行 `resume`。不要在校验前恢复 Web/worker。

本工具不会自动删除旧集合、失败集合或回收站内容。保留数量和调度周期由部署者另行决定；删除前应至少完成一次隔离恢复演练。

## 恢复到新的隔离目录

为恢复使用独立的 Compose 项目、部署 ID 和空的四个目录。按 `docs/fnos.md` 的目录约定设置 UID/GID 和归属标记。将 `BIRDS_ROOTS_HOST` 指向新的专用父目录，由 `restorearchive` 初始化副本。备份集合只读挂载，Web 和 worker 不需要挂载它。

```sh
birds_compose run --rm --no-deps --volume "$BIRDS_BACKUP_SET:/backup/$backup_set_id:ro" web restorearchive prepare --backup-set "/backup/$backup_set_id" --session "$restore_session_id" --inbox-root /roots/inbox --data-root /roots/data --archive-root /roots/archive --claim-root /roots/claim
birds_compose run --rm --no-deps web restorearchive verify --session "$restore_session_id"
birds_compose run --rm --no-deps web restorearchive activate --session "$restore_session_id"
birds_compose up -d --no-build --pull never web worker
```

`restore_session_id` 为本次恢复的 UUID。中断后沿用同一会话和同一集合重试。若遇到归属冲突，请保留诊断并使用另一组新的隔离目录。先用 `verify` 核对目标文件、链接和任务依赖，等 `activate` 发布新部署登记后再启动 Web 和单个 worker 监督者。运行中的恢复实例仅挂载新根。

依赖齐全的待导入、首次归档、归档后更正、回收、从回收站恢复和在途识别可以接续。离线模型材料随备份复制到新 data 根，保留历史模型摘要及识别任务身份。核验遇到缺失依赖或未知任务阶段时会报错，保留失败会话供对账。仍在接收或发布中的上传须在备份前处理到结束状态。

启动后从工作台查看待办是否继续，核对同一张照片的归档或回收站状态，下载原图并对照备份的 SHA-256。通过照片页提交一项新的管理操作，核对操作回执和新根中的文件位置。

当前流程覆盖从原始部署的备份恢复到新根。以已激活的恢复实例为来源再次备份并恢复，需要补齐连续恢复支持。

保留备份集合、对应镜像和源码材料、模型离线材料及部署参数模板。密钥和 Tunnel 凭据从受限的部署配置重新提供。
