# BioCLIP 2 首个 CPU 评测候选

这是准备安装审阅的候选方案。尚未安装识别依赖、下载权重、运行真实模型、访问 NAS 或选定生产模型。`preparemodel --check-review` 通过只说明本地元数据清单闭合；03-09 的具体安装及 NAS 范围授权、03-10 实测、03-11 选型证据仍必需。

## 精确范围

`installation-inventory.json` 是完整清单，逐项记录版本、官方项目/下载 URL、wheel 文件名、SHA-256、许可、用途、传递依赖、安装目录及容量未知项。36 个不同包分为：

| 环境 | 内容 | 精确锁 |
|---|---|---|
| `${BIRDS_NAS_EVAL_ROOT}/.venv-evaluation` | 当前基础应用 6 个包；Django 5.2.18、Pillow 12.3.0、gunicorn 26.2.0、whitenoise 6.12.0、asgiref 3.12.1、sqlparse 0.6.0 | inventory 的 `.venv-evaluation.lock_content`；相同内容另保存在本计划 `evidence/03-08/evaluation-linux.lock` |
| `${BIRDS_NAS_EVAL_ROOT}/.venv-recognition` | 31 个包；唯一模型角色，其中 Pillow 与基础环境重叠 | `requirements-recognition.lock` |

两个环境均针对 Linux x86_64、CPython 3.12、glibc ≥2.28。禁止复制本机 Darwin 二进制、安装系统 Python 包、CUDA 变体和 sdist 构建回退。NAS 的 Python/venv/ensurepip/pip 是待查前置环境，本清单没有授权升级它们。

固定顶层组合为 OpenCLIP `3.3.0`、torch `2.13.0+cpu`、torchvision `0.28.0+cpu`。CPU wheel 的 PEP 658 元数据 hash 与官方 CPU 索引一致，torchvision 要求 `torch==2.13.0`，PEP 440 允许对应 `+cpu` 本地版本。其余依赖按保存的官方版本 JSON、Linux/Python marker 和可用 wheel 解析；所有激活的 Requires-Dist 均用精确版本复核。没有安装或执行这些包，ABI/导入兼容性仍须实测。

清单包括 OpenCLIP 必需的 Hub/timm 及其网络相关传递依赖，正常识别不调用其获取能力。没有 pybioclip、transformers、训练 extras 或云识别 SDK。研究时六项 SUS 原观察保留；其原因是当时 seam 未获得年龄/下载/仓库信息，不是查明恶意。本轮补充官方元数据和许可证，不自行把原 verdict 改为 OK，也不代替 03-09 核准。

官方原始证据保存在 `.planning/phases/03-recognition-batch-review/evidence/03-08/metadata/`；清单检查会重新核对版本 JSON/CPU 元数据、实际 wheel hash、marker、传递闭包与两个环境的锁摘要。元数据采集脚本只允许指定官方域名的小型 JSON、配置、源码、tokenizer 和许可证，不允许 wheel/权重 GET；本轮没有包安装。

## 模型材料和许可

固定 [Hugging Face 模型 revision](https://huggingface.co/imageomics/bioclip-2/tree/2957b322090f9cb17ae72c71981c7218a28d81e0)：`2957b322090f9cb17ae72c71981c7218a28d81e0`。完整必需下载/保留清单共 5 项，精确 URL/hash/大小均见 inventory：

- `open_clip_model.safetensors`：1,710,517,724 字节；官方 LFS SHA-256 `b7b2bf6fbc95799e42630e394cf95803892ab447c1a8ab629dbc82fbeaf7dfef`。本轮仅读取元数据，未取得权重字节。
- `open_clip_config.json`：534 字节，SHA-256 `1bf947e96e943fe50efd5c3e26c37f843a2fa3c358967719a68c8a6d17ce68c8`，固定原生 ViT-L/14、768 维、224 输入、shortest resize、bicubic、模型 mean/std。
- 固定 revision `README.md`：完整模型卡及作者归属，明确模型 MIT 声明。
- OpenCLIP v3.3.0 的 `bpe_simple_vocab_16e6.txt.gz`：1,356,917 字节，SHA-256 `924691ac288e54409236115652ad4aa250f48203de50a9e4722a6ecd48d6804a`。本路径显式使用 `SimpleTokenizer(bpe_path=本地文件, context_length=77)`；不会构造 HFTokenizer。HF 的 tokenizer.json/config/special_tokens_map/merges/vocab 不参与此执行路径，清单逐项记录排除理由。
- BioCLIP 2 官方代码仓库固定 commit 的 MIT LICENSE 全文；commit 和文件 SHA 在 inventory 中。模型卡的模型许可与代码许可分别保留，不把训练图像的许可概括为 MIT。

获取权重后仍必须重新计算 SHA-256/大小，再进入模型包核验。采用 safetensors；不取得 `.bin` pickle checkpoint、不加载远端代码，不自动升级到模型卡链接的 BioCLIP 2.5。

官方接口依据：[OpenCLIP v3.3.0 工厂](https://raw.githubusercontent.com/mlfoundations/open_clip/v3.3.0/src/open_clip/factory.py)、[tokenizer](https://raw.githubusercontent.com/mlfoundations/open_clip/v3.3.0/src/open_clip/tokenizer.py)、[transform](https://raw.githubusercontent.com/mlfoundations/open_clip/v3.3.0/src/open_clip/transform.py)、[PyTorch CPU 索引](https://download.pytorch.org/whl/cpu/torch/)、[torchvision CPU 索引](https://download.pytorch.org/whl/cpu/torchvision/)。这些是版本/代码依据，不是本项目的前向结果。

## 授权后的本地包准备顺序

以下为 03-10 的接续步骤，本轮没有执行。所有路径限于用户批准的独立 `${BIRDS_NAS_EVAL_ROOT}`，不读取私人图库或主数据库。

1. 03-09 先绑定 `installation-inventory.json` 的规范化 JSON 摘要，批准两个环境、官方获取、公开 JPEG 缓存、模型目录、报告目录及 NAS 通道。修改任一版本/hash/范围后重新核对，不沿用旧摘要授权。
2. 在授权的 NAS 环境使用 Linux wheel/hash 锁创建两个隔离环境；保持原 `requirements.in/lock` 不变。检查 `pip check`、实际包版本、CPU-only 导入和平台，不从源码补装失败包。任何安装失败先核实具体包，禁止换相似名称。
3. 在评测隔离 DB 导入已校验的完整 AviList 2025b snapshot，使用 `.venv-evaluation/bin/python manage.py preparemodel --export-labels --snapshot <该快照UUID> --output <全新目录> --settings=config.settings_test`。显式共享测试根仍须通过既有 `birds-tests-*` marker/owner/路径检查；不能让命令默认临时 DB 冒充持久评测工作区。
4. 导出恰好 11,131 个 `SpeciesConcept`，只接受 `source_kind=catalog` 且仍属于该快照的物种；按 `(scientific_name, external_id)` 全局排序，无地域筛选，不加入 local 自定义名。冻结 snapshot、labels 与 prompts 三文件。prompt 为 `a photo of {scientific_name}.`；不是模型固有类别或检测器。没有一对一概念依据时保留 `species_id=null` 和原始学名，不猜映射；中文名称由现有合法来源 DB/UI 处理，缺失明确回退。
5. 在同一本地模型目录组装候选 manifest：`schema_version=1`、provider `open_clip_cpu_bioclip2`、repository/revision、MIT/attribution、绝对 local_root；`files` 每项 role/path/sha256/size_bytes。必需 role 为 weights/config/tokenizer/labels/prompts/snapshot/preprocess/runner/dependency_lock/license，最终增加 text_features。path 只允许目录内单个普通文件，拒绝 symlink、远端路径、额外 checkpoint、Python/pickle 文件。
6. `preprocess.json` 为官方 preprocess_cfg 加 `size=224, mode=RGB, exif_transpose=true`；`runner.json` 为 `version=openclip-cpu-eager-v1, device=cpu, dtype=float32, batch_size=1, intra_threads=2, inter_threads=1, embedding_chunk_size=32`。manifest 同时保存 config/tokenizer/label/snapshot/preprocess/prompt/runner/dependency_lock 的字节 SHA-256；预计算后增加 text_features_digest。`score_kind=cosine_similarity`；初始 `threshold_version=uncalibrated-v1, margin_threshold=null, calibration_evidence=null`。
7. 使用 `preparemodel --prepare-vocabulary --manifest <初始manifest> --python <.venv-recognition/bin/python> --settings=config.settings_test`。该显式维护模式在新角色内先设 6 GiB 独立地址空间预算、nice 与线程，再加载本地模型；chunk=32，在 `inference_mode()` 下编码并归一化全球 prompts，保存 safetensors 及一个新的 `prepared-manifest.json`，不覆写旧 manifest。这是预训练文本推理，不是训练。矩阵仅算术体积 34,194,432 字节；实际文件开销、加载、临时张量与 RSS 均另测，不能拿矩阵/权重大小当运行内存。该准备预算是待 NAS 核对的保守起点，未证明设备可运行。
8. `preparemodel --verify --manifest <prepared-manifest>` 完整核对本地字节；部署模型目录只读，并通过 03-10 的实际出站封锁测试。`--register` 只登记 candidate/is_current=false，供 03-12 隔离候选审核；不会切生产 current 或重跑历史照片。

文件总容量已知部分、wheel 下载体积与未知的安装后容量均单独列出。torch/torchvision CPU wheel 体积本轮未下载测量，保持未知；模型完整权重和派生矩阵亦未落地。

## 推理、评测及激活合同

生产调用保持 `infer_readonly(fd, input_sha256=..., input_size=..., artifact={provider, manifest})`。先校验只读普通 FD/JPEG/hash/大小；只在独立模型角色懒加载可选库，CPU fp32 eager、eval、inference_mode、batch=1。EXIF 方向和 resize 只操作副本；原 JPEG 不改写。按冻结全球标签的 cosine 排序稳定取前三，不做 softmax，不称准确概率。无映射候选保留 raw_label/unmapped。Web/普通文件 worker 没有 torch 仍能导入并完成手动归档。

运行角色启用 Hub/Transformers offline、禁 telemetry，并拒绝 DNS/socket connect/sendto；代码路径只读取核验的本地 config、safetensors 和 BPE。该进程内 guard 不替代 OS 网络隔离；NAS 出站封锁、只读挂载、RLIMIT_AS/OOM 与模型实际运行仍由 03-10 验证。

`run_cpu_evaluation(*, model_manifest, manifest, protocol, data_root, smoke)` 返回 results envelope，03-07 负责验证/评分/写报告。轻量控制角色保留启动容量，用生产同一 `InferenceChannel`、只读匿名副本、进程锁、nonce、单 in-flight 与超时监督；不从已降 hard 的文件worker创建大模型。protocol.execution 在运行前明确 `python`、`model_lock`、`memory_mb`、`timeout_seconds`。Linux procfs 采集模型 VmHWM/VmPeak、每 20ms 的模型+控制角色 RSS，以及 wait4 CPU；采样树 RSS 不冒充 cgroup 峰值。离线资源成本未知。每阶段时间来自子角色，冷加载项包括每次包 hash 校验；词表时间为 0 表示已冻结预计算矩阵，并非把预计算耗时计作免费。

默认入口产出未获 NAS 归属证明的 `model_cpu`，不会根据设备字符串自行升级 `nas_cpu`。03-09/10 若显式提供 `protocol.execution.nas_attestation={path,sha256}`，需由已批准的目标记录给出 `schema_version=1, approved_target=true, target_id, approval_evidence_ref, host`；host 必须与执行时的 `machine`、`kernel_release`、`cpu_identity_sha256`、`memory_total_bytes`（MemTotal字节）及 `network_interfaces` 全部相符，且 x86_64、仅有 lo 网络接口。

CPU身份规范v1由`_cpu_identity(cpuinfo)`纯函数定义：逐逻辑CPU提取`vendor_id / cpu family / model / model name / stepping / flags`，压缩值中的空白；flags去重排序；保留逻辑CPU总数，型号记录去重后按上述字段排序，形成`{schema_version:1, logical_processors, models}`。`cpu_identity_sha256`为该对象按项目规范化JSON的SHA-256。明确忽略`cpu MHz`、bogomips、processor编号与记录顺序；字段缺失或重复拒绝，厂商/型号/stepping/flags变化会改变摘要。不能用整份`/proc/cpuinfo`字节摘要代替这个规范。

子角色还须实际报告软硬地址空间预算已生效。这只是已审阅目标记录与实时核对的关联，不是密码学签名或独立验收；03-11仍复核授权来源、实际NAS身份和原始资源/网络证据，才可采用 `nas_cpu`。当前没有该记录或设备证据，不能关闭 EVAL-01/02。smoke 仅冻结分区第一张，完整评测使用同一字节摘要，报告输出必须为全新目录。

分数接近策略绑定 model revision、label/prompt/preprocess 摘要及策略版本。未校准时不放行各自候选批量确认；后续校准仅可引用 calibration 分区，增加本地 calibration role、report_digest、阈值及上述摘要，`calibration_evidence=sha256:<该文件字节摘要>`。不能用任意常数或 evaluation 分区调线。单张仍明确选种、最后确认才归档。

`preparemodel --activate --artifact <UUID> --selection <03-11选择文件>` 重新验证完整报告、NAS预算、三模式实际轨迹、各附件摘要、模型/词表/prompt关联与本地包后才修改 selected/current。不完整 selection、control、smoke、缺必需场景/真实轨迹均拒绝；候选登记不等于获选。

公开 JPEG 来源、逐图许可、自然多鸟真值和现存小样本限制以 `public-jpeg-manifest.json`、`corpus-audit.md` 为准。获取后放 `${BIRDS_NAS_EVAL_ROOT}/public-jpegs`，权重放 models，报告放全新 reports 子目录；只下载既定公开资料，不向云识别外传照片。当前 8 张纳入/2 张校准不足以代表私人照片或可靠校准，五项 Phase 3 需求仍 Pending。
