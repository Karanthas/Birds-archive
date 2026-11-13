# AGENTS.md

<!-- AUTONOMY DIRECTIVE — DO NOT REMOVE -->
YOU ARE AN AUTONOMOUS CODING AGENT. EXECUTE TASKS TO COMPLETION WITHOUT ASKING FOR PERMISSION.
DO NOT STOP TO ASK "SHOULD I PROCEED?" — PROCEED. DO NOT WAIT FOR CONFIRMATION ON OBVIOUS NEXT STEPS.
IF BLOCKED, TRY AN ALTERNATIVE APPROACH. ONLY ASK WHEN TRULY AMBIGUOUS OR DESTRUCTIVE.
USE CODEX NATIVE SUBAGENTS FOR INDEPENDENT PARALLEL SUBTASKS WHEN THAT IMPROVES THROUGHPUT. THIS IS COMPLEMENTARY TO OMX TEAM MODE.
<!-- END AUTONOMY DIRECTIVE -->

## Project-specific operating rules

- 用户于 2026-10-13 明确要求“Git 我自己弄”。除非用户之后明确授权，代理不得提交、推送、创建标签或分支、修改 Git 忽略规则；`commit_docs: false`。此约束覆盖 GSD 工作流中的自动提交建议。
- 用户已明确执行第一阶段；当前已完成其本地构建与验证，第二阶段待规划。后续实现从明确的阶段任务开始，不把规划完成写成功能完成。
- 用户于2026-10-15明确取消应用内登录：个人相册打开即用，不加账号、密码、PIN或自动登录伪账号。旧计划／测试中的管理员登录约定已被覆盖；保留CSRF、Host校验、no-store与文件边界，真实网络入口仍由部署阶段配置。
- 先阅读 `.planning/PROJECT.md`、`.planning/REQUIREMENTS.md`、`.planning/ROADMAP.md` 与 `.planning/STATE.md`；研究结论在 `.planning/research/`。
- 保护原 JPEG 字节；确认后的文件移动、改鸟种／年份、回收站与恢复必须可恢复且不覆盖其他文件。测试使用临时夹具和隔离目录。
- 网络接入、真实 NAS 路径、私人照片上传、付费 API 和密钥均由相应阶段显式配置；公开资料调研不等于已取得这些凭据。
- 用户界面和项目说明默认中文。资料缺失显示未知，识别分数不得假称经过校准的准确概率。
- 并行子任务限定文件所有权；最终交付前检查需求覆盖与文件／数据库一致性测试证据。
- 若未来用户授权提交，遵循 Lore Commit Protocol：首行说明变更意图；按需附 `Constraint`、`Rejected`、`Confidence`、`Scope-risk`、`Directive`、`Tested`、`Not-tested` trailers。

<!-- GSD:project-start source:PROJECT.md -->

## Project

**Birds Archive｜私人鸟类归档相册**

部署在个人飞牛 OS NAS 上的鸟类照片归档与展示应用。用户将调整好的 JPEG 放入指定文件夹，系统后台识别鸟种，用户确认或纠正后，应用实际移动文件到鸟种／年份目录，并提供默认摄影相册、可切换的鸟种图鉴及中文鸟类介绍。

首版仅本人使用；局域网直接访问，远程访问通过 Cloudflare Tunnel。照片与归档的可靠性优先于识别速度，照片可以后台慢慢处理，稍后集中确认。

**Core Value:** 让本人拍摄的鸟类照片在人工确认后可靠地按鸟种归档，并能在私有相册中找回、欣赏照片和了解所拍鸟种。

### Constraints

- **数据安全**：应用会移动真实文件，归档成功必须包含文件与数据库一致性检查；禁止静默覆盖同名文件。
- **硬件**：16 GB NAS 同时承担存储服务，识别任务应限制并发与资源占用。
- **准确性**：最终鸟种需要人工确认；模型在用户照片上的准确率尚无证据。
- **地域**：广东与江苏是常见拍摄地，不得硬编码排除全球鸟种。
- **速度**：后台处理可慢；需要可见进度、可重试和重启后继续处理。
- **成本与隐私**：用户愿意比较本地和云端；尚未确定云服务商、预算、凭证或授权实际上传样片。规划可使用公开资料，真实评测另行配置。
- **工作范围**：第一阶段本地实现已完成；不包含部署到真实 NAS、开通云服务或上传个人照片，后续阶段需按相应范围明确启动。
- **版本控制**：用户明确“Git 我自己弄”；规划文档只落地本地，代理不提交、不推送，也不代管 Git 忽略规则。

<!-- GSD:project-end -->

<!-- GSD:stack-start source:STACK.md -->

## Technology Stack

规划采用 Python／Django、模板与 HTMX、SQLite、Pillow；同一代码库运行 Web 和单个后台 worker。识别模型／云 API 尚待样片比较。版本、取舍和未验证条件见 `.planning/research/STACK.md`；第一阶段已有已安装且隔离运行验证的本地应用；真实NAS部署与模型接入仍未验证。
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->

## Conventions

Conventions not yet established. Will populate as patterns emerge during development.
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->

## Architecture

规划架构见 `.planning/research/ARCHITECTURE.md` 和 `SUMMARY.md`：导入补扫、稳定照片／物种身份、人工确认与识别候选分存、唯一文件执行者、可重放迁移日志、受保护媒体、来源可追溯的鸟种资料。第一阶段已实现导入补扫、稳定来源版本、人工元数据CAS、唯一worker、私有媒体及全球名录；可恢复归档迁移日志等后续阶段能力仍是方案，不能据此宣称已交付。
<!-- GSD:architecture-end -->

<!-- GSD:skills-start source:skills/ -->

## Project Skills

No project skills found. Add skills to any of: `.claude/skills/`, `.agents/skills/`, `.cursor/skills/`, `.github/skills/`, or `.codex/skills/` with a `SKILL.md` index file.
<!-- GSD:skills-end -->

<!-- GSD:workflow-start source:GSD defaults -->

## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:

- `$gsd-quick` for small fixes, doc updates, and ad-hoc tasks
- `$gsd-debug` for investigation and bug fixing
- `$gsd-execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->

<!-- GSD:profile-start -->

## Developer Profile

> Profile not yet configured. Run `$gsd-profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->
