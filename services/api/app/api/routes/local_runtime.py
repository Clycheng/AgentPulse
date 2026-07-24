from __future__ import annotations

import hmac
import json
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException

from app.api.deps import get_current_user, get_workspace_id
from app.core.config import settings
from app.core.database import Database, Row, get_db
from app.runtime.local_device_auth import (
    create_local_device_token,
    decode_local_device_token,
    hash_device_token,
)
from app.schemas.local_runtime import (
    LocalDeviceHeartbeatIn,
    LocalDeviceRegisterIn,
    LocalProjectAuthorizeIn,
    LocalProjectCreateIn,
    RunCompleteIn,
    RunEventIn,
    RunFailIn,
)
from app.runtime.runs import RunStatus, append_run_step, transition_run
from app.runtime.runs import list_run_steps
from app.runtime.runner import resolve_hermes_profile
from app.orchestration.capability_catalog import CATALOG
from app.services.execution_receipts import get_receipt
from app.services.workspace import add_message
from app.services.workspace import new_id, now_iso


router = APIRouter(tags=["local-runtime"])


def _runtime_capability_status(
    *,
    capability_key: str,
    authorized: bool,
    profile_ready: bool,
    worker_online: bool,
    project_available: bool,
) -> str:
    if not authorized:
        return "unauthorized"
    capability = CATALOG[capability_key]
    local_tools = {"file", "terminal", "browser", "computer_use"}
    if capability.toolsets and set(capability.toolsets) & local_tools:
        if not worker_online:
            return "waiting_worker"
        if not project_available and "file" in capability.toolsets:
            return "waiting_project"
        if "computer_use" in capability.toolsets:
            return "approval_required"
    if not profile_ready and capability_key not in {
        "task_delegation",
        "meeting_scheduling",
        "project_reporting",
    }:
        return "blocked"
    if capability.risk_gate in {"approval", "prohibited_auto"}:
        return "approval_required"
    return "runtime_ready"


def _lease_expiry() -> str:
    return (datetime.now(UTC) + timedelta(seconds=settings.task_run_lease_seconds)).isoformat()


def _device_for_user(
    conn: Database, device_id: str, user_id: str, workspace_id: str
) -> Row:
    row = conn.execute(
        """SELECT * FROM local_devices
        WHERE id = ? AND user_id = ? AND workspace_id = ?""",
        (device_id, user_id, workspace_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="本机设备不存在")
    return row


def _device_from_token(
    conn: Database,
    authorization: str | None,
    expected_device_id: str | None = None,
) -> Row:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少本机 Worker Token")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        claims = decode_local_device_token(token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if expected_device_id and claims.get("device_id") != expected_device_id:
        raise HTTPException(status_code=403, detail="设备 Token 与设备不匹配")
    row = conn.execute(
        "SELECT * FROM local_devices WHERE id = ? AND workspace_id = ?",
        (claims["device_id"], claims["workspace_id"]),
    ).fetchone()
    if row is None or row["status"] == "revoked":
        raise HTTPException(status_code=403, detail="设备已撤销")
    if not hmac.compare_digest(hash_device_token(token), row["device_token_hash"]):
        raise HTTPException(status_code=403, detail="设备 Token 已失效")
    return row


def _serialize_device(
    row: Row, token: str | None = None, expires_at: int | None = None
) -> dict:
    payload = {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "device_name": row["device_name"],
        "platform": row["platform"],
        "architecture": row["architecture"],
        "worker_version": row["worker_version"],
        "hermes_version": row["hermes_version"],
        "status": row["status"],
        "last_heartbeat_at": row["last_heartbeat_at"],
        "capabilities": json.loads(row["capabilities_json"] or "{}"),
    }
    if token is not None:
        payload["device_token"] = token
        payload["token_expires_at"] = expires_at
    return payload


@router.post("/local-devices/register")
def register_device(
    payload: LocalDeviceRegisterIn,
    current_user: Row = Depends(get_current_user),
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> dict:
    device_id = new_id("device")
    now = now_iso()
    token, expires_at = create_local_device_token(
        workspace_id=workspace_id,
        user_id=current_user["id"],
        device_id=device_id,
    )
    conn.execute(
        """INSERT INTO local_devices (
          id, workspace_id, user_id, device_name, platform, architecture,
          worker_version, hermes_version, status, device_token_hash,
          token_expires_at, last_heartbeat_at, capabilities_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'online', ?, ?, ?, ?, ?, ?)""",
        (
            device_id,
            workspace_id,
            current_user["id"],
            payload.device_name,
            payload.platform,
            payload.architecture,
            payload.worker_version,
            payload.hermes_version,
            hash_device_token(token),
            str(expires_at),
            now,
            json.dumps(payload.capabilities, ensure_ascii=False),
            now,
            now,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM local_devices WHERE id = ?", (device_id,)
    ).fetchone()
    return _serialize_device(row, token, expires_at)


@router.post("/local-devices/{device_id}/heartbeat")
def heartbeat_device(
    device_id: str,
    payload: LocalDeviceHeartbeatIn,
    authorization: str | None = Header(default=None),
    conn: Database = Depends(get_db),
) -> dict:
    row = _device_from_token(conn, authorization, device_id)
    now = now_iso()
    token, expires_at = create_local_device_token(
        workspace_id=row["workspace_id"],
        user_id=row["user_id"],
        device_id=device_id,
    )
    conn.execute(
        """UPDATE local_devices SET status = 'online',
          worker_version = COALESCE(?, worker_version),
          hermes_version = COALESCE(?, hermes_version),
          capabilities_json = COALESCE(?, capabilities_json),
          device_token_hash = ?, token_expires_at = ?, last_heartbeat_at = ?, updated_at = ?
        WHERE id = ?""",
        (
            payload.worker_version,
            payload.hermes_version,
            json.dumps(payload.capabilities, ensure_ascii=False)
            if payload.capabilities is not None
            else None,
            hash_device_token(token),
            str(expires_at),
            now,
            now,
            device_id,
        ),
    )
    conn.commit()
    updated = conn.execute(
        "SELECT * FROM local_devices WHERE id = ?", (device_id,)
    ).fetchone()
    return _serialize_device(updated, token, expires_at)


@router.get("/local-devices/{device_id}/status")
def device_status(
    device_id: str,
    current_user: Row = Depends(get_current_user),
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> dict:
    row = _device_for_user(conn, device_id, current_user["id"], workspace_id)
    return _serialize_device(row)


@router.post("/local-devices/{device_id}/disconnect")
def disconnect_device(
    device_id: str,
    current_user: Row = Depends(get_current_user),
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> dict:
    _device_for_user(conn, device_id, current_user["id"], workspace_id)
    conn.execute(
        "UPDATE local_devices SET status = 'revoked', updated_at = ? WHERE id = ?",
        (now_iso(), device_id),
    )
    conn.commit()
    return {"id": device_id, "status": "revoked"}


@router.get("/local-devices/{device_id}/runs/next")
def next_local_run(
    device_id: str,
    authorization: str | None = Header(default=None),
    conn: Database = Depends(get_db),
) -> dict:
    device = _device_from_token(conn, authorization, device_id)
    run = conn.execute(
        """SELECT * FROM runs
        WHERE workspace_id = ? AND execution_target = 'local_desktop'
          AND status = 'queued' AND (device_id = ? OR device_id IS NULL)
        ORDER BY created_at, id LIMIT 1""",
        (device["workspace_id"], device_id),
    ).fetchone()
    return {"run": dict(run) if run else None}


@router.post("/local-projects")
def create_local_project(
    payload: LocalProjectCreateIn,
    current_user: Row = Depends(get_current_user),
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> dict:
    _device_for_user(conn, payload.device_id, current_user["id"], workspace_id)
    project_id = new_id("project")
    now = now_iso()
    scopes = payload.allowed_scopes or ["read"]
    conn.execute(
        """INSERT INTO local_projects (
          id, workspace_id, device_id, display_name, path_hash,
          allowed_scopes_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            project_id,
            workspace_id,
            payload.device_id,
            payload.display_name,
            payload.path_hash.lower(),
            json.dumps(scopes),
            now,
            now,
        ),
    )
    conn.commit()
    return dict(
        conn.execute(
            "SELECT * FROM local_projects WHERE id = ?", (project_id,)
        ).fetchone()
    )


@router.get("/local-projects")
def list_local_projects(
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM local_projects
        WHERE workspace_id = ? AND active = 1 ORDER BY created_at""",
        (workspace_id,),
    ).fetchall()
    return [dict(row) for row in rows]


@router.delete("/local-projects/{project_id}")
def delete_local_project(
    project_id: str,
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> dict:
    conn.execute(
        """UPDATE local_projects SET active = 0, updated_at = ?
        WHERE id = ? AND workspace_id = ?""",
        (now_iso(), project_id, workspace_id),
    )
    conn.commit()
    return {"id": project_id, "active": False}


@router.post("/local-projects/{project_id}/authorize")
def authorize_local_project(
    project_id: str,
    payload: LocalProjectAuthorizeIn,
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> dict:
    conn.execute(
        """UPDATE local_projects SET allowed_scopes_json = ?, updated_at = ?
        WHERE id = ? AND workspace_id = ? AND active = 1""",
        (
            json.dumps(payload.allowed_scopes or ["read"]),
            now_iso(),
            project_id,
            workspace_id,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM local_projects WHERE id = ? AND workspace_id = ?",
        (project_id, workspace_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="本机项目不存在")
    return dict(row)


@router.get("/local-runtime/status")
def local_runtime_status(
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> dict:
    devices = conn.execute(
        """SELECT * FROM local_devices
        WHERE workspace_id = ? AND status <> 'revoked'
        ORDER BY updated_at DESC""",
        (workspace_id,),
    ).fetchall()
    projects = conn.execute(
        """SELECT * FROM local_projects
        WHERE workspace_id = ? AND active = 1 ORDER BY created_at""",
        (workspace_id,),
    ).fetchall()
    from app.services.local_runtime import online_device

    return {
        "devices": [_serialize_device(row) for row in devices],
        "projects": [dict(row) for row in projects],
        "online": online_device(conn, workspace_id) is not None,
    }


@router.get("/agents/{agent_id}/runtime-capabilities")
def agent_runtime_capabilities(
    agent_id: str,
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> dict:
    """Return authorization and current execution readiness separately.

    The capability catalog is static policy. The rows on the employee are
    authorization. A connected desktop worker and an authorized project are
    runtime prerequisites, so the UI must never collapse these into one
    misleading "enabled" flag.
    """
    agent = conn.execute(
        "SELECT id, name, role FROM agents WHERE id = ? AND workspace_id = ?",
        (agent_id, workspace_id),
    ).fetchone()
    if agent is None:
        raise HTTPException(status_code=404, detail="员工不存在")
    capability_rows = {
        row["capability_key"]: row
        for row in conn.execute(
            "SELECT * FROM agent_capabilities WHERE agent_id = ?",
            (agent_id,),
        ).fetchall()
    }
    profile_ready = resolve_hermes_profile(conn, agent_id) is not None
    devices = conn.execute(
        "SELECT * FROM local_devices WHERE workspace_id = ? AND status = 'online'",
        (workspace_id,),
    ).fetchall()
    from app.services.local_runtime import online_device

    worker_online = online_device(conn, workspace_id) is not None
    project_available = bool(
        conn.execute(
            "SELECT 1 FROM local_projects WHERE workspace_id = ? AND active = 1 LIMIT 1",
            (workspace_id,),
        ).fetchone()
    )
    capabilities = []
    for key, definition in CATALOG.items():
        row = capability_rows.get(key)
        authorized = row is not None and row["status"] not in {"disabled", "revoked"}
        capabilities.append(
            {
                "key": key,
                "description": definition.description,
                "authorized": authorized,
                "authorization_status": row["status"] if row else "not_granted",
                "runtime_status": _runtime_capability_status(
                    capability_key=key,
                    authorized=authorized,
                    profile_ready=profile_ready,
                    worker_online=worker_online,
                    project_available=project_available,
                ),
                "risk_gate": definition.risk_gate,
                "toolsets": list(definition.toolsets),
                "required_credentials": list(definition.required_credentials),
            }
        )
    return {
        "agent": {"id": agent["id"], "name": agent["name"], "role": agent["role"]},
        "profile_ready": profile_ready,
        "worker_online": worker_online,
        "project_available": project_available,
        "capabilities": capabilities,
    }


def _run_for_device(conn: Database, run_id: str, device: Row) -> Row:
    row = conn.execute(
        "SELECT * FROM runs WHERE id = ? AND workspace_id = ?",
        (run_id, device["workspace_id"]),
    ).fetchone()
    if row is None or row["device_id"] not in (None, device["id"]):
        raise HTTPException(status_code=404, detail="Run 不属于当前设备")
    return row


@router.post("/runs/{run_id}/lease")
def lease_run(
    run_id: str,
    authorization: str | None = Header(default=None),
    conn: Database = Depends(get_db),
) -> dict:
    device = _device_from_token(conn, authorization)
    run = _run_for_device(conn, run_id, device)
    if run["status"] == RunStatus.QUEUED:
        conn.execute(
            """UPDATE runs SET status = 'running', device_id = ?, lease_owner = ?,
            lease_expires_at = ?, started_at = COALESCE(started_at, ?)
            WHERE id = ? AND status = 'queued'""",
            (device["id"], device["id"], _lease_expiry(), now_iso(), run_id),
        )
    elif run["status"] == RunStatus.RUNNING and run["lease_owner"] == device["id"]:
        conn.execute(
            "UPDATE runs SET lease_expires_at = ? WHERE id = ?",
            (_lease_expiry(), run_id),
        )
    else:
        raise HTTPException(status_code=409, detail="Run 当前不可领取")
    conn.commit()
    return dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())


@router.post("/runs/{run_id}/events")
def append_worker_event(
    run_id: str,
    payload: RunEventIn,
    authorization: str | None = Header(default=None),
    conn: Database = Depends(get_db),
) -> dict:
    device = _device_from_token(conn, authorization)
    run = _run_for_device(conn, run_id, device)
    if run["lease_owner"] != device["id"]:
        raise HTTPException(status_code=409, detail="Run lease 不属于当前设备")
    if payload.event_seq <= int(run["last_event_id"] or 0):
        return {"step_id": None, "event_seq": int(run["last_event_id"] or 0), "duplicate": True}
    step_id = append_run_step(
        conn,
        run_id=run_id,
        type=payload.type,
        status=payload.status,
        title=payload.title,
        detail=payload.detail,
        payload=payload.payload,
    )
    conn.execute(
        "UPDATE runs SET last_event_id = CASE WHEN last_event_id < ? THEN ? ELSE last_event_id END WHERE id = ?",
        (payload.event_seq, payload.event_seq, run_id),
    )
    conn.commit()
    return {"step_id": step_id, "event_seq": payload.event_seq}


@router.post("/runs/{run_id}/complete")
def complete_worker_run(
    run_id: str,
    payload: RunCompleteIn,
    authorization: str | None = Header(default=None),
    conn: Database = Depends(get_db),
) -> dict:
    device = _device_from_token(conn, authorization)
    run = _run_for_device(conn, run_id, device)
    if run["lease_owner"] != device["id"]:
        raise HTTPException(status_code=409, detail="Run lease 不属于当前设备")
    if not payload.message.strip() and not payload.output_message_id:
        raise HTTPException(status_code=400, detail="完成 Run 必须提供最终产出")
    output_message_id = payload.output_message_id
    if payload.message and not output_message_id and run["conversation_id"]:
        message = add_message(
            conn,
            conversation_id=run["conversation_id"],
            sender_type="agent",
            sender_id=run["agent_id"],
            content=payload.message,
            provider="hermes",
            model="",
        )
        output_message_id = message["id"]
    transition_run(
        conn,
        run_id,
        RunStatus.COMPLETED,
        output_message_id=output_message_id,
    )
    conn.execute(
        """UPDATE runs SET usage_json = ?, lease_owner = NULL,
        lease_expires_at = NULL WHERE id = ?""",
        (json.dumps(payload.usage, ensure_ascii=False), run_id),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())


@router.post("/runs/{run_id}/fail")
def fail_worker_run(
    run_id: str,
    payload: RunFailIn,
    authorization: str | None = Header(default=None),
    conn: Database = Depends(get_db),
) -> dict:
    device = _device_from_token(conn, authorization)
    run = _run_for_device(conn, run_id, device)
    if run["lease_owner"] != device["id"]:
        raise HTTPException(status_code=409, detail="Run lease 不属于当前设备")
    transition_run(conn, run_id, RunStatus.FAILED, error=payload.error)
    conn.execute(
        "UPDATE runs SET lease_owner = NULL, lease_expires_at = NULL WHERE id = ?",
        (run_id,),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())


@router.get("/runs/{run_id}/events")
def read_run_events(
    run_id: str,
    after_step_id: str | None = None,
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> dict:
    run = conn.execute(
        "SELECT * FROM runs WHERE id = ? AND workspace_id = ?",
        (run_id, workspace_id),
    ).fetchone()
    if run is None:
        raise HTTPException(status_code=404, detail="Run 不存在")
    return {"run": dict(run), "events": list_run_steps(conn, run_id, after_step_id=after_step_id)}


@router.get("/runs/{run_id}/execution-receipt")
def read_run_execution_receipt(
    run_id: str,
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> dict:
    run = conn.execute(
        "SELECT id, execution_receipt_id FROM runs WHERE id = ? AND workspace_id = ?",
        (run_id, workspace_id),
    ).fetchone()
    if run is None:
        raise HTTPException(status_code=404, detail="Run 不存在")
    receipts = conn.execute(
        "SELECT * FROM execution_receipts WHERE run_id = ? ORDER BY created_at, id",
        (run_id,),
    ).fetchall()
    return {
        "run_id": run_id,
        "execution_receipt_id": run["execution_receipt_id"],
        "receipts": [get_receipt(conn, row["id"]) for row in receipts],
    }
