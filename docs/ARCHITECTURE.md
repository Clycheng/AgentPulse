# AgentPulse 架构与技术路线

> 本文件是 [AGENTS.md](../AGENTS.md) §2/§3 的详细展开：架构分层、每个决策的理由、以及支撑决策的调研结论与出处。
> 决策的原子记录见 [decisions/](decisions/)。

## 1. 核心判断

- **AgentPulse 要自研的是「公司协作大脑」**：拉群 → 讨论对齐 → 分工 → 拍板 → 追踪。
- **每个 AI 员工的「专业能力 + 记忆 + 自我进步」交给 Hermes**（不自建 Runtime）。
- 两者通过 Hermes 的 HTTP(Runs API) / kanban 对接。

这与早期 [ROADMAP.md](../ROADMAP.md) 的"自建 Orchestrator、不自建 Runtime"一致；差异在于 Runtime 从"多 CLI 适配(Codex/Claude/...)"**收敛为单一 Hermes**，适配层因此大大简化。

## 2. 分层架构

```
┌─────────────────────────────────────────────────────────────┐
│ 产品层 (AgentPulse)                                          │
│   公司 / 部门 / 员工卡 · 自然语言捏 agent · 技能市场          │
│   idea 中心 · 任务看板 · 群聊 & 私聊 UI                       │
├─────────────────────────────────────────────────────────────┤
│ 协作编排层 (自研 · 核心)         —— 照 AutoGen 骨架           │
│   群讨论协议：单一共享 transcript + 发言路由(轮询/LLM选/人工/ │
│   自定义) + transition 约束门(没对齐前执行 agent 不许发言) + │
│   human 作为参与者 + 可组合终止条件(区分"已对齐"vs"回合上限")│
│   讨论达成 → 产出结构化「共识 brief」→ 才建 Task/Run          │
│   Task / Run / RunStep / Approval / 分工路由                 │
├─────────────────────────────────────────────────────────────┤
│ 运行时层 (Hermes)                                            │
│   每员工 = 1 个 profile(独立 HERMES_HOME):                   │
│     SOUL.md 人格 + Skills 技能 + Memory 记忆 + 可选模型       │
│   自我学习 = 技能沉淀 + 记忆积累(原生，非微调)                │
│   多模态 = 辅助视觉模型 + Whisper STT + video/web 工具        │
├─────────────────────────────────────────────────────────────┤
│ 执行 / 部署层                                                │
│   后端服务器常驻 Hermes daemon(Docker/systemd, headless)     │
│   7×24 = daemon + cron + webhooks + /goal + kanban dispatcher│
│   后端 / 官网 / 后台 / 客户端 分处不同机器，通过 API 互联     │
│   客户端 = 窗口(关机不影响服务器上的员工工作)                 │
└─────────────────────────────────────────────────────────────┘
```

## 3. Hermes 能力（调研结论）

Hermes Agent = Nous Research 2026-02 开源(MIT)的自治 agent，标语「the agent that grows with you」。**它学习靠记忆+技能积累，不动模型权重。**

### 3.1 决定 agent「专业性」的关键 = system prompt 的组装槽位
| 槽位 | 来源 | 决定 |
|---|---|---|
| 身份 | `SOUL.md` | 人格、角色、边界、口吻（"他是谁"） |
| 技能 | Skills / SKILL.md | 会哪些标准作业流程（🔑 **专业性最核心**） |
| 项目上下文 | `AGENTS.md`/`HERMES.md` | 具体公司/项目规则 |
| 记忆 | `MEMORY.md`/`USER.md` | 积累的事实与教训 |
| 工具权限 | toolsets / MCP | 能动手做什么 |

→ **一个"内容主笔"和"财务助理"的差别 = 不同 SOUL.md + 不同技能包 + 各自记忆。**

### 3.2 自然语言定制 agent
- 人格：`SOUL.md` 就是自然语言 markdown → 用户一句话描述，LLM 生成。
- 技能：`/learn` 把 agent 指向文档/URL，它**自动写出合规 SKILL.md**；复杂任务后也会**自动沉淀技能**。
- 技能包共享：`taps`（装 SKILL.md 的 GitHub 仓库）→ 可做"官方技能市场"，招员工=选人格+挂起手技能包。
- 可控：`skills.write_approval: true` 让技能写入先暂存待批。

### 3.3 自我学习（学习循环）
完成复杂任务(5+ 工具调用)→自动沉淀技能；每轮对话后后台复盘悄悄存记忆/改技能；用户纠正→变持久记忆/技能；全文检索(FTS5)按需取技能。**无微调**——好处是成长可查、可编辑、可迁移(技能就是文件)。

### 3.4 编程驱动（后端如何调 Hermes）

本机 Hermes v0.18 实测不存在旧的 REST `/v1/runs` 执行接口（见 [ADR 0007](decisions/0007-hermes-v0.18-interface-acp.md)）。当前生产路径由 `HermesBackend` 启动 `hermes --profile <profile> acp`，通过 stdio JSON-RPC 驱动会话；`new_session` 必须携带绝对 workdir。

每次 Run 可动态传 `HttpMcpServer`，不修改 profile 静态配置：

- 任务 Run 获得 `/mcp/company-tools`，短期 token 绑定 workspace/plan/task/run/agent，只能访问当前计划和公司资料。
- 有受控业务能力的聊天/任务 Run 获得独立 `/mcp/business-tools`，token 绑定 workspace/conversation/run/agent/可选 task。
- 业务 provider 密钥只在 AgentPulse 的加密凭证库中；Hermes 只表达调用意图，无法拿到密钥或绕过审批直接调用 provider（[ADR 0011](decisions/0011-controlled-business-actions.md)）。

技术危险动作仍由 Hermes ACP `request_permission` 进入数据库审批轮询；发邮件、发布、退款、付款等业务风险由 AgentPulse 业务工具门判断。

### 3.5 多实例 & 编排
- **profiles**：`hermes -p <name>`，每 profile 独立 HERMES_HOME(人格/记忆/技能/密钥/模型/gateway)。→ 一员工一实例。
- **delegate_task**：进程内子 agent(隔离上下文、限工具、默认并发 3、支持后台)。
- **`hermes kanban`**：跨实例编排——共享 SQLite 板 + dispatcher 循环把任务派给指定 profile，**以独立 OS 进程启动**。orchestrator profile 负责路由/分解。
- (跨厂商 ACP-client 编排是 proposal(#5257)，未发布；我们单基座不需要。)

### 3.6 7×24 / 无 idle
- 原生：常驻 daemon(systemd/launchd/Docker 自启) + **cron**(60s tick 主动跑，无人 prompt) + webhooks + `/goal`(标准目标跨轮) + kanban dispatcher。
- **不原生**：自发"发呆时想 idea"——学习是每轮对话后触发的。
- → **idea 中心 = 我们用 cron 编排**：如每员工挂 `每 30min：回顾公司近况+职责→产出 1 条机会 idea 投 idea 中心；无 idea 则 /learn 补一个薄弱技能`。定位是"编排出来的勤奋"，非自发意识，但产品体验足够。

### 3.7 多模态（DeepSeek 文本模型如何处理图/音/视频）
Hermes 能力感知**自动路由**：主模型能看图就原生看；主模型是文本模型(DeepSeek)则：
- 图片 → `vision_analyze` 路由到**辅助视觉模型**(配 `auxiliary.vision`，如 `google/gemini-2.5-flash` 或本地 `qwen2.5-vl`)描述→注入文本。
- 音频 → 内置 **Whisper STT**(`stt.provider: local` 可免费离线 / groq / openai / mistral)。
- 视频 → `video_analyze`(字幕/场景/时间戳)。
- PDF → `web_extract`(转文本；扫描件 OCR 未确认原生)。
- **净效果：DeepSeek 永远不用"看/听"，Hermes 把一切模态转成文本。** 可给不同员工配不同主/辅模型(profiles)。

### 3.8 模型选择的影响
- 硬要求：主模型 ≥64k 上下文 + 会 function-calling(否则工具调用退化/不可靠)。DeepSeek V3 两条都过。
- 配置在 `~/.hermes/config.yaml` 的 `model` 块；每 profile 可不同模型。

### 3.9 部署
- TD-12 首版采用薄 Electron + 云端单节点：Oracle ARM64 运行一个 AgentPulse API/Hermes 容器和 Caddy，Supabase 只提供 PostgreSQL，Vercel 承载官网，公开 GitHub Releases 承载安装包（[ADR 0012](decisions/0012-cloud-hosted-desktop-distribution.md)）。
- workspace 自带 DeepSeek Key。密文由独立凭证密钥保护，只在对应 Hermes ACP 子进程环境中注入；profile、日志、Run 和 API 都不保存或返回明文。
- 托管 Hermes 使用白名单环境和安全工具集，公网只开放 `/api`，动态 company/business MCP 仅容器内可达。生产单副本是首版边界，profile、记忆和 workdir 放持久卷，业务状态以 PostgreSQL 为准。
- 单实例 footprint 约 1 vCPU/1GB 起(无浏览器)，公开内测配置 2 OCPU/12GB。免费层无 SLA，多实例受 RAM/CPU、模型速率和本地 profile 状态约束。

### 3.10 地基验证结论（实测于 2026-07-05，见 [ADR 0005](decisions/0005-hermes-poc-safety-findings.md)）
2026-07-05 曾在当时版本上验证 HTTP Runs API；该接口结论已被 2026-07-10 的 Hermes v0.18 ACP 实测和 ADR 0007 取代，当前实现以 §3.4 为准。那次验证发现的以下两条安全结论仍然有效：

- **⚠️ `terminal.working_dir` 默认是相对路径 `.`，绝不能信任默认值。** 编程化/后台驱动 Hermes 时，必须显式把它设成绝对路径的隔离目录(如 `<server_data_root>/runs/<run_id>/`)，否则 agent 的文件操作可能落到调用方进程当时的任意 cwd 上——实测中曾因此把生成内容写进了一个完全无关的真实项目仓库。启动任何 Run 前，创建并绑定绝对路径 workdir 是不可跳过的前置步骤。
- **⚠️ SOUL.md 里的硬性规则不保证被遵守。** 实测中"背景不清楚必须先反问"这条规则被完全无视，agent 编造背景直接开工。这不是 bug，是"人格指令是引导不是强制"的固有性质(与 CLAUDE.md/AGENTS.md 对 Claude 的效力一样)。**结论：§4 群讨论协议的"讨论对齐后才能建 Task/Run"必须是编排层的结构性强制(如 Task/Run 创建 API 要求携带已确认的共识 brief，缺失即拒绝)，不能只在 SOUL.md 里写一条规则指望 agent 自觉。**

技术危险动作审批门后来已用真实 Hermes ACP 的删除操作验证批准、拒绝和超时三条路径；业务风险不依赖 Hermes 识别，统一走 ADR 0011 的持久业务动作门。

### 3.11 本机执行边界（TD-14R）

服务端 Hermes 只能看到服务端 workdir，不能读取老板电脑。需要本机文件或电脑能力时，API 创建绑定 `workspace/device/local_project` 的持久 Run，由 Electron 主进程 Worker 领取并在用户授权目录执行。设备 Token 和本机绝对路径只保存在主进程安全存储；API 仅保存设备、项目显示名、路径哈希和授权范围。

本机第一切片只执行只读项目分析，并通过 `execution_receipts`、Run lease 和事件接口回传事实。写文件、终端、浏览器登录、computer_use 和业务外部动作仍需审批运行时。没有 Worker、项目授权、Hermes profile 或真实最终产出时，Run 必须阻塞/失败，聊天不能把模型文本当作完成凭证。

## 4. 群讨论协议（自研 · 照 AutoGen 骨架）

Hermes 不做多 agent 围坐讨论；Multica 的"协作"只是 leader 派活(delegation)。**所以群讨论是 AgentPulse 自研的核心。** AutoGen 已验证需要哪些零件：

1. **单一共享、有序 transcript**，编排器持有，每轮广播给下一个发言者。
2. **可插拔发言选择**：轮询 / LLM 选(靠每个 agent 的 description) / 人工 / 自定义函数。
3. **transition 约束图**：限制"谁之后能轮到谁" → 强制"讨论没对齐前，执行 agent 不许发言"。
4. **human 作为普通参与者** + "等待人类输入"事件 → 客户端弹出澄清、运行暂停；支持 inline 和 暂停/恢复两种粒度。
5. **可组合终止条件**：区分「已对齐/批准」(共识信号) vs 「回合上限」(安全兜底)。
6. **handoff 作为显式消息类型**：讨论→执行的干净切换。
7. **有状态、可恢复的 run**：讨论现在、执行稍后。
8. **两阶段组合**：讨论阶段的产出 = 显式的「共识 brief」对象，用它 seed 执行阶段(别只靠 raw transcript)。参照 Magentic-One 的 Task Ledger(事实+计划)→ Progress Ledger(执行+卡住重规划)。
9. **一套引擎两种形态**：DM = 2 参与者，群聊 = N 参与者 + 真选择器。别造两套系统。

产品映射：讨论阶段 = 员工们 + 老板(UserProxy) 的 team，终止于"已对齐"信号并产出共识 brief；然后受约束地 handoff 进执行阶段(建 Task/Run，交给对应员工的 Hermes profile)。

## 5. 参考出处（调研于 2026-07）

- Hermes 文档：https://hermes-agent.nousresearch.com/docs/ ｜仓库 https://github.com/NousResearch/hermes-agent
  - API/Runs：/docs/user-guide/features/api-server ｜Profiles：/docs/user-guide/profiles ｜Kanban：/docs/user-guide/features/kanban
  - Cron：/docs/user-guide/features/cron ｜Skills：/docs/user-guide/features/skills ｜Memory：/docs/user-guide/features/memory
  - Vision/多模态：/docs/user-guide/features/vision ｜Voice(STT)：/docs/user-guide/features/voice-mode ｜SOUL.md：/docs/user-guide/features/personality
  - Prompt 组装：/docs/developer-guide/prompt-assembly ｜ACP：/docs/developer-guide/acp-internals
- AutoGen：https://microsoft.github.io/autogen/stable/ (Teams / SelectorGroupChat / Swarm / Magentic-One / human-in-the-loop / termination)
- Multica：https://multica.ai/docs/how-multica-works ｜https://github.com/multica-ai/multica
- ACP 标准：https://agentclientprotocol.com/
