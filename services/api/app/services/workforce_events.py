"""Immutable workforce event projection and workspace SSE source."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator

from app.core.database import Database, connect
from app.services.company_memory import record_company_event
from app.services.workspace import now_iso


def emit_workforce_event(
    conn: Database,
    *,
    workspace_id: str,
    event_type: str,
    source_id: str,
    title: str,
    content: str = "",
    conversation_id: str | None = None,
    task_id: str | None = None,
    actor_agent_id: str | None = None,
    actor_user_id: str | None = None,
    notify_owner: bool = False,
    metadata: dict | None = None,
) -> dict:
    if not event_type.startswith("workforce_"):
        raise ValueError("workforce event types must use the workforce_ prefix")
    event_metadata = {"notify_owner": notify_owner, **(metadata or {})}
    return record_company_event(
        conn,
        workspace_id=workspace_id,
        event_type=event_type,
        source_id=source_id,
        title=title,
        content=content,
        conversation_id=conversation_id,
        task_id=task_id,
        actor_agent_id=actor_agent_id,
        actor_user_id=actor_user_id,
        metadata=event_metadata,
    )


def _serialize(row: dict) -> dict:
    payload = dict(row)
    payload["metadata"] = json.loads(row.get("metadata_json") or "{}")
    payload.pop("metadata_json", None)
    return payload


async def stream_workforce_events(
    *, workspace_id: str, after_event_id: str | None = None
) -> AsyncGenerator[dict | None, None]:
    cursor_time = now_iso()
    cursor_id = ""
    idle_seconds = 0
    if after_event_id:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT created_at, id FROM company_events WHERE id = ? AND workspace_id = ?",
                (after_event_id, workspace_id),
            ).fetchone()
            if row:
                cursor_time, cursor_id = row["created_at"], row["id"]
        finally:
            conn.close()
    while True:
        conn = connect()
        try:
            rows = conn.execute(
                """SELECT * FROM company_events WHERE workspace_id = ?
                AND event_type LIKE 'workforce_%'
                AND (created_at > ? OR (created_at = ? AND id > ?))
                ORDER BY created_at, id LIMIT 100""",
                (workspace_id, cursor_time, cursor_time, cursor_id),
            ).fetchall()
        finally:
            conn.close()
        if rows:
            idle_seconds = 0
            for row in rows:
                cursor_time, cursor_id = row["created_at"], row["id"]
                yield _serialize(row)
            continue
        await asyncio.sleep(1)
        idle_seconds += 1
        if idle_seconds >= 15:
            idle_seconds = 0
            yield None
