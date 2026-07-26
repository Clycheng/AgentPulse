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
from app.runtime.company_tools_auth import create_company_tool_token
from app.schemas.local_runtime import (
    LocalDeviceHeartbeatIn,
    LocalDeviceRegisterIn,
    LocalProfilesSyncIn,
    LocalProjectAuthorizeIn,
    LocalProjectCreateIn,
    LocalReceiptFinishIn,
    LocalReceiptStartIn,
    LocalRunApprovalIn,
    LocalRunPauseCompleteIn,
    LocalRuntimeSessionIn,
    RunCompleteIn,
    RunEventIn,
    RunFailIn,
)
from app.schemas.workforce import LocalRunClaimIn
from app.runtime.runs import RunStatus, append_run_step, transition_run
from app.runtime.runs import list_run_steps
from app.runtime.runner import resolve_hermes_profile
from app.orchestration.capability_catalog import CATALOG
from app.services.execution_receipts import begin_receipt, finish_receipt, get_receipt
from app.services.local_runtime_profiles import build_profile_manifests
from app.services.local_runtime_session import (
    LocalRuntimeSessionError,
    seal_runtime_credential,
)
from app.services.local_runtime import retire_replaced_device
from app.services.local_run_claims import claim_local_runs as claim_local_runs_service
from app.services.model_credentials import (
    ModelCredentialError,
    ModelCredentialRequired,
    get_workspace_model_api_key,
    serialize_model_provider,
)
from app.services.workspace import add_message
from app.services.workspace import new_id, now_iso
from app.services.workforce import release_run_resources
from app.services.workforce_events import emit_workforce_event


router = APIRouter(tags=["local-runtime"])


def _runtime_capability_status(
    *,
    capability_key: str,
    authorized: bool,
    profile_ready: bool,
    worker_online: bool,
    project_available: bool,
    worker_capabilities: dict,
) -> str:
    if not authorized:
        return "unauthorized"
    capability = CATALOG[capability_key]
    local_tools = {"file", "terminal", "browser", "computer_use", "web", "code_execution"}
    runtime_requirements = {
        "file": "read_file",
        "terminal": "terminal",
        "browser": "browser",
        "computer_use": "computer_use",
        "web": "web",
        "code_execution": "code_execution",
    }
    if capability.toolsets and set(capability.toolsets) & local_tools:
        if not worker_online:
            return "waiting_worker"
        if any(
            not worker_capabilities.get(runtime_requirements[toolset], False)
            for toolset in capability.toolsets
            if toolset in runtime_requirements
        ):
            return "unsupported"
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
    retire_replaced_device(
        conn,
        workspace_id=workspace_id,
        user_id=current_user["id"],
        replaced_device_id=payload.replaces_device_id,
        updated_at=now,
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
    # A Worker can issue tool events while its heartbeat is in flight. Rotating
    # the database hash here invalidates those legitimate in-flight requests,
    # so a heartbeat only proves liveness. The desktop re-registers after this
    # short-lived token expires or is explicitly rejected.
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    claims = decode_local_device_token(token)
    now = now_iso()
    conn.execute(
        """UPDATE local_devices SET status = 'online',
          worker_version = COALESCE(?, worker_version),
          hermes_version = COALESCE(?, hermes_version),
          capabilities_json = COALESCE(?, capabilities_json),
          last_heartbeat_at = ?, updated_at = ?
        WHERE id = ?""",
        (
            payload.worker_version,
            payload.hermes_version,
            json.dumps(payload.capabilities, ensure_ascii=False)
            if payload.capabilities is not None
            else None,
            now,
            now,
            device_id,
        ),
    )
    conn.commit()
    updated = conn.execute(
        "SELECT * FROM local_devices WHERE id = ?", (device_id,)
    ).fetchone()
    return _serialize_device(updated, token, int(claims["exp"]))


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


@router.get("/local-runtime/bootstrap")
def local_runtime_bootstrap(
    current_user: Row = Depends(get_current_user),
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> dict:
    """Return a secret-free snapshot for the desktop-owned Hermes profiles."""
    return {
        "workspace_id": workspace_id,
        "user_id": current_user["id"],
        "runtime": {
            "hermes_version": "0.18.2",
            "model_provider": "deepseek",
            "model": serialize_model_provider(conn, workspace_id)["model"],
            "model_configured": serialize_model_provider(conn, workspace_id)["configured"],
        },
        "profiles": build_profile_manifests(conn, workspace_id),
    }


@router.post("/local-devices/{device_id}/profiles/sync")
def sync_local_profiles(
    device_id: str,
    payload: LocalProfilesSyncIn,
    authorization: str | None = Header(default=None),
    conn: Database = Depends(get_db),
) -> dict:
    """Persist profile sync receipts, never profile contents or credentials."""
    device = _device_from_token(conn, authorization, device_id)
    expected = {
        manifest["agent_id"]: manifest
        for manifest in build_profile_manifests(conn, device["workspace_id"])
    }
    saved: list[dict] = []
    for profile in payload.profiles:
        manifest = expected.get(profile.agent_id)
        if manifest is None:
            raise HTTPException(status_code=404, detail="员工不属于当前工作区")
        if (
            profile.profile_name != manifest["profile_name"]
            or profile.manifest_hash != manifest["manifest_hash"]
        ):
            raise HTTPException(status_code=409, detail="本机 profile 清单已过期，请重新同步")
        now = now_iso()
        conn.execute(
            """INSERT INTO local_profile_syncs (
              device_id, workspace_id, agent_id, profile_name, manifest_hash,
              status, error, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (device_id, agent_id) DO UPDATE SET
              profile_name = excluded.profile_name,
              manifest_hash = excluded.manifest_hash,
              status = excluded.status,
              error = excluded.error,
              synced_at = excluded.synced_at""",
            (
                device_id,
                device["workspace_id"],
                profile.agent_id,
                profile.profile_name,
                profile.manifest_hash,
                profile.status,
                profile.error,
                now,
            ),
        )
        saved.append(
            {
                "agent_id": profile.agent_id,
                "profile_name": profile.profile_name,
                "manifest_hash": profile.manifest_hash,
                "status": profile.status,
                "error": profile.error,
                "synced_at": now,
            }
        )
    conn.commit()
    return {"profiles": saved}


@router.get("/local-devices/{device_id}/profiles/status")
def local_profile_status(
    device_id: str,
    current_user: Row = Depends(get_current_user),
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> dict:
    _device_for_user(conn, device_id, current_user["id"], workspace_id)
    rows = conn.execute(
        """SELECT agent_id, profile_name, manifest_hash, status, error, synced_at
           FROM local_profile_syncs WHERE device_id = ? AND workspace_id = ?
           ORDER BY agent_id""",
        (device_id, workspace_id),
    ).fetchall()
    return {"device_id": device_id, "profiles": [dict(row) for row in rows]}


@router.post("/local-devices/{device_id}/runtime-session")
def create_local_runtime_session(
    device_id: str,
    payload: LocalRuntimeSessionIn,
    authorization: str | None = Header(default=None),
    conn: Database = Depends(get_db),
) -> dict:
    """Seal one workspace model key to the current worker's ephemeral key."""
    device = _device_from_token(conn, authorization, device_id)
    run = _run_for_device(conn, payload.run_id, device)
    if (
        run["execution_target"] != "local_desktop"
        or run["status"] != RunStatus.RUNNING
        or run["lease_owner"] != device_id
    ):
        raise HTTPException(status_code=409, detail="Run 当前不能申请本机运行凭证")
    profile = conn.execute(
        """SELECT status FROM local_profile_syncs
           WHERE device_id = ? AND workspace_id = ? AND agent_id = ?""",
        (device_id, device["workspace_id"], run["agent_id"]),
    ).fetchone()
    if profile is None or profile["status"] != "ready":
        raise HTTPException(status_code=409, detail="当前员工 profile 尚未同步到本机")
    try:
        api_key = get_workspace_model_api_key(conn, device["workspace_id"])
    except ModelCredentialRequired as exc:
        raise HTTPException(status_code=409, detail="请先配置 DeepSeek API Key") from exc
    except ModelCredentialError as exc:
        raise HTTPException(status_code=409, detail="模型凭证不可用，请重新配置") from exc
    model = serialize_model_provider(conn, device["workspace_id"])["model"]
    try:
        envelope = seal_runtime_credential(
            client_public_key=payload.client_public_key,
            workspace_id=device["workspace_id"],
            user_id=device["user_id"],
            device_id=device_id,
            run_id=payload.run_id,
            api_key=api_key,
            model=model,
            ttl_seconds=settings.local_runtime_session_ttl_seconds,
        )
    except LocalRuntimeSessionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    plan_id = None
    if run["task_id"]:
        task = conn.execute(
            "SELECT task_plan_id FROM tasks WHERE id = ?", (run["task_id"],)
        ).fetchone()
        plan_id = task["task_plan_id"] if task else None
    mcp_servers = []
    if not run["task_id"] or plan_id:
        company_token = create_company_tool_token(
            workspace_id=device["workspace_id"],
            conversation_id=run["conversation_id"],
            plan_id=plan_id,
            task_id=run["task_id"] or None,
            run_id=run["id"],
            agent_id=run["agent_id"],
        )
        mcp_servers.append(
            {
                "name": "agentpulse-company",
                "url": settings.company_tools_url,
                "headers": {"Authorization": f"Bearer {company_token}"},
            }
        )
    return {
        "run_id": payload.run_id,
        "workspace_id": device["workspace_id"],
        "device_id": device_id,
        "mcp_servers": mcp_servers,
        **envelope,
    }


@router.post("/local-devices/{device_id}/runs/claim")
def claim_local_runs(
    device_id: str,
    payload: LocalRunClaimIn,
    authorization: str | None = Header(default=None),
    conn: Database = Depends(get_db),
) -> dict:
    device = _device_from_token(conn, authorization, device_id)
    runs = claim_local_runs_service(conn, device=device, payload=payload)
    conn.commit()
    return {"runs": runs}


@router.get("/local-devices/{device_id}/runs/next")
def next_local_run(
    device_id: str,
    authorization: str | None = Header(default=None),
    conn: Database = Depends(get_db),
) -> dict:
    device = _device_from_token(conn, authorization, device_id)
    runs = claim_local_runs_service(
        conn,
        device=device,
        payload=LocalRunClaimIn(max_runs=1, available_resources=[]),
    )
    conn.commit()
    return {"run": runs[0] if runs else None}


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
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(device_id, path_hash) DO UPDATE SET
          display_name = excluded.display_name,
          allowed_scopes_json = excluded.allowed_scopes_json,
          active = 1,
          updated_at = excluded.updated_at""",
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
            """SELECT * FROM local_projects
            WHERE device_id = ? AND path_hash = ? AND workspace_id = ?""",
            (payload.device_id, payload.path_hash.lower(), workspace_id),
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
    from app.services.local_runtime import online_device

    local_device = online_device(conn, workspace_id)
    worker_online = local_device is not None
    worker_capabilities = (
        json.loads(local_device["capabilities_json"] or "{}")
        if local_device is not None
        else {}
    )
    local_profile_ready = bool(
        local_device
        and conn.execute(
            """SELECT 1 FROM local_profile_syncs
               WHERE device_id = ? AND workspace_id = ? AND agent_id = ?
                 AND status = 'ready'""",
            (local_device["id"], workspace_id, agent_id),
        ).fetchone()
    )
    profile_ready = local_profile_ready or resolve_hermes_profile(conn, agent_id) is not None
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
                    worker_capabilities=worker_capabilities,
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
    if run["status"] in (RunStatus.QUEUED, RunStatus.LEASED) and run["lease_owner"] in (
        None,
        device["id"],
    ):
        conn.execute(
            """UPDATE runs SET status = 'running', device_id = ?, lease_owner = ?,
            lease_expires_at = ?, started_at = COALESCE(started_at, ?),
            runtime_status = 'running'
            WHERE id = ? AND status IN ('queued','leased')""",
            (device["id"], device["id"], _lease_expiry(), now_iso(), run_id),
        )
    elif run["status"] == RunStatus.RUNNING and run["lease_owner"] == device["id"]:
        conn.execute(
            "UPDATE runs SET lease_expires_at = ?, runtime_status = 'running' WHERE id = ?",
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
    session_id = payload.payload.get("hermes_session_id")
    if session_id:
        conn.execute(
            "UPDATE runs SET hermes_session_id = ? WHERE id = ?",
            (str(session_id)[:500], run_id),
        )
    conn.commit()
    refreshed = conn.execute(
        "SELECT pause_requested_at FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    return {
        "step_id": step_id,
        "event_seq": payload.event_seq,
        "pause_requested": bool(refreshed and refreshed["pause_requested_at"]),
    }


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
        runtime_status = 'completed',
        lease_expires_at = NULL WHERE id = ?""",
        (json.dumps(payload.usage, ensure_ascii=False), run_id),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())


@router.post("/runs/{run_id}/pause-complete")
def complete_worker_pause(
    run_id: str,
    payload: LocalRunPauseCompleteIn,
    authorization: str | None = Header(default=None),
    conn: Database = Depends(get_db),
) -> dict:
    device = _device_from_token(conn, authorization)
    run = _run_for_device(conn, run_id, device)
    if run["lease_owner"] != device["id"]:
        raise HTTPException(status_code=409, detail="Run lease 不属于当前设备")
    if not run.get("pause_requested_at"):
        raise HTTPException(status_code=409, detail="Run 未请求暂停")
    open_receipt = conn.execute(
        """SELECT id FROM execution_receipts WHERE run_id = ? AND status = 'started'
        LIMIT 1""",
        (run_id,),
    ).fetchone()
    if open_receipt:
        raise HTTPException(status_code=409, detail="原子工具操作尚未结束，不能暂停")
    if run["status"] == RunStatus.RUNNING:
        transition_run(conn, run_id, RunStatus.PAUSING)
    transition_run(conn, run_id, RunStatus.PAUSED)
    checkpoint = {
        "paused_at": now_iso(),
        "source": "local_worker_safe_point",
        **payload.checkpoint,
    }
    conn.execute(
        """UPDATE runs SET checkpoint_json = ?, lease_owner = NULL,
        lease_expires_at = NULL, runtime_status = 'paused' WHERE id = ?""",
        (json.dumps(checkpoint, ensure_ascii=False), run_id),
    )
    if run.get("task_id"):
        conn.execute(
            """UPDATE tasks SET status = '待执行', workflow_status = 'paused',
            waiting_reason = 'preempted', checkpoint_json = ?, updated_at = ?
            WHERE id = ?""",
            (json.dumps(checkpoint, ensure_ascii=False), now_iso(), run["task_id"]),
        )
    release_run_resources(conn, run_id=run_id)
    emit_workforce_event(
        conn,
        workspace_id=device["workspace_id"],
        event_type="workforce_run_paused",
        source_id=run_id,
        title="本机 Run 已在安全点暂停",
        task_id=run.get("task_id"),
        actor_agent_id=run["agent_id"],
        metadata={"device_id": device["id"], "checkpoint": checkpoint},
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
        "UPDATE runs SET lease_owner = NULL, lease_expires_at = NULL, runtime_status = 'failed' WHERE id = ?",
        (run_id,),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())


@router.post("/runs/{run_id}/receipts")
def begin_local_execution_receipt(
    run_id: str,
    payload: LocalReceiptStartIn,
    authorization: str | None = Header(default=None),
    conn: Database = Depends(get_db),
) -> dict:
    device = _device_from_token(conn, authorization)
    run = _run_for_device(conn, run_id, device)
    if run["lease_owner"] != device["id"] or run["status"] != RunStatus.RUNNING:
        raise HTTPException(status_code=409, detail="Run 未由当前设备执行")
    receipt_id = begin_receipt(
        conn,
        workspace_id=device["workspace_id"],
        agent_id=run["agent_id"],
        run_id=run_id,
        tool_name=payload.tool_name,
        arguments=payload.arguments,
    )
    conn.commit()
    return {"id": receipt_id, "status": "started"}


@router.post("/runs/{run_id}/receipts/{receipt_id}")
def finish_local_execution_receipt(
    run_id: str,
    receipt_id: str,
    payload: LocalReceiptFinishIn,
    authorization: str | None = Header(default=None),
    conn: Database = Depends(get_db),
) -> dict:
    device = _device_from_token(conn, authorization)
    run = _run_for_device(conn, run_id, device)
    receipt = conn.execute(
        """SELECT * FROM execution_receipts
           WHERE id = ? AND run_id = ? AND workspace_id = ?""",
        (receipt_id, run_id, device["workspace_id"]),
    ).fetchone()
    if receipt is None:
        raise HTTPException(status_code=404, detail="执行回执不存在")
    if receipt["status"] != "started":
        return {"id": receipt_id, "status": receipt["status"], "duplicate": True}
    finish_receipt(
        conn,
        receipt_id,
        status=payload.status,
        result=payload.result,
        error=payload.error,
    )
    if payload.status == "succeeded":
        conn.execute(
            """UPDATE runs SET execution_receipt_id = COALESCE(execution_receipt_id, ?)
               WHERE id = ?""",
            (receipt_id, run_id),
        )
    conn.commit()
    return {"id": receipt_id, "status": payload.status}


@router.post("/runs/{run_id}/approvals")
def create_local_run_approval(
    run_id: str,
    payload: LocalRunApprovalIn,
    authorization: str | None = Header(default=None),
    conn: Database = Depends(get_db),
) -> dict:
    """Create a durable owner decision for one Local Worker operation."""
    device = _device_from_token(conn, authorization)
    run = _run_for_device(conn, run_id, device)
    if run["lease_owner"] != device["id"] or run["status"] != RunStatus.RUNNING:
        raise HTTPException(status_code=409, detail="Run 未由当前设备执行")
    receipt_id = (
        begin_receipt(
            conn,
            workspace_id=device["workspace_id"],
            agent_id=run["agent_id"],
            run_id=run_id,
            tool_name=payload.tool_name,
            arguments=payload.arguments,
        )
        if payload.create_receipt
        else None
    )
    approval_id = new_id("appr")
    details = {
        "execution_target": "local_desktop",
        "tool_name": payload.tool_name,
        "arguments": payload.arguments,
        "execution_receipt_id": receipt_id,
    }
    conn.execute(
        """INSERT INTO approvals (
          id, workspace_id, task_id, conversation_id, agent_id, title,
          description, status, risk_level, type, run_id, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 'high', 'high_risk', ?, ?, ?)""",
        (
            approval_id,
            device["workspace_id"],
            run["task_id"],
            run["conversation_id"],
            run["agent_id"],
            payload.title,
            payload.description,
            run_id,
            json.dumps(details, ensure_ascii=False),
            now_iso(),
        ),
    )
    transition_run(conn, run_id, RunStatus.WAITING_APPROVAL)
    conn.execute(
        "UPDATE runs SET runtime_status = status WHERE id = ?",
        (run_id,),
    )
    conn.commit()
    return {"id": approval_id, "execution_receipt_id": receipt_id, "status": "pending"}


@router.get("/runs/{run_id}/approvals/{approval_id}")
def local_run_approval_status(
    run_id: str,
    approval_id: str,
    authorization: str | None = Header(default=None),
    conn: Database = Depends(get_db),
) -> dict:
    device = _device_from_token(conn, authorization)
    _run_for_device(conn, run_id, device)
    approval = conn.execute(
        """SELECT id, status, payload_json, created_at FROM approvals
           WHERE id = ? AND run_id = ? AND workspace_id = ?""",
        (approval_id, run_id, device["workspace_id"]),
    ).fetchone()
    if approval is None:
        raise HTTPException(status_code=404, detail="本机审批不存在")
    status = approval["status"]
    if status == "pending":
        try:
            created_at = datetime.fromisoformat(str(approval["created_at"]))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            if datetime.now(UTC) - created_at >= timedelta(
                seconds=settings.approval_bridge_timeout_seconds
            ):
                conn.execute(
                    """UPDATE approvals SET status = 'expired', resolved_at = ?
                       WHERE id = ? AND status = 'pending'""",
                    (now_iso(), approval_id),
                )
                conn.execute(
                    "UPDATE runs SET status = 'running', runtime_status = 'running' WHERE id = ?",
                    (run_id,),
                )
                conn.commit()
                status = "expired"
        except ValueError:
            status = "expired"
    elif status in {"approved", "rejected"}:
        conn.execute(
            "UPDATE runs SET status = 'running', runtime_status = 'running' WHERE id = ?",
            (run_id,),
        )
        conn.commit()
    return {"id": approval["id"], "status": status}


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
