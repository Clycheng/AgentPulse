"""TD-13 company world-model and context audit endpoints."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import get_db, get_workspace_id
from app.core.database import Database
from app.services.company_memory import (
    get_context_manifest,
    list_events,
    list_memories,
    list_relationships,
    rebuild_workspace_events,
    search_company_memory,
    send_internal_ping,
)


router = APIRouter(tags=["company-memory"])


class PingRequest(BaseModel):
    to_agent_id: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=4000)
    run_id: str | None = Field(default=None, max_length=80)


def _agent(conn: Database, workspace_id: str, agent_id: str):
    row = conn.execute(
        "SELECT * FROM agents WHERE id = ? AND workspace_id = ?",
        (agent_id, workspace_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="员工不存在")
    return row


@router.get("/memory/search")
def search_memory_route(
    q: str,
    agent_id: str | None = None,
    limit: int = 12,
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> list[dict]:
    if agent_id:
        _agent(conn, workspace_id, agent_id)
    return search_company_memory(
        conn,
        workspace_id=workspace_id,
        query=q,
        agent_id=agent_id,
        limit=limit,
    )


@router.get("/agents/{agent_id}/memory")
def list_agent_memory_route(
    agent_id: str,
    limit: int = 50,
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> list[dict]:
    _agent(conn, workspace_id, agent_id)
    return list_memories(
        conn, workspace_id=workspace_id, agent_id=agent_id, limit=limit
    )


@router.get("/agents/{agent_id}/relationships")
def list_agent_relationships_route(
    agent_id: str,
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> list[dict]:
    _agent(conn, workspace_id, agent_id)
    return list_relationships(conn, workspace_id=workspace_id, agent_id=agent_id)


@router.get("/runs/{run_id}/context")
def get_run_context_route(
    run_id: str,
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> dict:
    run = conn.execute(
        "SELECT * FROM runs WHERE id = ? AND workspace_id = ?",
        (run_id, workspace_id),
    ).fetchone()
    if run is None:
        raise HTTPException(status_code=404, detail="Run 不存在")
    manifest_id = run.get("context_manifest_id")
    if not manifest_id:
        row = conn.execute(
            """SELECT id FROM context_manifests
            WHERE workspace_id = ? AND run_id = ?
            ORDER BY created_at DESC LIMIT 1""",
            (workspace_id, run_id),
        ).fetchone()
        manifest_id = row["id"] if row else None
    if not manifest_id:
        return {"run_id": run_id, "manifest": None}
    return {
        "run_id": run_id,
        "manifest": get_context_manifest(
            conn, workspace_id=workspace_id, manifest_id=manifest_id
        ),
    }


@router.post("/agents/{agent_id}/ping")
def ping_agent_route(
    agent_id: str,
    payload: PingRequest,
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> dict:
    _agent(conn, workspace_id, agent_id)
    _agent(conn, workspace_id, payload.to_agent_id)
    try:
        return send_internal_ping(
            conn,
            workspace_id=workspace_id,
            from_agent_id=agent_id,
            to_agent_id=payload.to_agent_id,
            content=payload.content,
            run_id=payload.run_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/company-events")
def list_company_events_route(
    limit: int = 100,
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> list[dict]:
    result = list_events(conn, workspace_id=workspace_id, limit=limit)
    for item in result:
        try:
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        except (TypeError, ValueError):
            item["metadata"] = {}
    return result


@router.post("/company-events/rebuild-index")
def rebuild_company_events_route(
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> dict:
    return rebuild_workspace_events(conn, workspace_id)
