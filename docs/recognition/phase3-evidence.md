# Phase 3 分层证据

2026-10-23：复用已有证据完成最终验收，14/14计划、13项验收及五需求已闭合；详见 [03-UAT](../../.planning/phases/03-recognition-batch-review/03-UAT.md) 与 [阶段核对](../../.planning/phases/03-recognition-batch-review/03-VERIFICATION.md)。两处驱动修正已应用，未重做模型评测或整套测试。

| 层次 | 事实与边界 |
|---|---|
| 软件 | 沿用最终 Django524／0skip／117.281秒及 check、迁移漂移、JS语法、Node13 通过记录；65 产品源／锁未变。本轮新增仅驱动修正和验收证据。 |
| 进程 | 沿用 03-10 NAS Linux46/46；本次只启动归档 worker，无识别模型进程。 |
| 真实模型 | 沿用 03-10 七图一次留出、14校准与三cohort；7/7映射失败、7次threshold skipped、私照泛化与训练重叠未知均保留。r12实际25次普通新导入成功、75候选，不追加科学分母。 |
| 浏览器 | r16真实跨页各自候选1受理/2跳过、统一集合2受理；单张与多鸟种确认、精确重放、5次新原图下载均通过。旧500/503仍延期未修复。 |
| NAS | r16原资源校验通过，639.717秒、CPU上界475.340秒、峰值内存163119104字节、峰值存储246132736字节；177原始SSH均0并回收，全部所属进程与容器关闭。 |

入口：[机器索引](phase3-evidence.json)、[本次收尾](../../.planning/phases/03-recognition-batch-review/evidence/03-11/actual-selected-delivery-r14/README.md)、[03-11摘要](../../.planning/phases/03-recognition-batch-review/03-11-SUMMARY.md)。科学证据：[选型](selection.md)、[基线](nas-baseline/evidence.md)、[03-10摘要](../../.planning/phases/03-recognition-batch-review/03-10-SUMMARY.md)。

r12的原批量／资源失败及r13–r15启动拒绝保持；r16沿用同一隔离应用的原图路径、数据库及候选，先与停止后封存SQLite/WAL逻辑行核对，再完成剩余功能。未重置旧时钟、改写旧原件或启用私人生产。源JPEG、模型、阈值、依赖和产品代码未变；本轮新模型运行、科学样本、安装和Git写入均0。

缺模型手动回退及新旧current/queued语义、有限重试、显式重跑、慢刷新焦点／草稿／选择继续引用既有软件和进程合同，不扩展实际故障矩阵。真实跨挂载、硬件断电、私人图库上线和LAN／Tunnel仍属Phase6。
