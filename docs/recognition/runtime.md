# 本地后台运行与模型切换

真实私人NAS部署属于Phase6。下面维护命令仅在已配置的隔离应用环境运行：指定独立导入／data／archive／claims根和数据库，使用已经核准的Python3.12、36个CPU包、BioCLIP2材料及SQLite3.53.1镜像。不得继承私人路径；不下载、训练、安装包或自动更新。Web保持免登录、CSRF、Host、no-store与原图边界。

纯手动入口：`BIRDS_RECOGNITION_ENABLED=0 python -B manage.py runworker`。JPEG导入、人工单／多鸟种、本地鸟名和明确无法确定仍可用，缺模型显示原因。关闭识别须停止所属监督进程并等待其子进程退出，再启动唯一普通worker，不能并存两个文件执行者。

识别监督入口：`python -B manage.py runrecognitionworker`。环境显式配置 `BIRDS_RECOGNITION_ENABLED=1`、`BIRDS_RECOGNITION_PYTHON`、`BIRDS_RECOGNITION_MODEL_DIR`、`RECOGNITION_MEMORY_MB`、`RECOGNITION_TIMEOUT_SECONDS`；监督入口独立启动唯一文件worker和受限推理子进程。本次复用2CPU、8GiB、swap0容器上限，推理单并发、intra2/inter1、nice10、600秒单请求上限和6144MiB地址空间预算，文件worker1024MiB；实际值以03-11封存scope和实际配置为准，不能把开发机限制当NAS证明。

模型维护由独立命令处理：`python -B manage.py preparemodel --verify --manifest <candidate-model.json>`；`--register --manifest <candidate-model.json>`返回目标应用自己的不可变artifact UUID。然后 `python -B manage.py preparemodel --activate --artifact <该UUID> --selection docs/recognition/selection.json`。激活会按selection中NAS完整final-report目录重新核七摘要、许可／离线／资源／人工安全／纠正负担与冻结判据，再核目标artifact的模型、labels、prompts摘要，最后原子切current。它不接受“只改selected标志”的捷径，也没有 `--only-new-imports` 开关。selection选择决定不等于已执行激活。

新导入首次创建识别意图时快照current。切换不会改旧候选、旧队列artifact或已确认人工集合；显式重跑才为所选待确认照片创建新代次，旧可用候选保留到新结果发布。手动重试保留原artifact与累计attempt；安全白名单临时错误有限退避，源变化、包错误、超时与内存失败不能无限重试。候选发布须满足源版本、内容身份、人工metadata版本与generation CAS；晚到结果不能覆盖人工决定或归档。

离线复核可在开发机运行 `.venv/bin/python -B manage.py evaluaterecognition --validate-report docs/recognition/nas-baseline --require-nas --selection docs/recognition/selection.json --settings=config.settings_test`，只读取正式JSON/CSV，不加载模型或连接NAS。完整评测／smoke应显式使用配对 `docs/recognition/nas-baseline/manifest.json` 与同目录protocol，不能混用旧顶层public-jpeg-manifest；本次禁止重复一次性M10科学留出。

模型revision、权重、label/prompt、预处理、阈值、依赖或设备资源变化时必须重新验证受影响证据；不能复制旧selected结果。全图margin只描述分数接近，候选不证明完整集合，最终人工仍不可省略。后台安静刷新须保留草稿、焦点和跨页选择；失败后可手动归档。跨挂载、硬件断电和持续生产负载的保障尚待Phase6。
