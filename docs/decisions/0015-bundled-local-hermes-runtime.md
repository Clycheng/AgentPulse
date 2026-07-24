# 0015. 桌面安装包内置 Hermes runtime 与 Local Worker

- 状态: 已接受
- 日期: 2026-07-25
- 决策者: AgentPulse 项目所有者

## 背景

本机文件、终端和电脑控制必须在用户电脑运行，云端 API 无法安全替代。此前 Electron 开发切片在内置 runtime 缺失时回退到系统 `hermes` PATH，并使用 one-shot safe mode；用户即使安装了 AgentPulse 仍要安装或启动 Hermes，且无法获得动态 MCP 和持久审批。这不符合桌面产品的开箱即用承诺。

## 决策

macOS Apple Silicon 和 Windows x64 安装包各自携带固定 Hermes v0.18.2、Python 3.11 和本机执行依赖。Electron 启动不可见的 Local Worker sidecar；主进程只管理登录态、safeStorage、目录授权、系统权限和 UI。生产环境只执行包内 runtime，开发模式才允许 `AGENTPULSE_HERMES_BIN` 覆盖。

Worker 用 ACP 驱动每个员工的本机 Hermes profile，按 Run 注入动态公司 MCP，并通过 API 的持久 Run/审批/回执接口恢复执行。API 继续加密保存 DeepSeek Key；Key 只以 device+run 短期运行会话供给 Worker，在 Hermes 子进程内存中使用，不写 profile 或普通磁盘。

## 理由

包内 runtime 才能让用户不安装 Python/Hermes，同时固定依赖和安全审计边界。ACP 是 Hermes v0.18 的实际编程接口，支持动态 MCP 与审批回调；one-shot safe mode 不具备这些语义。将文件根目录和设备 token 留在桌面端，能避免云端获得用户绝对路径或任意本机访问权。

## 后果

安装包体积和构建时间增加，需要针对 macOS/Windows 单独构建、校验和冒烟测试。macOS Accessibility、Windows UI Automation 等系统权限仍需用户授权，应用不得绕过。当前 PATH Hermes 开发切片在完整 runtime/profile/ACP/审批验收前不能被称为开箱即用产品。
