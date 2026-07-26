# Tech-Design（技术设计 + 任务拆解）

本目录把已拍板的方向（[docs/decisions/](../decisions/) 的 ADR）落成**可执行的技术设计**和**可分配、可验收的 tech-task**。

- **ADR（`docs/decisions/`）** 回答"为什么这么决定"（方向/取舍，改动前须新开 ADR）。
- **Tech-Design（本目录）** 回答"具体怎么实现"：数据模型、API 契约、模块边界、时序流程，以及拆成哪些 task。
- 一个 tech-design 文件（`TD-NN-<slug>.md`）= 一个可独立推进的**阶段**，内含两段：`## 技术设计` + `## Tech-Tasks`（编号 `TD-NN-Tk`，各带验收标准/依赖/是否需 agentpulse 会话）。

## 阅读顺序（理解 → 架构 → 规格 → 任务）

| 文件 | 作用 |
|---|---|
| [EXECUTION-BOARD.md](EXECUTION-BOARD.md) | **⓪ 执行看板(唯一任务状态源)**：现在该做什么、顺序/依赖/会话要求/认领规则——冷启动第一跳 |
| [the-loop.md](the-loop.md) | **① 闭环走查锚文档**：一个具体场景把"目标→讨论→brief→建任务→执行→回写"整条闭环走一遍(带真实数据)，先懂闭环再看任务 |
| [ARCHITECTURE-DETAILED.md](ARCHITECTURE-DETAILED.md) | **② 实现级系统架构(脊梁)**：组件全景/部署拓扑/分层模块职责+接口/核心时序(NL→agent 供给、讨论→任务、任务→Hermes 执行)/横切关注点/Hermes 边界契约/各组件深化索引 |
| [DATA-MODEL-AND-API.md](DATA-MODEL-AND-API.md) | **③ 唯一真相源**：所有表/字段/接口/错误码的精确规格(§6 含 agent 供给新表+capability catalog) + 已知不对齐项(G1–G5)。写代码前先查这里，别照 ADR 里的初稿建表 |
| [agent-model-and-capabilities.md](agent-model-and-capabilities.md) | **④ agent 能力模型**：agent=底座+人格+技能+工具/MCP+凭证+模型；小秘书凭什么特殊；各工种能力逐条落地+现成开源程度+诚实边界 |

## 当前目标弧（从"第一片已完成"到"第一个真正可用的垂直闭环")

> 老板抛目标 → 员工群里讨论澄清 → 对齐出共识 brief → 门控建任务 → **真·Hermes 员工执行** → 结果/审批回到聊天 → 老板看到

第一片（讨论态 + brief + Task 创建门控，[ADR 0006](../decisions/0006-group-discussion-v1-first-slice.md)）**已实现并通过单测**（commit `c2054bf`）。剩下三个阶段：

| 阶段 | 文件 | 一句话 | 是否需 agentpulse 锚定会话 | 规模 |
|---|---|---|---|---|
| A | [TD-01](TD-01-verify-and-harden-slice-1.md) | 端到端手测并收尾第一片（含讨论态接线） | 是（要起后端+桌面端） | 小 |
| B | [TD-02](TD-02-multi-agent-discussion.md) | 多 agent 真正在群里接力讨论（照 AutoGen 骨架） | 否（纯 `services/api` 逻辑，暂不碰 Hermes） | 中大 |
| C | [TD-03](TD-03-hermes-execution.md) | 执行层从"直连 DeepSeek"换成"调 Hermes profile"(Run/RunStep + HermesBackend) | **是**（会真起 Hermes 进程，[ADR 0005](../decisions/0005-hermes-poc-safety-findings.md) 隔离规矩） | 大 |
| D | [TD-05](TD-05-capability-catalog.md) → [TD-04](TD-04-agent-provisioning.md) | 能力映射表 + Agent 供给(一句话→定制员工)。与 B/C 可并行；仅 TD-04-T6 需 agentpulse 会话 | 大部分否 | 中 |
| V | [HERMES-VERIFICATION-PLAYBOOK](HERMES-VERIFICATION-PLAYBOOK.md) | **验证剧本**：一次实测清掉全部〔待核〕(V1–V7)并回填各文档。建议最先执行——之后 C/D 的 T6 类任务零猜测 | **是** | 小 |
| E | [TD-11](TD-11-autonomous-content-execution.md) | 自媒体 AI 公司自动执行闭环：完整分工 brief → 一次确认 → 持久依赖调度 → 内容包 | **是** | 大 |
| F | [TD-12](TD-12-cloud-deployment-and-desktop-distribution.md) | 官网下载 → 桌面注册/BYOK → 云端 API/Hermes 的部署分发闭环 | **是** | 大 |
| G | [TD-13](TD-13-company-world-model.md) | 公司事件账本 → 记忆检索 → 独立同事协作 → 反思 | **是** | 大 |
| H | [TD-15](TD-15-workforce-scheduler.md) | WorkRequest → 百人员工资源调度 → 安全抢占/恢复 → 工作台 | **是** | 大 |

## Worker AI 执行协议（领任务前必读）

1. **读**：AGENTS.md → the-loop → ARCHITECTURE-DETAILED → DATA-MODEL(查你要动的表/接口) → 你领的 TD。
2. **领**：到 [EXECUTION-BOARD.md](EXECUTION-BOARD.md)(唯一任务状态源)挑「现在就做」里最靠前且会话类型匹配的任务；**开工第一个动作=把看板上该任务改成 🔵 进行中并 commit+push**(认领锁，防多 AI 撞车)。"需 agentpulse 会话"与当前会话不匹配就别领(尤其起 Hermes 的任务——ADR 0005)。
3. **做**：schema/接口以 [DATA-MODEL-AND-API.md](DATA-MODEL-AND-API.md) 为准，与 TD 冲突时以 DATA-MODEL 为准并上报；建表/加列必须双 schema(init_postgres+init_sqlite)都改。
4. **验**：按 task 的验收标准逐条自证(跑测试/截图/记录响应)；"声明完成前先验证"(AGENTS.md §5)。
5. **记**：更新 CHANGELOG；**回看板把任务标 ✅(注 commit) 并 push**(状态只在看板标，TD 文件不标)；实测出的〔待核〕真相**必须回填** DATA-MODEL/相关 TD；过时文档同步。
6. **禁**：不改 `docs/decisions/` 已有 ADR(要变新开)；不放宽任何 risk_gate；不在 UnitPulse 环境起进程。

**推荐顺序 A → B → C**：A 先确认前一片没白做（便宜、低风险）；B 是产品灵魂"先讨论"、且在任何会话做都安全；C 价值最大但工程量最大、且必须另开 agentpulse 锚定会话。B/C 的先后可调，但 A 应最先。

## 与旧文档的关系

- [docs/backlog.md](../backlog.md) / [docs/workflow.md](../workflow.md) 是 Hermes 转向**之前**的 Epic/Story 拆解（还在讲多 CLI "本地 runtime"），其**流程方法论**（Research→PRD→Architecture→Backlog→Build→QA）仍可参考，但**具体任务清单已过时**，以本目录为准。
