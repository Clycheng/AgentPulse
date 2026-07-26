"""Prompt fragments supplied to Hermes ACP Runs.

This module deliberately contains no provider client.  Model selection and
provider credentials belong to Hermes; AgentPulse only prepares company facts
and employee-facing context for an ACP Run.
"""

from app.schemas.run import HermesRunContext


def build_hermes_context_prompt(request: HermesRunContext) -> str:
    agent = request.agent
    skills = "、".join(agent.skills) if agent.skills else "暂无绑定技能"
    conversation = request.conversation_title or "当前会话"
    related_tasks = format_related_tasks(request.related_tasks)
    knowledge_sources = format_knowledge_sources(request.knowledge_sources)
    agent_experiences = format_agent_experiences(request.agent_experiences)
    cognitive_context = request.cognitive_context.strip() or "暂无额外的公司记忆"

    return f"""你是公司中的一名同事，负责在自己的岗位上基于事实和专业判断推进公司的目标。
你不是某一个人的附属助手：公司目标、客户现实、同事协作和长期结果共同决定工作的优先级。
对话中的其他参与者只通过姓名、岗位和他们说过或做过的事实来理解，不讨论运行时实现。

公司：{request.company_name}
当前会话：{conversation}
{related_tasks}
{knowledge_sources}
{agent_experiences}

本次根据公司事件和员工经验检索出的相关事实：
{cognitive_context}

你的员工档案：
- 姓名：{agent.name}
- 岗位：{agent.role or "未填写"}
- 部门：{agent.department or "未分配"}
- 技能：{skills}

你的工作职责 Prompt：
{agent.prompt}

{request.discussion_context}

回复规则：
1. 使用中文，语气专业、直接、可靠。
2. 如果目标或背景模糊，先指出缺失信息并推动团队澄清，而不是空泛鼓励。
3. 没有工具回执时，不要声称已创建、已读取、已发送、已发布或已操作系统。
4. 遇到风险、范围变化或不可逆动作时，明确说明需要人类决策者确认的问题和可选方案。
5. 如果需要其他同事协作，提出具体事实、问题和预期交付；没有收到对方结果前不要假装已经完成。
6. 如果有个人经验记忆，优先复用成功经验，避开复盘教训里已经暴露的问题。
7. 如果有公司资料库上下文，优先结合资料里的事实，不要编造资料中没有的公司事实。
8. 可以质疑不合理目标、拒绝违反规则的要求，并说明依据和替代方案。
9. 输出尽量结构化，优先给公司可以直接推进的下一步。
10. 不要在回复中讨论平台实现、内部标识或运行过程；只使用姓名、岗位和可核验事实。"""


def format_related_tasks(tasks: list) -> str:
    if not tasks:
        return "\n当前关联任务：无"
    lines = ["\n当前关联任务："]
    for index, task in enumerate(tasks, start=1):
        owner = f"，负责人：{task.owner_name}" if task.owner_name else ""
        description = f"\n   说明：{task.description}" if task.description else ""
        lines.append(
            f"{index}. [{task.priority}] {task.title} "
            f"({task.status}，进度 {task.progress}%{owner}){description}"
        )
    return "\n".join(lines)


def format_agent_experiences(experiences: list) -> str:
    if not experiences:
        return "\n个人经验记忆：暂无"
    lines = ["\n个人经验记忆："]
    for index, experience in enumerate(experiences, start=1):
        label = "成功经验" if experience.outcome == "success" else "复盘教训"
        lessons = f"\n   经验/教训：{experience.lessons}" if experience.lessons else ""
        lines.append(f"{index}. {label}：{experience.summary}{lessons}")
    return "\n".join(lines)


def format_knowledge_sources(sources: list) -> str:
    if not sources:
        return "\n公司资料库上下文：暂无"
    lines = ["\n公司资料库上下文："]
    for index, source in enumerate(sources, start=1):
        lines.append(
            f"{index}. [{source.category or '通用资料'}] {source.title}\n"
            f"   {source.content[:900]}"
        )
    return "\n".join(lines)
