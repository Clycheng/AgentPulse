# 详细系统架构（实现级脊梁）

> 这是从"技术方向"落到"能照着写代码"的系统架构。组件名与 `services/api/app/` 真实模块对齐。
> 可信度：`〔实〕`当前代码就是这样 / `〔本项目新增〕`要建、字段已钉死 / `〔待核〕`须真跑 Hermes 确认，别当既定。
> 字段/接口精确规格见 [DATA-MODEL-AND-API.md](DATA-MODEL-AND-API.md)；闭环叙事见 [the-loop.md](the-loop.md)；agent 能力模型见 [agent-model-and-capabilities.md](agent-model-and-capabilities.md)。每个组件的深化 tech-design 见文末 §9 索引。

## 1. 组件全景

```
                        ┌───────────── 客户端 (Electron, apps/desktop) ────────────┐
                        │  聊天/员工/任务/审批/共识纪要卡片 UI · 只读窗口+对讲机     │
                        └───────────────┬──────────────────────────────────────────┘
                                        │ HTTPS + (未来)WebSocket 流式
        ┌───────────────────────────────▼───────────────────────────────┐
        │                后端 services/api (FastAPI)                      │
        │  api/routes/  auth · workspace · briefs · runs · admin · health │  ← 边界层
        │  orchestration/  discussion · brief · gate                      │  ← 编排层(自研核心)
        │  services/  workspace(会话/任务/审批/消息) · templates          │  ← 业务服务层
        │  runtime/  deepseek(临时) → hermes_client(目标) · runner        │  ← 执行适配层
        │  core/  config · database(pg/sqlite) · security                 │  ← 基础设施
        └───────┬───────────────────────────────────┬─────────────────────┘
                │ psycopg                            │ HTTP Runs API (:8642)〔待核多profile〕
        ┌───────▼────────┐              ┌────────────▼─────────────────────────┐
        │ PostgreSQL     │              │ Hermes 运行时(每员工1 profile)         │
        │ (生产主存储)   │              │ SOUL.md+Skills+Memory+toolsets+MCP     │
        └────────────────┘              │ 常驻 gateway daemon · cron · /goal     │
                                        └───────┬────────────────────────────────┘
                                                │ 工具/MCP
                        ┌───────────────────────▼───────────────────────────┐
                        │ 外部能力: terminal/git · GitHub MCP · 部署CLI ·    │
                        │ 注册商API · SEO API · cli-anything(~150软件) ...    │
                        └─────────────────────────────────────────────────────┘
```

## 2. 运行时拓扑与部署（ADR 0003：多机、服务端常驻）

| 进程 | 跑在哪 | 端口/接口 | 说明 |
|---|---|---|---|
| 后端 FastAPI | 后端服务器 | :8000 `/api/*` | 无状态(除 DB)，可水平扩 |
| PostgreSQL | 后端服务器/托管 | :5432 | 生产主存储；测试用临时 SQLite(§4 双 schema 约束) |
| Hermes gateway ×N | 后端服务器 | :8642,:8643,…(一员工一端口) | 每员工一 profile；`API_SERVER_ENABLED=true`。✅ 已实测确认(2026-07-08 V4)：**不支持单网关多 profile**，就是一 profile 一进程一端口，用进程管理器拉起 |
| 客户端 Electron | 用户机 | — | 连后端 API；关机不影响服务器上员工 |
| 官网/后台 | 各自机器 | — | 独立部署，连同一后端 |

**凭证/密钥**：员工的外部服务凭证放各自 `profiles/<name>/.env`〔实测〕；AgentPulse 后端不持有员工执行凭证(减小泄漏面)，只在"供给 agent"时把用户提供的凭证写入对应 profile。

## 3. 分层与模块职责 + 关键接口

### 3.1 边界层 `api/routes/`〔实,briefs/runs 为近期新增〕
- `auth.py`(`/api/auth/*`)、`workspace.py`(`/api/me/bootstrap`,`/api/conversations*`,`/api/tasks*`,`/api/agents*` 等)、`briefs.py`(`/api/briefs*`)、`runs.py`(`/api/runs*`)、`admin.py`、`health.py`。
- 职责：鉴权(`api/deps.py` 的 `get_workspace_id`/`get_current_user(_id)`)、DTO 校验(`schemas/`)、调编排/服务层、异常→HTTP。**不写业务逻辑**。

### 3.2 编排层 `orchestration/`（自研核心）
- `discussion.py`〔实=状态机;〔本项目新增〕=发言编排 TD-02〕：会话讨论态(`discussing`/`aligned`)、`select_next_speaker`、`run_discussion_round`。
- `brief.py`〔实〕：共识 brief CRUD + 状态机(`draft→confirmed/rejected/superseded`)。**待补**：confirm/reject 联动 `discussion.set_discussion_status`(G3)。
- `gate.py`〔实〕：`validate_task_creation_gate`——建任务的结构性门控(必须 confirmed brief 或继承父)。
- 〔本项目新增〕`provisioning.py`(§5 agent 供给)、`router.py`(任务→员工路由)。

### 3.3 业务服务层 `services/workspace.py`〔实〕
会话/消息/任务/审批/事件/产出/员工的 CRUD 与业务规则(`create_task` 首行调 gate)。执行相关(`complete_agent_reply`)当前直连 DeepSeek，TD-03 换成调 `runtime/`。

### 3.4 执行适配层 `runtime/`
- `deepseek.py`〔实,临时〕：`DeepSeekChatClient.complete()`——当前唯一执行方式。
- 〔本项目新增,TD-03〕`hermes_client.py`(`HermesBackend.run(ctx)->AsyncIterator[AgentEvent]`) + `runner.py`(`RunService`：建 Run→建绝对 workdir→驱动→写 run_steps→审批→回写)。契约见 [DATA-MODEL §5](DATA-MODEL-AND-API.md)。

### 3.5 基础设施 `core/`〔实〕
- `database.py`：`Database` 抽象(pg/sqlite 双方言) + `init_postgres`/`init_sqlite`(⚠️双 schema，§4) + `ensure_column`。
- `config.py`(Pydantic Settings, `AGENTPULSE_*` 环境变量)、`security.py`(密码哈希/token)。

## 4. 完整数据模型（现有 + 本项目新增）

现有表精确列见 [DATA-MODEL-AND-API.md §1](DATA-MODEL-AND-API.md)：`consensus_briefs`〔实〕、`tasks(+consensus_brief_id)`〔实〕、`conversations(+discussion_status)`〔实〕、`messages`〔实〕、`runs`〔实,TD-03 扩〕、`run_steps`〔新增,TD-03〕、`agents`/`approvals`/`task_events`/`task_outputs`〔实〕。

### 4.1 〔本项目新增〕Agent 供给相关表（Q3 的 role_spec 落库）
三个新对象：**`agent_specs`**（一个员工一行：用户 NL 期望 + 供给状态机 draft→provisioning→(blocked_on_credentials⇄)→ready/failed）、**`agent_capabilities`**（员工被授予的每项能力一行：capability_key + skills/toolsets/mcp/凭证要求 + `risk_gate(auto|approval|prohibited_auto)` + status）、**`capability_catalog`**（能力映射表，代码内常量实现，capability_key→默认 bundle，系统核心资产）。

**精确列/约束/种子 catalog/新增 API 契约 → 权威规格统一在 [DATA-MODEL-AND-API.md §6](DATA-MODEL-AND-API.md)，此处不重复**（避免两处漂移，G1 教训）。

### 4.2 凭证存储
不进业务 DB 明文。方案〔本项目定〕：用户提供的员工凭证 → 直接写入对应 `profiles/<name>/.env`（Hermes 读它），后端只记"某能力的某凭证已提供"的布尔状态在 `agent_capabilities.status`。支付/域名类凭证按安全规则**不经 agent、不自动输入**。

## 5. 核心时序（3 条主流程，实现级）

### 5.1 一句话 → 供给出一个定制 agent（Q3/Q4 定制）
```
用户对小秘书: "我要个前端工程师"
1. POST /api/conversations/{id}/messages          (走群，小秘书接)
2. 小秘书(LLM)产出 role_spec 草稿                  orchestration/provisioning.py::draft_role_spec()
   { role_name, responsibilities[], desired_capabilities[] }
3. 映射: 每个 capability_key 查 capability_catalog  → 得 skills/toolsets/mcp/creds/risk
4. POST /api/agents (带 role_spec)                 → 建 agents 行 + agent_specs(status=provisioning)
                                                     + agent_capabilities 多行
5. provision():
   a. 生成 SOUL.md(LLM 按 role+responsibilities)   写 profiles/<name>/SOUL.md 〔待核 profile 创建走 API 还是 CLI〕
   b. 装技能(tap/内置/‑learn)                       〔待核 /learn 编程化触发〕
   c. config.yaml 写 model + 开 toolsets + mcpServers 〔待核 MCP 语法〕
   d. 缺凭证的能力 → status=credential_missing，向用户索要 → agent_specs.status=blocked_on_credentials
6. 凭证齐 → status=ready，员工出现在组织架构，idle
```
关键接口(新增，回填 DATA-MODEL)：`POST /api/agents` 扩收 role_spec；`POST /api/agents/{id}/credentials`(用户补凭证)；`GET /api/agents/{id}/spec`。

### 5.2 群讨论 → 对齐 → 建任务（TD-02 + 已实现门控）
见 [the-loop.md](the-loop.md) ①–⑤。实现级要点：`send_message`→`run_discussion_round`(选人/组prompt/临时执行层/写消息/判收敛)→主持人 `POST /api/briefs`→前端卡片→老板 confirm→`discussion_status=aligned`(G3)→`POST /api/tasks` 过 gate。

### 5.3 任务 → Hermes 执行 → 回写/审批（TD-03）
见 [the-loop.md](the-loop.md) ⑥–⑧ + [DATA-MODEL §5](DATA-MODEL-AND-API.md)。实现级：`RunService.start(task)`→建 `runs(queued)`→`mkdir <data_root>/runs/<run_id>`(绝对路径,ADR0005)→`HermesBackend.run(ctx)` 消费 SSE→每 `AgentEvent` 写 `run_steps`+推前端→`approval_required`→建 approval+Run=waiting_user→老板批准→`POST {hermes}/v1/runs/{id}/approval`→`final`→写回 message+更新 task。

## 6. 横切关注点（每个都要落到实现）

| 关注点 | 方案 |
|---|---|
| 鉴权 | `api/deps.py`：token→user→workspace；业务体不含 workspace_id/user_id〔实〕 |
| 凭证管理 | §4.2：写入 profile .env，DB 只存"已提供"状态；支付/域名类禁自动 |
| 审批门 | 能力级 `risk_gate`(auto/approval/prohibited_auto) + Hermes `approval_required`；复用现有内联审批卡片。**prohibited_auto 永远转人工** |
| 隔离(致命) | ① AgentPulse↔UnitPulse 零关联(AGENTS.md §5)；② 每 Run 绝对路径 workdir(ADR0005)；③ 每员工独立 HERMES_HOME/profile〔实测隔离成立〕 |
| 错误模型 | service 抛 `ValueError`→route 转 `HTTPException(400)`；不存在→404〔实〕。Run 失败→`runs.status=failed`+`error` |
| 双 schema | 加表/列必须 `init_postgres`+`init_sqlite` 都改，优先 `ensure_column`(§4/DATA-MODEL §0) |
| 可观测 | `run_steps` 是"过程可见"+审计的共同数据源；讨论 transcript = `messages` |
| 流式 | TD-03 起需要 后端→前端 的 SSE/WebSocket 把 run_steps 实时推送〔本项目新增，通道待定〕 |
| 幂等/并发 | Run 创建、brief confirm 需幂等(重复点击)；多员工并发 = 多 profile 进程，受 Hermes 并发/限流约束〔待核〕 |

## 7. Hermes 运行时边界契约（我们调它什么 / 假设它给什么）
- **调用面**：HTTP `POST /v1/runs`→SSE `/events`→`/approval`,`/stop`〔实测〕；员工=profile,配置在 `profiles/<name>/{SOUL.md,config.yaml,.env,skills/}`。
- **我们假设 Hermes 负责**：人格组装、技能检索、工具/MCP 执行、危险动作抛 `approval_required`、记忆/学习循环。**我们不自建这些**(ADR 0001)。
- ✅ **原〔待核〕清单已于 2026-07-08 全部实测关闭**（[验证报告](../research/hermes-verification-2026-07-07.md)，事实回填在 [DATA-MODEL §5.3](DATA-MODEL-AND-API.md)）：MCP=`hermes mcp add`；打包=`profile export/import/install`(⚠️import 写 wrapper 需清理)；审批=Tirith 四级+per-tool 覆盖；拓扑=一 profile 一 gateway 一端口；技能=`skills install/tap`；workdir=profile 级 work root+每 Run 子目录(架构决策见 TD-03)；模型名=`deepseek-v4-flash`。

## 8. 分期与"现在能细化到哪"
- **能立刻细化到实现级**(纯我们的层)：§3 编排/服务/边界、§4 数据模型(含新表)、§5.1/5.2 时序、§6 大部分横切。→ 落到 TD-01/TD-02 + 新增 provisioning TD。
- **只能到边界契约级、须实测补 5%**(贴 Hermes)：§5.3、§7、§4.1 的 `hermes_profile`/toolset/mcp 具体值。→ TD-03 + 一次 Hermes 验证会话。

## 9. 每个组件的详细 tech-design 索引（按此逐个加深）
| 组件 | 详细设计文件 | 状态 |
|---|---|---|
| 群讨论第一片收尾 | [TD-01](TD-01-verify-and-harden-slice-1.md) | 已拆 task，可深化 |
| 多 agent 讨论编排 | [TD-02](TD-02-multi-agent-discussion.md) | 已拆 task，可深化 |
| Hermes 执行接入 | [TD-03](TD-03-hermes-execution.md) | 已拆 task，含待核 |
| agent 供给(NL→agent) | [TD-04](TD-04-agent-provisioning.md) | 已拆 task(逻辑/物理供给分段，ProfileProvisioner 接缝切在待核边界) |
| 能力映射表 capability_catalog | [TD-05](TD-05-capability-catalog.md) | 已拆 task(代码常量+risk_gate 只升不降) |

## 10. TD-13 公司认知层

公司事件账本位于运行时与产品层之间：产品写入消息、任务、资料、产出和决策时同步投影不可变 `company_events`；Context Engine 在 Hermes ACP 或 DeepSeek 调用前从共享事件、员工私有记忆、同事关系和资料索引中检索并持久化 `context_manifests`。Hermes profile 继续保存员工人格、技能和运行时记忆，但不作为跨员工事实源。

员工间 MCP 只提供受 token 绑定的公司内协作能力。姓名/岗位语义在 MCP 契约层传递，服务端解析并校验内部员工记录；重复 ping、无新事实循环和计划范围调整仍由 AgentPulse 的持久服务层治理。反思 worker 先使用证据保留的确定性压缩，未来可替换为 Hermes 生成式总结而无需改变数据和权限边界。

Obsidian 集成位于桌面资料层：Electron 主进程让用户选择 Vault 后，只读取
`.agentpulse/managed/**/*.md`，renderer 将正文和相对路径提交到
`/api/knowledge-sources/obsidian-sync`。API 以 `workspace_id + origin + source_ref`
幂等更新资料索引，既不上传 Vault 其他目录，也不向本机回写文件。
