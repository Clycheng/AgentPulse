"""Hermes-only control Runs for structured internal decisions.

These Runs intentionally persist execution state but do not add a chat
message. They are used for product-level work such as discussion moderation and
team drafting, which must use the same Hermes runtime as employees rather than
calling a model provider from an API route.
"""

from __future__ import annotations

import os

from app.core.config import settings
from app.core.database import Database
from app.runtime.hermes_client import HermesBackend, RunContext
from app.runtime.runner import resolve_hermes_profile, start_run
from app.services.workspace import new_id


class HermesControlUnavailable(RuntimeError):
    """Raised when no real Hermes profile can execute a control operation."""


async def run_hermes_control(
    conn: Database,
    *,
    workspace_id: str,
    agent_id: str,
    conversation_id: str | None,
    prompt: str,
    purpose: str,
    input_message_id: str | None = None,
) -> str:
    """Run one non-chat Hermes operation and return its text output.

    The selected employee profile remains the runtime identity. ``purpose`` is
    included only in the isolated workdir name and error context, never shown
    as a fictional chat message.
    """
    profile = resolve_hermes_profile(conn, agent_id)
    if not profile:
        raise HermesControlUnavailable("负责该操作的 Hermes 员工尚未就绪")

    work_root = os.path.abspath(settings.hermes_work_root or ".hermes-data")
    ctx = RunContext(
        run_id="",
        prompt=prompt,
        workdir=os.path.join(
            work_root, profile, "control", purpose, new_id("run")
        ),
        profile=profile,
        agent_id=agent_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id or "",
    )
    result = await start_run(
        conn,
        ctx=ctx,
        backend=HermesBackend(hermes_bin=settings.hermes_bin),
        input_message_id=input_message_id,
        persist_message=False,
    )
    if result["status"] != "completed":
        raise HermesControlUnavailable(f"Hermes {purpose} 运行失败")
    text = result["text"].strip()
    if not text:
        raise HermesControlUnavailable(f"Hermes {purpose} 没有返回结果")
    return text
