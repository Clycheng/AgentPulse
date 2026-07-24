# 执行看板（唯一任务状态源 · AI 冷启动从这里知道"下一步干什么"）

> **任何 worker AI 开工流程**：读完 [README.md](README.md) 的"Worker AI 执行协议" → 在本文件挑「现在就做」里最靠前、且会话类型匹配的任务 → **开工第一个动作 = 把该任务状态改成 `🔵 进行中` 并 commit+push 本文件**（防止多 AI 撞车）→ 做完按协议验收/回填 → 状态改 `✅ 完成(commit)` 再 push。
> 任务状态**只在本文件标**（TD 文件里只有设计和验收标准，不标状态），避免两处漂移。

## ✅ 架构漂移已修复（2026-07-09）——TD-02-T5 完成，TD-03-T2/T3 解锁

`orchestration/discussion.py::run_discussion_round` 曾是死码（生产路由手写重复讨论循环 + `_llm_select_speaker`）。TD-02-T5 已把 `send_message`/`send_message_stream` 的讨论循环统一收回到 `run_discussion_round`（重写为 async 事件流），发言人选择唯一入口收敛到 `resolve_next_speaker`/`select_next_speaker`，路由层只注入"如何执行一轮 turn"和"如何调主持人 LLM"。三条禁止模式 grep 全干净，新增生产路径断言测试。**TD-03-T2 及以后现可开工。**

## 现在就做（按此顺序领）

| 序 | 任务 | 一句话 | 会话要求 | 状态 |
|---|---|---|---|---|
| 1 | TD-12 零成本云端部署与桌面分发闭环 | 官网下载 → 注册/BYOK → 云端 Hermes | **是**（真实 Hermes/Docker/桌面/线上） | 🔵 进行中（Codex，2026-07-24；代码/安全/镜像已验，待云资源与线上 E2E） |
| 2 | TD-13 公司世界模型与独立员工认知闭环 | 公司事件账本 → 记忆检索 → 独立同事协作 → 反思 | **是**（数据库/编排/Hermes/UI/E2E） | 🔵 进行中（Codex，2026-07-24；代码闭环/SQLite/API/UI/Obsidian 已验，真实 Hermes 四人认知 E2E 待执行） |
| 3 | TD-14R-P1 开箱即用的本机 Hermes 闭环 | 安装包内置 Hermes/Worker，项目读写/命令/电脑控制经真实审批和回执执行 | **是**（API/桌面/Hermes/真机） | 🔵 进行中（Codex，2026-07-25；M0 契约已开始。当前开发版不是开箱即用产品，禁止以系统 PATH Hermes 或 one-shot 路径作为验收） |
| 4 | TD-09-T3 剩余(渠道出站回复 + 微信/widget 适配器) | 渠道入站已通，出站未接 | 否（微信/widget 需真账号） | ⚪ |
| 5 | TD-08-T3 **剩余 UI 收尾**(空闲思考开关设置项，可选) | 前端 + IdleThinkService 均已完成✅ | 否（前端）| ⚪ |

## 有依赖，等前置完成后做

| 任务 | 等什么 | 会话要求 |
|---|---|---|
| TD-09-T3 **剩余**(ChannelReply 把回复发回原渠道 + 微信/widget 适配器) | **agentpulse**（需 Hermes 集成验证）|
| TD-09-T3(ChannelReply + 网页 Widget) | TD-09-T2✅ — deps met, ready to move | 否 |

## 已完成

| 任务 | commit | 备注 |
|---|---|---|
| **TD-10 受控业务工具门与真实邮件闭环**：独立 `/mcp/business-tools` + per-Run token；AgentPulse Fernet 加密凭证库；持久 `business_actions`/长期策略/审批轮询/租约重试；Resend 纯文本邮件 provider 与稳定幂等键；其余五类工具未配置时诚实失败；桌面端凭证、邮件渠道、审批恢复、动作审计和策略撤销。**真实 UI 验证**：录入/撤销假 key、刷新恢复审批、拒绝后 `attempt=0` 且 provider ID 为空；desktop/mobile 无溢出。338 passed / 9 skipped，desktop lint/build、三条架构 grep 全过；真实 Resend key 未提供，守护式真发信 E2E 跳过且本轮未发信 | `199f6a2` | [ADR 0011](../decisions/0011-controlled-business-actions.md) / [TD-10](TD-10-business-tool-gate.md)；下一步 TD-09 → TD-08 |
| **TD-11 自媒体 AI 公司自动执行闭环**：3-6 项严格分工 brief + 一次 launch + 数据库持久依赖调度/租约恢复 + 每 Run 动态 MCP 公司工具 + Markdown 接力 + `content_package_v1` 最终交付；默认四人内容团队和内容经营群；桌面端计划/Run/审批/内容包视图。**真机验证**：四人讨论小红书周计划，API 执行中重启后恢复，三项自动接力并全部完成；desktop/mobile 截图归档。325 passed / 8 skipped，desktop build、pip check、三条架构 grep 全过 | `129bf99` | [ADR 0010](../decisions/0010-durable-task-dispatch-and-company-tools.md) / [TD-11](TD-11-autonomous-content-execution.md)；下一步严格按 TD-10 → TD-09 → TD-08 |
| **讨论闭环核心修复（4 项断点）**：①**讨论→共识 brief 自动产出**——`run_discussion_round` 讨论轮结束（≥2 发言）自动跑收敛检查（TD-02-T3 起就是死码的 `check_convergence`/`build_convergence_prompt`/`build_brief_draft_prompt` 接入生产），已对齐则路由层 `create_brief` 落库（draft 去重）+ 流式补实现 `event: system` SSE，前端 BRIEF_CARD 实时上屏——此前 `POST /briefs` 只有测试调用，老板在 UI 里永远看不到共识卡片，"讨论→拍板→建任务"闭环第一次在生产真能跑；②**小秘优先 function_loop**——她有 ready Hermes profile 时旧路由直送 Hermes，拿不到只在 Bridge 上的系统工具，招人/建群/建任务只会嘴上答应，现在先走 Bridge、异常才回落 Hermes，顺手把静默 `except: pass` 改成 `logger.warning`；③`resolve_reply_agents` 删 `LIMIT 3`，12 人群全员可发言；④`extract_recruit_intent` 只在小秘私聊生效，群聊不再被正则截胡建纸片员工。**测试**：+9 例全过，全套 298 passed / 8 skipped / 1 xpassed；2 个预存失败已 stash 复现确认是 `.env` 真 key 网络泄漏，与本次无关。三条层界 grep 干净 | `ea53359` | 已知边界：aligned 后老路径逐成员各回一条（12 人群 12 条回复）；brief 确认后自动拆任务/派发编排留后续 |
| **自然语言团队编译器**（[ADR 0009](../decisions/0009-natural-language-team-compiler.md)）：产品北极星③"自然语言捏 agent"第一版——老板一段话描述团队 → `POST /agents/draft-team` 生成可编辑草稿（复用 TD-04-T3 一直未接生产入口的 `draft_role_spec`）→ 老板编辑姓名/部门/职责/能力 key → `POST /agents/create-team` 一次性真建员工 + 自动拉一个团队群。`app/services/workspace.py::provision_new_agent` 从模板招聘专属逻辑泛化成统一供给入口，人才市场招聘/秘书 bootstrap/小秘 `create_employee` 工具/团队编译器四条路径现在共用一个函数。小秘工具新增 `list_capabilities` + `create_employee` 的 `responsibilities`/`capability_keys` 参数，招人不再是纸片员工。桌面端新增 `TeamCompilerModal`（复用"+授予能力"的能力选择器模式），中英 i18n 齐。四点产品边界（不规划多群/不编业务技能/不加校验器/不做试运行）由项目所有者拍板否决记入 ADR。**真机验证**：真实场景描述（居家养老+抖音号）生成 3 人草稿→真建 3 员工+1 群，`hermes profile list` 可见真 profile；`curl` 单独验证单人创建的 spec/capability 落库。288 通过、tsc 无错 | 2026-07-20(见 CHANGELOG) | 3 个预存失败与本次无关（Python 3.14 async mock 兼容性 + function_loop 真调用泄漏，已登记独立跟进） |
| **人才市场招聘接真供给**：`recruit_from_template` 之前只 `create_agent`，从不建 `agent_specs`/供给——产品主推的招聘入口招来的全是纸片人，只有"创建员工"手动勾能力芯片才真供给。新增 `capability_catalog.split_by_credentials` 解决 `provision()` 全有或全无的坑（一个 bundle 混了缺凭证的能力会让整个员工卡住，即使其他能力本可以立刻工作）；补齐 4 个官方模板缺失的 `ROLE_BUNDLES` 映射。**真机验证**：真招"内容主笔"/"运营负责人"，profile 真建出来，混合 bundle 正确保留可用能力、只把缺凭证的那部分标记待补。280 通过 | 2026-07-19(见 CHANGELOG) | 测试踩过真建 5 个孤儿 Hermes profile 的坑，已清理+改成显式 mock provisioner |
| **service-claw-cloud 借鉴（3 项小改）**：见 [docs/research/service-claw-cloud.md](../research/service-claw-cloud.md)——①启动冒烟检查（`hermes_provisioning=true` 但找不到 `hermes` 二进制时 fail loud 拒绝启动）②`RunOut.waiting_on`（挂起的 run 直接显示"等老板批准：{描述}"，不用点进 steps）③`/me/bootstrap.anomaly_count_24h`（过去 24h 失败 run + 超时审批计数，不算 rejected，桌面端 logo 小红点）。TD-10 也顺带把 MCP 接入方式改成了更好的"每次 `new_session` 动态传"（推翻了写死进 profile config.yaml 的初版设想）。全套 277 通过、tsc 无错 | 2026-07-19(`c421dfd` `bd00965`) | 心跳/机器注册表、命令队列跨进程改造只记录方向，未拆 TD |
| **审批门分片④⑤⑦ + 秘书默认能力**（[ADR 0008](../decisions/0008-human-in-the-loop-approval-model.md)）：④挂起超时对齐——实测 Hermes ACP 路径的 60s 超时硬编码在 `acp_adapter/permissions.py`，`approvals.timeout` 配置对该路径无效，`approval_bridge.await_decision` 改用 `asyncio.wait_for`(50s，可配) 抢先收敛，超时标 `approvals.status='expired'`（不再永远 pending）；⑦删 clarification/capability 的 agent 自触发伪装，SOUL 不再指示调用不存在的 `clarify` 工具，新增老板发起的"+ 授予能力"真路径(`GET /api/capabilities` + `POST /agents/{id}/capabilities`)，且这条路径现在对**没有任何 profile 的员工**（默认秘书、Talent Market 招的员工）也能用——会从零 bootstrap 一个真 spec+profile，而不是要求员工已经是真 Hermes 员工。顺带：默认秘书在 `hermes_provisioning` 打开时现在直接带 5 个免凭证默认能力（write_code/run_tests/task_delegation/content_writing/data_analysis）上岗，不用老板手动补。**真机验证**：真 Hermes 员工 `rm -rf` 触发审批→批准/拒绝/超时三条路径都测过；`+授予能力`对全新 profile-less 员工真的建出了 profile（`hermes profile list` 可见）；全量单测 272 通过 | 2026-07-16/17(`123d8a2` `228ee2e` `8ca69f3` `a282843`) | 分片⑥（业务受控工具门）拆出独立 TD-10，设计文档已完成待实现 |
| **审批门真强制 分片1-3（ADR 0008，技术危险动作）**：①provisioner 设 `approvals.mode: manual` ②`hermes_client` 修关键 bug——批准原返回 `SelectedPermissionOutcome` 被 Hermes 当 deny,改 `AllowedOutcome`/`DeniedOutcome` + 按 option_id 选 ③桥/端点/前端加 `scope` + 「允许一次/永远允许/拒绝」。**真机验证**(provisioner 供给+HermesBackend 执行,非 seed):真 agent `rm -rf` → 拒绝目录保留、批准目录真删,request_permission 各 1 次全 PASS。250 通过、tsc 无错 | 2026-07-14(见 CHANGELOG) | 修好 T4 根本缺口;北极星「老板拍板制」技术类**真强制**;剩业务工具门/超时对齐/求援伪装清理 |
| ⚠️ **聊天内审批/求援/能力升级卡片**（代码真、**真运行时触发不了**，见 2026-07-14 审计）：SSE `approval` 事件（带 category+approval_id+tool_call）→ 前端插一条 `APPROVAL_CARD:` 系统消息；新增 `ApprovalCard` 组件按类型渲染——高风险(批准/驳回)、澄清(文本框+提交回复→`/answer`)、能力升级(可改能力 key+批准并升级→`/resolve` 带 approved_capability_key)；`resolveChatApproval`/`answerChatClarification` 经 App→ChatPanel→MessageItem 传入；点后卡片原地翻转「已批准/已回复」。styles.css 加 `.approval-card`/三类配色。**浏览器实测**：3 类卡片渲染正确、点「批准」→run 关联 /resolve 翻转、点「批准并升级」→execute_upgrade 真装 `web_scraping`→`agent_capabilities` 落 `enabled`、无 console 报错 | 2026-07-14(见 CHANGELOG) | 至此「讨论→拍板→执行→挂起→续跑」在 UI 上真正可操作可演示 |
| **TD-06-T3 成长轨迹前端（员工档案）**：`AgentDetail` 抽屉新增「成长轨迹」区——已获得能力（拉 `GET /agents/{id}/spec` 的 capabilities，`已启用`/`待补凭证`/`已停用` 徽章配色）+ 已习得技能（拉 `GET /agents/{id}/skills`，卡片显示 SKILL.md 标题+摘要）+「触发反思」按钮（调 `POST /reflect`，内联状态/错误）。styles.css 加 `.growth-*`/`.cap-badge`/`.skill-card`（复用 teal token）。**浏览器实测**：起 API(8001)+renderer(5175，`.env.local` 指向)→登录→员工→阿伦→成长轨迹正确渲染（social_content 已启用 + email_sending 待补凭证 + 技能空状态），tsc 无错、无 console 报错、截图留证 | 2026-07-14(见 CHANGELOG) | 纯前端；让 TD-06-T1/T2 的自进化成果「可见」；聊天内审批/求援卡片另立一项 |
| **TD-06-T2 主动能力升级申请**：`runtime/upgrade.py::execute_upgrade`——老板批准能力升级审批→`resolve_bundle` 校验 key→`ProfileProvisioner.add_capability`(装 toolsets/skills+reload) 装到员工 profile→upsert `agent_capabilities`(有 required_credentials→`credential_missing`，否则 `enabled`)；`_persist_run_approval` 加 `capability_upgrade` 分支(写 payload_json 带 suggested key)；`/approvals/{id}/resolve` 识别 capability_upgrade→批准先装能力再唤醒 run；`ResolveApprovalRequest` 加 `approved_capability_key`(老板可改)；SOUL 加"缺工具主动申请升级"指令。**顺带修 bug**：另一会话重写 resolve 时把 `approved/rejected` 直接传桥，但 hermes_client 只认 `allow_once/deny`→**批准被误判成拒绝**，已改为正确映射。**零回归**：新增 `test_upgrade.py` 6 例(装能力+enabled/credential_missing、未知 key/无 profile 报错、幂等 upsert、resolve 端点全链)；全套 **250 通过 + 7 skipped**。三条 grep 干净 | 2026-07-14(见 CHANGELOG) | 北极星④自进化第二半闭环；能力真装到 profile；成长轨迹前端=TD-06-T3 |
| ⚠️ **TD-03-T4 审批 suspend/resume + clarification**（机制真、**真运行时不触发**，2026-07-14 审计降级）：`approval_bridge` 进程内 Future + `make_bridge_resolver` 原地挂起 ACP→`/resolve`/`/answer` 唤醒续跑；但实测 Hermes ACP 不触发 `request_permission`→真 agent 产生不了审批,只有 seed 行能走。**审批门未在执行层真强制,已回到"现在就做🔴1"** | 2026-07-13(`d9e9fdd`) | 需真修:见🔴1 |
| **TD-01-T2 端到端手测**：4/5 步 API 级验证通过（brief 创建→拒绝→确认→建任务、门控拒绝），1 步 xfail（DeepSeek SOCKS 代理环境问题，不影响业务流程） | 2026-07-13 | 新增 `test_e2e_brief_lifecycle.py`（4 passed + 1 xfailed）；TD-01-T1/T1b 之前已实现；**brief 全链路已验证** |
| **TD-03-T5 员工↔profile 生命周期（自动供给）**：`build_provisioner_from_settings` 按 `hermes_provisioning` 选 LocalHermes/RecordOnly；`supply.provision` 走真 provisioner——建 profile+写 SOUL(角色/职责/铁律)+配 model(`deepseek/deepseek-v4-flash`)+toolsets+装 skills+写 DeepSeek key→回填 `hermes_profile`/status=ready；profile 名合法化(lowercase alnum)。**真机 e2e 过**：provision→真 Hermes profile(model+SOUL+key)可跑，spec ready。招人→真员工全自动 | 2026-07-10(见 CHANGELOG) | 配 `AGENTPULSE_HERMES_PROVISIONING=true` 开启 |
| **TD-03-T3 后半：热路径切换**：`send_message_stream` 的 DM + 群讨论两条路径都改成经 `runner.stream_agent_run` 调 Hermes（员工有 ready profile 时），否则回退临时 DeepSeek——**零回归**（现有 205 测试全过，无 profile 的 agent 走原路径）；`runner` 加 `resolve_hermes_profile` + 流式 `stream_agent_run`；approval_required 走 deny-by-default + SSE `approval` 事件。**真机 e2e 过**：DM 经 `/messages/stream` → runs(provider=hermes,completed)+run_steps(message,final)+agent 消息 "OK" 落库 | 2026-07-10(见 CHANGELOG) | 审批 suspend/resume 与自动供给留 T4/T5 |
| **TD-03-T3 写半：RunService**（`runtime/runner.py`）：`start_run` 消费 backend 事件流→按 TD-03-T1 生命周期建 run/转状态、聚合 thinking/message 各落 1 run_step、tool 逐条落、结果写回 agent message；**真机 e2e 过**（RunService→HermesBackend→真 Hermes→run_steps+message"OK"）。2 常开(fake backend)+1 guarded e2e；全套 205 过 | 2026-07-10(见 CHANGELOG) | 剩热路径切换+审批闭环 |
| **TD-03-T2 HermesBackend（ACP 传输）**：`runtime/hermes_client.py`——起 `hermes --profile <p> acp` 子进程、用 `agent-client-protocol==0.9.0` 走 ACP、把 session_update 流映射成 `AgentEvent`(message/thinking/tool_call/tool_result/approval_required/usage/final)；approval→选 offered option；fs 读写限制在 workdir 内(ADR 0005)。**真机 e2e 实测通过**：agentpulse profile 跑"reply OK"→收到 thinking 流+message"OK"+usage+final。3 e2e 单测(HERMES_E2E=1) + 2 常开安全测；全套 203 过 | 2026-07-10(见 CHANGELOG) | agent-client-protocol 入 requirements |
| **Hermes 接入地基**：本机 Hermes v0.18.2 验证——真实 DeepSeek key 经 isolated profile 一次性跑通("OK")；**发现 REST /v1/runs 已不存在**→ 新增 [ADR 0007](../decisions/0007-hermes-v0.18-interface-acp.md)(改用 ACP 传输、作废端口模型)；实现 **TD-04-T6 `LocalHermesProvisioner`**(真 CLI 建/配/删 profile，强制绝对 workdir)+ 2 always-on 安全单测 + 1 guarded e2e(HERMES_E2E=1 实测过) | 2026-07-10(见 CHANGELOG) | 201 测试全过；DeepSeek key 存 gitignored .env |
| Idea 中心前端（TD-08-T3 前端半）：桌面端新增「想法」视图（侧栏入口 + 摘要 + 分类过滤 + 想法卡片 + 接受/忽略/转为讨论）；转为讨论走 `/api/ideas/{id}/convert` 后自动重载并跳进新群。接 TD-08-T1 API | 2026-07-10(见 CHANGELOG) | 纯前端；tsc 无错、浏览器实测 seed 2 条→列出→转讨论跳转全走通、无 console 报错。idle 自动生成 idea 仍等 Hermes(TD-08-T2) |
| TD-07-T2 前端半（创建员工"按职位快速配置"）：CreateAgentModal 拉 `/api/role-bundles` 渲染 12 个角色芯片，点选自动填能力+名称+部门 + "已选 N 项能力"摘要；顺手把该弹窗残留的写死紫色能力芯片改成 teal token。**TD-07-T2 全部完成** | 2026-07-10(见 CHANGELOG) | 纯前端；tsc 无错、浏览器实测选角色自动配好、无 console 报错 |
| 渠道管理前端（TD-09-T3 前端半）：桌面端新增「渠道」视图（侧栏入口 + 创建表单 + 列表卡片 + webhook URL 复制 + 启停），接 TD-09-T2 的 `/api/channels`；tsc 通过、浏览器实测创建/列出/复制/停用全走通 | 2026-07-10(见 CHANGELOG) | 纯前端；tsc 无错、无 console 报错 |
| TD-07-T2 API 半：`GET /api/role-bundles`(列 12 预配角色+resolved 效果) + `POST /api/agents` 新增 `role_spec.role_bundle_key`(展开并入 capability_keys) | 2026-07-10(见 CHANGELOG) | 199 测试全过；只差"按职位选"前端 UI |
| TD-07-T1(业务能力目录扩展)：`capability_catalog.py` 补 31 个业务能力(客服/内容/数据/HR/法务/财务/项目) + `ROLE_BUNDLES`(12 预配角色) + `get_role_bundle`/`list_role_bundles`；对齐 social_content 与真相源；10 新单测(条目合法性/MCP 必带 creds/角色 bundle key 全在 catalog/解析不报错) | 2026-07-10(见 CHANGELOG) | 纯常量+单测；194 测试全过；TD-07-T2 解锁 |
| TD-09-T2(渠道管理 API + 公开 webhook 端点)：`services/channels.py`(CRUD/stats/HMAC 验签/token) + `routes/channels.py`(/api/channels 认证 CRUD + 软删) + `routes/webhooks.py`(公开 `/webhooks/{type}/{token}` 验签→route_inbound→尽力触发 agent 回复) + email 适配器；8 单测(CRUD/webhook 入站/去重/验签/未知或停用 token/不支持类型/mock LLM 触发回复) | 2026-07-10(见 CHANGELOG) | 纯 API，可 curl 验证；184 测试全过；TD-09-T3 解锁 |
| TD-09-T1(channel_configs 表 + Router 核心)：`channel_configs` 表 + `conversations.source_channel/external_conversation_id` + `messages.external_message_id`(双 schema+迁移)；`app/channels/`(router `route_inbound`/`find_or_create_conversation`/dedup + generic 适配器 + 注册表)；6 单测(归一化/dedup/按外部用户归会话/固定群路由/自定义路径) | 2026-07-10(见 CHANGELOG) | 纯数据+适配器；176 测试全过；TD-09-T2 解锁 |
| TD-08-T1(ideas 表 + API)：`ideas` 表 + `agent_specs` 3 列 + `conversations.idea_id`(双 schema+迁移)；`schemas/idea.py` + `services/ideas.py`(CRUD/review/convert/idle-thinking) + `routes/ideas.py`(GET/POST/review/convert + PATCH idle-thinking)；8 单测(CRUD/流转/CHECK/convert 建会话并回链 idea_id) | 2026-07-10(见 CHANGELOG) | 纯数据+API；170 测试全过；TD-08-T2 仍等 Hermes |
| TD-03-T1(Run/RunStep 数据模型)：runs 扩 task_id/hermes_profile_id/hermes_run_id/workdir，新增 run_steps 表，approvals 加 run_id+type，agents 加 hermes_gateway_port(两 schema 都改+ensure_column 迁移)；新增 `runtime/runs.py` 生命周期状态机(queued→running→waiting_user/clarify→completed/failed) + 13 单测 | 2026-07-09(见 CHANGELOG) | 纯 schema+lifecycle；162 测试全过；解锁 TD-03-T2 |
| TD-02-T5(路由归位)：send_message/stream 讨论循环统一收回 run_discussion_round(改为 async 事件流)，删除路由层 `_llm_select_speaker`/`_extract_mention_simple`/`_build_discussion_context`，发言人选择收敛到 `resolve_next_speaker`；+8 编排单测 +1 生产路径断言测试 | 2026-07-09(见 CHANGELOG) | 三条 grep 全干净；149 测试全过；解锁 TD-03-T2/T3 |
| 架构复核：发现 TD-02 路由层重复实现漂移，新增 TD-02-T5 阻塞项 | 2026-07-08(见 CHANGELOG) | 纯发现+文档纠正，代码未改 |
| 验证事实回填 DATA-MODEL/TD-03/04/05/ARCHITECTURE + workdir 架构决策 + 看板重建 | 2026-07-08(见 CHANGELOG) | |
| TD-02 T1–T4(多 agent 讨论编排+372 行测试) + SSE 流式 | `b61005e` | ⚠️ 编排函数本身正确，但**未被路由实际调用**——见上方漂移说明，需 TD-02-T5 完成整合 |
| 一键启动脚本 `npm run dev` | `4ec9e96` |
| SQLite 20 个失败测试修复 | `141592f` |
| TD-04-T5(API+前端最小闭环) | `c4d1e3e` |
| TD-04-T4(供给编排+状态机) | `81af20d` |
| TD-04-T3(role_spec 起草+SOUL 生成) | `f55d0b1` |
| TD-04-T2(ProfileProvisioner+RecordOnly) | `0ed930d` |
| TD-04-T1(agent_specs/capabilities 建表+DTO) | `cd52af8` |
| TD-05-T1(capability_catalog) | `dd595de` |
| TD-01-T1b(TaskOut consensus_brief_id) | `0c745b0` |
| TD-01-T1(讨论态接线) | `a9b2b06` |
| PLAYBOOK V1–V7 验证报告 | `19c209b` |
| 群讨论第一片(ADR 0006 实现) | `c2054bf` |
| 全部设计文档(架构/DATA-MODEL/TD-01~05/剧本/本看板) | 见 CHANGELOG 2026-07-03~08 |

## 维护规则
- 领任务/完成任务的 AI **必须**更新本文件并 push（这就是认领锁）。
- 新增 TD/task 时同步登记到上面两张表。
- **链接必须指向真实存在的文件**；完成状态必须带真实 commit 号，不写"待提交"。
- 状态含义：⚪ 待领 / 🔵 进行中(注明领取方+日期) / ✅ 完成(注明 commit) / ⛔ 阻塞(注明等什么)。
