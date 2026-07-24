# TD-14R-P1：开箱即用的本机 Hermes 闭环

## 验收合同

用户在没有预装 Python、Hermes 或本机服务的 macOS Apple Silicon / Windows x64 电脑上安装 AgentPulse，登录并配置 DeepSeek Key 后，可以授权一个项目目录，让小秘真实读取、修改文件、运行命令和操作电脑。读取在授权范围内自动执行；写入、命令、浏览器登录和 computer use 必须等待数据库持久审批。每一步都要有 RunStep 和 `execution_receipt`，没有回执不得显示成功。

当前开发版只验证了设备/项目/只读队列契约，仍会回退到开发机 PATH Hermes，因此不满足上述合同，也不得作为产品验收。

## 设计

1. 安装包内置 Hermes v0.18.2（固定 `e4ea0a0ed7fc24761b2b425146893561a73216e1`）、Python 3.11、本机执行所需的 ACP/MCP、browser、computer-use 和平台辅助程序。生产环境只接受资源目录内的 runtime；PATH fallback 仅限开发模式。
2. Electron 主进程管理 safeStorage、目录选择和系统权限；独立 Local Worker sidecar 管理设备心跳、profile 同步、Run lease、Hermes ACP、审批等待和事件回传。用户不需要看见或手动启动该 Worker。
3. API 是公司事实、调度和审批控制面。它只保存项目 ID、显示名、路径哈希和授权范围；绝对路径只保留在桌面端。桌面端提交消息前将路径转换成 `project://<id>/...`，API 入库时再次清洗。
4. API 返回无密钥的员工 runtime manifest（SOUL、职责、技能、记忆版本和能力）；Worker 在 AppData 的隔离 Hermes Home 写入本机 profile。DeepSeek Key 继续由 API Fernet 加密保存，按 device+run 短期密封给 Worker，只在子进程内存环境中出现。
5. 本机执行使用 `hermes --profile <profile> acp`，通过 ACP `new_session` 动态注入 `/mcp/company-tools`。禁止生产路径使用 `-z/--safe-mode`，因为它无法承载持久审批和动态 MCP。
6. 本机文件、终端和 computer-use 调用必须通过 realpath/符号链接/项目范围校验、风险判断、数据库审批、输出截断和执行回执。macOS Accessibility 或 Windows UI Automation 不可用时必须阻塞并说明原因。

## 里程碑

| 阶段 | 可测试结果 | 不得宣称完成前提 |
|---|---|---|
| M0 | 文档、数据/API 契约和验收边界已提交 | 当前开发版仍不可作为产品验收 |
| M1 | 干净环境中安装包能启动其内置 Hermes runtime | 不依赖系统 PATH、Python 或手工 Hermes 安装 |
| M2 | 登录/Key 后四人 profile 自动同步，本机 Hermes 真对话 | Key 不落 profile、日志或 renderer |
| M3 | 小秘真实读项目、公司 MCP、写入/命令/computer-use 审批 | 回执、拒绝不执行、路径隔离和重启恢复均通过 |
| M4 | macOS/Windows 包与 Release 资产可在干净环境安装 | 两个平台 runtime/包内检查/端到端证据齐全 |

## 关键不变量

- 云端不接收或保存本机绝对路径。
- 生产包不能回退到系统 Hermes；缺 runtime 是安装失败，不是静默降级。
- 本机 Run 同时绑定 workspace、device、agent、run 和 local_project。
- 无真实最终产出、未解决审批或未落回执的 Run 不能完成。
- 小秘缺少本机条件时只能报告阻塞，不能虚构读取、招人或建任务。
