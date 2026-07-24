from collections.abc import AsyncGenerator
from typing import Any
import json

import httpx

from app.core.config import settings
from app.schemas.run import LlmChatMessage, LlmChatRequest, LlmChatResponse


class DeepSeekNotConfigured(RuntimeError):
    pass


class DeepSeekAPIError(RuntimeError):
    pass


class DeepSeekChatClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.deepseek_api_key
        self.base_url = (
            base_url if base_url is not None else settings.deepseek_base_url
        ).rstrip("/")
        self.model = model if model is not None else settings.deepseek_model
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.deepseek_timeout_seconds
        )

    async def complete(
        self, request: LlmChatRequest, *, system_prompt_override: str | None = None
    ) -> LlmChatResponse:
        """system_prompt_override: skip the normal colleague persona framing
        that build_system_prompt() adds, and send
        this text as the system message verbatim instead. For utility calls
        that need a specific, unpolluted instruction — e.g. "output strict
        JSON, nothing else" — where the conversational-reply boilerplate
        (tone rules, "don't claim to have sent an email", etc.) would only
        make the model less likely to follow the actual instruction."""
        if not self.api_key:
            raise DeepSeekNotConfigured(
                "DeepSeek API Key 未配置，请先在 AgentPulse 中完成模型设置"
            )

        system_prompt = (
            system_prompt_override
            if system_prompt_override is not None
            else build_system_prompt(request)
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                *[message_to_deepseek(message) for message in request.messages],
            ],
            "stream": False,
            "thinking": {
                "type": "enabled" if settings.deepseek_thinking_enabled else "disabled"
            },
        }
        if not settings.deepseek_thinking_enabled:
            payload["temperature"] = settings.deepseek_temperature

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise DeepSeekAPIError("DeepSeek 请求超时，请稍后重试") from exc
        except httpx.HTTPError as exc:
            raise DeepSeekAPIError(f"DeepSeek 请求失败：{exc}") from exc

        if response.status_code >= 400:
            raise DeepSeekAPIError(
                f"DeepSeek API 返回 {response.status_code}：{extract_error(response)}"
            )

        try:
            data: dict[str, Any] = response.json()
        except ValueError as exc:
            raise DeepSeekAPIError("DeepSeek 返回了无法解析的响应") from exc

        content = extract_reply(data)
        if not content:
            raise DeepSeekAPIError("DeepSeek 没有返回有效回复")

        return LlmChatResponse(
            reply=clean_agent_prefix(content, request.agent.name),
            provider="deepseek",
            model=str(data.get("model") or self.model),
            usage=data.get("usage"),
        )

    async def complete_stream(
        self, request: LlmChatRequest
    ) -> AsyncGenerator[str, None]:
        """Stream LLM response, yielding text chunks as they arrive.

        Yields content strings. The caller is responsible for collecting
        the full reply and persisting it.
        """
        if not self.api_key:
            raise DeepSeekNotConfigured(
                "DeepSeek API Key 未配置，请先在 AgentPulse 中完成模型设置"
            )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": build_system_prompt(request)},
                *[message_to_deepseek(message) for message in request.messages],
            ],
            "stream": True,
            "thinking": {
                "type": "enabled" if settings.deepseek_thinking_enabled else "disabled"
            },
        }
        if not settings.deepseek_thinking_enabled:
            payload["temperature"] = settings.deepseek_temperature

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                ) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        raise DeepSeekAPIError(
                            f"DeepSeek API 返回 {response.status_code}：{body.decode()[:500]}"
                        )

                    agent_name = request.agent.name
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                        except ValueError:
                            continue
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield clean_agent_prefix(content, agent_name)
                            # Only clean prefix from first chunk
                            agent_name = ""
        except httpx.TimeoutException as exc:
            raise DeepSeekAPIError("DeepSeek 请求超时，请稍后重试") from exc
        except httpx.HTTPError as exc:
            raise DeepSeekAPIError(f"DeepSeek 请求失败：{exc}") from exc


def build_system_prompt(request: LlmChatRequest) -> str:
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
3. 不要声称已经发送邮件、创建外部文档、发布内容或操作第三方系统；第一版你只能回复消息和提出计划。
4. 遇到风险、范围变化或不可逆动作时，明确说明需要人类决策者确认的问题和可选方案。
5. 如果需要其他同事协作，提出具体事实、问题和预期交付；没有收到对方结果前不要假装已经完成。
6. 如果有个人经验记忆，优先复用成功经验，避开复盘教训里已经暴露的问题。
7. 如果有公司资料库上下文，优先结合资料里的品牌、业务、客户、流程等事实，不要编造资料中没有的公司事实。
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
        content = source.content[:900]
        lines.append(
            f"{index}. [{source.category or '通用资料'}] {source.title}\n   {content}"
        )
    return "\n".join(lines)


def message_to_deepseek(message: LlmChatMessage) -> dict[str, str]:
    speaker = f"{message.name}：" if message.name else ""
    return {"role": message.role, "content": f"{speaker}{message.content}"}


def extract_reply(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return ""

    message = first_choice.get("message")
    if not isinstance(message, dict):
        return ""

    content = message.get("content")
    return content.strip() if isinstance(content, str) else ""


def clean_agent_prefix(content: str, agent_name: str) -> str:
    cleaned = content.strip()
    prefixes = [
        f"{agent_name}：",
        f"{agent_name}:",
        f"{agent_name}：{agent_name}：",
        f"{agent_name}:{agent_name}:",
    ]
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix) :].lstrip()
                changed = True
    return cleaned


def extract_error(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:500]

    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message
        message = data.get("message")
        if isinstance(message, str):
            return message

    return str(data)[:500]
