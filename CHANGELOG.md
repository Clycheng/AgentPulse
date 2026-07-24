# Changelog

本文件记录 AgentPulse 值得留痕的改动。**每次做完实质工作请在顶部追加一条**（见 [AGENTS.md](AGENTS.md) §5）。
架构/方向级决策另记在 [docs/decisions/](docs/decisions/)。格式参考 [Keep a Changelog](https://keepachangelog.com/)。

## [Unreleased]

### 2026-07-24（TD-13：公司世界模型与独立员工认知闭环）

- **feat(memory)**：新增 workspace 隔离的 `company_events`、`agent_memories`、`memory_links`、`agent_relationships` 和 `context_manifests`。聊天/私聊、任务事件、产出、知识资料和员工经验在原写入事务中进入不可变公司事件账本；原文保留，旧 transcript 超过预算时生成带证据链接的 episode。
- **feat(runtime)**：新增 Context Engine，按词法相关性、近因、重要性和可信度检索共享事实、员工私有记忆与同事关系，给 Hermes ACP、DeepSeek 和任务调度注入专属 manifest；Run、任务快照和会话 Run 轨迹返回使用过的事件/记忆 ID。
- **feat(company-tools)**：`/mcp/company-tools` 新增同事列表、公司记忆检索、持久 ping/内部消息、范围内内部任务、观察和关系事实工具；MCP 面向员工使用姓名/岗位语义，服务端继续用 token 校验 workspace、Run、负责人和计划边界，重复 ping 会被拦截。
- **feat(product)**：员工档案增加认知记忆、同事关系和证据审计区；手动/idle reflection 同时沉淀 evidence-backed reflection，私有判断不会作为共享记忆暴露。
- **feat(obsidian)**：桌面端只读用户选择的 Vault 下 `.agentpulse/managed` Markdown，新增 `POST /api/knowledge-sources/obsidian-sync` 按工作区和相对路径幂等同步；不扫描或覆盖 Vault 其他内容，文档更新继续写入公司事件账本。
- **test**：新增 TD-13 事件共享/私有隔离/上下文 manifest/overflow episode/ping loop/Obsidian managed sync/prompt 语义测试；全套 API **361 passed / 9 skipped**，desktop lint/build 通过。
- **architecture**：新增 [ADR 0013](docs/decisions/0013-company-world-model-and-cognitive-memory.md) 与 [TD-13](docs/tech-design/TD-13-company-world-model.md)，明确数据库事实源、员工独立认知投影、Hermes 唯一运行时和现有外部审批门不变。

### 2026-07-24（TD-12：零成本云端部署与桌面分发闭环，进行中）

- **feat(cloud/BYOK)**：新增 workspace 级 DeepSeek 模型凭证和设置 API；注册即创建四人内容团队与内容经营群，保存并验证 Key 后幂等供给全部员工，撤销后阻止新 Run。密钥使用独立 `credential_encryption_key` 派生的版本化 Fernet 密文，只按 Run 注入 Hermes ACP 环境，不写 profile、日志或 API 响应。
- **feat(desktop)**：生产 Electron 改用 `app://agentpulse` 安全协议并固定连接 `https://api.agentpulse.cc/api`；JWT 经 preload IPC 和 `safeStorage` 保存，renderer 不再持久化 token。主进程限制导航/弹窗，页面增加 CSP、API 兼容检查和 DeepSeek onboarding/settings。
- **feat(distribution)**：新增固定 Hermes `v0.18.2` 提交的 ARM64 Docker 镜像、单副本 Compose/Caddy、加密 PostgreSQL 备份、UFW bootstrap 和健康检查失败回滚脚本；Caddy 从固定 `v2.11.4` 提交以 Go 1.26.5/gRPC 1.82.1 自建，避开官方镜像仍存在的可修复高危项。新增 SHA 固定的 API 镜像与跨平台桌面发布 Actions。独立公开 `Clycheng/agentpulse-releases` 已创建，官网增加系统识别下载、GitHub Release、SHA256 和未签名安装说明。
- **feat(analytics)**：Vercel `agentpulse-web` 已启用无 Cookie Web Analytics，官网加载 Insights；下载/安装帮助事件只进入 API 的 UTC 日聚合表，不保存原始 IP、UA 或用户标识。
- **security**：JWT 强制 issuer/audience/type/jti/iat/exp；登录/注册使用数据库持久限流和 HMAC 匿名 IP；生产配置对默认/重复/过短密钥、非 HTTPS CORS、未启用托管 Hermes 安全模式和提前开放 webhook fail-close。Hermes 子进程环境改为白名单，托管模式禁用本地文件、终端、浏览器、网页、代码执行和 computer use；公网 Caddy 阻断 MCP。容器以非 root、只读文件系统、零 capabilities 运行并接入 `pip-audit`、Bandit、npm audit 和 Trivy 发布门。
- **verification**：API **355 passed / 9 skipped**，desktop lint/build、npm audit、Bandit、API/Hermes 双 `pip-audit` 均通过；ARM64 API 与 Caddy 两个镜像的可修复 HIGH/CRITICAL 均为 0，生产式只读非 root API ready smoke 通过。macOS DMG/ZIP 完整性已验证；Oracle/Supabase、DNS、Windows CI 资产、v0.1.0 Release 和线上完整 E2E 尚未完成，TD-12 保持进行中。
- **architecture**：新增并修订 [ADR 0012](docs/decisions/0012-cloud-hosted-desktop-distribution.md) 与 [TD-12](docs/tech-design/TD-12-cloud-deployment-and-desktop-distribution.md)，确定薄桌面客户端、Oracle 单节点 Hermes、Supabase PostgreSQL、用户 BYOK、独立发布仓和生产 fail-close 边界；Vercel 上误建的 API Function 不修复，`api.agentpulse.cc` 将迁移到 Oracle。

### 2026-07-23（TD-10：受控业务工具门与真实邮件闭环）

- **feat(runtime)**：新增与公司内部工具隔离的 `/mcp/business-tools`，RunService 在数据库 Run 建立后按员工已启用能力动态注入短期签名 token；token 绑定 workspace/conversation/run/agent/可选 task，服务端再次校验 Run 归属、工具映射和员工能力。聊天 Run 只获得业务工具，任务 Run 可同时获得 company/business tools。
- **feat(security)**：新增 AgentPulse 托管的加密 `agent_credentials`，使用 `auth_secret_key` 派生版本化 Fernet key；API、数据库审计和 Hermes profile 均不暴露明文。凭证录入会启用全部依赖能力，撤销会统一降级为 `credential_missing`，启动迁移会修复历史上“已启用但无凭证”的假状态。
- **feat(execution)**：新增持久化 `business_actions` 队列与 `business_tool_policies`；独立 worker 使用 30 秒租约、稳定幂等指纹和最多两次 provider 尝试，支持重启恢复、网络失败安全重试、成功动作复用和拒绝/过期后重新申请。业务审批改为数据库轮询，50 秒未处理 fail-close；长期放行严格限定员工 + 具体工具，付款类禁止长期放行。
- **feat(email)**：首个真实动作 `send_email` 接入 Resend `/emails`，支持 1-50 个收件人、纯文本主题/正文和可选 reply-to；只会选择绑定当前员工的唯一活跃邮件渠道，并使用永久动作 ID 生成稳定 `Idempotency-Key`。社媒发布、退款、广告出价、薪酬和付款仅提供诚实的未配置骨架，不产生假成功。
- **feat(product)**：员工档案新增掩码凭证录入/撤销、最近对外动作和长期放行管理；渠道页新增 Resend 发件地址、名称和员工绑定；聊天每秒恢复待审批项，任务快照展示关联业务动作。审批卡自然语言展示发件人、收件人、主题与正文摘要，并提供允许一次、永远允许、拒绝；消息与渠道页完成桌面/移动端响应式走查。
- **test/e2e**：新增 13 个常开业务工具测试和 1 个守护式真实 Resend E2E，覆盖密文/错密钥/撤销、legacy 状态修复、token 越权、审批/策略/超时、租约/重试/幂等、未实现工具诚实失败、聊天/任务 MCP 隔离、SQLite CHECK 升级及公开 API；全套 **338 passed / 9 skipped**，desktop lint/build、三条架构边界 grep 和真实 UI 拒绝路径均通过。未提供真实 Resend key 时守护式 E2E 明确跳过，本轮没有发出真实邮件。
- **architecture**：新增 [ADR 0011](docs/decisions/0011-controlled-business-actions.md)，确定“业务密钥由 AgentPulse 托管、业务动作持久执行、内部公司工具与外部业务工具隔离”；修订 [TD-10](docs/tech-design/TD-10-business-tool-gate.md)，复用 TD-11 的动态 MCP 和数据库审批，不再依赖进程内 `approval_bridge`。

### 2026-07-23（TD-11：自媒体 AI 公司自动执行闭环）

- **feat(orchestration)**：共识 brief 增加 3-6 项严格分工合同（负责人/交付/依赖/最终项），生成时携带群成员 ID、岗位和职责；非法负责人、重复 key、未知依赖、依赖环或缺少唯一 `content_package_v1` 最终项时修复一次，仍失败则继续讨论。群讨论中的小秘禁用执行型 Action Bridge，主持人选择 NONE 时仍能用历史 transcript 收敛，避免讨论门被绕过或永不收口。
- **feat(plan-api)**：新增 `task_plans`、`task_dependencies`、任务计划字段和 Run 租约字段；`POST /briefs/{id}/launch` 原子确认并创建根任务、子任务、依赖和首批 Run，支持并发幂等、legacy confirmed 补建和 ready profile 前置检查。兼容 `/confirm` 委托同一服务；新增计划快照、任务 Run 轨迹和阻塞恢复 API。
- **feat(runtime)**：常驻 `TaskScheduler` 每 2 秒领取就绪任务，每 workspace 最多并发 2 个 Run，30 秒租约/10 秒续租；依赖完成后自动接力，重启回收过期租约并自动重试一次，持续失败才阻塞老板，全部完成后根任务与计划自动归档。RunService 可执行数据库中已有 queued Run，并在工具 RunStep 后提交事务，避免 SQLite 长写事务饿死心跳。
- **feat(mcp)**：新增每 Run 动态 `acp.schema.HttpMcpServer` 公司工具，短期 token 绑定 workspace/plan/task/run/agent；受控暴露资料检索、进度、交付、子任务、支援和阻塞六个工具，Hermes 不接触数据库凭证。固定 `mcp==1.26.0`、`sse-starlette==2.1.3`，保留 `starlette==0.41.3`。
- **feat(delivery)**：中间项支持 Markdown 与最终回复兜底；最终项必须提交合法 `content_package_v1`，校验发布排期、内容字段、来源引用和 assumptions，并支持 Markdown 导出。审批改为数据库决定轮询（250ms/50s fail-close），API 只记录决定，RunService 负责最终状态迁移。
- **feat(product)**：新 workspace 默认创建小秘、内容策划、内容主笔、运营执行和“内容经营群”，开启 Hermes 供给时四个 profile 自动上岗。桌面端 brief 卡片自然语言展示分工接力，一次点击 launch；计划每 3 秒刷新任务/Run/审批/产出，任务详情支持阻塞恢复，最终包提供发布日历、逐篇预览、来源、未知项和 Markdown 导出。
- **test/e2e**：测试配置清空真实 key 并在非 `HERMES_E2E=1` 时禁止外网/真实 profile；新增 brief/launch/调度/MCP/审批/内容包/日志回归。全套 **325 passed / 8 skipped**；desktop TypeScript/Vite/Electron build 通过；真实 Hermes 四人“小红书周计划”从群讨论、一次确认、自动接力、API 重启恢复到结构化内容包全链跑通。
- **architecture**：新增 [ADR 0010](docs/decisions/0010-durable-task-dispatch-and-company-tools.md) 与 [TD-11](docs/tech-design/TD-11-autonomous-content-execution.md)，确定数据库持久调度 + 动态 MCP 公司工具边界；不接真实发布平台，后续顺序为 TD-10 → TD-09 → TD-08。

### 2026-07-21（讨论闭环核心修复：共识 brief 自动产出 + 小秘真招人 + 群成员全员可发言）

- **feat(orchestration)**：讨论轮结束自动产出共识 brief——`run_discussion_round` 的讨论循环正常结束后，本轮实际发言 ≥2 轮就用注入的主持人 LLM 回调跑收敛检查（`build_convergence_prompt` → `check_convergence` → `build_brief_draft_prompt`，这三个函数 TD-02-T3 起就是只有测试引用的死码，本次接入生产），判定已对齐则 yield `brief_draft` 事件。编排层不写库；LLM 异常/JSON 垃圾/缺 goal 一律静默兜底为"不出 brief"，绝不影响发消息。
- **feat(api)**：路由层消费 `brief_draft` 事件调 `create_brief` 落库（同会话已有 draft 状态 brief 则去重跳过），BRIEF_CARD 系统消息并入非流式响应；流式端点补上了 docstring 里早已声明但从未实现的 `event: system` SSE 帧。桌面端 SSE 消费循环加 `system` 分支，共识卡片实时上屏（渲染逻辑零改动，`mapApiMessage` 天然命中已有的 `BRIEF_CARD:` 卡片）。**这是"讨论→拍板→建任务"闭环第一次在生产路径上真正能跑出来**——此前 `POST /briefs` 全仓库只有测试调用，老板在 UI 里永远看不到共识卡片。
- **fix(api)**：小秘（`source == "system_secretary"`）优先走 Agent Action Bridge——她 bootstrap 后有 ready Hermes profile，旧路由顺序（Hermes 优先）让她永远拿不到只挂在 function_loop 上的系统工具，`create_employee`/`create_group`/`create_task` 只会嘴上答应。现在先走 function_loop，异常或空产出才回落她的 Hermes profile → DeepSeek；其他 agent 顺序不变（有 ready profile 仍直走 Hermes，审批门可达）。顺手把多处 `except Exception: pass` 静默吞掉改成 `logger.warning("function_loop_failed")`。
- **fix(api)**：`resolve_reply_agents` 删除群成员 `LIMIT 3`——建群允许 12 人，但第 4 人起永远不会被选为发言人。
- **fix(api)**：`extract_recruit_intent` 招聘意图识别只在小秘私聊生效——此前任何会话（含群讨论）里说"招个分析师"都会被正则截胡，直接建一个无能力、不供给的纸片员工（`source="chat_factory"`）。
- **test**：新增 9 例——编排层收敛检查 4 例（converged 出事件/未 converged 不出/<2 轮不检查/LLM 异常静默）、路由层 5 例（自动落 brief+卡片入响应+二次去重、SSE `event: system`、4 人群全量可回复、小秘有 ready profile 时流式/非流式都先走 function_loop、群聊与普通 DM 不触发招聘意图）。全套 **298 passed / 8 skipped / 1 xpassed**；2 个预存失败（`test_group_chat_returns_multiple_agent_replies_when_not_mentioned`、`test_task_api_updates_and_injects_related_context`）已用 stash 在改动前代码上复现确认与本次无关（本机 `.env` 真 key 导致 function_loop 泄漏真实网络调用）。三条架构层界 grep 全干净。Verified: `run_discussion_round` called via production path `POST /conversations/{id}/messages(.stream)`。
- **已知边界（非本次范围）**：aligned 状态后老路径仍逐成员各回一条（12 人群会有 12 条回复）；brief 确认后只建一条任务记录，自动拆分/派发编排与 TD-10 业务工具门留后续。

### 2026-07-20（自然语言团队编译器：一段话描述团队 → 一次性建成真实员工）

- **feat(api)**：新增 [ADR 0009](docs/decisions/0009-natural-language-team-compiler.md)——「最后一公里」的第一版：老板用一段话描述需要的团队（角色、分工、业务背景），系统拆解成可编辑的员工草稿，确认后一次性建成真实 Hermes 员工并自动拉进一个团队群。四点产品边界（不自动规划多群、不编造业务技能内容、不加独立校验器、不做试运行）均来自项目所有者的直接否决，记在 ADR 里防止下一个 AI 重新加回去。
- **refactor(services)**：`app/services/workspace.py` 新增 `provision_new_agent`（从原来模板专属的 `_provision_recruited_agent` 泛化而来）——统一的"角色规格 + 能力 key → 真实 Hermes 员工"入口，内部走 `split_by_credentials` 处理混合能力 bundle。人才市场招聘、默认秘书 bootstrap、小秘 `create_employee` 工具、团队编译器四条路径现在全部调用同一个函数，不再各写一份供给逻辑。
- **feat(api)**：`app/orchestration/team_compiler.py`（`build_team_draft_prompt`/`parse_team_draft`）+ `app/api/routes/team_compiler.py`（`POST /agents/draft-team` 纯预览无副作用、`POST /agents/create-team` 真建人+自动建群）。复用了 TD-04-T3 早已实现但从未接生产入口的 `draft_role_spec`（单人职责/能力精修），编译器本身只负责"拆出几个人"这一层。
- **feat(runtime)**：`DeepSeekChatClient.complete()` 新增 `system_prompt_override` 参数——严格 JSON 输出的工具调用不再被"AI 员工回复老板"的人设框架稀释指令。
- **feat(api)**：小秘 `create_employee` 工具 schema 补齐 `responsibilities`/`capability_keys`，新增 `list_capabilities` 工具——小秘被要求招人时能先查目录再真建人，不再是挂名字的纸片员工。
- **feat(desktop)**：新增 `TeamCompilerModal`（员工花名册页头"描述你的团队"按钮）——一段话 → 可编辑草稿卡片（姓名/岗位/部门/描述/职责逐行编辑、能力 key 以可移除 chip + 复用"+授予能力"已有的 `/api/capabilities` 选择器新增）→ 确认创建 → 自动跳进新团队群（单人则跳花名册）。中英文 i18n 均已补齐。
- **test**：`tests/test_team_compiler.py`（9 例：`parse_team_draft` 正常/异常路径、两个 HTTP 端点、含"部分能力缺凭证"混合 bundle 场景）+ `tests/test_function_loop.py` 新增 2 例（小秘 `create_employee` 真供给 + `list_capabilities` 返回目录）。全套 288 passed / 8 skipped / 1 xpassed（3 个预存失败与本次改动无关，见下）。
- **真机验证**：`AGENTPULSE_HERMES_PROVISIONING=true` + 真 DeepSeek key，浏览器走完整链路——用项目所有者本人给出的"民政局居家养老 + 个人抖音号"场景描述生成 3 人草稿（质检专员/运营负责人/文案，能力 key 逐人不同）、编辑能力 chip、确认创建 → 3 个真实员工 + 1 个"新团队"群自动建成，`hermes profile list` 可见真 profile；另用 `curl` 单独验证 `POST /agents/create-team`（单人）的 `agent_specs.status=ready`/`agent_capabilities` 落库，随即清理了测试产生的真实 Hermes profile。`npm run lint`（`tsc --noEmit` 两个 tsconfig）无错。
- **known issue（非本次引入，仅记录）**：`test_workspace_flow.py` 里 2 个测试在本机 Python 3.14 venv 下失败（另一个 Python 3.12 worktree 通过），疑似 async mock 兼容性差异；`test_knowledge_source_is_injected_into_agent_context` 会绕过 mock 真的打到 DeepSeek——根因是 `function_loop.py::run_function_loop` 走原始 `httpx` 调用、不经过被 mock 的 `DeepSeekChatClient.complete()`。已用 `mcp__ccd_session__spawn_task` 登记为独立跟进项，未在本次改动范围内修复。

### 2026-07-19（人才市场招聘接上真供给，不再是纸片人）

- **fix(api)**: 人才市场"招聘"（`recruit_from_template`）之前只 `create_agent`，从不建 `agent_specs`、从不 `provision()`——产品主推的招聘入口招来的员工永远卡在临时 DeepSeek 兜底层，只有"创建员工"表单手动勾能力芯片那条路才会真供给。现在 `hermes_provisioning` 打开时，招聘会按官方模板名查 `ROLE_BUNDLES` 拿能力清单，真建 profile。
- **fix(orchestration)**: 新增 `capability_catalog.split_by_credentials`——`provision()` 是全有或全无的：一个角色 bundle 只要混了一个缺凭证的能力（比如"运营负责人"的 `data_analysis`(免凭证) + `ad_bidding`(需要没人配过的 AD_API_KEY)），员工就会卡在 `blocked_on_credentials`、**一个能力都用不上**，包括本来能立刻工作的那些。现在先只用免凭证的能力建真 profile，缺凭证的能力事后走"profile 已存在"快速路径（复用 `runtime.upgrade.execute_upgrade`）补上，标成"待补凭证"，不拖累其余能力。补齐了 4 个官方模板（运营负责人/内容主笔/短视频策划/销售顾问）之前缺失的 `ROLE_BUNDLES` 映射。
- **真机验证**（不是纸上谈兵）：真招募"内容主笔"→ 真 Hermes profile `ap596c1d3f80df44`，两个能力都 `enabled`；真招募"运营负责人"→ 真 profile `ap596c1d10a8d1ed`，`data_analysis`/`report_generation` 真 `enabled`，`ad_analysis`/`ad_bidding` 正确停在 `credential_missing` 而不是拖累整个员工。
- **踩坑记录**：给这个改动写测试时，第一版直接让 `settings.hermes_provisioning=True` 走真代码路径，结果每次跑测试都在这台机器上真建了 Hermes profile（5 个孤儿 profile，`hermes profile list` 里能看到）——已清理，测试改成显式 mock `build_provisioner_from_settings` 返回 `RecordOnlyProvisioner()`，不再依赖"这台机器凑巧装了真 hermes"这个隐藏前提。
- **test**: `test_recruit_from_template_provisions_credential_free_role`、`test_recruit_from_template_mixed_role_stays_ready`（显式断言"运营负责人不会被卡住"）、`test_recruit_from_template_noop_when_provisioning_disabled`。全套 280 passed / 8 skipped / 1 xpassed。

### 2026-07-19（service-claw-cloud 借鉴：启动冒烟检查 + waiting_on + 24h 异常聚合）

- **docs**: 新增 [docs/research/service-claw-cloud.md](docs/research/service-claw-cloud.md)——只读调研 UnitPulse（零关联，未触碰其任何文件/进程）的 `service-claw-cloud`（物业播本调度器的云端镜像：本地 Mac 跑 Playwright + 登录态 Chrome，云端只管状态镜像/UI/命令队列）。核心借鉴：它今天就在过 AgentPulse 迟早要过的桥——国内平台没有官方发布 API，TD-10 的 `publish_social_content` 真落地也要靠登录态浏览器/`computer_use`，已写进 TD-10"未来扩展"；命令队列+轮询认领模式是 `approval_bridge`（现在是进程内 Future，docstring 自己写明单进程 only）未来跨进程/跨机器时的现成范本；心跳/机器注册表戳破了员工卡"在线待命"目前是写死字符串的事实。
- **feat(api)**: 启动冒烟检查——`AGENTPULSE_HERMES_PROVISIONING=true` 但 `hermes` 二进制不在 PATH 上时，`app/main.py` 的 lifespan 直接拒绝启动并打印清楚的错误，而不是等第一次有人发消息触发供给才在运行时才发现（借鉴 service-claw-cloud 的 lifespan `SELECT count(*)` 冒烟测试）。
- **feat(api)**: `RunOut.waiting_on`——run 处于 `waiting_user`/`waiting_clarify` 时，查同一 run 上 pending 的审批行，拼一句"等老板批准：{描述}"/"等老板回答：{描述}"，运行轨迹卡片直接显示（借鉴 `playbook_matter_state.waiting_on`）。桌面端 `RunTraceModal` 同步渲染。
- **feat(api)**: `count_anomalies_24h` + `/me/bootstrap` 新增 `anomaly_count_24h`——过去 24 小时 `status='failed'` 的 run + `status='expired'` 的审批计数，刻意不算 `rejected`（老板自己的决定，不是异常）。借鉴 `playbook_runs.anomaly_count_24h` 的"封面页缓存汇总数字"模式。桌面端 logo 上一个红色小徽章 + hover 提示（中英文案都加了）。
- **test**: 新增 `test_main_startup.py`(3 例：关闭/找到/找不到二进制)、`test_conversation_runs_endpoint_surfaces_waiting_on`、`test_bootstrap_anomaly_count_24h`(含"rejected 不计入"的显式断言)。全套 277 passed / 8 skipped / 1 xpassed；`tsc --noEmit` 无错。

### 2026-07-17（能力授予补一个 bootstrap 缺口 + 秘书默认能力 + TD-10 设计文档）

- **fix(runtime)**: `execute_upgrade` 之前对**完全没有 `agent_specs` 行**的员工（默认秘书、以及当时 Talent Market 招募流程还没走供给的员工）一律拒绝——"+ 授予能力"按钮对这类员工形同虚设。现在会走跟"创建员工时勾能力芯片"完全一样的 `create_agent_spec`+`provision` 路径，从零 bootstrap 一个真 spec + 真 Hermes profile，再装上被授予的能力；已经是真 Hermes 员工的走原有快速路径不受影响。真机验证：给一个全新的、`hermes_profile=None` 的秘书授予 `write_code`，`hermes profile list` 里真的多出一条对应 profile。
- **feat(services)**: `create_workspace_for_user` 现在会在 `settings.hermes_provisioning` 打开时，给每个新公司的默认秘书自动配好一套免凭证、`risk_gate=auto` 的默认能力（`write_code`/`run_tests`/`task_delegation`/`content_writing`/`data_analysis`），秘书上岗即是真 Hermes 员工，不用老板手动点"+授予能力"才能用。刻意只挂全部无需凭证的能力——`provision()` 是全有或全无的供给（一个能力缺凭证就整体卡在 `blocked_on_credentials`，见"阿测"这个真实反例），塞进任何需要凭证的能力都会让秘书比现在还糟。刻意用 `hermes_provisioning` 这个已有的 flag 做门控（而不是"是否走了 HTTP 注册路由"），因为整个测试套件从不设这个 flag，如果无条件生效会让几十个假设"秘书走 DeepSeek 兜底"的既有测试全部悄悄改变行为。
- **docs**: 新增 [TD-10-business-tool-gate.md](docs/tech-design/TD-10-business-tool-gate.md)——ADR 0008 分片⑥（业务受控工具门）的设计文档：整体架构（自建 MCP 服务 + 复用现有 `approval_bridge`/`approvals` 表挂起机制，而不是等 Hermes 自己识别业务危险——它压根不认识"发布/花钱/对外发送"这类语义）、`capability_catalog.CapabilityDef` 新增 `business_tool` 字段、run_id 定位的已知技术限制、v1 范围（`send_email` 试点 + 明确排除动态风险分级）、两个 Tech-Task。更新 `EXECUTION-BOARD.md`（分片④⑤⑦+秘书默认能力移入已完成，TD-10 列为"设计已完成待实现"）。
- **test**: 新增 `test_upgrade_bootstraps_profile_for_agent_with_no_spec`（含幂等性断言）+ `test_secretary_gets_default_capabilities_when_provisioning_enabled` / `test_secretary_has_no_spec_when_provisioning_disabled`。顺手修了一个预先就存在、与本次改动无关的失败（`git stash` 验证过）：`test_login_secretary_chat_persists_deepseek_metadata` 的 mock 返回硬编码了旧的 model 字符串，改成读 `settings.deepseek_model`。全套 272 passed / 8 skipped / 1 xpassed。

### 2026-07-16（审批门分片④⑥补齐 + 真机全链路验证 + 截图拍摄清单）

- **fix(runtime)**: 补齐 ADR 0008 分片④——挂起超时对齐 Hermes ACP 的 fail-close。实测确认 `acp_adapter/permissions.py::make_approval_callback` 硬编码 60s 超时、且从不读 `approvals.timeout` 配置（该 key 只喂 CLI 交互式审批路径），所以没有去写这个死配置，而是给 `approval_bridge.await_decision` 加 `asyncio.wait_for`（新配置 `settings.approval_bridge_timeout_seconds`，默认 50s，明显小于 Hermes 的 60s 以确保我们先收敛），超时返回 `"expired"` 哨兵值并把对应 `approvals` 行标记 `status='expired'`（不再永远卡在 `pending`）。`/resolve`、`/answer` 端点对已过期的审批返回明确的"已超时自动拒绝"提示。
- **fix(orchestration)**: 补齐分片⑥——去掉 clarification/capability_upgrade 的 agent 自触发伪装。SOUL.md 不再指示员工调用 ACP 会话里根本不存在的 `clarify` 工具，改为"直接在对话里正常提问"；`runner.py` 里对应的建审批分支保留但标注为兼容历史行/未来⑤。新增**老板发起**的能力授予真路径：`GET /api/capabilities`（目录）+ `POST /api/agents/{id}/capabilities`（直接调 `execute_upgrade`，不经过挂起审批）+ 员工详情页"+ 授予能力"选择器；顺手插入 `capability_upgrade` 类型的已批准审批行供审计追溯。
- **docs**: 更新 [ADR 0008](docs/decisions/0008-human-in-the-loop-approval-model.md)（勾掉分片5/7，分片6拆成独立 TD-10）、[EXECUTION-BOARD.md](docs/tech-design/EXECUTION-BOARD.md)、`AGENTS.md` §4（更正"审批门未强制"的旧结论——路由 bug 已在 `27e34bf` 修好）。
- **fix(chore)**: `app/core/logging.py` 之前只存在于主仓库的未提交工作区（`git status` 显示 `??`），从未真正进过任何 commit——导致任何全新 clone/worktree 在 `app.main` 都会 `ModuleNotFoundError`。补提交这个文件。
- **test**: 新增审批超时的常开单测（`test_run_expires_and_marks_approval_row`）+ 真机 HERMES_E2E 测试（`test_run_expires_pending_approval_instead_of_hanging`，需要 `agentpulse` profile 提前 `config set approvals.mode manual`）；新增老板发起能力授予的 HTTP 层测试。全套 269 passed / 8 skipped / 1 xpassed（1 个无关的既有失败：`test_login_secretary_chat_persists_deepseek_metadata` 断言的 mock 模型名与 `settings.deepseek_model` 默认值不一致，与本次改动无关）。
- **真机全链路验证**（复制主仓库真实 `.env`，`AGENTPULSE_HERMES_PROVISIONING=true` 起 dev server，走真实浏览器）：招了一个真 Hermes profile 员工「阿工」，DM 它执行 `rm -rf`——审批卡真实弹出（允许一次/永远允许/拒绝三选项）；一次手动批准（真删除+员工确认消息）、一次故意晾着直到 50s 自动过期（`approvals.status='expired'`，run 正常 `completed`，员工自己汇报"第二步被系统拦截"）；用新的"+ 授予能力"按钮给阿工授予 `run_tests`，真实写入 profile + `agent_capabilities`。过程中意外发现小秘（老板秘书）在真群聊里自主判断团队缺人、自己招了 4 个新员工（产品经理/市场/设计/运营）拉进群——Agent Action Bridge 的真实自主协作能力超出预期。
- **发现（未修，已登记）**：① Talent Market 的"Hire"官方模板招聘流程从不创建 `agent_specs`/走供给，只有"Create employee"自定义招聘（带 `role_spec`）才会真的拿到 Hermes profile——意味着从人才市场招的员工永远走临时 DeepSeek 兜底，成长轨迹/真执行都摸不到。② 本地 `services/api/.env` 若含真实 DeepSeek key，裸跑 `pytest`（不加 `HERMES_E2E`）会让 3 个原本 mock 掉的测试改走真实网络，把 8 秒的测试跑成 22 分钟且引入新的假失败——和已知的 Hermes-provisioning-泄漏问题同源但是另一个坑，值得给 `.env`加一条类似的警告注释。
- **docs**: 新增 [docs/SCREENSHOT-CHECKLIST.md](docs/SCREENSHOT-CHECKLIST.md)——README 双语截图拍摄清单（10 个场景 × 中英文，基于本次会话已经拍好的真实丰富状态：8 员工/真群聊/真审批卡）。AI 助手没有能把 Browser 截图存成文件的工具，本轮改为交付清单，图片由人工拍摄后按清单文件名接上 README。

### 2026-07-15（新 logo：Checkpoint 标记，全项目/官网统一替换）
- **feat(brand)**: 设计并落地新 logo——「Checkpoint」：几何化字母 A，横杠切成一个卡口 + 一个常亮检查点，呼应产品"每个高风险动作都要过老板这道闸"的核心承诺；同时仍可读作字母 A（AgentPulse），能和文字标一起用。手绘矢量 SVG（无图像生成模型可用，矢量本就是 logo 该有的交付形态），在 16px favicon 尺寸下实测依然清晰。
  - `apps/site/favicon.svg`：换成新标（深底 + teal 渐变 A + 卡口 + 检查点）。
  - `apps/site/index.html` + `styles.css`：nav/footer 的 `.brand-mark` 从"跳动圆点"换成新标（深色 A + teal 卡口 + 亮点，贴合已有 teal 渐变容器）；保留一个克制的检查点呼吸动效（`prefers-reduced-motion` 已有全局兜底覆盖）。
  - `apps/desktop/src/main.tsx` + `styles.css`：侧栏 `.brand-mark`、登录页 `.auth-mark`、onboarding 弹窗 3 处 `.onboarding-logo`（均为原先的"✦"字符）统一换成新标；`.onboarding-logo` 顺手把容器底色从遗留的硬编码蓝 `#3b5bdb`（6 处，teal 改版时漏改的）换成 teal 渐变 token，否则新标会和旧蓝底冲突。
  - **浏览器实测**（官网 + 桌面端真登录，走完认证页→onboarding 三步→侧栏）：5 处 mark 位置渲染一致、无 console 报错；`tsc --noEmit` 桌面端无误。
  - 过程：用 brandkit 方法论构思了 3 个方向（Roster/Handoff/Checkpoint），做成对比页发布为 Artifact 供选型，选定 Checkpoint 后实现。
  - 暂未覆盖：Electron 应用本身的 dock/窗口图标（.icns/.ico，当前项目本就没有自定义过，用的是 Electron 默认图标）——如需要是独立的多分辨率图标生成工作，未来可补。

### 2026-07-14（官网 agentpulse.cc + 双语 README）
- **feat(site)**: 新增官方营销官网 `apps/site`——纯静态(HTML/CSS/JS,零构建依赖),英文版。深色「操作驾驶舱」+ teal「脉搏」品牌信号;AI 特效:hero 脉搏网络 canvas(节点+沿边流动的脉冲)、辉光 aurora、滚动 reveal(带**兜底**:过渡在无头/后台会冻结导致空白 → 超时强制显形,遵循设计 skill 的"reveal 只增强、绝不门控可见性")、实时群聊打字演示(讨论→共识纪要卡→审批卡循环)。字体避开训练集默认:Bricolage Grotesque + Hanken Grotesk + JetBrains Mono。`prefers-reduced-motion` 全兜底、响应式、无 gradient-text/side-stripe 等 slop。`vercel.json`(cleanUrls+安全头);Vercel 原生 Git 集成 → 设 Root Directory=`apps/site` 即每次 push 自动部署(见 `apps/site/README.md`);自定义域 agentpulse.cc 在 Vercel 面板绑定。浏览器实测:9 区块/内容齐全、hero 渲染正确、无 console 报错。
- **docs(readme)**: README 改为**多语言、默认英文**——`README.md`(英文,默认)+ `README.zh-CN.md`(中文),顶部互相语言切换 + 官网徽章;两版共用 `docs/images/` 截图。

### 2026-07-14（ADR 0008 分片1-3：技术危险动作的真审批门 —— 已真机验证）
- **feat(runtime+api+desktop)**: 让高危动作在真执行路径上**真的挂起等老板拍板**,批准才跑、拒绝真拦。修掉 2026-07-14 审计发现的"审批门不生效"根因。
  - **分片1**（`profile_provisioner.configure`）：供给员工 profile 时设 `approvals.mode: manual`。此前没配 → Hermes 落 `smart` 档用辅助模型自动放行危险命令;`manual` 让它走 ACP `request_permission` → 我们的 client。
  - **分片2**（`hermes_client.request_permission`）—— **关键 bug 修复**：之前批准返回 `SelectedPermissionOutcome`,但 Hermes(`acp_adapter/permissions.py`)只认 `AllowedOutcome`,其余一律当 deny → **"批准"被静默读成"拒绝",批了也放不了行**。改为:允许→`AllowedOutcome(outcome="selected", option_id=…)`,拒绝→`DeniedOutcome(outcome="cancelled")`;按 **option_id**(allow_once/allow_session/allow_always/deny)选项(kind 有歧义:Hermes 给 allow_session 的 kind 也是 allow_always)。
  - **分片3**（审批桥/端点/前端）：`ResolveApprovalRequest` 加 `scope: once|always`;`/resolve` 把决定映射成 `allow_once|allow_always|deny` 喂桥;桥/resolver 贯通三态;前端审批卡按钮改为 **允许一次 / 永远允许 / 拒绝**（能力升级卡为"批准并升级"）。"永远允许"→ `allow_always` → Hermes 自己把规则写进 allowlist,下次同类不再问。
  - **真机验证**（一次性 profile,经 `LocalHermesProvisioner` 供给 + `HermesBackend` 执行,非 seed）：让真 agent `rm -rf <dir>` —— **拒绝→目录保留(命令被拦)、批准→目录真被删(命令执行)**,`request_permission` 各触发 1 次,均 PASS。config.yaml 确认 `approvals.mode: manual` 落地。
  - **零回归**：全套 **250 通过 + 7 skipped**;前端 `tsc` 无错。
  - 剩余（ADR 0008 后续分片）：挂起超时与 Hermes 60s fail-closed 对齐;业务受控工具门（发布/花钱/对外发送,独立 TD）;删 clarification/capability 的 agent 触发伪装(求援已天然走普通对话、能力升级改老板发起)。

### 2026-07-14（审计修正：审批门在真运行时不生效 —— 纠正 T4/卡片的过度声称）
- **audit + fix**: 对真 Hermes 做了实测审计,发现之前 TD-03-T4「审批 suspend/resume」及聊天卡片的「闭环可演示」声称**对真运行时不成立**,之前的"验证"全靠 fake backend + seed 的 approval 行。如实纠正:
  - **实测(真 Hermes ACP,agentpulse profile)**:① 让 agent 执行 `rm -rf ...` —— agent **直接执行成功,`request_permission` 触发 0 次**。ACP 执行路径下 Hermes 不为内置危险工具(terminal 等)调客户端审批,`--yolo` 未传也一样。② 让 agent 求援 —— agent 思考里明说"**我没有 clarify 工具**"(clarify toolset 虽 CLI 标 enabled,但 ACP 会话未暴露给模型),于是**用普通消息把问题问出来就结束**了。
  - **结论**:high_risk / clarification / capability_upgrade 三类审批**真 agent 都产生不了**;审批桥、卡片、`/resolve`、`/answer`、`execute_upgrade` 都是能跑的真代码,但**只有 seed/手动建的 approval 行能触发**。→ 北极星「老板拍板制」**当前未在执行层真强制**(危险命令裸跑)。求援其实已天然可用——agent 发消息问、老板回、下轮带答案继续,**不需要专门的求援卡**。
  - **本次已修**:① `LocalHermesProvisioner.reload_gateway` 之前调 `hermes gateway reload`——**该子命令不存在(实测 v0.18.2 只有 run/start/stop/restart/…)**,且我们每次 run 起新 `hermes acp` 会重读 config、加的工具下一次 run 自动生效,无需 reload → 改为诚实 no-op。② AGENTS.md §4 Hermes 行由"🟢 已接入含 T4"改为"🟡 执行真、审批门未真强制";看板 T4/卡片条目加 ⚠️。
  - **真修法(待定/看板)**:审批门要在**执行层真强制**,候选:(a) 最小权限——不给员工 profile 开 terminal/code_execution/computer_use,危险能力必须老板显式授予;(b) 调研 ACP `initialize` 的 client permission 能力握手,看能否让 Hermes 真触发 `request_permission`;(c) 自研门(在我们这层拦截/不给危险工具)。这是架构决策,需项目所有者拍板后再实现,不再靠 seed 假装。

### 2026-07-14（聊天内审批/求援/能力升级卡片 —— 让「拍板」在 UI 上可操作）
- **feat(desktop)**: 员工执行挂起时，聊天里直接弹出可操作卡片，老板当场批准/驳回/回答/确认升级，续跑同一个 run。补上了 TD-03-T4 / TD-06-T2 缺的前端一环。
  - SSE `approval` 事件（`_persist_run_approval` 发的，带 `approval_id`+`category`+`tool_call`）此前前端**根本没处理**（switch 里没有分支）→ 现在插一条 `APPROVAL_CARD:{json}` 系统消息进当前会话。
  - 新增 `ApprovalCard` 组件，按 category 渲染：**高风险**（标题+说明+批准/驳回）、**澄清**（问题+文本框+提交回复 → `POST /approvals/{id}/answer`）、**能力升级**（可编辑的能力 key + 批准并升级 → `POST /approvals/{id}/resolve` 带 `approved_capability_key`）；点击后卡片原地翻转成「已批准 / 已回复 / 已批准升级：<key>」。
  - `resolveChatApproval` / `answerChatClarification` 两个 handler 在 App 定义，经 `ChatPanel`→`MessageItem`（新增 `onResolveCardApproval` / `onAnswerCardClarification` props，与任务侧 `onResolveApproval` 区分）传入。
  - `styles.css` 加 `.approval-card` 及三类配色（risk=warning、clarify=primary、upgrade=紫），浅深主题通用。
  - **浏览器实测**（独立 API 8001 + renderer 5175）：登录 demo → 群聊出现 3 类卡片、渲染/配色正确；点「批准」→run 关联 `/resolve` 成功、卡片翻转；点「批准并升级」→`execute_upgrade` 真把 `web_scraping` 装进 `agent_capabilities`（status=enabled）、卡片翻转；`tsc` 无错、无 console 报错。
  - 至此「群讨论 → 共识拍板 → 执行 → 高危/求援挂起 → 老板在聊天里拍板 → 续跑」整条闭环在 UI 上**可操作、可演示**。

### 2026-07-14（TD-06-T3：成长轨迹前端 —— 让自进化「可见」）
- **feat(desktop)**: 员工详情抽屉 `AgentDetail` 新增「成长轨迹」区，把 TD-06-T1/T2 的后端成果接成用户能看的界面。
  - **已获得能力**：拉 `GET /api/agents/{id}/spec` 的 `capabilities`，渲染能力徽章 + 状态（`已启用`/`待补凭证`/`待生效`/`已停用`，`credential_missing` 用 warning 色、`disabled` 用 subtle 色）。
  - **已习得技能（自动沉淀）**：拉 `GET /api/agents/{id}/skills`，卡片显示 SKILL.md 首行标题 + 正文摘要；空态"干活满一定轮次后会自动沉淀可复用技能"。
  - **触发反思**按钮：调 `POST /api/agents/{id}/reflect`，内联显示"新沉淀 N 条技能 / 这轮暂无 / 错误信息"，成功后刷新技能列表。
  - `AgentDetail` 加 `token` prop；`styles.css` 加 `.growth-head`/`.growth-sub`/`.cap-badge`/`.skill-card`/`.button.small`（全用现有 teal / warning token，浅深主题通用）。
  - **浏览器实测**：起独立 API(8001)+renderer(5175，`.env.local` 指过去，避开另一会话占用的 8000/5174)→登录 demo 工作区→员工→阿伦→成长轨迹正确渲染（`social_content 已启用` + `email_sending 待补凭证` + 技能空态）；`tsc --noEmit` 无错、无 console 报错、截图留证。
  - 剩余：聊天内「审批/求援/能力升级」卡片（SSE `approval` 事件已带 category+approval_id，前端按类型渲染并调 `/resolve`\|`/answer`）另立一项。

### 2026-07-14（TD-06-T2：主动能力升级申请 —— 北极星④自进化第二半）
- **feat(runtime+api)**: 员工碰到"有任务但缺工具"时主动申请升级，老板一键批准即自动把能力装到它的 Hermes profile。
  - `runtime/upgrade.py::execute_upgrade`：读老板确认的 `approved_capability_key` → `resolve_bundle` 校验并合并出 toolsets/skills/mcp/creds/risk_gate → `ProfileProvisioner.add_capability`（真装：`tools enable` + `skills install` + `gateway reload`）→ upsert `agent_capabilities` 行（有 `required_credentials` → `credential_missing` 交给现有凭证流程要，否则 `enabled`）。未知 key / 无 profile → `UpgradeError`。
  - `runner._persist_run_approval`：加 `capability_upgrade` 分支——标题"员工申请能力升级"、`payload_json` 存 `capability_description` + `suggested_capability_key`。
  - `routes/workspace.py` `/approvals/{id}/resolve`：识别 `type=capability_upgrade`——批准时先 `execute_upgrade`（用老板确认或 agent 建议的 key）装好能力，再唤醒挂起的 run。`ResolveApprovalRequest` 加可选 `approved_capability_key`（老板可改 agent 猜的 key）。
  - **顺带修 bug**：另一会话重写 resolve 端点时把 `approved`/`rejected` 原样传给 `approval_bridge.resolve_pending`，但 `hermes_client` 的 permission 映射只认 `allow_once`/`deny` → **批准会被误映射成拒绝**。已改为 `bridge_decision = "allow_once" if approved else "deny"`，高危审批的批准路径现在真的放行。
  - SOUL(`_build_soul`)：铁律加"因缺工具/MCP/权限无法完成时，用 `clarify` 提交 `capability_upgrade` 申请，等老板批准自动获得能力"。
  - **零回归**：新增 `test_upgrade.py` 6 例（无凭证→enabled、有凭证→credential_missing、未知 key/无 profile→报错、幂等 upsert 不重复、resolve 端点全链：批准→装 write_code→`agent_capabilities` 落行 + run 续跑）；全套 **250 通过 + 7 skipped**。三条架构 grep 干净。
  - 说明：TD-06-T2 的 provisioner 方法（`add_capability`/`reload_gateway`）由另一会话先行提交（step 2/6，commit `e3a8101`），本次补齐 UpgradeService + 审批创建/解析接线 + SOUL + 测试，完成整条闭环。
  - Verified: execute_upgrade 经生产路径 `POST /api/approvals/{id}/resolve`（type=capability_upgrade 批准分支）调用。

### 2026-07-13（清理：合并两套 TD-03-T4 的残留债务）
- **chore(cleanup)**: 两个会话平行实现了 TD-03-T4，快进合并后留下债务，本次清理（方向未跑偏——三条架构 grep 干净、生产路径已收敛到 `make_bridge_resolver`）：
  - 删除死码 `runner._make_approval_resolver`（已被 `make_bridge_resolver`+`_persist_run_approval` 取代、不在生产路径）及其 `tests/test_approval_suspend.py`（258 行只测死码，正是 AGENTS.md §5 警告的反模式）；去掉 `runner.py` 重复定义的 `_now_iso` 与随之无用的 `asyncio`/`json`/`new_id` import。
  - 恢复被 `a7d3227` 从 CHANGELOG 误删的 5 条 2026-07-13 记录（下列 T4/TD-06-T1/TD-08-T2/许可证/截图）。
  - 全套 **测试通过、零回归**。

### 2026-07-13（TD-03-T4：审批 suspend/resume + 求援 —— 北极星「老板拍板制」双向闭环）
- **feat(runtime+api)**: 高风险动作现在能**挂起 run → 等老板批 → 原地续跑同一个 run**（此前是 deny-by-default 只能拒）。
  - `runtime/approval_bridge.py`：进程内 Future 注册表（单 uvicorn 进程）。`await_decision(approval_id)` 挂起、`resolve_pending(approval_id, decision)` 用 `call_soon_threadsafe` 线程安全唤醒。因 ACP 是 stdio 子进程、run 状态活在子进程里，**只能唤醒活协程、不能 detach 重连**——所以续跑=唤醒挂起中的 `request_permission`，不是重发 Hermes。
  - `hermes_client.py`：`request_permission` 生成 `approval_id`、发 `approval_required` 事件（带 approval_id + category）、`await permission_resolver(...)` **原地挂起**（ACP 会话与 run 一起停住），拿到决定再映射成 allow/deny 选项返回；run() 事件循环在挂起期**禁用超时**。
  - `runner.py`：`make_bridge_resolver()`（resolver 注册并 await 桥）；`stream_agent_run` 收到 approval_required → 落 `approvals`(带 `run_id`+`type`) + 转 run 到 `waiting_user`/`waiting_clarify` + commit + 推 SSE。`_persist_run_approval` 按 category 生成标题/风险级。
  - 热路径 `_stream_reply_events` 注入 `make_bridge_resolver()`，员工高危动作真正挂起等批（idle/reflection 仍传 None → deny-by-default，安全）。
  - `/api/approvals/{id}/resolve`：run 关联审批 → 转 run 回 `running` + `resolve_pending(allow_once/deny)`，**跳过**旧的"审批即完成任务"逻辑（那是给非 run 的手动审批卡的）；批准→agent 续跑执行，驳回→agent 收到 reject 走替代/收尾。
  - `/api/approvals/{id}/answer`（新）：clarification 类——记录答复为会话消息 + 唤醒续跑。**已知限制**：ACP permission 响应只带 allow/deny，答复**文本**目前经会话历史带回（agent 下一轮读到），真·inline 注入待 Hermes resume API，跟进项。
  - SOUL 已含"需求不清先问 / 高危等老板"铁律（无需改）。
  - **零回归**：`test_approval_flow.py` 9 例（桥 await/resolve、未知 resolve 返回 False、fake backend 全链路挂起→approve 续跑 / deny 走替代、resolve 端点转 run、answer 记录消息+续跑、非 clarification /answer→404）。三条架构 grep 干净。
  - Verified: make_bridge_resolver 经生产路径 `workspace.py::_stream_reply_events` → `stream_agent_run` 注入；resolve/answer 经 `POST /api/approvals/{id}/resolve|answer` 驱动 `approval_bridge.resolve_pending`。

### 2026-07-13（TD-06-T1：技能自动沉淀 —— 北极星④「越用越懂你」后端闭环）
- **feat(runtime)**: `app/runtime/reflection.py` —— 员工把最近工作提炼成可复用技能，沉淀进自己的 Hermes profile。
  - `_summarize_recent_steps`：拉该员工最近 N 个 completed run 的 `run_steps`（tool_call/tool_result/message/final）压成流水文本。
  - `run_reflection`：注入提炼 prompt（强制严格 JSON）→ 经员工自己的 Hermes profile 执行 → `parse_skills` 容错解析（去围栏/取数组/校验/裁剪/最多 3 条）→ 逐条 `ProfileProvisioner.update_skill` 写进 profile `skills/auto/<name>.md` → 重置 `runs_since_last_reflection` + stamp `last_skill_reflection_at`（空输出/后端异常/无流水也会 stamp，不 hot-loop）。不绑会话、不建 runs 行。
  - `bump_reflection_counter`：`runner.stream_agent_run` 每完成一个 run 给该 agent +1，到 `reflection_interval`（默认 5）触发；实际反思由后台 `run_reflection_tick` 跑（off hot path）。
  - `ProfileProvisioner` 加 `update_skill` / `list_skills`（RecordOnly 记内存 + LocalHermes 读写 `skills/auto/`；**修复**：全中文技能名会 sanitize 成空导致文件名 `skill.md` 互相覆盖 → 空 slug 时用名字 sha1 哈希保唯一）。
  - schema：`agent_specs` 加 `runs_since_last_reflection`/`last_skill_reflection_at`/`reflection_interval`（双 schema，ensure_column 迁移）。
  - API：`GET /api/agents/{id}/skills`（成长轨迹）+ `POST /api/agents/{id}/reflect`（手动触发，调试/演示）。
  - SOUL：`_build_soul` 追加"每完成一项任务就用 skills.learn 记一条经验"的自我进步指令。
  - cron：后台循环在 idle tick 后追加 `run_reflection_tick`（复用 `idle_thinking_cron` 开关 + 同一 backend/provisioner）。
  - **真机验证**：LocalHermesProvisioner 对真 agentpulse profile 的 `update_skill`/`list_skills` 物理写读通过（并清理）；`test_run_reflection_real_hermes`（`HERMES_E2E=1`）跑通。
  - **零回归**：新增 `test_reflection.py` 14 单测；三条架构 grep 干净。
  - 偏差说明：TD-06 设计里的 `run_steps(type='skill_learned')` 因 `run_steps.type` 有 CHECK 白名单**暂略**；技能可见性改由 `GET /skills` 读 profile 技能目录提供，不影响 DoD。
  - Verified: bump_reflection_counter 经生产路径 `runner.stream_agent_run` 完成分支调用；run_reflection 经 `POST /api/agents/{id}/reflect` 与后台 `main.py::_idle_cron_loop` 驱动。

### 2026-07-13（TD-08-T2：空闲员工主动想 idea —— 北极星⑤后端闭环）
- **feat(runtime)**: `app/runtime/idle_think.py` —— 落地"没有 idle 员工"：员工空闲够久就自发反思并产出 idea。
  - `find_due_idle_agents`：选出符合条件的员工（`agent_specs.status='ready'` + `idle_thinking_enabled` + 有 `hermes_profile` + 距 `last_idle_think_at` 超过 `idle_think_interval_hours` + 无活跃 run）。
  - `trigger_reflection`：注入反思 prompt（改进/机会/风险/学习，强制严格 JSON 输出）→ 经与 RunService 相同的 `RunBackend` 接口调 Hermes → `parse_ideas` 容错解析 → 写入 `ideas` 表 → stamp `last_idle_think_at`。空数组/解析失败/后端异常都会 stamp 且不抛，避免 cron hot-loop。不绑会话、不建 runs 行。
  - `run_idle_tick`：一轮扫描所有 due 员工逐个反思，返回 `{agents_processed, ideas_created}`。
  - cron：`main.py` startup 起后台 asyncio 循环，`config.py` 加 `idle_thinking_cron`（默认 false）+ `idle_cron_interval_seconds`（默认 3600）；默认关，测试/无 Hermes 环境不受影响。
  - **零回归**：新增 `test_idle_think.py` 12 常开单测 + 1 guarded e2e（`HERMES_E2E=1`，真机跑通）。三条架构 grep 干净。
  - Verified: run_idle_tick 经生产路径 `main.py::_idle_cron_loop`（startup 后台循环，`idle_thinking_cron` 开启时）驱动。

### 2026-07-13（许可证改用 PolyForm Noncommercial + README 截图）
- **docs(license)**: 把自写的"学习免费/商业需授权"自定义协议换成业界成熟标准 **[PolyForm Noncommercial License 1.0.0](LICENSE)**（非商业用途免费、商业另需授权），法律措辞更规范可靠。商业授权联系方式从 README 移到独立的 [COMMERCIAL.md](COMMERCIAL.md)；README 不再出现商业授权招揽内容，License 徽章/段落同步更新。
- **docs(readme)**: 新增桌面工作台真实截图——顶部 hero（群讨论 → 共识纪要 → 老板拍板）+「界面预览」画廊（任务中心 / 人才市场 / 想法中心 / 深色主题），图存 `docs/images/`。截图前把后端(SQLite)+前端端到端跑通并走查了登录→群讨论→共识纪要卡→任务→人才市场→想法全流程，生产 UI 路径均正常。

### 2026-07-10（TD-03-T5：自动供给 —— 招人即真员工）
- **feat(api+runtime)**: 招一个员工（带 role_spec）就自动创建一个**可运行的 Hermes profile**，热路径随即自动路由到它——不再手动置 spec。
  - `profile_provisioner.build_provisioner_from_settings()`：按 `settings.hermes_provisioning` 选 `LocalHermesProvisioner`（真 CLI）或 `RecordOnlyProvisioner`（默认，测试/无 Hermes 环境）。
  - `orchestration/supply.provision`：默认走配置选出的 provisioner；能力就绪时——建 profile → **写 SOUL.md**（`_build_soul`：角色/职责/工作方式 + "不清楚先问、高风险等老板"铁律）→ 配 `deepseek/deepseek-v4-flash`+toolsets → 装 skills → **写 DeepSeek key 进 profile .env**（员工即刻可跑）→ 回填 `agent_specs.hermes_profile`+status=ready；profile 名合法化为 lowercase alnum（Hermes 要求）。
  - `config.py`：加 `hermes_provisioning`（默认 false）。
  - **真机 e2e 过**（`test_auto_provision.py`，`HERMES_E2E=1`）：`provision` 开供给开关 → 真 Hermes profile 落地（config 有 v4-flash、SOUL 含角色、.env 有 key）、spec ready，然后清理。
  - **零回归**：默认 RecordOnly，全套 **207 通过 + 5 skipped**；新增 2 常开 wiring 测（SOUL/credentials 被记录、profile 名格式、flag off→RecordOnly）。
  - 至此 TD-03 主链闭环：**招人→自动建真 profile→发消息→Hermes 执行→run_steps+回写**，全部真机验证过。只剩 TD-03-T4 审批 suspend/resume（现 deny-by-default，安全）。

### 2026-07-10（TD-03-T3 后半：热路径切换 —— 员工在 App 里真的经 Hermes 干活）
- **feat(api+runtime)**: 把跑着的消息流从"临时 DeepSeek"切成"经 RunService 调 Hermes"，**有 ready profile 的员工走 Hermes、其余回退 DeepSeek（零回归）**。
  - `runtime/runner.py`：`start_run` 重构为流式 `stream_agent_run`（边持久化 run_steps + 回写 message，边 yield `{type:chunk|message|tool_call|...}` 给 SSE）；新增 `resolve_hermes_profile(agent_id)`（`agent_specs.hermes_profile` 且 status=ready 才走 Hermes）。
  - `routes/workspace.py`：新增共享 `_stream_reply_events`——按 agent 是否有 ready profile 选 Hermes(`stream_agent_run`+`HermesBackend`) 或 DeepSeek 回退；`send_message_stream` 的**群讨论 turn_executor 与 DM/单人两条分支都改用它**（run_discussion_round 契约不变）。Hermes prompt = 讨论上下文 + 老板消息（人格来自 profile SOUL）。approval_required → SSE `approval` 事件（deny-by-default，安全）。
  - `core/config.py`：加 `hermes_work_root`（绝对，空则运行时解析为 cwd 下绝对 `.hermes-data`）+ `hermes_bin`。
  - **真机 e2e 过**（`test_hot_path_hermes.py`，`HERMES_E2E=1`）：把一个 agent 的 `agent_specs` 置 `hermes_profile=agentpulse`/ready → DM 经 `/messages/stream` 发消息 → 断言产生 `runs(provider='hermes',completed)` + `run_steps(message,final)` + agent 消息 "OK" 落库。
  - **零回归**：现有 205 测试全过（无 profile 的 agent 一律走原 DeepSeek 路径，行为不变）；4 guarded e2e。
  - 剩余：审批 suspend/resume 闭环（TD-03-T4）、招人自动建 profile（`LocalHermesProvisioner` 接进 supply，TD-03-T5）——现在要手动置 spec。

### 2026-07-10（TD-03-T3 写半：RunService —— run→run_steps→回写消息）
- **feat(runtime)**: `app/runtime/runner.py::start_run` —— 把一次 Hermes 执行完整落库。
  - 消费 backend（`HermesBackend` 或测试用 fake）的 `AgentEvent` 流：按 TD-03-T1 生命周期建 `runs`(queued→running→completed/failed)；thinking/message 分别缓冲、每轮各落 1 条 `run_steps`；tool_call/tool_result/approval_required 逐条落；聚合出的 agent 回复经 `add_message` 写回会话；`final` 步 + 状态转移 + `output_message_id` 回填；错误→run=failed 记 error。
  - **真机 e2e 过**：RunService → HermesBackend(ACP) → 真 Hermes(`agentpulse` profile, DeepSeek) → run 完成、run_steps 有 message+final、agent 消息 "OK" 落库。
  - 测试：新增 `test_runner.py`——2 常开（fake backend：断言 thinking 聚合成一条、tool_call 逐条、message 回写、run 完成并回填 output_message_id；error→failed）+ 1 guarded 真机 e2e（`HERMES_E2E=1`，实测过）。全套 **205 通过 + 3 skipped**。
  - 剩余（TD-03-T3 后半，需 agentpulse 会话）：把群讨论/回复热路径的执行从临时 DeepSeek 切成经 RunService 调 Hermes（员工↔profile 映射）+ approval 挂起/续跑闭环。

### 2026-07-10（TD-03-T2：HermesBackend —— 员工经 ACP 真执行）
- **feat(runtime)**: `app/runtime/hermes_client.py` —— 让 AI 员工**真正跑起来**（照 [ADR 0007](docs/decisions/0007-hermes-v0.18-interface-acp.md) 走 ACP，不是 REST）。
  - 起 `hermes --profile <p> acp` 子进程，用官方 `agent-client-protocol==0.9.0`（已入 requirements）走 ACP（newline JSON-RPC，`use_unstable_protocol=True` 对齐 agent 侧）：`initialize` → `session/new(cwd=绝对 workdir)` → `session/prompt`。
  - 把 ACP `session_update` 流映射成统一 `AgentEvent`（message / thinking / tool_call / tool_result / usage / status / final）；`request_permission` → 发 `approval_required` 事件 + 由注入的 resolver 决策并**总是选中 agent 提供的某个选项**（默认 deny=选 reject_once）；`read/write_text_file` 限制在 `workdir` 内（ADR 0005，路径逃逸即抛错）；terminal 工具默认拒绝。
  - `RunContext`/`AgentEvent` 数据类；`run()` 是 async 生成器，子进程/连接在 finally 里干净收尾，带超时。
  - **真机 e2e 实测通过**：`agentpulse` profile 收到 "reply OK" → 流式 thinking（"The user wants me to…"）+ message "OK" + usage(tokens) + final(end_turn)，16 事件映射正确。
  - 测试：新增 `test_hermes_backend.py`——2 常开安全测（workdir 必须绝对、`_safe_path` 拒绝逃逸）+ 1 guarded 真机 e2e（`HERMES_E2E=1`，实测过：断言到 final + message 含 "OK"）。全套 **203 通过 + 2 skipped**。
  - 解锁 TD-03-T3（RunService：消费事件流写 run_steps + 结果回写 + 审批挂起/续跑）。

### 2026-07-10（Hermes 接入地基 + TD-04-T6 供给器 + ADR 0007 接口修正）
- **feat(runtime)**: 真正把 Hermes 接进项目的第一步（本机 Hermes v0.18.2，项目所有者提供真实 DeepSeek key）。
  - **key 验证**：新建 isolated profile `agentpulse`（`deepseek/deepseek-v4-flash`、绝对 workdir、key 写进 profile 的 gitignored `.env`），`hermes --profile agentpulse -z "..."` 实测返回结果——key + 模型 + agent loop 全通。key 存 `services/api/.env`（gitignored），未入库/日志/commit。
  - **重大接口修正（[ADR 0007](docs/decisions/0007-hermes-v0.18-interface-acp.md)）**：v0.18.2 **没有** 旧设计假设的 REST `POST /v1/runs`+SSE（`hermes gateway` 现在是消息平台网关）。程序化接口是 `hermes acp`（ACP，stdio JSON-RPC，`--check` OK）与 `hermes serve`（JSON-RPC/WS）。决定 `HermesBackend` **改用 ACP** 传输；`agents.hermes_gateway_port`（端口/一员工一网关）作废；作废 DATA-MODEL/TD-03 §5.3 的 REST 段（已加醒目标注）。
  - **TD-04-T6 `LocalHermesProvisioner`**（`app/runtime/profile_provisioner.py`）：用实测过的 `hermes` CLI 真建/配/删 profile——`profile create --no-alias --no-skills`、`config set model`、`config set terminal.working_dir <绝对路径>`（ADR 0005 硬约束，构造函数强制 `work_root` 绝对）、`tools enable`、`skills install --yes`、凭证追加进 profile `.env`（值不入日志）。
  - 测试：新增 `test_local_provisioner.py`——2 条 always-on（work_root 必须绝对、hermes 不存在报错），1 条 guarded e2e（`HERMES_E2E=1` 真跑 hermes：建 profile→配 model+绝对 workdir+toolsets→写 SOUL/creds→删，已实测通过）。全套 **201 通过 + 1 skipped**。
  - DeepSeek 实测事实：provider `deepseek`、key env `DEEPSEEK_API_KEY`、base `https://api.deepseek.com/v1`、模型 `deepseek/deepseek-v4-flash`|`-pro`。

### 2026-07-10（Idea 中心前端：桌面端「想法」视图）
- **feat(desktop)**: 把 TD-08-T1 的 idea API 接成界面（TD-08-T3 前端半），落地北极星"没有 idle 员工 → idea 中心"的用户界面。
  - `main.tsx`：`View` 增加 `ideas`，侧栏新增「想法」入口（lightbulb 图标）；新增 `IdeasView` 组件——自取 `GET /api/ideas`，顶部摘要（共 N 条 / 来自几位员工）+ 分类过滤 tabs（改进/机会/风险/学习）+ 想法卡片（员工头像取 per-agent hue、分类标签配色、标题、正文、时间）+ 三个动作：转为讨论 / 接受 / 忽略。
  - "转为讨论"走 `POST /api/ideas/{id}/convert` → 由 App 回调重载 bootstrap 并 `openChat` 跳进新建的讨论群（首条系统消息=想法内容）。
  - `styles.css`：新增想法视图样式，全部用 teal 设计 token。
  - 验证：`tsc --noEmit` 无错；浏览器实测经 API 播种 2 条想法 → 列表正确渲染、分类标签/头像正常 → 点"转为讨论"成功建群并自动跳转、群里第一条即想法内容、无 console 报错。idle 自动产 idea（TD-08-T2）仍待 Hermes。

### 2026-07-10（TD-07-T2 前端半：创建员工"按职位快速配置"）
- **feat(desktop)**: 把"按职位一键招人"接到创建员工弹窗，TD-07-T2 完整闭环。
  - `CreateAgentModal` 新增 `token` 入参 + 拉取 `GET /api/role-bundles`；顶部新增"按职位快速配置（可选）"芯片行（12 个预配角色），点选即把该角色的能力清单写入选择、并在名称/部门为空时自动填成角色名；再点一次取消。
  - 新增"已选 N 项能力：…"摘要行，让通过角色选中的业务能力（即使不在手动能力网格里）也可见。
  - 顺手清理：该弹窗残留的写死紫色（`#6c5ce7`/`#f0edff`）能力芯片改用 teal 设计 token（`.cap-chip` / `.role-bundle-chip` 类 + `styles.css`），与全局重做一致。
  - 验证：`tsc --noEmit` 无错；浏览器实测点"数据分析师"→ 芯片高亮、名称/部门自动填、摘要显示 `data_query · data_analysis · report_generation`、无 console 报错。

### 2026-07-10（渠道管理前端：桌面端「渠道」视图）
- **feat(desktop)**: 把 TD-09-T2 的渠道 API 接成界面（TD-09-T3 前端半）。
  - `apps/desktop/src/main.tsx`：`View` 增加 `channels`，侧栏新增「渠道」入口（hub 图标）；新增 `ChannelsView` 组件——自取 `GET /api/channels`，含创建表单（名称/类型/默认分配员工）、渠道卡片列表（类型标签、启用状态带 pulse 点、webhook 完整 URL + 一键复制、停用），全部走 `apiRequest`。
  - `styles.css`：新增渠道视图样式（表单/卡片/URL 行/状态点），全部用现有 teal token 与 pulse-ring 动画。
  - 验证：`tsc --noEmit` 无错；浏览器实测创建 2 个渠道 → 卡片正确渲染带完整 webhook URL、复制/停用可用、无 console 报错。TD-09-T3 仅剩 ChannelReply 回发 + 微信/widget 适配器（需真实账号）。

### 2026-07-10（TD-07-T2 API 半：按职位招人的后端契约）
- **feat(api)**: 为"按职位一键招人"提供后端契约（前端"按职位选"UI 待做）。
  - `GET /api/role-bundles`（`routes/catalog.py`，认证）：列出 12 个预配角色及其 `capability_keys` 与 `resolved`（合并后的 toolsets/mcp/creds/risk_gate），供创建员工弹窗渲染选项。
  - `POST /api/agents` 的 `role_spec` 新增可选 `role_bundle_key`：有值时经 `get_role_bundle` 展开并"bundle 优先去重"并入 `capability_keys`（可与手动勾选叠加）；未知角色 → 400。
  - 测试：新增 `test_role_bundles_api.py` 5 例（列角色 + 认证要求、按角色建员工能力自动填齐、bundle 与显式 key 合并、未知角色 400）。全套 **199 测试通过**（+5）。

### 2026-07-10（TD-07-T1：业务岗位能力目录扩展）
- **feat(api)**: 让"用一句话招人"覆盖业务岗，不只是技术岗。
  - `orchestration/capability_catalog.py` 在技术岗种子外补齐 **7 大类 31 个业务能力**：客服（customer_service/ticket_management/refund_processing/customer_data_lookup）、内容运营（content_writing/image_creation/email_drafting/email_sending/seo_content/ad_analysis/ad_bidding）、数据（data_query/data_analysis/report_generation/web_scraping）、HR、法务、财务（含 `payment_execution` = `prohibited_auto`，花钱不可逆永不自动）、项目管理。
  - 新增 `ROLE_BUNDLES`（12 个预配角色：客服专员/数据分析师/HR 专员…→ 能力清单）+ `get_role_bundle(name)`/`list_role_bundles()` 访问器；供 TD-07-T2"按职位一键招人"用。
  - 对齐修正：种子里 `social_content` 的 toolsets 由代码侧 `[web,vision]` 补成与 DATA-MODEL §6.3 真相源一致的 `[web,vision,image_gen]`（此前 TD-05 代码与真相源不一致）。
  - 测试：`test_capability_catalog.py` 扩到 33 例——把"CATALOG 恰好 8 个种子键"的断言改为"种子键 ⊆ CATALOG"，新增业务条目合法性（description/risk_gate 合法、有 MCP 必声明凭证）、`ROLE_BUNDLES` 全部 key 都在 catalog、每个角色能 resolve_bundle 不报错、未知角色报错。全套 **194 测试通过**（+10）。DATA-MODEL §6.3 同步。

### 2026-07-10（TD-09-T2：渠道管理 API + 公开 Webhook 端点）
- **feat(api)**: 把 TD-09-T1 的 channel router 接成一条可 curl 验证的完整入站链路。
  - `app/services/channels.py`：渠道 CRUD（create 生成 `token` + 返回 `webhook_url`、list、get+stats、patch、软删 active=0）、`channel_stats`（今日入站数 / 活跃外部用户数）、`verify_signature`（URL token 为主凭证；config 里配了 `secret` 则额外校验 `X-Signature` = HMAC-SHA256(raw_body)）。
  - `app/api/routes/channels.py`：`/api/channels` 认证 CRUD（含 target_agent/target_conversation 归属校验）。
  - `app/api/routes/webhooks.py`：**公开** `POST /webhooks/{channel_type}/{token}`（不挂 /api、无 JWT）——按 token+type 查 active 渠道 → 验签 → 解析 JSON → `route_inbound` → 若渠道 pin 了 target_agent，经现有 `complete_agent_reply` **尽力触发一次回复**（执行层报错则吞掉、仍返回 200，消息已落库）。
  - 新增 email 适配器（SendGrid/Mailgun inbound-parse 风格，按 from/text 归一化）；注册表现覆盖 generic_webhook + email；微信(XML+加密)/widget 留 TD-09-T3。
  - 测试：新增 `test_channels_api.py` 8 例（CRUD + 未知 target 拒绝、webhook 入站落库、去重、stats、未知/停用 token→404、缺/对 HMAC 签名→401/200、不支持渠道类型→400、mock LLM 下 target_agent 触发 agent 回复）。全套 **184 测试通过**（+8）。DATA-MODEL §9.2/9.3 标记已实现。

### 2026-07-10（TD-09-T1：外部渠道数据模型 + Router 核心）
- **feat(api)**: 外部渠道接入地基——外部消息(微信/邮件/网页/通用 webhook)进来后归一化成标准消息、进入普通会话流，**agent 完全不感知渠道**（纯数据 + Router，不接 webhook 端点/不碰 Hermes）。
  - schema（双 schema + `ensure_column`）：新增 `channel_configs`（channel_type CHECK、token UNIQUE、config_json、target_agent_id/target_conversation_id、active 存 INTEGER 0/1）；`conversations` 加 `source_channel`/`external_conversation_id`；`messages` 加 `external_message_id`（webhook 重发去重）。
  - `app/channels/`：`adapters/`(base `ChannelMessage` + `generic` 适配器按可配置点路径 `message_path`/`user_id_path`/`message_id_path` 提取 + 注册表 `get_adapter`)；`router.py` 的 `route_inbound`(归一化→找/建会话→去重→落 `sender_type='user'` 消息)、`find_or_create_conversation`(pin 固定群 / 按 `(source_channel, external_user_id)` 归线程)、`message_already_processed`。留 TD-09-T2 触发 agent 回复的接缝。
  - 测试：新增 `test_channels.py` 6 例（建会话+消息+外部字段、按 external_message_id 去重、同一外部用户归一会话且不同用户分开、pin 固定群路由、无 message_id 不去重、自定义嵌套路径 + 缺内容报错）。全套 **176 测试通过**（+6）。DATA-MODEL §9.1 标记已实现。

### 2026-07-10（TD-08-T1：Idea 中心数据模型 + API）
- **feat(api)**: 落地北极星 §1.5「没有 idle 员工 → idea 中心」的后端地基（纯数据 + API，不接 Hermes）。
  - schema（双 schema + `ensure_column` 迁移）：新增 `ideas` 表（category/status 带 CHECK 约束，`converted_brief_id` 追溯转化）；`agent_specs` 加 `last_idle_think_at`/`idle_think_interval_hours`/`idle_thinking_enabled`；`conversations` 加 `idea_id`（追溯从哪个 idea 转来的会话）。
  - `app/services/ideas.py`：create/get/list（按 new 优先 + status/agent/category 过滤）/review（accept→accepted、dismiss→dismissed）/convert（建 group 会话 + 拉入 source agent + 首条系统消息=idea 内容 + 回链 `conversations.idea_id` + idea→converted）/set_idle_thinking。
  - `app/api/routes/ideas.py`（注册进 main）：`GET/POST /api/ideas`、`GET /api/ideas/{id}`、`POST /api/ideas/{id}/review`、`POST /api/ideas/{id}/convert`、`PATCH /api/agents/{id}/idle-thinking`；业务逻辑在 service 层，路由只做 HTTP。
  - 两处实现偏差（已回填 DATA-MODEL §8）：`idle_thinking_enabled` 两方言统一存 `INTEGER 0/1`（迁移用单一定义字符串），API 序列化为 bool；idle-thinking 接口返回精简的 `IdleThinkingSettings` 而非 `AgentSpecOut`（无 spec 的员工返回 404）。
  - 测试：新增 `test_ideas.py` 8 例（CRUD、new 优先与过滤、review、convert 建会话并直查 DB 断言 `idea_id` 回链、重复 convert 拒绝、schema 422、DB CHECK 约束、idle-thinking 更新与无 spec 404）。全套 **170 测试通过**（+8）。DATA-MODEL §8.1 标记已实现。

### 2026-07-09（前端重做：桌面端设计系统升级）
- **feat(desktop)**: 用 impeccable 前端 skill 重做桌面端 UI（`apps/desktop/src/styles.css`，纯样式层，不动 `main.tsx` 逻辑/结构，功能不受影响）。
  - **方向**：从"通用浅色 + SaaS 蓝 + 扁平卡片"升级为「一人 AI 公司运营驾驶舱」。register=product（对标 Linear/Raycast 的克制）。配色策略 Restrained：中性 ink 底 + **单一品牌色 teal「脉搏」**（语义=员工 7×24 在工作），teal 只用于主操作/选中/focus/live 状态；success/warning/danger 与品牌色分色相；保留并沿用 per-agent `hue`（外壳克制、人物带色）。
  - **Token 层重写**：`:root`（浅色）+ `[data-theme='dark']`（近黑 cockpit + 更亮 teal）全套 token——中性梯度、品牌 teal 家族（含 `--on-primary`/`--primary-strong`）、语义色带 soft 变体、4 级 `--shadow-*` 高度、teal `--ring`、圆角(含 pill)、`--ease-out`；字体 Latin=Inter + CJK=Noto Sans SC。
  - **组件精修**：全局 `:focus-visible` teal ring + 交互过渡 + `prefers-reduced-motion` 兜底 + `pulse-ring` 关键帧（仅 live 指示器）；按钮/小按钮补齐 hover/active/disabled 全套状态与阴影；侧栏品牌 mark 改 teal 渐变芯片、active 导航加 teal 指示条；search-box 变真输入(border+focus ring)、composer focus ring + send 全状态、卡片加 xs 阴影、pill 圆角、清理残留写死蓝（auth 渐变/focus 改 teal token）。
  - **对比度**：按 skill 要求把 `--subtle`（placeholder/timestamp）在两套主题都调到达标。
  - 浏览器预览验证：消息/人才市场/任务/员工四视图 + 浅色&深色两主题均已截图确认级联正确、无 console 报错。新增 `apps/desktop/AGENTS.md` 固化设计系统，防后续 AI 改回通用蓝。

### 2026-07-09（TD-03-T1：Run/RunStep 数据模型）
- **feat(api)**: 为接 Hermes 执行铺好数据地基（纯 schema + 生命周期，不起 Hermes）。
  - `database.py` 两套 schema（SQLite + Postgres）+ `ensure_column` 迁移：`runs` 扩 `task_id`/`hermes_profile_id`/`hermes_run_id`/`workdir`（新列暂可空，NOT NULL 与"每个 Run 必属 Task"由 TD-03-T3 的 RunService 在应用层强制）；新增 `run_steps` 表（映射 Hermes SSE 事件：message/thinking/tool_call/tool_result/approval_required/status/final）；`approvals` 加 `run_id`（批准后据此恢复对应 Run）+ `type`（high_risk/clarification/capability_upgrade，行为在 T4）；`agents` 加 `hermes_gateway_port`（一员工一 gateway 一端口）。引用 `runs` 的 FK 列走 `ensure_column`（在 runs 建表之后加），规避 Postgres 前向引用报错。
  - 新增 `app/runtime/runs.py`：Run 生命周期状态机（`queued → running → waiting_user|waiting_clarify → completed|failed`，非法转移抛 `RunStateError`，终态 stamp `completed_at`）+ `create_run`/`transition_run`/`append_run_step`/`list_run_steps`（支持 `after_step_id` 增量拉取，对接 TD-03 前端轮询契约）。
  - 测试：新增 `test_run_lifecycle.py` 13 例——状态机合法/非法转移、终态无出边、失败记 error、run_steps 追加+增量列出、以及对真实 `init_db()` 跑迁移并断言新列/新表存在且幂等。全套 **162 测试通过**（+13）。
  - DATA-MODEL §5.1/§5.2 标记为已实现；解锁 TD-03-T2（HermesBackend，需 agentpulse 会话起 Hermes）。

### 2026-07-09（TD-02-T5：路由归位，修复架构漂移）
- **fix(api+orchestration)**: 消除 2026-07-08 复核发现的结构性漂移，把群讨论编排真正收回编排层。
  - `orchestration/discussion.py::run_discussion_round` 从"只被单测调用的死码 + 同步 dict 返回"**重写为 async 事件流**（yield `speaker`/`chunk`/`message`/`error`/`end` 事件），成为群讨论的**唯一生产入口**。`send_message`（非流式）和 `send_message_stream`（流式）都改为 `async for event in run_discussion_round(...)` 驱动它——路由层只注入 `turn_executor`（如何执行一轮 agent 回复 + 持久化）和 `llm_complete`（主持人 LLM 执行层薄封装，由新的 `make_speaker_selector()` 构造），再把事件翻译成各自传输格式（SSE 帧 / 累积列表）。
  - 发言人选择逻辑（@提及 → 主持人 LLM → 轮询降级）全部收敛到编排层：新增 `resolve_next_speaker`（端到端解析，内部经注入的 `llm_complete` 调 LLM），路由层的 `_llm_select_speaker`/`_extract_mention_simple` **删除**；`_build_discussion_context` 迁移为编排层 `build_discussion_context`。
  - 编排层保持零外部 HTTP：LLM 调用一律经注入回调，`app/orchestration/` 内无 `httpx`/`requests`/`hermes_client`。
  - 测试：`test_discussion.py` 按新 async 契约重写 `TestRunDiscussionRound`，新增 `TestResolveNextSpeaker`/`TestParseSpeakerJson`/`TestBuildDiscussionContext`（覆盖从路由迁入的 LLM 选人逻辑）；`test_workspace_flow.py` 新增 `test_stream_group_discussion_routes_through_orchestration`——mock `run_discussion_round`，向 `/messages/stream` 发真 HTTP 请求断言生产路径确实经过编排入口（不是单测直调函数的假验收）。全套 **149 测试通过**（较基线 137 +12）。
  - 三条禁止模式 grep 全干净（路由无业务循环/私有 LLM 选人、生产入口真调 run_discussion_round、编排无直连 HTTP）。
  - Verified: run_discussion_round called via production path POST /api/conversations/{id}/messages/stream（及 /messages）；解锁 TD-03-T2/T3。

### 2026-07-08（架构复核：发现真实漂移）
- **docs(architecture-audit)**: 用户直接质问"AI 干的活方向有没有飘"，做了一次彻底代码复核（不只是接口签名，而是追调用链），确认**是，且是结构性漂移，需要纠正**：
  - `orchestration/discussion.py::run_discussion_round`(TD-02 设计的核心编排入口)在生产环境是**死代码**——只被单测调用，`api/routes/workspace.py` 的 `send_message`/`send_message_stream` 从未真正调用它。372 行单测全过，但只测了一个生产请求走不到的函数，给出"TD-02 已完成"的假象。
  - 路由层里独立写了**两份重复的讨论循环**(非流式/流式各一份)，外加一套单独的 `_llm_select_speaker`+`_extract_mention_simple`(跟编排层的 `select_next_speaker` 功能重叠、优先级还反过来)，直接违反 ARCHITECTURE-DETAILED.md §3.1 的边界层职责("不写业务逻辑")。
  - 风险点：TD-03-T3 原计划替换 `complete_agent_reply` 的执行部分，但该函数只被前端已弃用的非流式 `/messages` 调用；前端现在只走 `/messages/stream`(`_stream_agent_reply`)。若不纠正直接做 TD-03，会把 Hermes 接到用户实际走不到的死路径。
  - **纠正**：新增 [TD-02-T5](docs/tech-design/TD-02-multi-agent-discussion.md#td-02-t5)（路由归位：重复实现收回 orchestration 层，唯一化发言人选择入口），列为看板最高优先级，**TD-03-T2 及以后新增依赖 TD-02-T5**（TD-03-T1 纯 schema 不受影响）。TD-03 设计文档同步更正真实执行入口。看板"已完成"表补注该风险，避免误读为"TD-02 已可靠交付"。
  - 未直接修改代码（分工：本侧只出设计/纠正，实现交给 worker AI）。

### 2026-07-08（架构侧收尾）
- **docs(backfill)**: 完成验证报告(`19c209b`)遗留的回填义务——V1–V7 实测事实写入 DATA-MODEL §5.3(Runs API 真实请求体/Tirith 审批配置/一 profile 一 gateway 一端口/MCP·skills·profile 打包命令/25 个 toolset 真名)；§6.3 catalog 种子改用真名(`file` 不是 `files`)；TD-03 开放问题全关(含 **workdir 架构决策**：Runs API 无 per-run cwd → profile 级绝对 work root + 每 Run 子目录约定，硬边界在员工 work root)；TD-04-T6/TD-05/ARCHITECTURE/agent-model 的〔待核〕全部替换为实测事实。
- **docs(board)**: 重建执行看板——修复指向不存在文件的坏链接、错乱编号；"待提交"条目全部换成真实 commit 号(TD-02=`b61005e`、TD-04-T3=`f55d0b1`、T4=`81af20d` 等)；新队列=端到端手测 / TD-03-T1 / TD-04-T6(已解锁)。维护规则加两条：链接必须指向真实文件、完成必须带 commit 号。
- **chore(cleanup)**: 删除验证会话遗留的 `~/.local/bin/vtest3` wrapper(报告自标的污染)；UnitPulse 干净检查通过；全量测试 137 passed 复核通过。

### 2026-07-08（四）
- **docs(readme)**: README 全面重写——修掉 ADR 0001 之前的过时内容(旧 mermaid 里的 Local Runtime/Tool Broker/多 LLM Provider、Roadmap Phase 3 的 provider adapters、"Agent runtime: Planned" 状态)，对齐当前架构(自研编排层 + Hermes 每员工一 profile)与真实进度(群讨论第一片✅、Hermes PoC✅)。SEO/GEO 优化：双语定义句前置 + 英文 Overview、徽章、三方对比表(vs 聊天机器人 vs 自动化平台)、FAQ 问答区(6 问，LLM 引用友好)、关键词/Topics 区、按读者分流的文档导航表、精简 Quick Start。

### 2026-07-08（三）
- **docs(tech-design)**: 新增 [EXECUTION-BOARD.md](docs/tech-design/EXECUTION-BOARD.md)——唯一任务状态源，让任何 AI 冷启动读仓库即知"下一步做什么"(现在就做队列/依赖阻塞表/已完成表/认领锁规则：开工第一动作=改看板为进行中并 push)，不再需要人类口头派单。AGENTS.md §4"下一步"改为第一跳指看板；执行协议第 2/5 条改为"领任务/完成都在看板标状态"(TD 文件不标，防两处漂移)。

### 2026-07-08（二）
- **docs(tech-design)**: 分工定版——本侧只产设计文档，实现由 worker AI 执行。为此补齐三块：① [HERMES-VERIFICATION-PLAYBOOK](docs/tech-design/HERMES-VERIFICATION-PLAYBOOK.md)(把清〔待核〕变成可执行剧本 V1–V7：toolsets 真名/MCP 语法/技能安装/单网关多 profile/审批触发/打包分发/workdir 语义，含硬性安全前置与回填清单)；② TD-03 加深到字段级(SSE事件→run_steps 映射表、RunService 签名、v1 前端轮询决策、**发现并补上 `approvals.run_id` schema 缺口**——没它批准后找不到该恢复哪个 Run，已进 DATA-MODEL §5.1)；③ TD-02 加深到字段级(send_message 接线点、配置常量、发言人选择严格 JSON 契约+降级、收敛判定契约、prompt 约束、防刷屏)。README 新增 V 线(建议最先执行)与"Worker AI 执行协议"6 条。

### 2026-07-08
- **docs(tech-design)**: 新增 [TD-05](docs/tech-design/TD-05-capability-catalog.md)(能力映射表：代码常量模块+resolve_bundle 合并规则+risk_gate 只升不降+domain_register 永远 prohibited_auto) 与 [TD-04](docs/tech-design/TD-04-agent-provisioning.md)(Agent 供给完整详细设计：v1 入口=创建员工表单而非聊天以解耦 TD-02；供给分逻辑/物理两段，`ProfileProvisioner` 接口把接缝正好切在〔待核〕Hermes 边界上，RecordOnly 实现让 T1–T5 全部可先做可测；role_spec LLM 起草+代码硬校验；SOUL 生成；凭证不落库直写 profile .env；供给状态机+幂等重试；6 个 task 中仅 T6 需 agentpulse 锚定会话)。阶段表新增 D 线(与 B/C 可并行)。

### 2026-07-07（深夜）
- **docs(tech-design)**: 新增 [ARCHITECTURE-DETAILED.md](docs/tech-design/ARCHITECTURE-DETAILED.md)——实现级系统架构脊梁：组件全景(对齐真实模块树)/运行时拓扑与部署/分层职责+接口/完整数据模型/三条核心时序(NL→agent 供给、讨论→任务、任务→Hermes 执行)/横切关注点(鉴权/凭证/审批/隔离/错误/双schema/流式/幂等)/Hermes 边界契约(调用面+假设+待核清单)/分期与组件深化索引(TD-04 供给、TD-05 catalog 待建)。
- **docs(tech-design)**: [DATA-MODEL-AND-API.md](docs/tech-design/DATA-MODEL-AND-API.md) 新增 §6"Agent 供给"权威 schema——`agent_specs`/`agent_capabilities` 两张新表(精确列/约束/状态机)、capability_catalog 种子(8 个 capability_key→bundle+risk_gate)、4 个新增 API 契约(POST /api/agents 扩 role_spec、credentials、provision、spec)；架构文档 §4.1 改为引用此节，杜绝两处漂移(G1 教训)。

### 2026-07-07（夜）
- **docs(tech-design)**: 新增 [agent-model-and-capabilities.md](docs/tech-design/agent-model-and-capabilities.md)，回答"系统怎么实现"的核心架构问题：agent = 基础 profile + 人格(SOUL.md) + 技能(教流程) + 工具/MCP(给执行力) + 凭证 + 模型；小秘书=编排角色(职责+工具面不同，非更强底座)；一句话→role_spec→自动 provision 出定制 agent 的数据流；前端工程师/小红书运营两个工种的能力逐条落地(每能力=技能+工具/MCP+凭证+风险审批门+现成开源程度)，含诚实边界(域名/生产部署/花钱须人工；小红书无开放发布 API)；能力主要靠组装现成积木(内置工具+MCP 生态+cli-anything+技能 tap)。文末列"待核清单"(per-profile MCP 语法、profile install、per-tool 风险配置等，编码前须对 Hermes 实测确认)。附注：本轮两路联网研究因环境 web 工具连续 600s 超时失败，本文档基于本会话早前成功研究 + 本机实测 Hermes 一手材料写成，推断处均标注可信度。

### 2026-07-07（傍晚）
- **docs(tech-design)**: 把 tech-design 拉到"任何人/AI 拿到即可开工"的标准。新增 [DATA-MODEL-AND-API.md](docs/tech-design/DATA-MODEL-AND-API.md)(唯一真相源：所有表/字段/类型/约束/接口/错误码精确规格，含 TD-02/TD-03 的目标 schema) 和 [the-loop.md](docs/tech-design/the-loop.md)(闭环走查锚文档，带真实数据)。核对实现代码发现并记录 4 处不对齐(G1–G5)：DB `participant_agent_ids_json` vs API `participant_agent_ids`(ADR 0006 写错，已加勘误)；`TaskOut` 缺 `consensus_brief_id`(加 TD-01-T1b 修)；`discussion_status` 未接线(TD-01-T1)；`database.py` 双 schema(init_postgres/init_sqlite)须两处同步改的硬约束。README/AGENTS.md 已指向这两份新文档。

### 2026-07-07（下午）
- **docs(tech-design)**: 新增 `docs/tech-design/` 目录，把"从第一片已完成 → 第一个真正可用的垂直闭环"的剩余工作拆成技术设计 + tech-task：[TD-01](docs/tech-design/TD-01-verify-and-harden-slice-1.md)(端到端手测并收尾第一片)、[TD-02](docs/tech-design/TD-02-multi-agent-discussion.md)(多 agent 群讨论，照 AutoGen 骨架)、[TD-03](docs/tech-design/TD-03-hermes-execution.md)(执行层换真·Hermes：Run/RunStep + HermesBackend + workdir 隔离 + 审批闭环)。每个 TD 含技术设计 + 编号 tech-task(带验收标准/依赖/是否需 agentpulse 锚定会话)。推荐顺序 A→B→C。AGENTS.md §4"下一步"与文档索引已指向这些。

### 2026-07-07
- **feat(orchestration)**: 实现 [ADR 0006](docs/decisions/0006-group-discussion-v1-first-slice.md) 群讨论协议第一片(commit `c2054bf`)：新增 `consensus_briefs` 表 + `tasks.consensus_brief_id` + `conversations.discussion_status`；新建 `orchestration/`(discussion/brief/gate)模块；`/api/briefs` 路由(create/confirm/reject/get)；**从 `send_message` 移除正则自动建任务**，Task 创建改为必须携带 confirmed brief 的门控(`gate.py`)；前端渲染共识纪要卡片(BRIEF_CARD 前缀)+ 确认/继续讨论按钮。14 tests 通过。
- **docs**: 同步文档到实际状态(上一条实现提交遗漏了此步)——`AGENTS.md` §4 从"群讨论 ❌ 未实现"更新为"🟢 第一片已实现(仅单测,未端到端手测)"并重列下一步三选项；ADR 0006 状态行标注已实现。另经实测复核 `services/api` 测试确为 14 passed、UnitPulse 仓库未被本次实现污染。

### 2026-07-05（下午）
- **docs**: 做了一次"冷读 handoff 测试"（一个零上下文 AI 只读仓库判断能否继续），结论：大方向/架构/硬规矩都接得住，但"最近这一步的具体计划"没写进仓库、且 AGENTS.md §4 旧"下一步"与实际商定计划不一致。据此新增 [ADR 0006](docs/decisions/0006-group-discussion-v1-first-slice.md) 把已认可的"群讨论协议第一片"计划（讨论态 + 共识 brief + Task 创建门、对齐用人工确认、本片不碰 Hermes）落进仓库，并附"待与所有者敲定"清单（consensus_brief schema、编排模块位置、对齐信号形式等）；同步更新 AGENTS.md §4"下一步"指向 ADR 0006，消除歧义。

### 2026-07-05
- **docs**: 完成 Hermes 本机地基验证——pip 装 `hermes-agent`、建多个 profile、用 DeepSeek 作主模型、HTTP Runs API + SSE 流式事件全链路跑通，多 profile 人格隔离验证成立。同时发现两个必须处理的坑并记为 [ADR 0005](docs/decisions/0005-hermes-poc-safety-findings.md)：① `terminal.working_dir` 默认相对路径不可信任，必须显式绝对路径隔离（验证中曾误写文件到无关的真实项目仓库，已确认全部为全新文件并清理干净，未造成数据丢失）；② SOUL.md 硬性规则不保证被遵守，印证 ADR 0002 的讨论对齐门必须由编排层结构性强制。已同步更新 `docs/ARCHITECTURE.md` §3.10 和 `AGENTS.md` §4。

### 2026-07-04
- **docs(research)**: 新增 `docs/research/skill-source-repos.md`——调研 `HKUDS/CLI-Anything`、`msitarzewski/agency-agents`、`anbeime/skill` 三个仓库能否为 Hermes 员工补技能。结论：前两个已自带官方 Hermes 集成，可分别作"工具接入"(约150项软件自动化)和"人格/技能素材"(250+ agent 人格，映射到默认员工编制)；第三个授权状态不清晰，仅作发现索引、逐一审计后引用。已列出可实现清单和建议顺序，尚未拍板/实现。
- **docs**: 补全 AGENTS.md 文档索引——之前遗漏了已存在的 `docs/prd.md`/`docs/backlog.md`/`docs/workflow.md`/`docs/research/`；并消除 `docs/ARCHITECTURE.md`(本次新增) 与 `docs/workflow.md`/`docs/backlog.md` 里提到的 Architecture 阶段产出物 `docs/architecture.md`(旧规划，从未创建) 之间的命名歧义——明确两者是同一份文档。

### 2026-07-03
- **docs**: 消除 `ROADMAP.md` 与 ADR 0001–0004 的技术歧义——不再只加警告，直接改写「Agent 底层设计」一节里所有假设"多 CLI 适配 / Codex 优先 / 本机检测多运行时"的内容(Multica 结论、Runtime 取舍表、建议架构、本机 Daemon、Runtime 优先级、后端模块目录、Day 15–19 计划)，改为与"Hermes 为唯一基座"一致的正确版本；产品愿景/MVP 边界/执行节奏方法论保留不变。
- **docs**: 对齐 AGENTS.md 开放标准 / Claude Code 官方实践——`CLAUDE.md` 改用 `@AGENTS.md` import(会话开头自动加载)；AGENTS.md 补充"文档随项目生长"约定(嵌套 AGENTS.md 就近生效 + `.claude/rules` 路径域 + skills)。
- **docs**: 新增项目基准文档，供后续 AI/开发者接手即对齐、防跑偏：
  - `AGENTS.md`（北极星 + 架构决策 + 开发规范 + 文档索引）、`CLAUDE.md`（指向 AGENTS.md）
  - `docs/ARCHITECTURE.md`（详细架构 + 调研结论 + 出处）
  - `docs/decisions/` ADR：0001 Hermes 为基座、0002 自研群讨论、0003 服务端7×24+idea中心、0004 多模态经 Hermes
  - 本 `CHANGELOG.md`
- **决策**：确定技术路线 = Hermes 为员工运行时基座 + 自研群讨论协作层(照 AutoGen) + 服务端 7×24 + idea 中心。详见 ADR 0001–0004。
- **feat(desktop)**: 聊天内联审批卡片——审批请求直接出现在对应会话，老板可当场批准/驳回（`apps/desktop`）。
- **feat(desktop)**: 聊天头部关联任务栏——会话正在驱动的任务以可点击 chip 展示（等级+状态），点击开任务详情。

<!-- 追加新条目到此区块顶部；发版时归档为带版本号的小节 -->

## 2026-07-15 — 企业级加固

### 认证
- **JWT 标准化**：自研 HMAC token → `python-jose` HS256 JWT（标准 `sub`/`iat`/`exp`/`jti`/`iss` claims）。旧格式兼容 30 天过渡期，现有 session 不中断。
- **默认密钥强制检测**：`auth_secret_key` 为默认值 `agentpulse-local-dev-secret` 时启动直接拒绝，除非设 `AGENTPULSE_ALLOW_DEFAULT_SECRET=1`（仅限本地开发）。

### 数据库
- **连接池**：PostgreSQL 路径引入 `psycopg_pool`（min=1, max=10），替换每次请求新建连接。`shutdown_db()` 在 app lifespan 结束时优雅关闭。
- **占位符加固**：`?` → `%s` 替换改为逐字符扫描，跳过单引号字符串内的 `?`，防止误替换。
- **Alembic 迁移框架**：初始化 `alembic/`，`env.py` 从 `app.core.config.settings` 读取数据库 URL。基线 revision `99f23f8da74b` 已创建。

### 可观测性
- **结构化日志**：新增 `app/core/logging.py`，支持 JSON Lines 输出（`AGENTPULSE_LOG_JSON=1`）和彩色控制台模式。关键路径（Hermes/DeepSeek 路由、群讨论进入）已加日志点。
- **FastAPI lifespan**：`@app.on_event("startup")` → `lifespan` async context manager，正确处理启动/关闭生命周期。

### 依赖
- 新增：`python-jose[cryptography]==3.5.0`、`psycopg_pool==3.3.1`、`alembic==1.18.5`

### 文档
- 新增 `apps/desktop/AGENTS.md`：前端当前状态 + 模块化路线图

### 测试
- 全量 247 passed / 7 skipped / 1 xpassed，零回归

## 2026-07-15（续）— Agent Action Bridge 🔧

**核心突破：Agent 能动手，不只是动嘴。**

### 新增模块

- **`app/tools/registry.py`** — 10 个工具定义（OpenAI function-calling 格式）+ 对应 handler
  - `create_employee` / `create_task` / `create_group` / `add_group_member` / `list_agents` / `list_tasks` / `claim_task` / `update_task` / `send_group_message` / `no_action_needed`
  - Handler 直接调内部 service 函数（不走 HTTP），共享请求 DB 连接
- **`app/tools/function_loop.py`** — Function-calling 循环
  - 用户发消息 → LLM 决策调工具 → 执行 → 结果回喂 → 重复（最多 5 轮）
  - `run_function_loop()` 生成 {type: chunk|tool_call|tool_result} 事件流
- **`app/tools/__init__.py`**

### 接入热路径

- **`send_message`**（非流式 DM）：优先走 function loop，失败 fallback 原路径
- **`send_message_stream`**（SSE 流式）：同上，结果实时流式返回
- **`complete_agent_reply`**：新增 `use_tools=True` 参数

### 门控调整

- **`app/orchestration/gate.py`**：`validate_task_creation_gate` 新增 `bypass_gate` 参数
- **`app/services/workspace.py`**：`create_task` 新增 `bypass_gate` 参数
- agent 通过 tool 创建任务时 `bypass_gate=True`，跳过共识 brief 要求

### 测试

- **`tests/test_function_loop.py`** — 12 个新测试覆盖全部工具执行路径：
  - tool_call 解析、系统 prompt、创建员工/任务/群组、拉人、列表查询、错误处理
- 原有测试适配（`test_login_secretary_chat` 不再断言硬编码文本）

### 效果

老板跟小秘说「帮我建一个市场团队，要内容策划、运营增长、设计师」→ agent 实际调 `create_employee` x3 → 返回结果。
从此 agent 不再只是"打字"，而是真正能**操作公司系统**。
