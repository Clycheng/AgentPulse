"""Supply state machine and provisioning orchestration (TD-04-T4).

State machine for agent_specs.status:
  draft → provisioning → (blocked_on_credentials ⇄) → ready
                         ↓ (any step fails)
                       failed (retry: provision() is idempotent)

Core function: provision(conn, agent_id, provisioner)
- Creates agent_capabilities rows from role_spec capability_keys
- Drives ProfileProvisioner calls
- Handles credential_missing → blocked_on_credentials
- Idempotent: re-provisioning only processes pending/failed steps
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.core.database import Database
from app.orchestration.capability_catalog import (
    CATALOG,
    get_capability,
    resolve_bundle,
    validate_capability_keys,
)
from app.runtime.profile_provisioner import (
    ProfileProvisioner,
    build_provisioner_from_settings,
)

HERMES_MODEL = "deepseek/deepseek-v4-flash"


class ProvisioningError(Exception):
    """Raised when provisioning fails."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    from uuid import uuid4
    return f"{prefix}_{uuid4().hex}"


def create_agent_spec(
    conn: Database,
    *,
    agent_id: str,
    workspace_id: str,
    role_name: str,
    source_request: str = "",
    responsibilities: list[str] | None = None,
    capability_keys: list[str] | None = None,
) -> dict:
    """Create an agent_spec row with capabilities from catalog.

    This creates the spec in 'draft' status and populates
    agent_capabilities rows from the catalog.

    Args:
        conn: Database connection
        agent_id: Agent ID (must exist)
        workspace_id: Workspace ID
        role_name: Role name
        source_request: User's NL request
        responsibilities: List of responsibilities
        capability_keys: List of validated capability keys

    Returns:
        Serialized spec dict
    """
    cap_keys = list(dict.fromkeys(capability_keys or []))
    validate_capability_keys(cap_keys)

    spec_id = _new_id("spec")
    now = _now_iso()
    resp_json = json.dumps(responsibilities or [], ensure_ascii=False)

    # Create spec row
    conn.execute(
        """INSERT INTO agent_specs
        (id, agent_id, workspace_id, role_name, source_request,
         responsibilities_json, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?)
        """,
        (spec_id, agent_id, workspace_id, role_name, source_request, resp_json, now, now),
    )

    ensure_agent_capability_rows(
        conn,
        agent_id=agent_id,
        workspace_id=workspace_id,
        capability_keys=cap_keys,
        created_at=now,
    )

    return _serialize_spec(conn, spec_id)


def ensure_agent_capability_rows(
    conn: Database,
    *,
    agent_id: str,
    workspace_id: str,
    capability_keys: list[str],
    created_at: str | None = None,
) -> None:
    """Add catalog capability rows without duplicating an existing spec.

    Bootstrap paths can run more than once (for example when an old workspace
    is opened after a catalog upgrade). Keeping this operation idempotent means
    the secretary can be backfilled without creating a second spec or resetting
    capabilities the owner has already disabled.
    """
    cap_keys = list(dict.fromkeys(capability_keys))
    validate_capability_keys(cap_keys)
    now = created_at or _now_iso()
    existing = {
        row["capability_key"]
        for row in conn.execute(
            "SELECT capability_key FROM agent_capabilities WHERE agent_id = ?",
            (agent_id,),
        ).fetchall()
    }
    for key in cap_keys:
        if key in existing:
            continue
        cap_def = get_capability(key)
        cap_id = _new_id("cap")
        conn.execute(
            """INSERT INTO agent_capabilities
            (id, agent_id, workspace_id, capability_key,
             skill_refs_json, toolset_refs_json, mcp_refs_json,
             required_credentials_json, risk_gate, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cap_id,
                agent_id,
                workspace_id,
                key,
                json.dumps(sorted(cap_def.skills), ensure_ascii=False),
                json.dumps(sorted(cap_def.toolsets), ensure_ascii=False),
                json.dumps(sorted(cap_def.mcp), ensure_ascii=False),
                json.dumps(sorted(cap_def.required_credentials), ensure_ascii=False),
                cap_def.risk_gate,
                "pending",
                now,
                now,
            ),
        )


def provision(
    conn: Database,
    agent_id: str,
    provisioner: ProfileProvisioner | None = None,
) -> dict:
    """Provision an agent — drive the supply state machine.

    Idempotent: only processes capabilities not yet enabled.
    Steps:
    1. Set spec status → provisioning
    2. For each capability:
       - If no credentials needed → enable
       - If credentials needed but missing → credential_missing
       - If prohibited_auto → always credential_missing (manual only)
    3. Generate hermes_profile name
    4. Call ProfileProvisioner for enabled capabilities
    5. If any credential_missing → blocked_on_credentials
    6. If all enabled → ready
    7. On error → failed

    Args:
        conn: Database connection
        agent_id: Agent ID
        provisioner: ProfileProvisioner (defaults to RecordOnlyProvisioner)

    Returns:
        Updated serialized spec dict

    Raises:
        ProvisioningError: If spec not found or not in provisionable state
    """
    if provisioner is None:
        provisioner = build_provisioner_from_settings()

    spec = conn.execute(
        "SELECT * FROM agent_specs WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    if spec is None:
        raise ProvisioningError(f"agent_spec not found for agent {agent_id}")

    spec_id = spec["id"]

    # Only provision from draft or failed states
    if spec["status"] not in ("draft", "failed", "blocked_on_credentials"):
        # Already provisioning or ready — return current state
        return _serialize_spec(conn, spec_id)

    now = _now_iso()

    # Set status → provisioning
    conn.execute(
        "UPDATE agent_specs SET status = 'provisioning', updated_at = ? WHERE id = ?",
        (now, spec_id),
    )

    try:
        # Get capabilities for this agent
        capabilities = conn.execute(
            "SELECT * FROM agent_capabilities WHERE agent_id = ? ORDER BY created_at",
            (agent_id,),
        ).fetchall()

        # Process each capability
        any_credential_missing = False
        for cap_row in capabilities:
            key = cap_row["capability_key"]
            cap_status = cap_row["status"]

            # Skip already enabled
            if cap_status == "enabled":
                continue

            cap_def = get_capability(key)
            required_creds = cap_def.required_credentials

            # prohibited_auto: always blocked (manual only)
            if cap_def.risk_gate == "prohibited_auto":
                conn.execute(
                    "UPDATE agent_capabilities SET status = 'credential_missing', updated_at = ? WHERE id = ?",
                    (now, cap_row["id"]),
                )
                any_credential_missing = True
                continue

            # No credentials needed → enable directly
            if not required_creds:
                conn.execute(
                    "UPDATE agent_capabilities SET status = 'enabled', updated_at = ? WHERE id = ?",
                    (now, cap_row["id"]),
                )
                continue

            # Credentials needed — check if they're provided
            # For v1: we check if any credentials have been written
            # by looking at a per-agent credentials store
            # For now: mark as credential_missing (credentials API is TD-04-T5)
            conn.execute(
                "UPDATE agent_capabilities SET status = 'credential_missing', updated_at = ? WHERE id = ?",
                (now, cap_row["id"]),
            )
            any_credential_missing = True

        enabled_keys = [
            cap["capability_key"]
            for cap in conn.execute(
                "SELECT capability_key FROM agent_capabilities "
                "WHERE agent_id = ? AND status = 'enabled'",
                (agent_id,),
            ).fetchall()
        ]

        if any_credential_missing:
            final_status = "blocked_on_credentials"
        else:
            profile_name = spec["hermes_profile"] or _generate_profile_name(
                conn, agent_id, spec["workspace_id"]
            )

            # Build bundle from enabled capabilities
            bundle = resolve_bundle(enabled_keys)

            # Call provisioner: create profile, write persona (SOUL), configure
            # model/tools and install skills. Model credentials are injected
            # into each ACP subprocess by RunService; they never live in profile.
            provisioner.create_profile(profile_name)
            provisioner.write_soul(profile_name, _build_soul(conn, agent_id, spec))
            provisioner.configure(
                profile_name,
                model=HERMES_MODEL,
                toolsets=bundle["toolsets"],
                mcp=bundle["mcp"],
            )
            if bundle["skills"]:
                provisioner.install_skills(profile_name, bundle["skills"])
            # Update spec
            conn.execute(
                "UPDATE agent_specs SET hermes_profile = ?, status = 'ready', updated_at = ? WHERE id = ?",
                (profile_name, now, spec_id),
            )
            final_status = "ready"

        # Update spec status
        conn.execute(
            "UPDATE agent_specs SET status = ?, updated_at = ? WHERE id = ?",
            (final_status, now, spec_id),
        )

    except Exception:
        # On any error → failed
        conn.execute(
            "UPDATE agent_specs SET status = 'failed', updated_at = ? WHERE id = ?",
            (now, spec_id),
        )
        raise

    return _serialize_spec(conn, spec_id)


def _generate_profile_name(conn: Database, agent_id: str, workspace_id: str) -> str:
    """Generate a unique Hermes profile name.

    Hermes requires lowercase alphanumeric names, so strip everything else.
    """
    import re

    ws_short = re.sub(r"[^a-z0-9]", "", workspace_id.lower())[-6:]
    ag_short = re.sub(r"[^a-z0-9]", "", agent_id.lower())[-8:]
    return f"ap{ws_short}{ag_short}"


def _build_soul(conn: Database, agent_id: str, spec) -> str:
    """Build the employee's SOUL.md from its role + responsibilities + prompt.

    Persona is deterministic (no LLM call); the boundary rules encode the
    product's "ask when unclear / boss approves risky actions" norms.
    """
    agent = conn.execute(
        "SELECT name, role, description, prompt FROM agents WHERE id = ?",
        (agent_id,),
    ).fetchone()
    name = agent["name"] if agent else spec["role_name"]
    prompt = (agent["prompt"] if agent else "") or "（按岗位职责推进工作）"
    responsibilities = json.loads(spec["responsibilities_json"] or "[]")
    resp_lines = "\n".join(f"- {r}" for r in responsibilities) or "- （待补充）"
    soul = (
        f"# {name} \u00b7 {spec['role_name']}\n\n"
        f"你是老板的 AI 员工「{name}」，岗位是{spec['role_name']}。\n\n"
        "## 职责\n"
        f"{resp_lines}\n\n"
        "## 工作方式\n"
        f"{prompt}\n\n"
        "## 铁律\n"
        "- 需求不清楚或关键信息缺失时，**直接在对话里正常提问**，"
        "说明缺少什么信息、需要做什么决策，然后停下等老板回复，"
        "绝不臆测就执行。\n"
        "- 高风险动作（对外发布、部署上线、任何花钱或不可逆操作）"
        "必须先等老板确认，按 `approval` 流程等待审批。\n"
        "- 如果因缺少工具 / MCP 连接 / 权限而无法完成任务，不要猜测绕路，"
        "在对话里如实说明卡在哪个任务、缺什么能力，"
        "然后停下——**不要自己申请能力升级**，老板会从你的员工档案里主动授予。\n"
        "\n"
        "## 自我进步\n"
        "- 每完成一项任务，想一个值得记住的经验——踩过的坑、有效的工具调用顺序、"
        "这家公司/客户的偏好——用 `skills` 工具的 learn 功能一句话记下来。"
        "系统会定期把这些碎片整理成正式技能，让你越用越懂这家公司。\n"
    )
    return soul


def _serialize_spec(conn: Database, spec_id: str) -> dict:
    """Serialize an agent_spec with its capabilities."""
    spec = conn.execute(
        "SELECT * FROM agent_specs WHERE id = ?", (spec_id,)
    ).fetchone()
    if spec is None:
        raise ProvisioningError(f"spec {spec_id} not found")

    capabilities = conn.execute(
        "SELECT * FROM agent_capabilities WHERE agent_id = ? ORDER BY created_at",
        (spec["agent_id"],),
    ).fetchall()

    return {
        "id": spec["id"],
        "agent_id": spec["agent_id"],
        "workspace_id": spec["workspace_id"],
        "role_name": spec["role_name"],
        "source_request": spec["source_request"],
        "responsibilities": json.loads(spec["responsibilities_json"] or "[]"),
        "hermes_profile": spec["hermes_profile"],
        "status": spec["status"],
        "capabilities": [
            {
                "id": cap["id"],
                "agent_id": cap["agent_id"],
                "capability_key": cap["capability_key"],
                "skill_refs": json.loads(cap["skill_refs_json"] or "[]"),
                "toolset_refs": json.loads(cap["toolset_refs_json"] or "[]"),
                "mcp_refs": json.loads(cap["mcp_refs_json"] or "[]"),
                "required_credentials": json.loads(cap["required_credentials_json"] or "[]"),
                "risk_gate": cap["risk_gate"],
                "status": cap["status"],
                "created_at": cap["created_at"],
                "updated_at": cap["updated_at"],
            }
            for cap in capabilities
        ],
        "created_at": spec["created_at"],
        "updated_at": spec["updated_at"],
    }
