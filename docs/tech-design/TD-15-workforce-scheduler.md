# TD-15 百人 AI 公司调度系统

关联：[ADR 0017](../decisions/0017-resource-aware-workforce-scheduler.md)

## 技术设计

### 1. 不变量

- 先讨论并确认 brief，才允许创建持续工作 Task；WorkRequest 的快速回答不建 Task。
- 每个员工最多一个前台 Run；等待审批/信息/同事的 Run 不占执行槽，但保留会话与检查点。
- 所有真实员工回复、请求决定和争议协调均由绑定员工 profile 的 Hermes ACP Run 产生。
- `runs.status` 为真相源。兼容字段只能从它派生，禁止独立更新出另一套状态。

### 2. 调度路径

`Scheduler.tick` 调用 `orchestration/workforce.py` 把待处理 WorkRequest、CoordinationCase 和就绪 Task 入队，再按资源容量、员工单前台约束、优先级和老化领取候选；runtime 只物化专用 Run 并执行 Hermes ACP。PostgreSQL 使用 `FOR UPDATE ... SKIP LOCKED`，并以事务级 advisory lock 串行员工槽、服务端容量和资源键；领取成功后才持有 Run lease 与任务资源 lease。

优先级公式：

```text
(3*business_value + 2*urgency + 2*unblock_score + risk_reduction + age_bonus)
/ story_points - switching_cost
```

`age_bonus` 每天 0.25、最多 3。自动抢占同时满足 `incoming >= current*1.5`、`incoming >= current+2`、可抢占、无原子工具、小时窗口少于 2 次。

### 3. 状态

- 员工：`offline / available / triaging / focused / waiting / blocked / degraded`
- 请求：`delivered / acknowledged / evaluating / answered / accepted / deferred / rejected / needs_info / withdrawn`
- 任务：`queued / ready / in_progress / waiting_dependency / waiting_information / waiting_review / waiting_approval / waiting_resource / paused / completed / cancelled`
- Run：`queued / leased / running / pausing / paused / waiting_approval / waiting_information / waiting_colleague / completed / failed / cancelled / timed_out`
- 计划：`launching / active / degraded / blocked / completed / cancelled`

### 4. 数据与接口

新增 `work_requests / priority_assessments / agent_work_states / resource_leases / task_resource_requirements / task_reviews / coordination_cases`。扩展 Task、Dependency、Plan、Run 的状态、评分、检查点、session 和资源字段。硬产出依赖必须存在匹配交付类型的持久 `task_output`。迁移 `15a100c0f001` 仅向前增加并回填，不删除历史数据，并将兼容 `runtime_status` 从 canonical `runs.status` 重新派生。

主要接口：

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/work-requests` | 创建请求并返回 `delivered` |
| GET | `/api/agents/{id}/workbench` | 当前工作、请求、队列、暂停项、依赖和资源 |
| POST | `/api/work-requests/{id}/decision` | 仅对应 triage Hermes Run 决策 |
| POST | `/api/work-requests/{id}/disputes` | 提交独立协调 |
| POST | `/api/coordination-cases/{id}/decision` | 仅对应 coordination Hermes Run 裁决 |
| POST | `/api/tasks/{id}/dependencies` | 增加产出/信息/评审/资源依赖 |
| POST | `/api/tasks/{id}/reviews` | 创建独立评审任务 |
| POST | `/api/runs/{id}/pause`、`resume` | 安全控制与调度恢复 |
| POST | `/api/local-devices/{id}/runs/claim` | 按资源原子批量领取 |
| GET | `/api/workforce/events` | workspace workforce SSE |

### 5. 本机执行

Electron 使用资源感知进程池。写授权任务先创建独立 Git worktree；非 Git 项目拒绝并发写。sidecar 在安全事件且无 `started` execution receipt 时接收 pause，调用 ACP cancel；`pause-complete` 保存 checkpoint、释放 lease。恢复时用原 `hermes_session_id` 调 `load_session/resume_session`。

## Tech-Tasks

| 编号 | 工作 | 验收 |
|---|---|---|
| TD-15-T1 | 向前 schema、状态与兼容回填 | SQLite/Alembic upgrade 通过，旧数据不删 |
| TD-15-T2 | WorkRequest、评分、抢占和独立协调 | Hermes 专用 Run + MCP 权限测试 |
| TD-15-T3 | 类型依赖、评审与资源 Broker | 环检测、退回、独占租约和 degraded 分支测试 |
| TD-15-T4 | Scheduler V2 与 session 暂停恢复 | 不重复、同员工不重入、租约恢复、100×10 压测 |
| TD-15-T5 | 本机批量 claim、进程池和 worktree | 多终端并行、computer_use 独占、安全暂停 |
| TD-15-T6 | 公司总览、员工工作台和事件流 | 桌面 build、桌面截图和窄窗口布局无溢出 |
| TD-15-T7 | 真实运行时发布验收 | 真 Hermes 同事请求抢占恢复、macOS/Windows、1-30 槽压测 |

T1-T6 的代码和自动测试已实现，桌面 1440x900 真图与几何边界已通过。2026-07-26 已完成 PostgreSQL 老库连续冷启动、真实 Hermes WorkRequest `delivered → answered`、Electron Worker 重启换代和本机只读 `README.md` Run/Receipt/最终消息 E2E。当前 in-app Browser 不提供视口切换；窄窗口、请求安全抢占后恢复、Windows 与 1-30 真执行槽仍随 T7 安装包真机补证，完成前看板保持进行中。
