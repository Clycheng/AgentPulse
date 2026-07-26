"""RunService — drive a Hermes run, persist it, and stream it (TD-03-T3/T4).

``stream_agent_run`` consumes a backend's ``AgentEvent`` stream (``HermesBackend``
in prod, a fake in tests), records the run lifecycle + ``run_steps`` (TD-03-T1),
writes the aggregated agent reply back as a message, and yields route-shaped
events (``{"type": "chunk"|"message"|...}``) so the SSE hot path can stream
Hermes output exactly like the temporary DeepSeek path did. ``start_run`` drains
it for non-streaming callers.

Message/thinking deltas are buffered and stored once per turn (not per delta);
tool activity + approvals get one step each — matching the run_steps design.

TD-03-T4 suspension: when a Hermes backend fires ``request_permission`` it emits
an ``approval_required`` event; ``stream_agent_run`` persists an ``approvals`` row
(via ``_persist_run_approval``) and transitions the run to ``waiting_user`` /
``waiting_clarify``. The injected resolver polls that durable row while the ACP
connection remains open. The approval API only records a decision; RunService
observes it and resumes the run in place, even when no process-local Future exists.
"""

from __future__ import annotations

import asyncio
import json as _json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Protocol

from app.core.database import Database
from app.runtime.hermes_client import RunContext
from app.runtime.business_tools_auth import create_business_tool_token
from app.runtime.company_tools_auth import create_company_tool_token
from app.services.business_actions import enabled_business_tools
from app.runtime.runs import (
    RunStatus,
    RunStepType,
    append_run_step,
    create_run,
    get_run,
    transition_run,
)
from app.services.workspace import add_message


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class RunBackend(Protocol):
    def run(self, ctx: RunContext, *, permission_resolver: Any = None): ...


def make_bridge_resolver(conn: Database | None = None):
    """Poll the durable approval decision instead of relying on a local Future."""
    if conn is None:
        raise ValueError("database-backed approval resolver requires a connection")

    async def resolver(info: dict) -> str:
        timeout = 50
        from app.core.config import settings

        timeout = settings.approval_bridge_timeout_seconds
        elapsed = 0.0
        while elapsed < timeout:
            row = conn.execute(
                "SELECT status, payload_json FROM approvals WHERE id = ?",
                (info["approval_id"],),
            ).fetchone()
            if row and row["status"] in ("approved", "answered", "rejected", "expired"):
                if row["status"] not in ("approved", "answered"):
                    return "deny"
                import json as _json

                payload = _json.loads(row["payload_json"] or "{}")
                return "allow_always" if payload.get("scope") == "always" else "allow_once"
            await asyncio.sleep(0.25)
            elapsed += 0.25
        conn.execute(
            """UPDATE approvals SET status = 'expired', resolved_at = ?
            WHERE id = ? AND status = 'pending'""",
            (_now_iso(), info["approval_id"]),
        )
        conn.commit()
        return "deny"

    return resolver


def _persist_run_approval(
    conn: Database, *, approval_id: str, ctx: RunContext, run_id: str,
    category: str, payload: dict,
) -> None:
    """Insert a pending approval row keyed to this exact permission request."""
    import json as _json

    tool = payload.get("tool_call") or {}
    tool_name = str(tool.get("title") or tool.get("name") or "高风险动作")
    approval_payload: dict = {}
    if category == "clarification":
        # ADR 0008 §4/item 7: no real trigger source — the `clarify` toolset
        # was never exposed to the model over ACP, and the SOUL no longer
        # instructs agents to look for it (they just ask in normal chat
        # instead). Kept only so historical/seeded rows of this type still
        # render; a future business-tool gate (⑤, independent TD) could
        # reintroduce a real trigger here.
        title = "员工请求澄清"
        description = str(tool.get("question") or tool.get("text") or tool_name)
        approval_type, risk = "clarification", "low"
    elif category == "capability_upgrade":
        # TD-06-T2: agent hit a capability gap and asked to be upgraded.
        # ADR 0008 §5/item 7: same as above — SOUL no longer tells agents to
        # self-request this (no real `clarify` trigger existed anyway). The
        # real path is owner-initiated: see POST /api/agents/{id}/capabilities
        # in workspace.py, which calls execute_upgrade() directly without a
        # pending approval row. This branch stays for historical/seeded rows.
        title = "员工申请能力升级"
        description = str(tool.get("capability_description") or tool.get("text") or tool_name)
        approval_type, risk = "capability_upgrade", "medium"
        approval_payload = {
            "capability_description": description,
            "suggested_capability_key": tool.get("suggested_capability_key"),
        }
    else:
        title = f"高风险动作需确认：{tool_name}"
        description = str(tool.get("text") or tool_name)
        approval_type, risk = "high_risk", "high"
    conn.execute(
        """
        INSERT INTO approvals (
          id, workspace_id, task_id, conversation_id, agent_id,
          title, description, status, risk_level, type, run_id, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
        """,
        (
            approval_id,
            ctx.workspace_id,
            ctx.task_id or None,
            ctx.conversation_id or None,
            ctx.agent_id or None,
            title,
            description,
            risk,
            approval_type,
            run_id,
            _json.dumps(approval_payload, ensure_ascii=False),
            _now_iso(),
        ),
    )


def resolve_hermes_profile(conn: Database, agent_id: str) -> str | None:
    """Return the ready Hermes profile for an agent, or None when unavailable."""
    row = conn.execute(
        "SELECT hermes_profile, status FROM agent_specs WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    if row and row["hermes_profile"] and row["status"] == "ready":
        return row["hermes_profile"]
    return None


def _chunk_text(payload: dict) -> str:
    content = payload.get("content") or {}
    if isinstance(content, dict):
        return content.get("text", "") or ""
    return ""


async def stream_agent_run(
    conn: Database,
    *,
    ctx: RunContext,
    backend: RunBackend,
    input_message_id: str | None,
    permission_resolver: Any = None,
    persist_message: bool = True,
    existing_run_id: str | None = None,
) -> AsyncIterator[dict]:
    """Drive one run, persist run_steps + the reply, and yield route events.

    Yields dicts shaped for the SSE route:
      {"type": "chunk", "content": str}      per message delta
      {"type": "tool_call"|"tool_result"|"approval_required", "payload": dict}
      {"type": "message", "message": <row|None>}   once, at the end
    """
    if existing_run_id:
        existing = get_run(conn, existing_run_id)
        if existing is None or existing["status"] not in (
            RunStatus.QUEUED,
            RunStatus.LEASED,
        ):
            raise ValueError("existing run must be queued or leased")
        if ctx.task_id and existing["task_id"] != ctx.task_id:
            raise ValueError("existing run does not belong to task")
        run_id = existing_run_id
    else:
        run_id = create_run(
            conn,
            workspace_id=ctx.workspace_id,
            conversation_id=ctx.conversation_id,
            agent_id=ctx.agent_id,
            input_message_id=input_message_id,
            task_id=ctx.task_id or None,
            hermes_profile_id=ctx.profile,
            workdir=ctx.workdir,
            provider="hermes",
            status=RunStatus.QUEUED,
        )
    ctx.run_id = run_id
    if (
        ctx.workspace_id
        and ctx.agent_id
        and ctx.conversation_id
        and not ctx.task_id
    ):
        if not any(server.get("name") == "agentpulse-company" for server in ctx.mcp_servers):
            token = create_company_tool_token(
                workspace_id=ctx.workspace_id,
                conversation_id=ctx.conversation_id,
                plan_id=None,
                task_id=None,
                run_id=run_id,
                agent_id=ctx.agent_id,
            )
            from app.core.config import settings

            ctx.mcp_servers.append(
                {
                    "name": "agentpulse-company",
                    "url": settings.company_tools_url,
                    "headers": {"Authorization": f"Bearer {token}"},
                }
            )
    if ctx.context_manifest_id:
        from app.services.company_memory import attach_manifest_to_run

        attach_manifest_to_run(conn, ctx.context_manifest_id, run_id)
        conn.execute(
            "UPDATE runs SET context_manifest_id = ? WHERE id = ?",
            (ctx.context_manifest_id, run_id),
        )
    if ctx.workspace_id:
        from app.services.model_credentials import runtime_model_environment

        ctx.environment.update(runtime_model_environment(conn, ctx.workspace_id))
    business_tools = enabled_business_tools(conn, ctx.agent_id) if ctx.agent_id else []
    if business_tools and ctx.workspace_id and ctx.conversation_id:
        from app.core.config import settings

        token = create_business_tool_token(
            workspace_id=ctx.workspace_id,
            conversation_id=ctx.conversation_id,
            run_id=run_id,
            agent_id=ctx.agent_id,
            task_id=ctx.task_id or None,
        )
        if not any(server.get("name") == "agentpulse-business" for server in ctx.mcp_servers):
            ctx.mcp_servers.append(
                {
                    "name": "agentpulse-business",
                    "url": settings.business_tools_url,
                    "headers": {"Authorization": f"Bearer {token}"},
                }
            )
        ctx.prompt += (
            "\n\n本次可用的受控业务工具："
            + "、".join(business_tools)
            + "。外部动作必须调用这些工具，不能用终端或网页绕过审批。"
        )
    transition_run(conn, run_id, RunStatus.RUNNING)
    conn.execute(
        "UPDATE runs SET started_at = COALESCE(started_at, ?) WHERE id = ?",
        (_now_iso(), run_id),
    )
    conn.commit()

    # The approval row and wait-state transition are persisted by the event
    # handler below; this resolver only waits for the durable decision.
    if permission_resolver is None and ctx.workspace_id and ctx.conversation_id:
        permission_resolver = make_bridge_resolver(conn)

    message_parts: list[str] = []
    thought_parts: list[str] = []
    usage: dict = {}
    error: str | None = None
    atomic_tool_active = False
    paused = False

    try:
        async for event in backend.run(ctx, permission_resolver=permission_resolver):
            etype = event.type
            current = get_run(conn, run_id)
            if (
                etype != "approval_required"
                and current
                and current["status"] in (RunStatus.WAITING_USER, RunStatus.WAITING_CLARIFY)
            ):
                transition_run(conn, run_id, RunStatus.RUNNING)
            if etype == "message":
                text = _chunk_text(event.payload)
                if text:
                    message_parts.append(text)
                    yield {"type": "chunk", "content": text}
            elif etype == "thinking":
                thought_parts.append(_chunk_text(event.payload))
            elif etype == "tool_call":
                atomic_tool_active = True
                append_run_step(
                    conn, run_id=run_id, type=RunStepType.TOOL_CALL,
                    title=str(event.payload.get("title", "")), payload=event.payload,
                )
                # Release SQLite's write lock before Hermes invokes the tool.
                # Company MCP handlers and the lease heartbeat use independent
                # connections while this run remains active.
                conn.commit()
                yield {"type": "tool_call", "payload": event.payload}
            elif etype == "tool_result":
                atomic_tool_active = False
                append_run_step(
                    conn, run_id=run_id, type=RunStepType.TOOL_RESULT,
                    payload=event.payload,
                )
                conn.commit()
                yield {"type": "tool_result", "payload": event.payload}
            elif etype == "approval_required":
                append_run_step(
                    conn, run_id=run_id, type=RunStepType.APPROVAL_REQUIRED,
                    payload=event.payload,
                )
                approval_id = event.payload.get("approval_id")
                category = event.payload.get("category", "high_risk")
                if approval_id:
                    # TD-03-T4: persist an approval and suspend the run; the ACP
                    # session stays paused while the resolver polls the row until
                    # the owner resolves, then resumes this same run in place.
                    _persist_run_approval(
                        conn, approval_id=approval_id, ctx=ctx, run_id=run_id,
                        category=category, payload=event.payload,
                    )
                    waiting = (
                        RunStatus.WAITING_CLARIFY
                        if category == "clarification"
                        else RunStatus.WAITING_USER
                    )
                    transition_run(conn, run_id, waiting)
                    conn.commit()
                yield {"type": "approval_required", "payload": event.payload}
            elif etype == "usage":
                usage = event.payload
            elif etype == "session":
                session_id = event.payload.get("session_id")
                if session_id:
                    conn.execute(
                        "UPDATE runs SET hermes_session_id = ? WHERE id = ?",
                        (session_id, run_id),
                    )
                    conn.commit()
            elif etype == "error":
                error = str(event.payload.get("detail", "unknown error"))
            current = get_run(conn, run_id)
            if (
                current
                and current.get("pause_requested_at")
                and not atomic_tool_active
                and etype not in ("approval_required", "error", "final")
            ):
                transition_run(conn, run_id, RunStatus.PAUSING)
                checkpoint = {
                    "message_text": "".join(message_parts)[-8000:],
                    "thought_text": "".join(thought_parts)[-4000:],
                    "atomic_tool_active": False,
                    "paused_after_event": etype,
                }
                conn.execute(
                    "UPDATE runs SET checkpoint_json = ? WHERE id = ?",
                    (_json.dumps(checkpoint, ensure_ascii=False), run_id),
                )
                transition_run(conn, run_id, RunStatus.PAUSED)
                if ctx.task_id:
                    conn.execute(
                        """UPDATE tasks SET workflow_status = 'paused',
                        waiting_reason = 'preempted', checkpoint_json = ?, updated_at = ?
                        WHERE id = ?""",
                        (_json.dumps(checkpoint, ensure_ascii=False), _now_iso(), ctx.task_id),
                    )
                append_run_step(
                    conn,
                    run_id=run_id,
                    type=RunStepType.STATUS,
                    status=RunStatus.PAUSED,
                    title="已在安全点暂停",
                    payload=checkpoint,
                )
                conn.commit()
                paused = True
                break
    except Exception as exc:  # backend/transport failure
        error = str(exc)

    if paused:
        yield {"type": "paused", "run_id": run_id}
        yield {"type": "message", "message": None}
        return

    thought = "".join(thought_parts).strip()
    if thought:
        append_run_step(
            conn, run_id=run_id, type=RunStepType.THINKING, detail=thought[:4000]
        )

    text = "".join(message_parts).strip()
    message_row = None
    if text:
        append_run_step(
            conn, run_id=run_id, type=RunStepType.MESSAGE, detail=text[:4000]
        )
        if persist_message and ctx.conversation_id:
            message_row = add_message(
                conn,
                conversation_id=ctx.conversation_id,
                sender_type="agent",
                sender_id=ctx.agent_id,
                content=text,
                provider="hermes",
                model="",
            )

    final_status = RunStatus.FAILED if error else RunStatus.COMPLETED
    append_run_step(
        conn, run_id=run_id, type=RunStepType.FINAL, status=final_status,
        payload={"usage": usage, "error": error} if (usage or error) else {},
    )
    transition_run(
        conn, run_id, final_status, error=error,
        output_message_id=(message_row["id"] if message_row else None),
    )
    if ctx.workspace_id and ctx.agent_id and text:
        try:
            from app.services.company_memory import record_run_observation

            record_run_observation(
                conn,
                workspace_id=ctx.workspace_id,
                agent_id=ctx.agent_id,
                run_id=run_id,
                conversation_id=ctx.conversation_id or None,
                task_id=ctx.task_id or None,
                reply=text,
                status=final_status,
            )
        except Exception:
            # The run result remains authoritative even if a derived memory
            # projection cannot be written on a legacy test database.
            pass
    if final_status == RunStatus.COMPLETED and ctx.agent_id:
        # TD-06-T1: count completed runs toward skill reflection (a background
        # tick runs the actual, expensive reflection off the hot path).
        try:
            from app.runtime.reflection import bump_reflection_counter

            bump_reflection_counter(conn, ctx.agent_id)
        except Exception:
            pass
    conn.commit()
    yield {"type": "message", "message": message_row}


async def start_run(
    conn: Database,
    *,
    ctx: RunContext,
    backend: RunBackend,
    input_message_id: str | None,
    permission_resolver: Any = None,
    persist_message: bool = True,
    existing_run_id: str | None = None,
) -> dict:
    """Non-streaming convenience: drain stream_agent_run, return a summary."""
    text_parts: list[str] = []
    message_id: str | None = None
    async for ev in stream_agent_run(
        conn,
        ctx=ctx,
        backend=backend,
        input_message_id=input_message_id,
        permission_resolver=permission_resolver,
        persist_message=persist_message,
        existing_run_id=existing_run_id,
    ):
        if ev["type"] == "chunk":
            text_parts.append(ev["content"])
        elif ev["type"] == "message" and ev.get("message"):
            message_id = ev["message"]["id"]
    run = get_run(conn, ctx.run_id)
    return {
        "run_id": ctx.run_id,
        "message_id": message_id,
        "text": "".join(text_parts).strip(),
        "status": run["status"] if run else RunStatus.FAILED,
    }
