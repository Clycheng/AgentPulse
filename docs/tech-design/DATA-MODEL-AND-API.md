# 数据模型 + API 契约（唯一真相源）

> 本文件是 AgentPulse 后端**表结构 / 字段 / 接口 / 错误码**的唯一权威规格。任何人/AI 照此可直接编码，无需再猜。
> 分两类：`【已实现】`= 当前代码就是这样（最后核对 `services/api`：2026-07-23）；`【目标】`= 尚未实现但字段已钉死可直接建。
> 改动本文件所列任何 schema/接口，必须同步改代码 + 更新本文件；表结构改动**必须两处都改**（见 §0 双 schema 硬约束）。

## 0. 全局约定（必读，否则直接踩坑）

- **⚠️ 双 schema 硬约束**：`services/api/app/core/database.py` 里 **`init_postgres()`（生产）和 `init_sqlite()`（测试）各写了一份完整建表 SQL**。**任何加表/加列都必须在这两个函数里都改**，否则测试(SQLite)和生产(Postgres)结构分叉、测试过了线上崩。加列优先用文件末尾的 `ensure_column(conn, table, col, decl)` 幂等助手（它对两种方言都生效）。
- **ID 格式**：主键都是 `TEXT`，格式 `<entity>_<hex>`（如 `task_ab12…`、`brief_77…`），由 `new_id("<entity>")` 生成。
- **时间**：所有 `*_at` 是 `TEXT`，ISO8601 UTC 字符串，由 `now_iso()` 生成。
- **多值字段序列化边界**：DB 里存 JSON 字符串的列以 `_json` 结尾（如 `participant_agent_ids_json`）；对应的 **API DTO 字段去掉 `_json` 后缀、类型是数组/对象**（如 `participant_agent_ids: list[str]`）。编排层负责 `json.dumps/loads` 互转。**看到 `_json` = DB 列；看到无后缀 = API 字段。**
- **鉴权**：所有业务接口经 `Depends(get_workspace_id)` / `get_current_user_id` / `get_current_user`（`services/api/app/api/deps.py`）拿到 workspace 和 user，**请求体里不传 workspace_id/user_id**。
- **错误约定**：service 层校验失败抛 `ValueError`；route 层 catch 成 `HTTPException(400, detail=str(e))`。资源不存在/不属于本 workspace → `404`。

---

## 1. 表结构

### 1.1 `consensus_briefs` 【已实现】
共识纪要。讨论的结构化产出；Task 创建的门控依据。一个讨论可产出多个 brief，一个 brief 可拆多个 Task。

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PK | `brief_xxx` |
| `workspace_id` | TEXT | NOT NULL, FK→workspaces ON DELETE CASCADE | |
| `discussion_conversation_id` | TEXT | NOT NULL, FK→conversations ON DELETE CASCADE | 从哪个讨论产生 |
| `status` | TEXT | NOT NULL DEFAULT `'draft'`, CHECK IN (`draft`,`confirmed`,`rejected`,`superseded`) | |
| `goal` | TEXT | NOT NULL | 目标（唯一必填语义字段） |
| `scope` | TEXT | NOT NULL DEFAULT `''` | 范围 |
| `constraints` | TEXT | NOT NULL DEFAULT `''` | 约束 |
| `success_criteria` | TEXT | NOT NULL DEFAULT `''` | 成功标准 |
| `owner_agent_id` | TEXT | FK→agents ON DELETE SET NULL, 可空 | 负责人 |
| `participant_agent_ids_json` | TEXT | NOT NULL DEFAULT `'[]'` | 参与者 agent id 数组的 JSON。**API 侧字段名 `participant_agent_ids`** |
| `work_items_json` | TEXT | NOT NULL DEFAULT `'[]'` | TD-11 分工合同。API 侧字段名 `work_items`；须通过 §10.1 校验 |
| `created_by_agent_id` | TEXT | NOT NULL, FK→agents ON DELETE CASCADE | 谁整理出的 |
| `supersedes_brief_id` | TEXT | FK→consensus_briefs ON DELETE SET NULL, 可空 | 取代哪个旧 brief |
| `derived_from_brief_id` | TEXT | FK→consensus_briefs ON DELETE SET NULL, 可空 | 派生自哪个 brief |
| `created_at` | TEXT | NOT NULL | |
| `confirmed_at` | TEXT | 可空 | 确认时间 |
| `confirmed_by_user_id` | TEXT | FK→users ON DELETE SET NULL, 可空 | 谁确认的（reject 也写此列，见 §3 待办 G5） |

语义字段（goal/scope/constraints/success_criteria）API 层限长 **≤500 字符**；`participant_agent_ids` API 层限 **≤12 个**。

### 1.2 `tasks` 【已实现，`consensus_brief_id` 为本项目新增】
| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PK | `task_xxx` |
| `workspace_id` | TEXT | NOT NULL, FK→workspaces CASCADE | |
| `title` | TEXT | NOT NULL | API 限长 ≤160 |
| `description` | TEXT | NOT NULL DEFAULT `''` | API 限长 ≤2000 |
| `priority` | TEXT | NOT NULL DEFAULT `'P2'` | P0/P1/P2 |
| `owner_agent_id` | TEXT | FK→agents SET NULL, 可空 | |
| `status` | TEXT | NOT NULL DEFAULT `'进行中'` | 待认领/待执行/进行中/待确认/阻塞/已完成 |
| `progress` | INTEGER | NOT NULL DEFAULT 0 | 0–100 |
| `conversation_id` | TEXT | FK→conversations SET NULL, 可空 | 来源会话 |
| `due_date` | TEXT | 可空 | |
| `parent_task_id` | TEXT | FK→tasks SET NULL, 可空 | 子任务 |
| `task_plan_id` | TEXT | 可空，FK 语义→task_plans | TD-11 所属执行计划；计划内 task 的 `(task_plan_id, plan_item_key)` 唯一 |
| `plan_item_key` | TEXT | 可空 | 对应 brief work item；根任务固定 `__root__` |
| `expected_output` | TEXT | NOT NULL DEFAULT `''` | 交付合同的自然语言说明 |
| `output_type` | TEXT | NOT NULL DEFAULT `'markdown'` | `markdown` / `content_package_v1` / `plan_summary` |
| `consensus_brief_id` | TEXT | FK→consensus_briefs SET NULL, 可空* | **门控依据**。经 `ensure_column` 加列。*建任务时：无 parent 则必填且须 confirmed（见 §2.5 门控） |
| `created_at` / `updated_at` | TEXT | NOT NULL | |

### 1.3 `conversations`（相关列）【`discussion_status` 为新增】
| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `discussion_status` | TEXT | NOT NULL DEFAULT `'discussing'`, CHECK IN (`discussing`,`aligned`) | 讨论态。confirm/launch 后置 `aligned`，reject 后回 `discussing` |

（`conversations` 其余列 id/workspace_id/kind/name 等见 database.py，本项目未改。）

### 1.4 `messages`（相关列）【已实现】
`id / conversation_id / sender_type(user|agent|system) / sender_id / content / provider / model / created_at`。
- 共识纪要卡片是一条 `sender_type='system'` 且 `content` 以 **`BRIEF_CARD:` 前缀** + brief JSON 的消息（前端据此渲染卡片）。这是当前"卡片"的传递方式。

### 1.5 `runs` 【已实现】
Run 既是聊天执行轨迹，也是 TD-11 的持久任务队列。核心列：`id / workspace_id / conversation_id / agent_id / task_id? / status / input_message_id? / output_message_id? / hermes_profile_id / hermes_run_id / workdir / provider / model / usage_json / error / attempt_no / lease_owner / lease_expires_at / started_at / created_at / completed_at`。
- `task_id`、`input_message_id` 均可空：聊天 Run 可以无 Task，任务 Run 可以无输入消息。
- 任务 Run 的 `(task_id, attempt_no)` 唯一；调度与租约语义见 §10.3。

---

## 2. 现有 API 契约 【已实现】

### 2.1 `POST /api/briefs` → 201/200 `BriefOut`
建 draft 纪要（agent 调）。请求体 `CreateBriefRequest`：
```
discussion_conversation_id: str            (必填)
goal: str                                  (必填, 1–500)
scope: str = ""                            (≤500)
constraints: str = ""                      (≤500)
success_criteria: str = ""                 (≤500)
owner_agent_id: str | null = null
participant_agent_ids: list[str] = []      (≤12)
work_items: list[BriefWorkItem] = []        (TD-11 可启动 brief 必须为 3-6 项，见 §10.1)
created_by_agent_id: str                   (必填)
supersedes_brief_id: str | null = null
derived_from_brief_id: str | null = null
```
错误：校验失败/会话不存在 → `400`。

### 2.2 `POST /api/briefs/{brief_id}/confirm` → `BriefOut`
兼容接口。内部委托与 `/launch` 相同的计划启动服务，继续返回 `BriefOut`。空请求体（user 来自 token）；成功后 brief 为 `confirmed`、讨论为 `aligned`，且已创建或复用唯一计划。错误 `400`（如 brief rejected/superseded 或负责人未就绪）。

### 2.3 `POST /api/briefs/{brief_id}/reject` → `BriefOut`
老板拒绝。空请求体。`status: draft→rejected`。错误 `400`。

### 2.4 `GET /api/briefs/{brief_id}` → `BriefOut`
不存在或不属于本 workspace → `404`。

**`BriefOut`（响应 DTO）**：§1.1 全部字段，但两个 JSON 列分别输出为 `participant_agent_ids: list[str]` 和 `work_items: list[BriefWorkItem]`。

### 2.5 `POST /api/tasks` → `TaskOut`（含**门控**）
请求体 `CreateTaskRequest`：`title(1–160,必填) / description(≤2000) / priority / owner_agent_id? / status / progress(0–100) / conversation_id? / due_date? / parent_task_id? / consensus_brief_id?`。
**门控逻辑**（`orchestration/gate.py::validate_task_creation_gate`，在 `create_task()` 第一步执行）：
- `consensus_brief_id` 有值 → 该 brief 须存在于本 workspace 且 `status=='confirmed'`，否则 `ValueError`→`400`。
- `consensus_brief_id` 空 但 `parent_task_id` 有值 → 放行（子任务继承父的 brief）。
- 两者都空 → 拒绝 `400`（"Task creation requires a confirmed consensus_brief_id, or parent_task_id"）。

`TaskOut` 包含 `consensus_brief_id`，可从任务追溯到 brief。

---

## 3. 已知不对齐 / 缺口（拿到 design 前必须知道，附解决方案）

| # | 问题 | 影响 | 解决（归属 task） |
|---|---|---|---|
| G1 | ADR 0006 把 DB 列写成 `participant_agent_ids`，实际是 `participant_agent_ids_json` | 照 ADR 建表会错 | 以本文件为准；ADR 0006 加勘误指向本文件（本次已加） |
| G2 | ~~`TaskOut` 缺 `consensus_brief_id` 字段~~ | 已解决 | TD-01-T1b 已补齐 |
| G3 | ~~brief confirm/reject 未接讨论状态~~ | 已解决 | confirm/launch→`aligned`；reject→`discussing` |
| G4 | `database.py` 双 schema（init_postgres / init_sqlite）需手工同步 | 漏改一处→生产/测试分叉 | 加列一律走 `ensure_column`；新建表两函数都加；本文件 §0 已列为硬约束 |
| G5 | reject 也写 `confirmed_by_user_id`（列名语义偏差） | 轻微；"谁拒绝的"存在"谁确认的"列 | 可接受；如需清晰后续可加 `resolved_by_user_id`，暂不改 |

---

## 4. 【目标】TD-02 新增契约（多 agent 讨论）

不新增表；扩 `orchestration/discussion.py` 的函数契约（纯 `services/api`，不碰 Hermes）：

```python
# 选下一个发言人：@优先；否则主持人(默认小秘)LLM 选；返回 None = 该停了
def select_next_speaker(conn, conversation_id: str, member_agent_ids: list[str],
                        last_message: Row | None) -> str | None: ...

# 非流式：跑一轮讨论，返回本轮结果摘要（用于单测/非流式 send_message 兼容层）
def run_discussion_round(conn, *, workspace_id: str, conversation_id: str,
                         max_agent_turns: int = 4) -> dict:
    # 返回: {"turns": int, "converged": bool, "brief_id": str | None, "speaker_ids": list[str]}

# 流式变体：给 send_message_stream 用，yield 每条 agent 发言 token（TD-02-T5 新增）
async def run_discussion_round_stream(conn, *, workspace_id: str, conversation_id: str,
                                      max_agent_turns: int = 4) -> AsyncIterator[dict]:
    # yield {"type": "token"|"turn_start"|"turn_end"|"converged"|"brief_created",
    #        "agent_id": str|None, "content": str, "brief_id": str|None}
    # 路由层消费此 generator，把 token 类事件转成 SSE text/event-stream 推前端
    # turn_start/turn_end 用于前端渲染"小秘正在发言..."等状态
```
- 约束：讨论态(`discussing`)下只允许产出 message/提问/brief 草稿，不允许执行类动作（为 TD-03 铺路）。
- 收敛：主持人判断"背景够拆任务" → 调 `POST /api/briefs` 出草稿；到 `max_agent_turns` 上限也强制产一版。
- **TD-02-T5 的接线点**：`send_message_stream` 改为 `async for event in run_discussion_round_stream(...)` 然后按 event.type 推 SSE；非流式 `send_message` 改为调非流式版本（降级为 API 兼容层）。
- 开放项（实现前定，见 [TD-02](TD-02-multi-agent-discussion.md) §开放问题）：主持人是否可配、发言人选择用 LLM 还是轮询。**这几项定了要回填本文件。**

---

## 5. TD-03 契约（接 Hermes 执行）

> **§5 已实现**：当前执行传输是 Hermes ACP，Run 可来自聊天或 TD-11 持久任务队列；生命周期状态机在 `app/runtime/runs.py`，动态 MCP 见 §10.4/§11。

### 5.1 `runs` 扩列（两 schema 都加）✅ 已实现
新增：
- `task_id TEXT FK→tasks CASCADE`（任务 Run 必填；聊天 Run 为空）
- `hermes_profile_id TEXT`（对应员工 profile 名，对应 `agent_specs.hermes_profile`）
- `hermes_run_id TEXT`（Hermes 侧 run id，用于调 approval/stop 接口）
- `workdir TEXT NOT NULL`（**绝对路径**隔离目录，格式 `<data_root>/agents/<profile>/work/runs/<run_id>`，见 §5.3 workdir 架构决策）

`status` 状态机扩为：`queued → running → waiting_user | waiting_clarify → completed | failed`
- `waiting_user`：高风险操作触发 Tirith 审批，等老板拍板
- `waiting_clarify`：agent 主动求援（需求不清），等人回答后继续（见 §5.5）

**`approvals` 扩列（两 schema 都加）**：
- `run_id TEXT REFERENCES runs(id) ON DELETE SET NULL`（可空——旧的任务状态审批无 run）。**没有它，老板批准后找不到该恢复哪个 Run**（恢复 = 用 `runs.hermes_run_id` 调 Hermes `POST /v1/runs/{hermes_run_id}/approval`）。
- `type TEXT NOT NULL DEFAULT 'high_risk' CHECK IN ('high_risk','clarification','capability_upgrade','business_tool')`：四种审批类型。

  | type | 含义 | payload_json schema | decision 合法值 |
  |---|---|---|---|
  | `high_risk` | Tirith 高危操作审批 | `{}` 或 tool 信息 | `approved` / `rejected` |
  | `clarification` | agent 执行中求援 | `{"question":str,"missing_context":str,"answer":str(批准后填)}` | `answered` |
  | `capability_upgrade` | agent 申请新能力 | `{"capability_description":str,"suggested_capability_key":str\|null,"approved_capability_key":str\|null,"failed_task_id":str\|null,"run_id":str\|null}` | `approved` / `rejected` |
  | `business_tool` | 发邮件等真实对外动作 | `{"business_action_id":str,"tool":str,"capability_key":str,"risk_gate":str,"preview":object,"scope":"once"\|"always"}` | `approved` / `rejected` |

**`agents` 扩列（两 schema 都加）**：
- `hermes_gateway_port INTEGER`（可空——员工未完成供给时为空）。端口分配规则：基础端口 `8642` + `agent_specs` 里按 workspace 顺序的序号（如第 1 个员工 8642，第 2 个 8643）；`RunService` 按此字段找 gateway 地址 `http://127.0.0.1:{port}`。供给时（TD-04-T6）由 `LocalHermesProvisioner` 写入。

### 5.2 `run_steps` 新表
| 列 | 类型 | 约束 |
|---|---|---|
| `id` | TEXT | PK `step_xxx` |
| `run_id` | TEXT | NOT NULL FK→runs CASCADE |
| `type` | TEXT | NOT NULL, IN (`message`,`thinking`,`tool_call`,`tool_result`,`approval_required`,`status`,`final`) |
| `status` | TEXT | NOT NULL |
| `title` | TEXT | NOT NULL DEFAULT `''` |
| `detail` | TEXT | NOT NULL DEFAULT `''` |
| `payload_json` | TEXT | NOT NULL DEFAULT `'{}'` |
| `created_at` | TEXT | NOT NULL |

### 5.3 `HermesBackend` 接口（`services/api/app/runtime/hermes_client.py`）

> ⚠️ **2026-07-10 作废重写中（见 [ADR 0007](../decisions/0007-hermes-v0.18-interface-acp.md)）**：本机实测 Hermes v0.18.2 **没有** REST `/v1/runs`+SSE 接口——`hermes gateway` 现在是消息平台网关。执行传输改用 **ACP（`hermes acp`，stdio JSON-RPC）**，供给用 CLI（`LocalHermesProvisioner` 已实现并过 e2e）。下方 REST 形状的描述仅作历史保留，实现以 ADR 0007 为准；`agents.hermes_gateway_port`（端口/一员工一网关）随之作废。DeepSeek 实测：provider `deepseek`、key env `DEEPSEEK_API_KEY`、模型 `deepseek/deepseek-v4-flash`。
>
> **当前实现（2026-07-23）**：`HermesBackend` 启动 `hermes --profile <profile> acp`，`new_session` 传绝对 `cwd` 与动态 `acp.schema.HttpMcpServer`。`RunContext.mcp_servers` 是每 Run 配置，TD-11 用它注入短期 token 的公司工具。下方 HTTP 网关内容只是历史验证记录，不是当前接口。

```python
@dataclass
class RunContext:
    run_id: str; workspace_id: str; agent_id: str        # agent_id → 决定用哪个 Hermes profile
    conversation_id: str; task_id: str
    prompt: str; workdir: str                            # workdir 必须绝对路径，否则构造即抛错
    timeout: int = 600
    mcp_servers: list[dict] = field(default_factory=list) # ACP HttpMcpServer 配置；每 Run 动态注入

@dataclass
class AgentEvent:                                        # 直接映射 Hermes SSE 事件
    type: str          # message|thinking|tool_call|tool_result|approval_required|status|final|usage
    payload: dict

class HermesBackend:
    async def run(self, ctx: RunContext) -> AsyncIterator[AgentEvent]: ...
```
内部走 Hermes HTTP（实测契约，[ADR 0005](../decisions/0005-hermes-poc-safety-findings.md) / [ARCHITECTURE §3.4](../ARCHITECTURE.md)）：
- 起网关：`hermes -p <profile> gateway run`，env `API_SERVER_ENABLED=true API_SERVER_PORT=8642 API_SERVER_KEY=<key>`。
- `POST http://127.0.0.1:8642/v1/runs`，头 `Authorization: Bearer <key>`，body `{"input": "<prompt>"}` → `{"run_id","status":"started"}`。
- `GET /v1/runs/{id}/events`（SSE，`Bearer`）→ `data: {"event":"message.delta","delta":...}` … `{"event":"run.completed","output":...,"usage":...}`。
- 审批：事件 `approval_required` → `POST /v1/runs/{id}/approval`（放行）；`/v1/runs/{id}/stop`（中止）。
- **模型名**：DeepSeek 侧实测可用 `deepseek-v4-flash` / `deepseek-v4-pro`（**不是** `deepseek-chat`），profile config.yaml `model: deepseek/deepseek-v4-flash`。

**✅ 2026-07-08 验证回填**（原〔待核〕已清，全文出处：[验证报告](../research/hermes-verification-2026-07-07.md)）：
- **`POST /v1/runs` 请求体实测形状**：`{"input", "session_id"?, "instructions"?, "conversation_history"?, "previous_response_id"?}`——**没有 cwd/workdir 参数**。
- **workdir 语义(架构决策)**：`terminal.working_dir` 是 **profile 级**绝对路径配置。采用"**profile 级绝对 work root + 每 Run 子目录约定**"：每员工 profile 配 `terminal.working_dir = <data_root>/agents/<profile>/work`(绝对，绝不继承进程 cwd——满足 ADR 0005)；Runner 每次 Run 前 `mkdir <work_root>/runs/<run_id>` 并在 prompt 中指定该子目录为本次工作区。硬边界=员工自己的 work root(即使 agent 不守约定,污染也出不了它自己的 root)；软约定=子目录。不可信任务后续用 `terminal.backend: docker` + `container_persistent: false` 加强。`runs.workdir` 列存该子目录绝对路径。
- **审批系统=Tirith**：config.yaml `approvals: {mode: manual|smart|off, timeout: <秒>, cron_mode: deny|approve}` + `tirith: {enabled: true}`；四级权限(read-only / low-risk writes / high-risk writes / destructive)，可 per-tool 覆盖；`HERMES_YOLO_MODE=1` 会跳过审批——**生产严禁**。
- **拓扑**：❌ 不支持单网关多 profile → **一员工一 gateway 进程一端口**(8642, 8643, …)。端口存在 `agents.hermes_gateway_port`（见 §5.1 agents 扩列），`RunService` 用 `http://127.0.0.1:{agent.hermes_gateway_port}` 访问该员工 gateway。端口冲突检测：provisioner 写入前 `SELECT MAX(hermes_gateway_port) FROM agents WHERE workspace_id=?`，取最大值 +1（初始值 8642）。
- **MCP 配置**：`hermes mcp add <name> --url <endpoint>` 或 `--command <cmd> --args ... --env KEY=VALUE`(stdio)，`--auth oauth|header`；落 profile 级 config.yaml。
- **技能安装**：`hermes skills install <github-path|url>`、`hermes skills tap add <repo>`；落 `profiles/<name>/skills/`。
- **profile 打包**：`hermes profile export <name> -o x.tar.gz`(含 config/.env/SOUL/skills/memory,不含 sessions) / `import --name <new>` / `install <git-url>` 均可用。⚠️ **import 会自动写 wrapper 到 `~/.local/bin/`**(即使原来用了 --no-alias)——供给流程 import 后必须清理该 wrapper。
- **toolsets 真名**(25 个,全表见验证报告 V1)：常用=`terminal` `file`(⚠️不是 files) `code_execution` `web` `browser` `vision` `image_gen` `tts` `skills` `memory` `todo` `clarify` `delegation` `cronjob` `computer_use`。开关：`hermes tools enable|disable <name>`。

### 5.4 审批复用（高危操作）
Hermes ACP `request_permission` → 建 `approvals` 行（`type='high_risk'`）→ 前端复用内联审批卡片。API 只在数据库记录决定；执行中的 Run 每 250ms 轮询，映射为 ACP `AllowedOutcome` / `DeniedOutcome`。50 秒未处理则 `expired` 并 deny；拒绝后允许 Hermes 解释，不由审批 API 提前迁移 Run 状态。

### 5.5 执行中求援（clarification_required）【TD-03-T4 新增】

**触发机制**：利用 Hermes 原生 `clarify` toolset。agent SOUL.md 里加指令：「遇到需求不清楚、依赖信息缺失时，调用 `clarify` 工具并附上问题和缺失的上下文，等待答复，不允许臆测继续」。`clarify` 工具调用 → Hermes 产生 `approval_required` 事件，`metadata.category='clarification'`（在 prompt 里约定）→ RunService 识别此类事件建 `approvals`（`type='clarification'`）。

**数据流（worker AI 直接照此实现）**：
```
agent 调 clarify 工具
→ Hermes SSE: approval_required + metadata.category='clarification'
→ RunService:
    建 approvals(type='clarification', payload_json={"question":..., "missing_context":...})
    runs.status = 'waiting_clarify'
    群里推 system 消息（type='CLARIFY_CARD:{...}'）→ 前端渲染提问卡片
→ 人/AI 在群里回答（POST /api/approvals/{id}/answer, body={"answer":"..."}）
    → approvals.payload_json.answer = 答案, decision = 'answered'
    → resume_after_clarification:
        把答案拼进 prompt 续跑 Hermes（POST /v1/runs/{hermes_run_id}/approval）
        runs.status = 'running'
```

**新增 API**：`POST /api/approvals/{id}/answer`，body `{"answer": str}`，仅 `type='clarification'` 的 approval 可调，否则 400。

开放项（见 [TD-03](TD-03-hermes-execution.md) §开放问题）：审批粒度用哪个高风险工具验证端到端。**定了回填本文件。**

---

## 6. 【目标】Agent 供给（NL→定制员工，TD-04/TD-05）

对应 [ARCHITECTURE-DETAILED.md](ARCHITECTURE-DETAILED.md) §4.1/§5.1 与 [agent-model-and-capabilities.md](agent-model-and-capabilities.md) §3。本节为权威 schema。

### 6.1 `agent_specs` 新表（一个员工一行：用户期望 + 供给状态 + 自进化跟踪）
| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PK | `spec_xxx` |
| `agent_id` | TEXT | NOT NULL, FK→agents ON DELETE CASCADE, UNIQUE | 一对一 |
| `workspace_id` | TEXT | NOT NULL, FK→workspaces CASCADE | |
| `role_name` | TEXT | NOT NULL | 如"前端工程师"，API 限长 ≤80 |
| `source_request` | TEXT | NOT NULL DEFAULT `''` | 用户原始 NL 需求，≤2000 |
| `responsibilities_json` | TEXT | NOT NULL DEFAULT `'[]'` | 职责数组(API 字段 `responsibilities: list[str]`，≤12 条，每条 ≤200) |
| `hermes_profile` | TEXT | 可空 | 对应 Hermes profile 名。命名规则：`wk<workspace前6>-<agent前6>`，建前查重 |
| `status` | TEXT | NOT NULL DEFAULT `'draft'`, CHECK IN (`draft`,`provisioning`,`blocked_on_credentials`,`ready`,`failed`) | 供给状态机：draft→provisioning→(blocked_on_credentials⇄)→ready；任一步失败→failed |
| `runs_since_last_reflection` | INTEGER | NOT NULL DEFAULT 0 | 距上次技能反思已完成的 Run 数；到 reflection_interval 时触发（TD-06-T1） |
| `last_skill_reflection_at` | TEXT | 可空 | 上次技能沉淀时间（TD-06-T1） |
| `reflection_interval` | INTEGER | NOT NULL DEFAULT 5 | 每隔几个 Run 自动反思一次，可按员工调整（TD-06-T1） |
| `created_at` / `updated_at` | TEXT | NOT NULL | |

### 6.2 `agent_capabilities` 新表（员工被授予的每项能力一行）
| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PK | `cap_xxx` |
| `agent_id` | TEXT | NOT NULL, FK→agents CASCADE | |
| `workspace_id` | TEXT | NOT NULL, FK→workspaces CASCADE | |
| `capability_key` | TEXT | NOT NULL | catalog 主键，如 `write_code`/`run_tests`/`git_push`/`deploy_preview`/`deploy_prod`/`domain_register`/`seo_audit`/`social_content`。同一 agent 内 UNIQUE(agent_id, capability_key) |
| `skill_refs_json` | TEXT | NOT NULL DEFAULT `'[]'` | SKILL 名数组 |
| `toolset_refs_json` | TEXT | NOT NULL DEFAULT `'[]'` | Hermes toolset 名数组〔具体合法值待核〕 |
| `mcp_refs_json` | TEXT | NOT NULL DEFAULT `'[]'` | MCP server 名数组〔配置语法待核〕 |
| `required_credentials_json` | TEXT | NOT NULL DEFAULT `'[]'` | 凭证名数组，如 `["GITHUB_TOKEN"]` |
| `risk_gate` | TEXT | NOT NULL DEFAULT `'auto'`, CHECK IN (`auto`,`approval`,`prohibited_auto`) | auto=直接干；approval=弹审批卡片；prohibited_auto=永远人工(花钱/不可逆) |
| `status` | TEXT | NOT NULL DEFAULT `'pending'`, CHECK IN (`pending`,`credential_missing`,`enabled`,`disabled`) | |
| `created_at` / `updated_at` | TEXT | NOT NULL | |

### 6.3 capability_catalog（能力映射表——系统静态资产，不建表）
实现为代码内常量 `services/api/app/orchestration/capability_catalog.py`〔本项目新增〕：`dict[capability_key] -> {"skills":[...],"toolsets":[...],"mcp":[...],"required_credentials":[...],"risk_gate":"auto|approval|prohibited_auto","description":str}`。初始条目按 [agent-model-and-capabilities.md](agent-model-and-capabilities.md) §4 表。改 catalog = 改代码 + 本文件同步。种子(v1)：
种子(v1，toolset 名已按 2026-07-08 [验证报告](../research/hermes-verification-2026-07-07.md) V1 实测真名校正——注意是 `file` 不是 `files`)：
```
write_code      → toolsets[terminal,file,code_execution]                      risk=auto
run_tests       → toolsets[terminal]                                          risk=auto
git_push        → toolsets[terminal] + mcp[github] + creds[GITHUB_TOKEN]      risk=approval
deploy_preview  → toolsets[terminal] + creds[平台token]                        risk=auto
deploy_prod     → toolsets[terminal] + creds[平台token]                        risk=approval
domain_register → (API 包装工具) + creds[注册商key+付费方式]                    risk=prohibited_auto
seo_audit       → toolsets[terminal,web]  (lighthouse 经 terminal 跑)          risk=auto
social_content  → toolsets[web,vision,image_gen] (发布环节人工，见能力文档 §5)   risk=approval
```
**TD-07（2026-07-10）已扩展**：在技术岗种子外补齐 7 大业务类共 31 个能力条目（客服/内容运营/数据/HR/法务/财务/项目管理，见 [TD-07](TD-07-business-capability-catalog.md)），并新增 `ROLE_BUNDLES` 常量（"客服专员""数据分析师"等 12 个预配角色 → 能力清单，供"按职位一键招人"）。同时把种子里 `social_content` 的 toolsets 从代码侧的 `[web,vision]` 修正为与本表一致的 `[web,vision,image_gen]`（此前是 TD-05 代码与本真相源不一致，已对齐）。付款类 `payment_execution` = `prohibited_auto`（花钱不可逆，永不自动）。访问器：`get_role_bundle(name)` / `list_role_bundles()`。

### 6.4 新增 API 契约
| 接口 | 请求体 | 响应 | 错误 |
|---|---|---|---|
| `POST /api/agents`（扩展现有） | 现有字段 + 可选 `role_spec: {role_name, source_request?, responsibilities[], capability_keys[]}` | 现有 AgentOut + `spec: AgentSpecOut` | 未知 capability_key→400 |
| `GET /api/agents/{id}/spec` | — | `AgentSpecOut`（含 `capabilities: list[AgentCapabilityOut]`） | 404 |
| `POST /api/agents/{id}/credentials` | `{"credential_name":"GITHUB_TOKEN","value":"..."}` | 更新后的 `AgentSpecOut` | 该 agent 无此凭证需求→400 |
| `POST /api/agents/{id}/provision` | 空体（幂等，重试供给） | `AgentSpecOut` | spec 不存在→404 |

**AgentSpecOut**：§6.1 字段(json 列 → 数组字段，去 `_json` 后缀)。**AgentCapabilityOut**：§6.2 同理。
**凭证安全（ADR 0011）**：凭证由 AgentPulse 写入 `agent_credentials.encrypted_value`，使用 `auth_secret_key` 派生的版本化 Fernet key 加密。API 仅返回 required credential 的配置布尔值；明文/密文均不进入响应、审批 payload、RunStep 或 Hermes profile。一个凭证会刷新所有依赖它的能力；撤销后相关能力回到 `credential_missing`。

双 schema 提醒：§6.1/§6.2 建表须同时加进 `init_postgres()` 和 `init_sqlite()`（§0 G4）。

---

## 7. 【目标】Agent 自进化（TD-06）

### 7.1 `ProfileProvisioner` 新增方法（对接 TD-04 接口）
```python
def update_skill(self, profile_name: str, skill_name: str, content: str) -> None:
    # 写入 profiles/<name>/skills/auto/<skill_name>.md
    # 或调 hermes skills learn '<content>'

def add_capability(self, profile_name: str, capability_key: str,
                   bundle: CapabilityBundle) -> None:
    # 追加 toolset → configure(); 安装 skills; 配 MCP; 更新 agent_capabilities 表

def reload_gateway(self, profile_name: str) -> None:
    # hermes -p <name> gateway reload（若不支持则 stop + start）
```

### 7.2 新增 API 契约（TD-06）
| 接口 | 请求体 | 响应 | 错误 |
|---|---|---|---|
| `GET /api/agents/{id}/skills` | — | `{"auto_skills":[{"name":str,"created_at":str,"preview":str}]}` | 404 |
| `POST /api/agents/{id}/reflect` | 空体（调试用，手动触发反思） | `{"skills_added": int, "skill_names": list[str]}` | spec 不存在/profile 未就绪→400 |

审批答复（能力升级分支）：复用现有 `POST /api/approvals/{id}/answer`，body `{"decision":"approved","payload":{"approved_capability_key":"git_push"}}`；后端按 `approvals.type='capability_upgrade'` 分发到 `UpgradeService.execute_upgrade()`。

---

## 8. Idea 中心（TD-08）

> **§8.1 schema + API 已实现**（TD-08-T1, 2026-07-10）：`ideas` 表、`agent_specs` 三列、`conversations.idea_id` 均落到两套 schema + `ensure_column`；service 在 `app/services/ideas.py`，路由在 `app/api/routes/ideas.py`。实现细节两处与本设计的偏差已标注在下方。§8 的"idle 反思自动产 idea"(TD-08-T2) 仍是【目标】，需 Hermes。

### 8.1 `ideas` 新表 ✅ 已实现
| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PK | `idea_xxx` |
| `workspace_id` | TEXT | NOT NULL FK→workspaces CASCADE | |
| `source_agent_id` | TEXT | NOT NULL FK→agents CASCADE | |
| `title` | TEXT | NOT NULL | ≤120 |
| `description` | TEXT | NOT NULL | ≤1000 |
| `category` | TEXT | NOT NULL CHECK IN (`improvement`,`opportunity`,`risk`,`learning`) | |
| `status` | TEXT | NOT NULL DEFAULT `'new'` CHECK IN (`new`,`reviewed`,`accepted`,`dismissed`,`converted`) | |
| `converted_brief_id` | TEXT | 可空 FK→consensus_briefs SET NULL | |
| `created_at` / `reviewed_at` | TEXT | reviewed_at 可空 | |

`agent_specs` 扩列（TD-08，双 schema）：`last_idle_think_at TEXT 可空` / `idle_think_interval_hours INTEGER NOT NULL DEFAULT 6` / `idle_thinking_enabled`。**实现偏差①**：`idle_thinking_enabled` 两套 schema 都用 `INTEGER NOT NULL DEFAULT 1`（不是 BOOLEAN）——因为 `ensure_column` 迁移对两方言用同一定义字符串，统一 0/1 最省事；API 层序列化为 `bool`。

`conversations` 扩列（TD-08，双 schema）：`idea_id TEXT 可空 FK→ideas SET NULL`（追溯从哪个 idea 转化来的会话）。

### 8.2 TD-08 API 见 [TD-08-idea-center.md](TD-08-idea-center.md)

**实现偏差②**：`PATCH /api/agents/{id}/idle-thinking` 返回 `IdleThinkingSettings`（`{agent_id, idle_thinking_enabled, idle_think_interval_hours, last_idle_think_at}`）而非 TD-08 写的 `AgentSpecOut`——避免为一个开关去扩 `AgentSpecOut` 及其所有消费方；无 agent_specs 行的员工（如默认小秘）返回 404。`POST /api/ideas`（agent 产出 idea 的入口，TD-08-T2 会用）已一并实现。

---

## 9. 外部渠道接入（TD-09）

> **§9.1 schema + Router + §9.2 webhook + §9.3 渠道管理 API 已实现**（TD-09-T1/T2, 2026-07-10）：`channel_configs` 表 + `conversations.source_channel/external_conversation_id` + `messages.external_message_id`（两套 schema + `ensure_column`，`active` 统一 `INTEGER 0/1`）；`app/channels/`（`router.route_inbound`/`find_or_create_conversation`/dedup + `adapters/` 的 generic & email 适配器 + 注册表）；`app/services/channels.py`（CRUD/stats/HMAC 验签/token 生成）；`routes/channels.py`（`/api/channels` 认证 CRUD + 软删）；`routes/webhooks.py`（公开 `POST /webhooks/{type}/{token}`，token+可选 HMAC 验签 → route_inbound → pin 有 target_agent 时经 `complete_agent_reply` 尽力触发回复）。**仍待做**：微信(XML+加密)/网页 widget 适配器、ChannelReply 把回复发回原渠道、渠道管理前端（TD-09-T3）。

### 9.1 `channel_configs` 新表 ✅ 已实现
| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PK | `chan_xxx` |
| `workspace_id` | TEXT | NOT NULL FK→workspaces CASCADE | |
| `channel_type` | TEXT | NOT NULL CHECK IN (`wechat`,`email`,`web_widget`,`generic_webhook`) | |
| `name` | TEXT | NOT NULL | 渠道别名，如"官网客服入口" |
| `token` | TEXT | NOT NULL UNIQUE | webhook URL token，随机生成 |
| `config_json` | TEXT | NOT NULL DEFAULT `'{}'` | 渠道配置（见 TD-09 §各渠道字段） |
| `target_agent_id` | TEXT | 可空 FK→agents SET NULL | 默认分配员工 |
| `target_conversation_id` | TEXT | 可空 FK→conversations SET NULL | 固定目标群（如客服总群） |
| `active` | BOOLEAN NOT NULL DEFAULT TRUE | | |
| `created_at` | TEXT | NOT NULL | |

`conversations` 扩列（TD-09，双 schema）：
- `source_channel TEXT 可空`（`wechat`/`email`/`web_widget`/`generic_webhook`/null）
- `external_conversation_id TEXT 可空`（外部用户标识，用于同一用户消息归同一会话）

`messages` 扩列（TD-09，双 schema）：
- `external_message_id TEXT 可空`（外部消息唯一 ID，去重用）

### 9.2 Webhook 端点（公开，无 JWT，靠 token + 渠道签名验证）
`POST /webhooks/{channel_type}/{token}` — 各渠道 inbound webhook 统一入口。

### 9.3 渠道管理 API 见 [TD-09-channel-adapters.md](TD-09-channel-adapters.md)

---

## 10. TD-11 自动执行闭环【已实现】

### 10.1 Brief work item

`BriefWorkItem` 字段固定为：`key / title / description / owner_agent_id / expected_output / output_type / depends_on_keys / final_delivery`。可 launch 的 brief 必须有 3-6 项；负责人都是群成员；key 唯一；依赖 key 存在且无环；必须且只能有一个 `final_delivery=true, output_type='content_package_v1'` 的最终项。

### 10.2 持久计划表

`task_plans`：`id / workspace_id / brief_id UNIQUE / root_task_id? / status(launching|active|blocked|completed|cancelled) / revision_count / blocked_reason / created_at / updated_at / completed_at?`。一个 brief 最多一个计划；`revision_count` 最多 2。

`task_dependencies`：`id / task_plan_id / task_id / depends_on_task_id / created_at`，边唯一且禁止自依赖。依赖环同时在 brief 校验和公司工具调整入口检查。

任务产出写 `task_outputs`：中间项为 Markdown；最终项为经过 `ContentPackageV1` 校验的 JSON。最终包包含 `platform / audience / objective / schedule[] / sources[] / assumptions[]`，每个 schedule 项包含发布时间、顺序、类型、标题、hook、正文或脚本、CTA、素材建议和来源引用。

### 10.3 调度与 API

- worker 每 2 秒扫描；每 workspace 最多 2 个 running Run；租约 30 秒、10 秒续租。
- 重启回收过期租约：原 Run 失败；常规自动执行最多 attempt 2，仍失败则计划阻塞。老板对阻塞任务调用 resume 后可建立下一 attempt。
- 只有全部前置任务完成才创建后继 queued Run；全部子任务完成后根任务与计划自动完成。
- RunStep 工具事件写入后立即提交事务，避免 SQLite 长写事务阻塞独立心跳与 MCP 请求。

| 方法 | 路径 | 语义 |
|---|---|---|
| POST | `/api/briefs/{id}/launch` | 原子确认/建计划/任务/依赖/首批 Run；重复或并发调用返回唯一计划 |
| POST | `/api/briefs/{id}/confirm` | 委托同一 launch service，兼容返回 `BriefOut` |
| GET | `/api/task-plans/{id}` | 计划、任务、依赖、Run 摘要、审批、产出和阻塞原因快照 |
| GET | `/api/tasks/{id}/runs` | 任务的全部 attempt、RunStep 和审批轨迹 |
| POST | `/api/tasks/{id}/resume` | body `{"message":"..."}`；仅阻塞的非根计划任务且无活跃 Run 时恢复 |

### 10.4 公司工具边界

`/mcp/company-tools/` 是 MCP streamable HTTP 端点。每 Run 的签名 token 绑定 workspace/plan/task/run/agent 与过期时间。任务交付工具为 `search_company_knowledge / report_progress / submit_output / create_subtask / request_support / block_task`，公司认知工具为 `list_colleagues / search_company_memory / ping_colleague / send_internal_message / propose_internal_task / record_observation / report_relationship_fact`；它们只调用服务层，Hermes 不持有数据库凭证，也不能改 brief 的目标、范围或成功标准。

## 11. TD-10 受控业务动作【已实现】

### 11.1 持久表

`agent_credentials`：`id / workspace_id / agent_id / credential_name / encrypted_value / created_at / updated_at`，`(agent_id, credential_name)` 唯一。只允许服务端解密；密钥轮换后需重新录入旧凭证。

`business_tool_policies`：`id / workspace_id / agent_id / tool_name / active / created_by / created_at / updated_at / revoked_at`，`(workspace_id, agent_id, tool_name)` 唯一。仅 `risk_gate=approval` 可命中；`prohibited_auto` 禁止长期放行。

`business_actions`：关联 workspace/run/task/conversation/agent，保存 capability/tool、规范化 `arguments_json` 及 hash、dedupe key、approval、provider/external id、结果、错误、attempt、30 秒租约和时间戳。状态固定为 `pending_approval / approved / executing / succeeded / rejected / expired / failed`。活动或成功 dedupe key 使用 partial unique index；任务 scope 为 task，聊天 scope 为 run。

### 11.2 动态 MCP 与执行

- `/mcp/business-tools/` 使用独立短期 token，绑定 workspace/conversation/run/agent/可选 task。RunService 只给具备已启用 `business_tool` 能力的 Run 动态追加该 MCP；聊天 Run 不获得 company tools，任务 Run 同时获得两者。
- 工具固定为 `send_email / publish_social_content / process_refund / update_ad_bid / submit_payroll / execute_payment`。v1 只有 `send_email` 有真实 provider；其余工具在落审批前明确失败，绝不返回假成功。
- BusinessActionWorker 每秒扫描 approved/过期租约动作，最多执行两次。Resend 请求使用永久稳定的 `Idempotency-Key: business-action/<id>`；成功 external id 永久保存，服务重启不会重复领取成功动作。

### 11.3 邮件与 API

`send_email(to[], subject, body, channel_id?, reply_to?)` 只支持纯文本、1-50 个合法收件人。邮件渠道必须 active、`provider=resend`、绑定发送员工，并配置 `from_address/from_name`；省略 channel_id 时必须恰有一个匹配渠道。

| 方法 | 路径 | 语义 |
|---|---|---|
| GET | `/api/business-tools` | 工具、能力、风险、所需凭证和 provider 实现状态 |
| GET | `/api/business-actions` | 按 agent/task/run/status 查询当前 workspace 动作审计 |
| GET | `/api/conversations/{id}/approvals?status=pending` | 聊天 Run 刷新后的审批恢复 |
| GET/DELETE | `/api/agents/{id}/business-tool-policies[/{tool}]` | 查看/撤销员工级长期放行 |
| POST/DELETE | `/api/agents/{id}/credentials[/{name}]` | 加密保存/撤销凭证并刷新依赖能力 |
| POST | `/api/approvals/{id}/resolve` | business_tool 一次/长期批准或拒绝；API 不调用 provider |

## 12. TD-13 公司世界模型与认知上下文【已实现基础闭环】

### 12.1 持久表

- `knowledge_sources` 的 `origin`（`manual|obsidian`）、`source_ref`（受控相对路径）和 `source_hash` 用于资料来源审计；Obsidian 原文不离开用户明确选择并同步的 managed 区域，更新不会覆盖本机 Vault。
- `company_events`：workspace 隔离的不可变事实账本，覆盖消息、任务事件、产出、知识、决策和外部结果；`(workspace_id, event_type, source_id)` 幂等。
- `agent_memories`：每员工的 `observation / episode / reflection / relationship / lesson` 投影；私有记忆只有所属员工可检索，共享记忆必须通过 `promoted` 标记。
- `memory_links`：长期记忆到证据事件的多对多链接；没有证据的记忆不得晋升为共享事实。
- `agent_relationships`：员工之间的合作摘要、可信度、互动次数和证据事件。
- `context_manifests`：每次上下文构建的检索焦点、事件/记忆 ID、prompt hash、预算和可选 Run 关联。

### 12.2 上下文与内部协作 API

| 方法 | 路径 | 语义 |
|---|---|---|
| GET | `/api/memory/search?q=...&agent_id=...` | 按 workspace 检索共享事实与指定员工可见记忆 |
| GET | `/api/agents/{id}/memory` | 员工记忆与证据事件 ID |
| GET | `/api/agents/{id}/relationships` | 同事关系记忆 |
| GET | `/api/runs/{id}/context` | Run 使用的 Context Manifest |
| POST | `/api/agents/{id}/reflect` | 触发技能反思和证据记忆反思 |
| POST | `/api/agents/{id}/ping` | 创建受限的员工间持久 DM |
| GET | `/api/company-events` | 查看当前 workspace 的公司事件账本 |
| POST | `/api/company-events/rebuild-index` | 回填历史消息、任务、产出和资料事件 |
| POST | `/api/knowledge-sources/obsidian-sync` | 接收桌面端 `.agentpulse/managed` Markdown 索引并按路径幂等更新 |

`/mcp/company-tools/` 另外提供 `list_colleagues / search_company_memory /
ping_colleague / send_internal_message / propose_internal_task /
record_observation / report_relationship_fact`。MCP 参数使用同事姓名和岗位语义，服务端再解析到内部员工记录；所有调用仍必须绑定任务 Run 和现有计划调整上限。
