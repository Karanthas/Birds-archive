# 本地识别模型选型

选用一个固定 BioCLIP2 CPU 提供方 `open_clip_cpu_bioclip2`，revision `2957b322090f9cb17ae72c71981c7218a28d81e0`。它只提供排序候选；用户必须逐图核对完整鸟种集合并明确确认，唯一文件 worker 才能归档。模型、预处理、11131全球标签、prompt、锁文件和阈值均由 [selection.json](selection.json) 绑定。`selected` 是本次证据审阅后的选择决定；原报告 `selection_state=pending` 保持原字节。新公开隔离应用已实际注册／激活，25次普通新导入成功生成75候选；跨页两模式、单张／多鸟种、归档下载及资源关闭已通过，见 [03-11收尾](../../.planning/phases/03-recognition-batch-review/evidence/03-11/actual-selected-delivery-r14/README.md)。私人图库尚未上线。

按M10前冻结的顺序先审许可、离线、资源与人工安全，再审纠正操作、候选覆盖及耗时。模型为MIT；公开JPEG逐图许可、作者、来源版本、完整truth和taxonomy在配对 [manifest](nas-baseline/manifest.json)。应使用本目录nas-baseline八件同组报告，不能混用旧顶层public-jpeg-manifest与新protocol。[独立NAS严格复算](../../.planning/phases/03-recognition-batch-review/evidence/03-10/selection-calibration-r8-main-r8-validation-r5/root-actual-delivery-review.json)验证五checks与七摘要；[完整主run审计](../../.planning/phases/03-recognition-batch-review/evidence/03-10/selection-calibration-r8-main-r8/root-full-delivery-resource-review.json)核对全部5067件来源、资源、离线和Linux46/46。上述条件支持保留首候选，无需增加BioCLIP1或第二个生产模型。

14张公开校准拟合margin `.015`（grid3），随后独立M10七图只验证一次：六张单鸟Top1/Top3各6/6；一张自然三只、两种鸟的Top3覆盖两种。样本是便利集，训练重叠和私人照片泛化未知。累计49张已消费／退役照片不能再进入未来独立科学分母；03-11操作复用不增加效果样本。margin只提示前两名分数接近程度，cosine不是校准概率，也不能证明鸟数或集合完整。

原报告7/7 `mapping_failure_images`、七次 `threshold_outcome=skipped` 均保留：评测转换只映射有独立taxonomy证据的公开concept，其余全球候选保留raw排名并标unmapped。生产适配器使用固定11131标签的species_id；这是另一层实际UI／DB验证，不能把评测限制说成全球映射全失败，也不能宣称过滤后完美precision。

同一observed_agent固定顺序manual→single→own_candidates，实际事件23/18/12、集合编辑16/0/6，均七图、无跳过。这里支持“候选减少本次操作纠正”这一有限选择理由；不是随机化用户研究，不是私人相册准确率。所有首次最终集合均与公开truth一致，不能由Top3直接代替人工真值。

| 资源范围 | 原始测量 |
|---|---|
| 七图adapter | wall65.057507s、CPU66.501394s、采样进程树RSS3839012864B、AS6112489472B |
| 完整main | wall2577.412030833s、CPU observed1401.739444s／保守upper2652.28324875s、容器峰值4553465856B、存储2081312768B |
| 电费 | unknown，无功耗与电价证据 |

同一模型对象被缓存；cache hit仍做manifest资产hash验证，`cold_load_seconds`含这部分，不等于每次请求重新加载模型。不进行本轮无关性能重构。[有界负载共存义务](../../.planning/phases/03-recognition-batch-review/evidence/03-10/selection-calibration-r8-coexistence-r3/root-plan-obligation-decision.json)通过；原same-ready-encode门FAIL保留。完整收据不在任何encode区间，DB commit是否在gen2内未知，不承诺持续负载SLA。

旧GET500／POST503仍延期、未复现且未修复。实际跨挂载、硬件断电、真实私人图库、LAN/Tunnel仍在Phase6。03-11已完成隔离应用的preparemodel激活、新导入／旧意图保留及真实浏览器→普通worker→下载原字节证明；本文件不单独登记需求或阶段完成。
