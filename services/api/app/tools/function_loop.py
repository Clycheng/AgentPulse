"""Function-calling loop: LLM decides tools → execute → feed results → repeat.

Drives the agent through a decision loop:
1. Stream the user message + system prompt + tools to the LLM over SSE, so
   the boss sees text arrive token-by-token exactly like a normal chat reply
   — not as one blob after the whole completion finishes.
2. While streaming, accumulate any tool_calls deltas. If the model decides to
   call a tool (finish_reason == "tool_calls") → execute it → feed the result
   back → start another streamed round.
3. If the model just replies with text (finish_reason == "stop") → the chunks
   already streamed to the caller are the final answer, done.

Max 5 tool-calling rounds per user message to prevent infinite loops.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from app.core.database import Database, Row
from app.schemas.run import LlmChatMessage
from app.tools.registry import (
    TOOLS,
    ToolCall,
    ToolResult,
    execute_tool,
    system_prompt_for_operator,
)

MAX_TOOL_ROUNDS = 5


async def run_function_loop(
    *,
    conn: Database,
    workspace_id: str,
    workspace_name: str,
    agent: Row,
    history: list[LlmChatMessage],
    user_message_content: str,
    deepseek_client: Any,  # DeepSeekChatClient
    related_tasks: list | None = None,
    knowledge_sources: list | None = None,
    agent_experiences: list | None = None,
    cognitive_context: str = "",
) -> AsyncGenerator[dict, None]:
    """Run the function-calling loop, yielding {type: chunk|tool_call|tool_result} events.

    The caller is responsible for collecting the chunks into a final agent message
    and persisting it.

    Yields:
        {"type": "chunk", "content": str}  — real-time text deltas from the LLM
        {"type": "tool_call", "name": str, "args": dict}  — tool being invoked
        {"type": "tool_result", "name": str, "result": str}  — tool result
    """
    messages: list[dict[str, Any]] = []

    # System prompt
    messages.append({
        "role": "system",
        "content": system_prompt_for_operator(
            workspace_name,
            agent["name"],
            agent.get("role", ""),
            related_tasks=related_tasks,
            knowledge_sources=knowledge_sources,
            agent_experiences=agent_experiences,
            cognitive_context=cognitive_context,
        ),
    })

    # History (skip system messages, keep user/assistant)
    for msg in history[-16:]:
        role = msg.role
        if role == "user":
            messages.append({"role": "user", "content": msg.content})
        else:
            messages.append({"role": "assistant", "content": msg.content})

    # Add the latest user message if not already included
    if not messages or messages[-1].get("role") != "user" or messages[-1].get("content") != user_message_content:
        messages.append({"role": "user", "content": user_message_content})

    async for ev in _run_rounds(deepseek_client, conn, workspace_id, agent, messages):
        yield ev


async def _run_rounds(
    client: Any,
    conn: Database,
    workspace_id: str,
    agent: Row,
    messages: list[dict[str, Any]],
) -> AsyncGenerator[dict, None]:
    for round_num in range(MAX_TOOL_ROUNDS):
        try:
            content_text, tool_calls = "", []
            async for ev in _stream_llm_round(client, messages):
                if ev["type"] == "chunk":
                    content_text += ev["content"]
                    yield ev
                elif ev["type"] == "_tool_calls":
                    tool_calls = ev["tool_calls"]
                elif ev["type"] == "_error":
                    raise RuntimeError(ev["detail"])
        except Exception:
            break

        if not tool_calls:
            return

        # Record the assistant's tool_calls turn for the next round's context
        messages.append({
            "role": "assistant",
            "content": content_text or None,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)},
                }
                for tc in tool_calls
            ],
        })

        for tc in tool_calls:
            yield {"type": "tool_call", "name": tc.name, "args": tc.arguments}

        tool_results: list[ToolResult] = []
        for tc in tool_calls:
            result = await execute_tool(conn, workspace_id, agent, tc)
            tool_results.append(result)
            yield {"type": "tool_result", "name": result.name, "result": result.content}

        # Persist tool execution results to database
        conn.commit()

        for tr in tool_results:
            messages.append({
                "role": "tool",
                "tool_call_id": tr.tool_call_id,
                "content": tr.content,
            })

    # Max rounds reached — do one final streamed completion without tools
    try:
        async for ev in _stream_llm_round(client, messages, with_tools=False):
            if ev["type"] == "chunk":
                yield ev
    except Exception:
        pass


async def _stream_llm_round(
    client: Any,
    messages: list[dict[str, Any]],
    with_tools: bool = True,
) -> AsyncGenerator[dict, None]:
    """Stream one LLM turn over SSE.

    Yields {"type": "chunk", "content": str} for each text delta in real time,
    then a final {"type": "_tool_calls", "tool_calls": list[ToolCall]} once the
    stream ends (empty list if the model just replied with text).
    """
    payload = {
        "model": client.model,
        "messages": messages,
        "stream": True,
    }
    if with_tools:
        payload["tools"] = TOOLS

    tool_call_builders: dict[int, dict[str, Any]] = {}

    async with httpx.AsyncClient(timeout=client.timeout_seconds, trust_env=False) as http:
        async with http.stream(
            "POST",
            f"{client.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {client.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise RuntimeError(f"LLM call failed: {resp.status_code} {body[:500]}")

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except ValueError:
                    continue
                choice = chunk.get("choices", [{}])[0]
                delta = choice.get("delta", {})

                content = delta.get("content")
                if content:
                    yield {"type": "chunk", "content": content}

                for tc_delta in delta.get("tool_calls") or []:
                    idx = tc_delta.get("index", 0)
                    builder = tool_call_builders.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc_delta.get("id"):
                        builder["id"] = tc_delta["id"]
                    func = tc_delta.get("function") or {}
                    if func.get("name"):
                        builder["name"] = func["name"]
                    if func.get("arguments"):
                        builder["arguments"] += func["arguments"]

    tool_calls: list[ToolCall] = []
    for builder in tool_call_builders.values():
        if not builder["id"] or not builder["name"]:
            continue
        try:
            args = json.loads(builder["arguments"]) if builder["arguments"] else {}
        except (json.JSONDecodeError, TypeError):
            args = {}
        tool_calls.append(ToolCall(id=builder["id"], name=builder["name"], arguments=args))

    yield {"type": "_tool_calls", "tool_calls": tool_calls}


def _extract_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
    """Parse tool calls out of a full (non-streaming-shaped) assistant message dict."""
    raw = message.get("tool_calls") or []
    result: list[ToolCall] = []
    for tc in raw:
        fid = str(tc.get("id", ""))
        func = tc.get("function", {})
        name = str(func.get("name", ""))
        raw_args = func.get("arguments", "{}")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except (json.JSONDecodeError, TypeError):
            args = {}
        if name and fid:
            result.append(ToolCall(id=fid, name=name, arguments=args))
    return result


def _extract_text(message: dict[str, Any]) -> str:
    """Extract text content from a full (non-streaming-shaped) assistant message dict."""
    return (message.get("content") or "").strip()
