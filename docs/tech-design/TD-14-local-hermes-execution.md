# TD-14R：真实能力执行、本机 Hermes 与 DeerFlow 能力增强

## 当前切片

1. DeepSeek OpenAI tool call 和 DSML 进入统一 fail-closed 解析器。
2. 工具只有进入真实 handler 后才生成 `execution_receipts`；失败结果使用确定性文案。
3. 聊天中检测到本机路径/项目请求时，没有在线 Worker 或授权项目就阻塞，不自动招聘替代员工。
4. 本机请求可以创建 `execution_target=local_desktop` 的持久 queued Run，绑定 `local_devices` 和 `local_projects`。
5. Electron 主进程负责设备注册、心跳、项目选择、只读 Run 领取和 Hermes 子进程结果回传；Token 与本机绝对路径不进入 renderer 或 API。
6. API 提供设备、项目、Run lease、事件、执行回执和运行时能力状态接口。

## 未完成边界

- 安装包还需要内置固定 Hermes runtime 和每个员工的本机 profile/模型供给。
- 只读 Worker 当前使用 Hermes one-shot safe mode；写文件、命令、浏览器和 computer_use 需要接入真正的本机审批回调后才能开放。
- 运行时能力 UI 目前先提供状态契约和项目授权入口，完整 Run 轨迹面板与跨重启 E2E 仍待补齐。

## 关键不变量

- 云端不接收本机绝对路径。
- 项目访问必须同时满足 workspace、device、local_project 和本机根目录范围校验。
- 工具解析失败、未知工具、参数不合法和 handler 异常都不能产生成功回执。
- 没有最终产出时，Worker 不能完成 Run。
- 原始聊天/事件不被摘要覆盖；后续上下文检索通过 TD-13 的公司事件账本接入。
