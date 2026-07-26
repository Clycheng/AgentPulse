# 0016. Hermes 是唯一员工与控制执行入口

- 状态: 已接受
- 日期: 2026-07-25
- 决策者: AgentPulse 项目所有者

## 背景

历史实现曾让 API 直接调用 DeepSeek 的聊天接口和 function loop。它绕过了员工 profile、动态 MCP、审批、Run 生命周期和 execution receipt，导致界面能显示模型编造的“已完成”，而实际没有动作。本机 Worker 首次接入后又暴露出 ACP 最终消息跨进程丢失的问题。

## 决策

所有员工发言、群讨论主持、团队草稿和任务执行一律创建或接管 Hermes ACP Run。API 只负责事实、调度、审批、上下文、MCP 和持久化；Electron Local Worker 只在用户电脑上执行绑定项目范围内的 Hermes ACP Run。

DeepSeek 可以继续作为 Hermes 配置的首个模型供应商，但只能由 Hermes 子进程使用短期运行凭证访问。AgentPulse API 不保留任何直接 `chat/completions` 员工回复或函数循环入口；旧 `/api/runs/llm-chat` 固定返回 410。无 Hermes profile、Worker、项目授权或真实回执时，系统必须阻塞或失败，不能降级为直接模型回复。

## 理由

单一运行时才能保证人格、技能、记忆、工具表面、审批和审计一致。把模型供应商限制在 Hermes 内部，也允许未来将 DeepSeek 换成其他远程或本地模型，而不会改变 AgentPulse 的产品行为。

## 后果

Hermes 或本机 Worker 未就绪时，功能会诚实不可用，不能以“先回复一句”掩盖缺口。开发环境默认也使用固定 `runtime-stage`，而不是系统 PATH；只有明确的 `AGENTPULSE_HERMES_BIN` / Python 覆盖可用于运行时开发。TD-14R-P1 的完整安装包、写入/命令审批和双平台验收仍需继续完成。
