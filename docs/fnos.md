# fnOS 专用根与显式启动

当前是 Phase 6 的部署骨架，生产路径、HTTPS／Tunnel、可靠上传、维护备份及跨根恢复仍按后续计划配置。2026-10-28已在隔离fnOS Docker实际通过同照片归档／下载／重建，以及本页新根初始化／六个拒绝方向／正常启动／静态文件／进程资源／重建核验。应用无账号，入口范围由部署网络控制。

单图业务证据是38b4130c-continuation-final-result.json。根协议由roots-protocol-ccf9fa295d844fc2884be1c78c647588.json/.jsonl的实际已过步骤与ccf9fa-roots-tail-result.json联合闭合；原总run因CSS事件输出参数冲突仍保持FAIL，不改写成PASS。尾段57.582s、SSH退出0，前后各唯一监督者／文件worker，实际合计2CPU／2GiB，真正更换容器ID，数据库／登记不变，正常checkruntime两次verified且errors=[]，旧控制socket／staticfiles警告消失，最后无本项目容器残留。CSS复用原实际200／10467B／SHA df55257f…67006a，未重复GET或照片业务旅程。

真实NFS／ACL负例、实际升级迁移、硬件断电、生产NAS路径及Tunnel仍未验收；macOS合成mountinfo／ACL分支测试不能替代这些证据。识别沿用已有材料与旧评测，本计划没有运行模型、安装新包或重跑整套测试。

## 配置采集

从 `deploy/env.example` 生成本次受限环境文件，不放密码／密钥进命令、日志或源码包。以下值必须来自这次专用目录与实际授权，不能照抄旧评测根：

| 项目 | 必填内容与约束 |
|---|---|
| `BIRDS_PROJECT`、`BIRDS_DEPLOYMENT_ID` | 独立项目与8～64位部署标识，仅字母、数字、下划线、短横线 |
| `BIRDS_ROOTS_HOST` | 预先新建的规范绝对父目录，root:root／0711；不与已有图库、备份或材料相交 |
| 四子根 | 父目录下 `inbox`、`data`、`archive`、`claim`，实际应用 UID/GID 所有、0700，无扩展POSIX ACL；四根互不包含／别名／链接 |
| `BIRDS_UID`、`BIRDS_GID` | 实际非root应用身份；容器user与环境值必须一致，不能靠容器启动递归chown接管目录 |
| `BIRDS_BASE_IMAGE`、`BIRDS_BASE_IMAGE_ID` | 固定已验离线镜像tag及ID；当前ID为 `sha256:605098b794ffa9ea41b9681ccd0159f40667079b5fe13d867b1fa4109e0f6452`，不是registry digest |
| `BIRDS_PYTHON_TREE_SHA` | 已核Linux依赖文件清单的规范JSON摘要；当前6包材料为 `25081d2ae611adeb728628e21294053b8dc9c50327740c14522f796f1880b69a`。构建和启动先用 `python -I -S` 隔离核对精确字节／文件及目录集合，拒额外文件、链接和特殊文件，再加载site-packages；摘要进入镜像ENV和部署登记，源码升级不能刷新材料身份 |
| `BIRDS_SOURCE_SHA`、`BIRDS_APP_IMAGE` | 本次显式源码清单/tar摘要与构建镜像，主机核image标签，不能用任意Python镜像替代 |
| 应用包 | 已核验Linux依赖树，Django5.2.18／Pillow12.3.0／Gunicorn26.2.0／WhiteNoise6.12.0；构建不安装或下载 |
| `BIRDS_SECRET_KEY`、Host／入口 | 强随机密钥与精确Host；实验loopback才设 `BIRDS_LOCAL_HTTP=1`，生产HTTPS由后续入口计划提供 |
| 内存 | 默认 web 512MiB、worker 1280MiB、两个代理各 128MiB。文件 worker 预算为 1024MiB，其容器另留 256MiB 给监督者。记录 NAS 其他服务和总内存空闲 |
| 模型材料 | 默认识别关闭，人工归档可用；启用前另采集Phase3已验模型／解释器／名录的只读来源、manifest、摘要与明确模型预算，不新下载或再评测 |
| 上传暂存 | 后续接收器配置独立受控临时区，不授予SMB用户直接写claim／归档／data的权限；当前入口不能冒充可靠上传已经交付 |
| 备份 | 另填独立目的地及离线材料恢复位置，必须与四根隔离。新根恢复使用 [备份与恢复](backup-restore.md) 中的显式会话 |

`inbox→claim` 必须在同一个VFS挂载中可rename，不能只比较 `st_dev`。Compose因此只将共同父目录bind到 `/roots`；四子根仍分别登记隔离身份。Linux每次启动都选当前namespace中各根最具体的mountinfo entry，要求inbox和claim的mount ID相同；该ID仅用于本次关系比较，不持久化，初始化的真实rename探针仍保留。访问和默认POSIX ACL均拒绝，UID或GID为0也明确拒绝。图库需要另盘时必须重新设计并实测明确子挂载，不改回四个独立bind。当前保守契约仅接受已知本地文件系统；data禁止NFS／SMB和未知类型。

## 首次初始化

初始化只对全新专用根执行一次。自动准备程序只能创建本次拥有的目录；每个空子根预先放一个普通JSON文件 `.birds-tracer-root.json`，内容精确为 `{"deployment_id":"本次部署ID","role":"inbox"}`，其他根的role分别为data/archive/claim。这是沿用实验准备器的归属标记名，不能当作已有文件可接管的许可。标记以应用UID/GID所有，建议0600。四根除该标记外必须为空，父目录配置文件root所有且0600。

在已核验的新源码目录离线构建，基础镜像ID和源码SHA写入镜像ENV，启动会与登记比较：

```sh
docker build --pull=false --network=none \
  --build-arg BIRDS_BASE_IMAGE="$BIRDS_BASE_IMAGE" \
  --build-arg BIRDS_BASE_IMAGE_ID="$BIRDS_BASE_IMAGE_ID" \
  --build-arg BIRDS_PYTHON_TREE_SHA="$BIRDS_PYTHON_TREE_SHA" \
  --build-arg BIRDS_SOURCE_SHA="$BIRDS_SOURCE_SHA" \
  -t "$BIRDS_APP_IMAGE" "$BIRDS_CODE_ROOT"
birds_compose() { docker compose --project-name "$BIRDS_PROJECT" --env-file "$BIRDS_ENV_FILE" -f "$BIRDS_CODE_ROOT/compose.yaml" "$@"; }
birds_compose config --quiet
birds_compose --profile initialize run --rm --no-deps init
birds_compose up -d --no-build --pull never web worker
```

init依次显式登记根→单次Django迁移→登记数据库文件身份→完整checkruntime。登记写入data/deployment-v1.json，记录schema=1、部署ID、四根dev/inode及Linux卷设备／文件系统／卷内位置、UID/GID、固定运行来源、应用迁移集合和数据库dev/inode。Linux不使用重建容器会变化的mount ID。初始化在自建随机探针目录中验证创建、文件和目录fsync、硬链接无覆盖发布及真实inbox→claim rename；只删除自己的探针。失败保留已有状态，不能通过删除登记、改数据库或清空图库来“修复”。

SQLite source ID必须精确等于 `2026-05-05 10:34:17 c88b22011a54b4f6fbd149e9f8e4de77658ce58143a1af0e3785e4e6475127e9`；现有checkruntime还核WAL／FULL／foreign_keys。Docker构建同时装配当前公开CSS/JS到staticfiles，保留WhiteNoise；不从照片根收集任何文件。Gunicorn使用已有26.2.0支持的 `--no-control-socket`，避免只读根下创建 `/.gunicorn`。

实验源码目录使用 `code/<source-sha>-<python-tree-sha>/`，精确核现存目录的源码、材料及清单集合，不接受额外文件／目录／链接。旧单SHA目录及原run证据原样保留，不原地迁移。

如果初始化在根登记后、迁移或数据库登记前中断，普通启动拒绝。先保留日志和当前根，不重复init。只有核对本次未完成初始化、迁移已成功且DB确为本次创建后，执行器才可显式运行 `python /app/manage.py checkruntime --deployment-finalize`；此入口不能再次登记已完成根，更不能修复被替换的数据库。此类中断不是普通启动自动恢复入口。

## 日常启动与显式升级

web／worker先执行 `checkruntime --deployment-start`。目录存在不代表卷已挂载；四根身份／实际卷位置、权限／有效访问／Linux ACL、部署ID、运行UID、数据库存在与文件身份、迁移集合或运行来源不符均拒绝。普通启动不mkdir、不自登记、不重绑，也不向照片根写入探针。根登记内容及内部路径不输出到checkruntime JSON，失败仅返回允许的错误码。未显式启用 `BIRDS_DEPLOYMENT_MODE=1` 的原开发环境仍按原checkruntime行为运行。

worker服务只启动runrecognitionworker，后者监督唯一runworker；既有worker.lock仍负责防止第二文件执行者。当前模型关闭，资源校验不运行模型；Linux worker检查cgroup能容纳文件worker／明确模型预算及256MiB监督余量。报告参数不等于已观测模型实际占用。

在部署主机上用 [环境样例](deployment/06-environment.example.json) 配置维护入口。运行用户须能操作本机 Docker，并读取受限环境文件、专用 data 根及备份集合。环境文件路径可相对于 JSON 所在目录填写，备份目录使用预先存在的独立绝对路径。升级需要两份环境文件，第二份只更换 `BIRDS_APP_IMAGE`，指向已离线构建并保留在本机的新镜像。保留旧镜像与原环境文件。

```sh
python3 deploy/maintain.py check --config "$BIRDS_MAINTENANCE_CONFIG"
python3 deploy/maintain.py status --config "$BIRDS_MAINTENANCE_CONFIG"
python3 deploy/maintain.py restart --config "$BIRDS_MAINTENANCE_CONFIG"
python3 deploy/maintain.py upgrade --config "$BIRDS_MAINTENANCE_CONFIG"
```

`check` 核对 Compose 角色、专用根、备份路径和本机镜像材料。Web 已运行时同时核对部署登记，未运行时报告 `not_running`。`restart` 等待后台和已进入的 HTTP 写入排空，在同一镜像中重建服务并核对数据库基线。`upgrade` 先创建、独立校验一致备份，再确认旧服务退出，通过 `maintenance` 服务的 `offline <session_id> migrate` 入口迁移一次。正常 Web 和 worker 启动先核对运行登记。原先运行的代理随 Web 重建。

保留每次命令返回的去密 JSON，尤其是失败阶段、`session_id` 和 `backup_set_id`。命令在新服务就绪、旧业务表与备份或停服基线一致后才解除维护。中断或超时后先用 `status` 查持久维护状态与离线动作结果。迁移失败时保留维护及原备份，按 [备份与恢复](backup-restore.md) 使用与备份 schema 匹配的镜像恢复到新隔离根。确认升级成功后，将后续日常操作配置的 `deploy_env_file` 改为已运行的新镜像环境文件。正式环境验证见 [验收清单](fnos-acceptance.md)。

## 最小NAS核验（主代理执行）

新建一个独立空run，复用已验离线材料和上述输入。只验证新根协议，不生成照片、不POST确认、不运行模型。保留源码SHA、镜像ID、命令退出码和去密日志到06-01新回执，原照片tracer及历史FAIL不修改。

1. 执行离线build、init；确认 `registered`、`verified`、完整checkruntime errors=[]。重复init应非零并返回deployment_already_registered。
2. 启动web/worker，观察两者running、唯一监督与文件worker；一次 `birds_compose exec -T web python manage.py checkruntime --deployment-start` 应退出0。获取CSS一次200并查看日志，确认没有旧控制socket错误和staticfiles缺失警告。停止并重新创建容器后复用同一根核验一次；不重做照片旅程。
3. 直接调用同镜像核验，错误部署ID和错误UID各一次应非零；使用 `birds_compose run --rm --no-deps -e BIRDS_DEPLOYMENT_ID=wrong-id --entrypoint python web /app/manage.py checkruntime --deployment-start`，以及将该 `-e` 替换为明确不同的 `BIRDS_UID`。错误JSON不得有路径／密钥。
4. 在本次拥有的额外空目录创建0700/正确UID的替代archive（不触碰正例目录），以 `birds_compose run --rm --no-deps -v "$BIRDS_NEGATIVE_ARCHIVE:/roots/archive" --entrypoint python web /app/manage.py checkruntime --deployment-start` 核验现存路径下换根应非零。另用 `-e BIRDS_DATA_ROOT=/roots/missing-data` 核验缺挂载路径应非零。最后额外挂载本次原inbox和原claim分别到其原容器路径（保持dev/inode和卷内路径），应以deployment_claim_separate_mount拒绝。所有负例都在web/worker停服时串行执行，不修改正例DB或JPEG。
5. 停止本次拥有的容器，保留新根/证据。真实NFS／ACL错误挂载未提供时如实记录未实测，本地mock只证明对应拒绝分支，不能宣称NAS该类负例通过。
