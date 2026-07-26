"""Atomic, resource-aware local Run claiming for TD-15."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.core.database import Database, Row
from app.orchestration.workforce import lock_schedule_key
from app.runtime.runs import RunStatus
from app.schemas.workforce import LocalRunClaimIn
from app.services.workspace import now_iso


def _lease_expiry() -> str:
    return (
        datetime.now(UTC) + timedelta(seconds=settings.task_run_lease_seconds)
    ).isoformat()


def claim_local_runs(
    conn: Database,
    *,
    device: Row,
    payload: LocalRunClaimIn,
) -> list[dict]:
    if conn.dialect == "sqlite" and not conn.conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    now = now_iso()
    lock_schedule_key(conn, f"local-device:{device['id']}")
    sql = """SELECT r.* FROM runs r
    WHERE r.workspace_id = ? AND r.execution_target = 'local_desktop'
      AND r.status = 'queued' AND (r.device_id = ? OR r.device_id IS NULL)
      AND (r.lease_expires_at IS NULL OR r.lease_expires_at < ?)
      AND NOT EXISTS (
        SELECT 1 FROM runs active WHERE active.agent_id = r.agent_id
          AND active.id <> r.id AND active.status IN ('leased','running','pausing')
      )
    ORDER BY r.created_at, r.id LIMIT ?"""
    if conn.dialect == "postgres":
        sql += " FOR UPDATE OF r SKIP LOCKED"
    candidates = conn.execute(
        sql,
        (device["workspace_id"], device["id"], now, max(payload.max_runs * 8, 32)),
    ).fetchall()
    available = {
        (item.resource_type, item.resource_key, item.mode)
        for item in payload.available_resources
    }
    selected: list[dict] = []
    selected_agents: set[str] = set()
    active_rows = conn.execute(
        """SELECT resource_requirements_json FROM runs WHERE device_id = ?
        AND status IN ('leased','running','pausing')""",
        (device["id"],),
    ).fetchall()
    computer_use_taken = any(
        any(
            str(requirement.get("resource_type")) == "computer_use"
            for requirement in json.loads(row["resource_requirements_json"] or "[]")
        )
        for row in active_rows
    )
    for candidate in candidates:
        if candidate["agent_id"] in selected_agents:
            continue
        lock_schedule_key(conn, f"agent:{candidate['agent_id']}")
        requirements = json.loads(candidate.get("resource_requirements_json") or "[]")
        if candidate.get("task_id"):
            requirements.extend(
                dict(row)
                for row in conn.execute(
                    """SELECT resource_type, resource_key, mode
                    FROM task_resource_requirements WHERE task_id = ?""",
                    (candidate["task_id"],),
                ).fetchall()
            )
        supported = True
        for requirement in requirements:
            resource_type = str(requirement.get("resource_type") or "")
            resource_key = str(requirement.get("resource_key") or "")
            mode = str(requirement.get("mode") or "exclusive")
            matches = any(
                offered_type == resource_type
                and (resource_key in ("", "*") or offered_key == resource_key)
                and not (mode == "exclusive" and offered_mode != "exclusive")
                for offered_type, offered_key, offered_mode in available
            )
            if not matches or (resource_type == "computer_use" and computer_use_taken):
                supported = False
                break
        if not supported:
            continue
        conn.execute(
            """UPDATE runs SET status = 'leased', device_id = ?, lease_owner = ?,
            lease_expires_at = ?, runtime_status = 'leased'
            WHERE id = ? AND status = 'queued'
              AND NOT EXISTS (
                SELECT 1 FROM runs active WHERE active.agent_id = runs.agent_id
                  AND active.id <> runs.id
                  AND active.status IN ('leased','running','pausing')
              )""",
            (device["id"], device["id"], _lease_expiry(), candidate["id"]),
        )
        claimed = conn.execute(
            "SELECT * FROM runs WHERE id = ?", (candidate["id"],)
        ).fetchone()
        if (
            not claimed
            or claimed["status"] != RunStatus.LEASED
            or claimed["lease_owner"] != device["id"]
        ):
            continue
        selected.append(dict(claimed))
        selected_agents.add(candidate["agent_id"])
        if any(
            str(item.get("resource_type")) == "computer_use"
            for item in requirements
        ):
            computer_use_taken = True
        if len(selected) >= payload.max_runs:
            break
    return selected
