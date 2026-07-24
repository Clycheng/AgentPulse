# 架构决策记录 (ADR)

本目录记录 AgentPulse 的**架构/方向级决策**。每条决策一个文件，让任何接手的 AI / 开发者能看清"为什么是现在这样"，避免推倒重来或跑偏。

## 什么时候写 ADR
- 选定或更换基座 / 框架 / 核心依赖
- 确定或改变系统分层、模块边界
- 确定产品方向上的重大取舍
- 推翻之前的某条 ADR（新开一条，标注 supersedes）

一般性代码改动**不**写 ADR，写进 [../../CHANGELOG.md](../../CHANGELOG.md)。

## 格式（复制此模板）
```markdown
# NNNN. 标题

- 状态: 已接受 | 已废弃 | 被 NNNN 取代
- 日期: YYYY-MM-DD
- 决策者: <谁拍的板>

## 背景
（要解决什么问题、有哪些约束）

## 决策
（决定做什么，明确、可执行）

## 理由
（为什么这么选；关键调研结论/权衡）

## 后果
（好处、代价、风险、后续要做的事）
```

## 编号规则
四位递增 `0001`、`0002`……文件名 `NNNN-简短标题.md`。已接受的 ADR 不改动（要变就新开一条取代它）。

## 索引
- [0001](0001-hermes-as-agent-runtime.md) — 以 Hermes 为员工运行时基座（不用 Codex/Claude CLI）
- [0002](0002-self-built-group-discussion.md) — 自研群讨论协作层（照 AutoGen 骨架）
- [0003](0003-server-side-24x7-idea-center.md) — 服务端 7×24 部署 + 无 idle + idea 中心
- [0004](0004-multimodal-via-hermes.md) — 多模态经 Hermes 辅助模型处理（DeepSeek 文本主模型）
- [0005](0005-hermes-poc-safety-findings.md) — Hermes 地基验证发现：workdir 隔离是硬需求，讨论对齐门不能只靠人格指令
- [0006](0006-group-discussion-v1-first-slice.md) — 群讨论协议 v1 第一片：讨论态 + 共识 brief + Task 创建门（对齐用人工确认），含待敲定清单
- [0007](0007-hermes-v0.18-interface-acp.md) — Hermes v0.18 集成接口：REST /v1/runs 已不存在 → 用 ACP(stdio) 驱动执行、CLI 做供给（`LocalHermesProvisioner` 已实现）；作废 DATA-MODEL/TD-03 §5.3 的 REST 假设
- [0008](0008-human-in-the-loop-approval-model.md) — 统一的人类介入审批模型：技术危险动作全链路已实现（`approvals.mode: manual` + 多选项 + 挂起超时对齐）；业务受控工具门拆为独立 TD-10
- [0009](0009-natural-language-team-compiler.md) — 自然语言团队编译器：一段话描述团队 → 可编辑草稿 → 一次性建成真实员工 + 自动拉一个团队群；`provision_new_agent` 统一四条招聘路径的供给入口
- [0010](0010-durable-task-dispatch-and-company-tools.md) — 数据库持久任务调度 + 每 Run 动态 Hermes MCP 公司工具：一次确认后自动接力、重启恢复，Hermes 不直写业务库
- [0011](0011-controlled-business-actions.md) — 受控业务动作：业务密钥由 AgentPulse 托管，独立动态 MCP + 持久动作队列在审批后执行真实外部调用
- [0012](0012-cloud-hosted-desktop-distribution.md) — 云端托管薄桌面客户端 + workspace BYOK + 零固定成本安装包分发
- [0013](0013-company-world-model-and-cognitive-memory.md) — 公司事件账本 + 独立员工记忆 + 每 Run 上下文检索
- [0014](0014-local-hermes-and-runtime-guards.md) — 本机 Hermes 执行、设备/项目绑定与真实性运行时守卫
