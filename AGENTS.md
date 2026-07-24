# AGENTS.md — 接手本项目的 AI / 开发者必读

> **动手前先读完本文件。** 它定义 AgentPulse 的产品方向、已拍板的架构决策、以及开发规范。
> 本文件是唯一的"北极星"。**任何偏离本文件方向的改动，动手前必须先跟项目所有者确认。**
> 如果你是被"甩"了这个仓库、没有对话上下文的 AI：读完 `AGENTS.md` → `docs/ARCHITECTURE.md` → `docs/decisions/` 就能完整对齐，直接开工。

---

## 0. 这是什么项目

**AgentPulse** = 面向一人公司 / 自媒体的「AI 公司工作台」。把"一个人经营公司"重组成：公司 → 招 AI 员工 → 交代目标 → 拆解任务 → 多员工协作 → 展示进度产出 → 高风险动作等用户拍板。产品愿景见 [README.md](README.md)。

Monorepo：`apps/`(web/desktop/admin，desktop 是主原型 Electron+React)、`services/api`(FastAPI + PostgreSQL)。

---

## 1. 北极星（产品方向 · 不可跑偏）

1. **用户是老板，不是系统管理员**：不把 prompt / schema / workflow DAG 丢给普通用户。
2. **协作模式 = 先拉群讨论，再分工执行**：要干一件事，先拉相关员工进群，**把背景/目标/分工讨论明白**，达成共识后才开工。**Agent 要像人——背景不清楚必须在群里发问，绝不允许被分配了就稀里糊涂开干。**
3. **自然语言捏 agent**：用户用一句话描述角色，系统生成不同技能 / 能力的 AI 员工。
4. **每个 agent 自我学习、持续进步**：在工作中沉淀技能、积累记忆，越用越懂这家公司。
5. **没有 idle 员工**：员工空闲时也在想业务、攒 idea(投到独立的「idea 中心」，区别于任务中心)、学技能。**7×24 不间断工作。**
6. **群聊 + 私聊都要支持**：同一套会话引擎，参与者=2 就是私聊。

---

## 2. 架构决策（已拍板 · 改动前须新增 ADR）

详细论证与调研出处见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，原子决策见 [docs/decisions/](docs/decisions/)。

### 分层
```
产品层 (AgentPulse)     公司/部门/员工卡 · 自然语言捏 agent · 技能市场 · idea中心 · 看板
协作编排层 (自研·核心)   群讨论协议：共享 transcript + 发言路由 + 对齐门 + 人类拍板 + 终止条件
   照 AutoGen 骨架       讨论达成 → 产出"共识 brief" → 才建 Task/Run · Task/Run/RunStep/Approval
运行时层 (Hermes)        每员工 = 1 个 Hermes profile(SOUL人格 + Skills技能 + Memory记忆 + 可选模型)
                        自我学习 = 技能沉淀 + 记忆(Hermes 原生) · 7×24 = daemon+cron(我们编排)
执行/部署层             后端服务器常驻 Hermes daemon(Docker/systemd)；后端/官网/后台/客户端分处不同机器互联
```

### 一句话
> **AgentPulse 自研「公司协作大脑」(拉群 → 讨论对齐 → 分工 → 拍板 → 追踪)；Hermes 提供「每个员工的专业能力 + 记忆 + 自我进步」；两者靠 HTTP(Runs API) / kanban 对接。**

### 复用什么
- **员工运行时 = [Hermes Agent](https://github.com/NousResearch/hermes-agent)**(Nous Research 开源，MIT)。人格(SOUL.md)、技能(SKILL.md)、记忆、多模态、7×24 daemon、多实例(profiles)、编排(kanban)都用它的原生能力。
- 基建模式参照 **Multica**(Server/Daemon/adapter/pull队列+心跳+隔离workdir)。

### 明确不做（DON'T — 防止后续 AI 跑偏）
- ❌ **不**以 Codex / Claude Code 这类"操作文件的工程师型" CLI 为基座。基座只用 Hermes。
- ❌ **不**自建 Agent 协议 / Runtime / 工具系统(用 Hermes 的)。
- ❌ **不**把现在 `services/api` 里"直连 DeepSeek 回一段话"那套当成目标形态——**那只是临时执行层**，最终由"后端调 Hermes profile"替换。
- ⚠️ [ROADMAP.md](ROADMAP.md) 是早期文档，其中"多 CLI 适配 / 先 Codex 后 Claude"等 Runtime 取舍**已被本决策取代**，勿照搬；产品愿景和数据模型草案部分仍有效。

---

## 3. 关键已验证事实（已深度调研，勿重复造轮子）

- **驱动 Hermes**：本机 Hermes v0.18 实测不存在旧 REST Runs API；执行统一用 ACP（`hermes --profile <profile> acp`，stdio JSON-RPC），供给用 CLI，见 [ADR 0007](docs/decisions/0007-hermes-v0.18-interface-acp.md)。任务 Run 动态注入公司 MCP；具备受控业务能力的聊天/任务 Run 动态注入独立业务 MCP，密钥只在 AgentPulse 加密库中。
- **多实例**：`profiles`(每员工独立 HERMES_HOME：人格/技能/记忆/密钥/模型隔离) + `hermes kanban`(orchestrator profile 派任务给 N 个 worker profile 独立进程)。已发布能力。
- **7×24 / 无 idle**：daemon + cron(60s tick 主动跑) + webhooks + `/goal` 原生；但"空闲自发想 idea"**不原生**(学习是每轮对话后触发) → **idea 中心 = 用 cron 编排"空闲即思考/学技能"**。
- **多模态**：DeepSeek 是文本模型也没关系——图片走 `vision_analyze`(配 `auxiliary.vision` 辅助视觉模型)、音频走内置 Whisper STT(可本地免费)、视频 `video_analyze`、PDF `web_extract`。Hermes 把一切模态转成文本喂给主模型。
- **模型要求**：主模型需 ≥64k 上下文 + 会 function-calling。DeepSeek V3 达标，可作默认主模型。
- **群讨论参照**：AutoGen 提供全部原语(共享 transcript、发言选择、transition 约束门、human-in-loop、可组合终止、Magentic-One 的 Task Ledger→Progress Ledger = "先讨论明白再开干"范本)。Multica 的"协作"只是 leader 派活(delegation)不是讨论，别照它做讨论层。

---

## 4. 当前状态 vs 目标

| 模块 | 现状 | 目标 |
|---|---|---|
| `services/api` | FastAPI + PostgreSQL/SQLite；群讨论、brief、持久 Run 调度、Hermes ACP、TD-10 业务动作与 TD-13 公司事件/记忆/Context Engine 均已接入；TD-12 workspace BYOK、限流、生产安全和 ARM64 镜像已实现 | 完成 Oracle/Supabase 线上部署后按看板进入 TD-09 |
| `apps/desktop` | 聊天/员工/任务/审批/brief/自动计划/内容包、业务凭证/渠道/动作审计、DeepSeek BYOK、TD-13 记忆证据审计和受控 Obsidian managed 同步 UI；生产 `app://`、固定 API 与 safeStorage 已接入 | 完成 macOS/Windows v0.1.0 发布后继续渐进拆分单文件原型 |
| 群讨论协议 | 🟢 讨论态状态机、多 agent 发言路由、共识 brief 和 Task 门控均已实现；TD-11 已真实跑通 UI 中的“四人讨论→带分工 brief→一次确认” | 后续增强长讨论与衍生 brief 体验 |
| Hermes 集成 | 🟢 ACP 真执行、技术危险动作审批、TD-11 公司工具和 TD-10 业务工具均已接入。业务工具使用独立 per-Run token；审批和外部动作持久化，Hermes 不持有 provider 密钥。Resend 邮件是真 provider，其余五类工具未配置时明确失败，不伪造执行 | 后续 provider 按 TD-09/新 ADR 逐项接入 |
| 自然语言捏 agent（北极星③） | 🟢 **第一版已实现并真机验证**：`POST /agents/draft-team`(一段话→可编辑草稿，复用 TD-04-T3 一直未接生产入口的 `draft_role_spec`) + `POST /agents/create-team`(确认后一次性真建员工+自动拉一个团队群)；`provision_new_agent` 统一四条招聘路径(人才市场/秘书 bootstrap/小秘 `create_employee`/团队编译器)的供给入口；桌面端 `TeamCompilerModal`。见 [ADR 0009](docs/decisions/0009-natural-language-team-compiler.md)。四点边界（不规划多群/不编业务技能/不加校验器/不做试运行）已拍板写死在 ADR，避免下一个 AI 又加回去 | 精细协作 SOP（如"机动备援"式动态支援角色）留给老板在群里/资料库后续补充，不在编译器范围 |
| 自动执行闭环 | 🟢 TD-11 已实现一次 launch、依赖接力、重启恢复和 `content_package_v1`；TD-10 已增加持久业务动作、长期策略与真实邮件能力；TD-13 已接入共享事件账本、独立记忆、每 Run Context Manifest 和有界内部协作 | TD-09 负责渠道出站和更多渠道适配 |
| 云端分发 | 🔵 TD-12 进行中：Vercel 官网/Analytics、公开 Releases 仓、BYOK、生产安全、ARM64 镜像和桌面打包链路已实现 | 完成 Supabase、Oracle、DNS、v0.1.0 Release 与线上 E2E |
| 公司世界模型 | 🔵 TD-13 代码与 SQLite/API/UI 已接入：公司事件账本、证据记忆、Context Engine、MCP 同事协作、反思 worker 和受控 Obsidian managed 同步；真实 Hermes 四人认知 E2E 待线上/真实运行时验证 | 继续补长讨论、深度反思和四人跨经历 E2E |

**下一步 → 直接看 [docs/tech-design/EXECUTION-BOARD.md](docs/tech-design/EXECUTION-BOARD.md)（执行看板 = 唯一任务状态源）**：它列着"现在就做"的任务队列(带顺序/依赖/会话要求/状态)和认领规则——**AI 不需要人类告诉下一步，读看板即知**。设计文档体系(架构/规格/各 TD)入口见 [docs/tech-design/README.md](docs/tech-design/README.md)。

---

## 5. 开发规范（必须遵守）

### Git 身份 —— ⚠️ 最容易出错，务必看清
- 本仓库是 **Clycheng 的私有仓库**(`git@github.com:Clycheng/agentpulse.git`)。
- 提交署名**必须**是 `Clycheng <30332511+Clycheng@users.noreply.github.com>`(已配在本仓库 local git config)。**绝不能用其他身份(尤其 unitpulse / UP 的邮箱)提交**——本机存在多套 git 身份，别串。
- 在 `main` 分支上开发(项目所有者指定)。
- commit message 用**英文**、祈使句(与现有历史一致)。

### 与 UnitPulse 零关联 —— ⚠️ 硬性原则，不是配置细节
- **AgentPulse 和 UnitPulse(本机另一个真实项目)必须零关联**——不只是 git 身份，执行环境、文件系统操作也不能有任何交集。项目所有者原话："他们可千万不能互相扯到一起。"
- **任何会真实执行进程/写文件系统的操作**(起 Hermes、装依赖、跑测试脚本…)，执行时的 ambient 工作目录**绝不能位于 UnitPulse 仓库/worktree 内**——参见 [ADR 0005](docs/decisions/0005-hermes-poc-safety-findings.md)：2026-07-05 曾因会话 cwd 在 UnitPulse worktree 里，导致 Hermes 把测试文件写进了 UnitPulse 主仓库(已清理，无损失，但被定性为不可接受的结构性风险，不是"下次小心点"能揭过的)。
- 理想做法：这类操作应在 cwd 直接锚定在 `/Users/liuxiajiang/Desktop/code/agentpulse` 的会话里做；任何第三方 agent runtime 的 workdir 配置必须显式设为绝对路径，不能信任默认相对路径。

### 动手前
- 读 `AGENTS.md` + `docs/ARCHITECTURE.md` + `docs/decisions/`。
- 任何偏离 §1/§2 方向的想法，**先跟项目所有者确认再动手**。

### 架构层界验收（防漂逸 · 完成任何 TD task 前必做）

**单测全过 ≠ 任务完成。** 上一轮 worker AI 就是因为只看单测，实现了功能等效但绕过了架构设计入口的代码（详见 [TD-02 🔴漂逸说明](docs/tech-design/TD-02-multi-agent-discussion.md)）。

完成路由/编排/运行时任何改动后，在 commit 前强制跑以下检查：

```bash
# 1. 路由层不得有业务循环 / 独立 LLM 选发言人
grep -rn "for.*turn\|while.*round\|_llm_select\|_extract_mention" services/api/app/api/routes/
# 结果必须为空，否则不得声明完成

# 2. 生产入口实际调用了编排函数（不是死码）
grep -n "run_discussion_round\|select_next_speaker" services/api/app/api/routes/workspace.py
# 必须有调用行出现；若只存在于 tests/ 则是死码

# 3. 编排层不直接访问 HTTP/Hermes（通过 runtime/ 接口）
grep -rn "httpx\|requests\|hermes_client\|HermesBackend" services/api/app/orchestration/
# 结果必须为空
```

commit message 里加一行声明：`Verified: <函数名> called via production path <路由>`，不能空口。

如果只修了 schema / 单测 / 文档 → 此检查跳过，但要说明原因。

### 做完后必须记录（重要 —— 让下一个 AI 也不跑偏）
1. **架构/方向级决策** → 在 `docs/decisions/` 新增一条 ADR(见该目录 README 的格式)。
2. **值得记的改动**(新功能、重构、依赖变化) → 更新 `CHANGELOG.md`。
3. **相关文档过时了** → 同步更新(尤其本文件 §2/§3/§4)。
4. **声明"完成"前先验证**：跑起来看真实行为(build/测试/截图)，别空口说做完了。

### 文档如何随项目生长（对齐 AGENTS.md / Claude Code 官方实践）
- **本文件保持精简可扫(目标 <200 行)**，是"入口 + 规则 + 索引"；厚重内容放 `docs/`。
- **子项目专属规范** → 在该目录放**嵌套 `AGENTS.md`**(agents 就近读取，最近的生效)，如 `apps/desktop/AGENTS.md`、`services/api/AGENTS.md`、未来的 Hermes 集成目录。
- **按文件类型/路径的专项规则** → 放 `.claude/rules/*.md`，可加 `paths:` frontmatter 只在动相关文件时加载(注：`.claude/` 已 gitignore，需要团队共享的规则要调整忽略策略)。
- **可复用的多步流程** → 做成 skill 按需加载，而不是塞进本文件。
- 目的：靠"就近嵌套 + 路径域规则 + skills"控制上下文，别把根 AGENTS.md 堆大。

---

## 6. 文档索引

| 文件 | 内容 |
|---|---|
| [README.md](README.md) | 产品愿景(高层，稳定) |
| [AGENTS.md](AGENTS.md) | **本文件**：方向 + 架构 + 规范(接手第一读) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 详细架构 + 调研结论 + 出处。**这就是 `docs/workflow.md`/`docs/backlog.md` 里 Architecture 阶段要产出的那份文档**——两边指的是同一份，只是大小写不同(历史遗留)，不要另建 `docs/architecture.md` |
| [docs/decisions/](docs/decisions/) | 架构决策记录(ADR)，含决策约定("为什么这么决定") |
| [docs/tech-design/](docs/tech-design/) | 技术设计 + tech-task 拆解("具体怎么实现")。**先读 [the-loop.md](docs/tech-design/the-loop.md)(闭环走查) + [DATA-MODEL-AND-API.md](docs/tech-design/DATA-MODEL-AND-API.md)(表/字段/接口唯一真相源)**，再看 TD-01/02/03，动手直接照做 |
| [docs/research/](docs/research/) | 竞品/开源项目调研(如 `dust.md`、`skill-source-repos.md`)，**已调研但未拍板**——区别于 `docs/decisions/`(已拍板) |
| [docs/prd.md](docs/prd.md) | MVP 产品需求文档：范围、用户故事、非目标 |
| [docs/backlog.md](docs/backlog.md) | Epic/Story/Task 拆分、验收标准、依赖图、assignee 建议 |
| [docs/workflow.md](docs/workflow.md) | 项目自己的研发闭环流程(Research→PRD→Architecture→Backlog→Build→QA→Retrospective)，含 `agentpulse-*` 角色分工 |
| [CHANGELOG.md](CHANGELOG.md) | 变更记录 |
| [ROADMAP.md](ROADMAP.md) | 早期路线(部分已被 §2 取代，见警告) |
