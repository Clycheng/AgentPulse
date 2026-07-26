# 0017. 资源感知的百人员工调度

- 状态: 已接受
- 日期: 2026-07-26
- 决策者: AgentPulse 项目所有者
- 取代: ADR 0010 的“每 workspace 固定并发 2”和“最多两次调整”首版限制

## 背景

ADR 0010 建立了数据库持久计划、依赖、Run 租约和每 Run 动态 MCP，但首版 worker 把每个 workspace 并发固定为 2，本机 Worker 也只有一个 busy 标志。定向 ping 只留下消息，不会唤醒员工评估；员工卡状态是静态展示。这些限制无法表达 30-100 名员工并行工作、同事临时确认、资源竞争、安全抢占和失败分支隔离。

## 决策

1. 继续以已确认 brief、`task_plans/tasks/task_dependencies/runs` 和动态 MCP 为执行合同；Hermes 仍是所有员工回复、评估和协调的唯一运行时。
2. 定向沟通先落 `work_requests` 并立即返回系统送达回执。目标员工的 Hermes triage Run 在安全点处理，决定为 `answered/accepted/deferred/rejected/needs_info`；只有持续工作才转 Task。
3. 每员工可有多个任务，但同一时刻只有一个 `leased/running/pausing` 前台 Run。跨员工并行度由服务端、本机设备、模型、浏览器、终端、项目写入和 `computer_use` 的资源容量决定，不再按 workspace 固定为 2。
4. 任务使用 Fibonacci `1/2/3/5/8/13` Story Point。排序综合业务价值、紧急度、解锁人数、风险降低和老化，再除以工作量并扣上下文切换成本；请求人身份不参与。自动抢占要求新分数至少为当前 1.5 倍且绝对高 2 分、当前无原子工具、每小时少于两次。
5. 硬产出、信息、评审三类依赖存 `task_dependencies`；资源依赖存 `task_resource_requirements/resource_leases`。单分支失败只使计划 `degraded`，不停止独立分支。
6. PostgreSQL 原子领取使用行锁、`SKIP LOCKED` 和事务级 advisory lock；后者按员工、容量池与资源键防止多调度器竞态。SQLite 保留确定性事务实现。`runs.status` 是唯一运行状态真相源，`runtime_status` 仅在一个兼容版本内由它派生。
7. 抢占通过 ACP `cancel/load_session/resume_session` 保存并恢复同一 `hermes_session_id`。本机 Worker 批量 claim、按资源并行；`computer_use` 独占，写任务使用独立 Git worktree。
8. 排序争议交给与请求双方独立的 Hermes coordination Run。协调者只依据已确认目标和证据裁决；只有目标本身不清楚时才通知老板补充目标。

## 理由

逻辑员工数、物理执行槽和独占设备不是同一维度。数据库原子领取和资源租约可以在 100 名员工、多个 API worker 与多台设备之间保持不重复执行；每员工单前台槽保留人类式上下文连续性。WorkRequest 将“问一句”和“创建新任务”分开，也让已送达、已评估与已完成不再混为一谈。

## 后果

调度策略归协作编排层，runtime 只执行 Hermes ACP、Run 生命周期和设备适配。数据库采用向前迁移并保留旧数据；旧中文任务状态和 `runtime_status` 在兼容期双读。服务端容量、模型额度和设备资源必须显式配置和观测。真实 Hermes 多员工抢占、macOS/Windows 安装包和 1-30 本机槽真机压测仍是发布门，不能由 fake backend 单测替代。
