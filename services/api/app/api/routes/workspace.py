import json
import os
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Body, Depends, HTTPException
from starlette.responses import StreamingResponse

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.logging import get_logger
from app.core.database import Database, Row, connect, get_db
from app.runtime.deepseek import DeepSeekAPIError, DeepSeekChatClient, DeepSeekNotConfigured
from app.runtime.hermes_client import HermesBackend, RunContext
from app.runtime.runner import (
    make_bridge_resolver,
    resolve_hermes_profile,
    stream_agent_run,
)
from app.runtime.runs import RunStatus
from app.runtime.profile_provisioner import build_provisioner_from_settings
from app.runtime.reflection import run_reflection
from app.schemas.run import (
    LlmAgentExperience,
    LlmChatAgent,
    LlmChatMessage,
    LlmChatRequest,
    LlmKnowledgeSource,
    LlmTaskContext,
    RunOut,
    RunStepOut,
)
from app.schemas.workspace import (
    AddConversationMembersRequest,
    BootstrapResponse,
    ClaimTaskRequest,
    CreateAgentRequest,
    CreateGroupRequest,
    CreateKnowledgeSourceRequest,
    CreateTaskRequest,
    ObsidianSyncRequest,
    RecruitAgentRequest,
    ResolveApprovalRequest,
    SendMessageRequest,
    SendMessageResponse,
    TaskOut,
    UpdateTaskRequest,
)
from app.schemas.agent_spec import (
    AgentSpecOut,
    CredentialRequest,
    GrantCapabilityRequest,
)
from app.services.workspace import (
    add_agent_experience,
    add_message,
    add_task_event,
    add_task_output,
    claim_task,
    create_agent,
    create_dm_conversation,
    create_knowledge_source,
    create_task,
    ensure_department,
    extract_recruit_intent,
    get_bootstrap,
    get_workspace_for_user,
    load_knowledge_context,
    new_id,
    now_iso,
    recruit_from_template,
    serialize_agent,
    serialize_approval,
    serialize_knowledge_source,
    serialize_message,
    serialize_task,
    sync_obsidian_documents,
    update_task,
)
from app.services.credentials import delete_credential, put_credential
from app.services.model_credentials import deepseek_client_for_workspace
from app.orchestration.supply import create_agent_spec, provision, ProvisioningError
from app.tools.function_loop import run_function_loop
from app.orchestration.brief import create_brief
from app.orchestration.discussion import (
    run_discussion_round,
    build_discussion_context,
)

logger = get_logger(__name__)

router = APIRouter(tags=["workspace"])


def _build_context_manifest(*args, **kwargs):
    # Import lazily: company_memory uses workspace primitives and must not
    # participate in the module import cycle at application startup.
    from app.services.company_memory import build_context_manifest

    return build_context_manifest(*args, **kwargs)


@router.get("/me/bootstrap", response_model=BootstrapResponse)
def bootstrap(
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
):
    workspace = get_workspace_for_user(conn, current_user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    return get_bootstrap(conn, workspace["id"])


@router.post("/me/onboarding/complete")
def complete_onboarding(
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
):
    workspace = get_workspace_for_user(conn, current_user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    conn.execute(
        "UPDATE workspaces SET onboarding_completed = ? WHERE id = ?",
        (True, workspace["id"]),
    )
    return {"ok": True}


@router.post("/agents")
def create_custom_agent(
    payload: CreateAgentRequest,
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
):
    workspace = get_workspace_for_user(conn, current_user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    department = ensure_department(conn, workspace["id"], payload.department_name)

    # "Hire by role" (TD-07-T2): a role_bundle_key expands into its preset
    # capability list, merged (dedup, bundle-first) with any explicit keys.
    if payload.role_spec and payload.role_spec.role_bundle_key:
        from app.orchestration.capability_catalog import get_role_bundle
        try:
            bundle_keys = get_role_bundle(payload.role_spec.role_bundle_key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        payload.role_spec.capability_keys = list(
            dict.fromkeys(bundle_keys + payload.role_spec.capability_keys)
        )

    # Collect skills from role_spec capability keys if provided
    skills: list[str] = []
    if payload.role_spec and payload.role_spec.capability_keys:
        from app.orchestration.capability_catalog import get_capability
        for key in payload.role_spec.capability_keys:
            try:
                cap = get_capability(key)
                skills.extend(cap.skills)
            except ValueError:
                pass  # unknown keys silently stripped
        skills = list(dict.fromkeys(skills))  # deduplicate preserving order

    agent_id = create_agent(
        conn,
        workspace_id=workspace["id"],
        department_id=department["id"],
        name=payload.name,
        role=payload.role_spec.role_name if payload.role_spec else (payload.description or "自定义员工"),
        description=payload.description,
        prompt=payload.prompt,
        skills=skills,
        mcps=[],
        source="custom",
    )
    create_dm_conversation(conn, workspace["id"], agent_id)
    agent = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()

    result = serialize_agent(agent)

    # If role_spec provided, create agent_spec + provision
    if payload.role_spec:
        try:
            spec = create_agent_spec(
                conn,
                agent_id=agent_id,
                workspace_id=workspace["id"],
                role_name=payload.role_spec.role_name,
                source_request=payload.role_spec.source_request,
                responsibilities=payload.role_spec.responsibilities,
                capability_keys=payload.role_spec.capability_keys,
            )
            spec = provision(conn, agent_id)
            result["spec"] = spec
        except (ValueError, ProvisioningError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return result


@router.post("/agents/recruit")
def recruit_agent(
    payload: RecruitAgentRequest,
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
):
    workspace = get_workspace_for_user(conn, current_user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    try:
        agent = recruit_from_template(
            conn,
            workspace_id=workspace["id"],
            template_id=payload.template_id,
            department_name=payload.department_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize_agent(agent)


@router.get("/agents/{agent_id}/spec", response_model=AgentSpecOut)
def get_agent_spec(
    agent_id: str,
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
):
    workspace = get_workspace_for_user(conn, current_user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    # Verify agent belongs to workspace
    agent = conn.execute(
        "SELECT id FROM agents WHERE id = ? AND workspace_id = ?",
        (agent_id, workspace["id"]),
    ).fetchone()
    if agent is None:
        raise HTTPException(status_code=404, detail="员工不存在")
    spec = conn.execute(
        "SELECT * FROM agent_specs WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    if spec is None:
        raise HTTPException(status_code=404, detail="该员工尚未配置角色规格")
    return _serialize_spec_from_row(conn, spec)


def _verify_agent_in_workspace(conn: Database, current_user: Row, agent_id: str) -> Row:
    workspace = get_workspace_for_user(conn, current_user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    agent = conn.execute(
        "SELECT id FROM agents WHERE id = ? AND workspace_id = ?",
        (agent_id, workspace["id"]),
    ).fetchone()
    if agent is None:
        raise HTTPException(status_code=404, detail="员工不存在")
    return agent


@router.get("/agents/{agent_id}/skills")
def list_agent_skills(
    agent_id: str,
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
):
    """TD-06-T1: auto-sedimented skills for an employee (growth trajectory)."""
    _verify_agent_in_workspace(conn, current_user, agent_id)
    spec = conn.execute(
        "SELECT hermes_profile FROM agent_specs WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    if spec is None or not spec["hermes_profile"]:
        return {"skills": []}
    provisioner = build_provisioner_from_settings()
    try:
        skills = provisioner.list_skills(spec["hermes_profile"])
    except Exception:
        skills = []
    return {"skills": skills}


@router.post("/agents/{agent_id}/capabilities")
def grant_agent_capability(
    agent_id: str,
    payload: GrantCapabilityRequest,
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
):
    """ADR 0008 §5: owner-initiated capability grant.

    The boss picks a capability from the catalog (GET /api/capabilities) and
    it's installed immediately — no suspended run/approval to resolve, because
    the owner is deciding directly rather than approving something an agent
    asked for (the agent-self-request path was a dead trigger to begin with;
    see the dormant `category == "capability_upgrade"` branch in runner.py).
    """
    workspace = get_workspace_for_user(conn, current_user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    _verify_agent_in_workspace(conn, current_user, agent_id)

    from app.runtime.upgrade import UpgradeError, execute_upgrade

    try:
        result = execute_upgrade(
            conn,
            approval={"agent_id": agent_id, "workspace_id": workspace["id"]},
            approved_capability_key=payload.capability_key,
            provisioner=build_provisioner_from_settings(),
        )
    except UpgradeError as exc:
        raise HTTPException(status_code=400, detail=f"授予能力失败：{exc}")

    # Audit trail only — already resolved, run_id=NULL (nothing was suspended).
    conn.execute(
        """
        INSERT INTO approvals (
          id, workspace_id, agent_id, title, description, status, risk_level,
          type, payload_json, resolved_by, resolved_at, created_at
        ) VALUES (?, ?, ?, ?, ?, 'approved', 'medium', 'capability_upgrade', ?, ?, ?, ?)
        """,
        (
            new_id("appr"),
            workspace["id"],
            agent_id,
            f"老板授予能力：{payload.capability_key}",
            f"老板从员工档案直接授予「{payload.capability_key}」能力。",
            json.dumps(
                {"capability_key": payload.capability_key, "owner_initiated": True},
                ensure_ascii=False,
            ),
            current_user["id"],
            now_iso(),
            now_iso(),
        ),
    )
    conn.commit()
    return result


@router.post("/agents/{agent_id}/reflect")
async def reflect_agent(
    agent_id: str,
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
):
    """Trigger the employee's skill and evidence-backed memory reflection."""
    _verify_agent_in_workspace(conn, current_user, agent_id)
    from app.services.company_memory import reflect_agent_memories

    workspace = get_workspace_for_user(conn, current_user["id"])
    memory_reflection = reflect_agent_memories(
        conn, workspace_id=workspace["id"], agent_id=agent_id
    )
    spec = conn.execute(
        "SELECT status, hermes_profile FROM agent_specs WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    names = []
    if spec is not None and spec["status"] == "ready" and spec["hermes_profile"]:
        names = await run_reflection(
            conn,
            agent_id=agent_id,
            backend=HermesBackend(hermes_bin=settings.hermes_bin),
            provisioner=build_provisioner_from_settings(),
            hermes_work_root=settings.hermes_work_root,
        )
    return {"skills_learned": names, "memory_reflection": memory_reflection}


@router.post("/agents/{agent_id}/credentials", response_model=AgentSpecOut)
def provide_credential(
    agent_id: str,
    payload: CredentialRequest,
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
):
    workspace = get_workspace_for_user(conn, current_user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    agent = conn.execute(
        "SELECT id FROM agents WHERE id = ? AND workspace_id = ?",
        (agent_id, workspace["id"]),
    ).fetchone()
    if agent is None:
        raise HTTPException(status_code=404, detail="员工不存在")

    spec = conn.execute(
        "SELECT * FROM agent_specs WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    if spec is None:
        raise HTTPException(status_code=404, detail="该员工尚未配置角色规格")

    # A credential may unlock more than one capability.
    caps = conn.execute(
        "SELECT * FROM agent_capabilities WHERE agent_id = ?",
        (agent_id,),
    ).fetchall()
    matching_caps = []
    for cap in caps:
        import json as _json
        required = _json.loads(cap["required_credentials_json"] or "[]")
        if payload.credential_name in required:
            matching_caps.append(cap)
    if not matching_caps:
        raise HTTPException(
            status_code=400,
            detail=f"该员工不需要凭证 {payload.credential_name}",
        )

    put_credential(
        conn,
        workspace_id=workspace["id"],
        agent_id=agent_id,
        credential_name=payload.credential_name,
        value=payload.value,
    )

    # Check if all capabilities are now enabled → update spec status
    _refresh_spec_status(conn, spec["id"])

    updated_spec = conn.execute(
        "SELECT * FROM agent_specs WHERE id = ?", (spec["id"],)
    ).fetchone()
    return _serialize_spec_from_row(conn, updated_spec)


@router.delete(
    "/agents/{agent_id}/credentials/{credential_name}", response_model=AgentSpecOut
)
def revoke_credential(
    agent_id: str,
    credential_name: str,
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
):
    workspace = get_workspace_for_user(conn, current_user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    spec = conn.execute(
        "SELECT * FROM agent_specs WHERE agent_id = ? AND workspace_id = ?",
        (agent_id, workspace["id"]),
    ).fetchone()
    if spec is None:
        raise HTTPException(status_code=404, detail="员工不存在或尚未配置角色规格")
    if not delete_credential(
        conn,
        workspace_id=workspace["id"],
        agent_id=agent_id,
        credential_name=credential_name,
    ):
        raise HTTPException(status_code=404, detail="凭证尚未配置")
    return _serialize_spec_from_row(conn, spec)


@router.post("/agents/{agent_id}/provision", response_model=AgentSpecOut)
def provision_agent(
    agent_id: str,
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
):
    workspace = get_workspace_for_user(conn, current_user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    agent = conn.execute(
        "SELECT id FROM agents WHERE id = ? AND workspace_id = ?",
        (agent_id, workspace["id"]),
    ).fetchone()
    if agent is None:
        raise HTTPException(status_code=404, detail="员工不存在")
    try:
        spec = provision(conn, agent_id)
    except ProvisioningError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return spec


def _serialize_spec_from_row(conn: Database, spec: Row) -> dict:
    """Serialize an agent_spec row with its capabilities."""
    import json as _json
    capabilities = conn.execute(
        "SELECT * FROM agent_capabilities WHERE agent_id = ? ORDER BY created_at",
        (spec["agent_id"],),
    ).fetchall()
    configured = {
        row["credential_name"]
        for row in conn.execute(
            "SELECT credential_name FROM agent_credentials WHERE agent_id = ?",
            (spec["agent_id"],),
        ).fetchall()
    }
    return {
        "id": spec["id"],
        "agent_id": spec["agent_id"],
        "workspace_id": spec["workspace_id"],
        "role_name": spec["role_name"],
        "source_request": spec["source_request"],
        "responsibilities": _json.loads(spec["responsibilities_json"] or "[]"),
        "hermes_profile": spec["hermes_profile"],
        "status": spec["status"],
        "capabilities": [
            {
                "id": cap["id"],
                "agent_id": cap["agent_id"],
                "capability_key": cap["capability_key"],
                "skill_refs": _json.loads(cap["skill_refs_json"] or "[]"),
                "toolset_refs": _json.loads(cap["toolset_refs_json"] or "[]"),
                "mcp_refs": _json.loads(cap["mcp_refs_json"] or "[]"),
                "required_credentials": _json.loads(cap["required_credentials_json"] or "[]"),
                "credential_status": {
                    name: name in configured
                    for name in _json.loads(cap["required_credentials_json"] or "[]")
                },
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


def _refresh_spec_status(conn: Database, spec_id: str) -> None:
    """Check capabilities and update spec status if all enabled."""
    spec = conn.execute(
        "SELECT * FROM agent_specs WHERE id = ?", (spec_id,)
    ).fetchone()
    if spec is None:
        return
    caps = conn.execute(
        "SELECT status FROM agent_capabilities WHERE agent_id = ?",
        (spec["agent_id"],),
    ).fetchall()
    if not caps:
        return
    all_enabled = all(cap["status"] == "enabled" for cap in caps)
    if all_enabled and spec["status"] == "blocked_on_credentials":
        # All capabilities enabled → provision to ready
        from app.orchestration.supply import provision as _provision
        _provision(conn, spec["agent_id"])


@router.post("/knowledge-sources")
def create_workspace_knowledge_source(
    payload: CreateKnowledgeSourceRequest,
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
):
    workspace = get_workspace_for_user(conn, current_user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    source = create_knowledge_source(
        conn,
        workspace_id=workspace["id"],
        title=payload.title,
        category=payload.category,
        content=payload.content,
        created_by=current_user["id"],
    )
    return serialize_knowledge_source(source)


@router.post("/knowledge-sources/obsidian-sync")
def sync_obsidian_knowledge_sources(
    payload: ObsidianSyncRequest,
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
) -> dict:
    """Import only the desktop-selected Vault's managed Markdown documents."""
    workspace = get_workspace_for_user(conn, current_user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    result = sync_obsidian_documents(
        conn,
        workspace_id=workspace["id"],
        created_by=current_user["id"],
        documents=[document.model_dump() for document in payload.documents],
    )
    result["origin"] = "obsidian"
    result["managed_area"] = ".agentpulse/managed"
    return result


@router.post("/conversations/group")
def create_group(
    payload: CreateGroupRequest,
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
):
    workspace = get_workspace_for_user(conn, current_user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")

    found = conn.execute(
        f"""
        SELECT id, name FROM agents
        WHERE workspace_id = ? AND id IN ({",".join("?" for _ in payload.member_ids)})
        """,
        (workspace["id"], *payload.member_ids),
    ).fetchall()
    if len(found) != len(set(payload.member_ids)):
        raise HTTPException(status_code=400, detail="群成员不存在")

    conversation_id = new_id("conv")
    created_at = now_iso()
    conn.execute(
        """
        INSERT INTO conversations (id, workspace_id, kind, name, unread, created_at, updated_at)
        VALUES (?, ?, 'group', ?, 0, ?, ?)
        """,
        (conversation_id, workspace["id"], payload.name, created_at, created_at),
    )
    for agent_id in payload.member_ids:
        conn.execute(
            """
            INSERT INTO conversation_members (conversation_id, agent_id)
            VALUES (?, ?)
            """,
            (conversation_id, agent_id),
        )
    names = "、".join(row["name"] for row in found)
    add_message(
        conn,
        conversation_id=conversation_id,
        sender_type="system",
        sender_id="",
        content=f"你创建了群聊，拉入了 {names}",
    )
    if payload.related_task_ids:
        unique_task_ids = list(dict.fromkeys(payload.related_task_ids))
        tasks = conn.execute(
            f"""
            SELECT id, title FROM tasks
            WHERE workspace_id = ? AND id IN ({",".join("?" for _ in unique_task_ids)})
            """,
            (workspace["id"], *unique_task_ids),
        ).fetchall()
        if len(tasks) != len(unique_task_ids):
            raise HTTPException(status_code=400, detail="关联任务不存在")
        for task in tasks:
            conn.execute(
                """
                UPDATE tasks
                SET conversation_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (conversation_id, now_iso(), task["id"]),
            )
            add_task_event(
                conn,
                workspace_id=workspace["id"],
                task_id=task["id"],
                kind="conversation_linked",
                title="任务已关联群聊",
                content=f"已关联到 #{payload.name}，群聊内员工回复会沉淀为任务产出。",
                conversation_id=conversation_id,
            )
        task_names = "、".join(task["title"] for task in tasks)
        add_message(
            conn,
            conversation_id=conversation_id,
            sender_type="system",
            sender_id="",
            content=f"已关联任务：{task_names}",
        )
    return {"id": conversation_id}


@router.post("/conversations/{conversation_id}/members")
def add_group_members(
    conversation_id: str,
    payload: AddConversationMembersRequest,
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
):
    workspace = get_workspace_for_user(conn, current_user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")

    conversation = conn.execute(
        """
        SELECT * FROM conversations
        WHERE id = ? AND workspace_id = ? AND kind = 'group'
        """,
        (conversation_id, workspace["id"]),
    ).fetchone()
    if conversation is None:
        raise HTTPException(status_code=404, detail="群聊不存在")

    unique_member_ids = list(dict.fromkeys(payload.member_ids))
    found = conn.execute(
        f"""
        SELECT id, name FROM agents
        WHERE workspace_id = ? AND id IN ({",".join("?" for _ in unique_member_ids)})
        """,
        (workspace["id"], *unique_member_ids),
    ).fetchall()
    if len(found) != len(unique_member_ids):
        raise HTTPException(status_code=400, detail="员工不存在")

    existing = conn.execute(
        """
        SELECT agent_id FROM conversation_members
        WHERE conversation_id = ?
        """,
        (conversation_id,),
    ).fetchall()
    existing_ids = {row["agent_id"] for row in existing}
    new_members = [row for row in found if row["id"] not in existing_ids]
    if not new_members:
        raise HTTPException(status_code=409, detail="这些员工已经在群聊里")

    for member in new_members:
        conn.execute(
            """
            INSERT INTO conversation_members (conversation_id, agent_id)
            VALUES (?, ?)
            """,
            (conversation_id, member["id"]),
        )

    names = "、".join(member["name"] for member in new_members)
    add_message(
        conn,
        conversation_id=conversation_id,
        sender_type="system",
        sender_id="",
        content=f"你拉入了 {names}",
    )
    return {"ok": True, "added_member_ids": [member["id"] for member in new_members]}


@router.post("/tasks", response_model=TaskOut)
def create_workspace_task(
    payload: CreateTaskRequest,
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
):
    workspace = get_workspace_for_user(conn, current_user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    try:
        task = create_task(
            conn,
            workspace_id=workspace["id"],
            title=payload.title,
            description=payload.description,
            priority=payload.priority,
            owner_agent_id=payload.owner_agent_id,
            status=payload.status,
            progress=payload.progress,
            conversation_id=payload.conversation_id,
            due_date=payload.due_date,
            parent_task_id=payload.parent_task_id,
            consensus_brief_id=payload.consensus_brief_id,  # Gate condition
            task_plan_id=payload.task_plan_id,
            plan_item_key=payload.plan_item_key,
            expected_output=payload.expected_output,
            output_type=payload.output_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_task(task)


@router.patch("/tasks/{task_id}", response_model=TaskOut)
def update_workspace_task(
    task_id: str,
    payload: UpdateTaskRequest,
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
):
    workspace = get_workspace_for_user(conn, current_user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    try:
        task = update_task(
            conn,
            workspace_id=workspace["id"],
            task_id=task_id,
            changes=payload.model_dump(exclude_unset=True),
        )
    except ValueError as exc:
        status_code = 404 if str(exc) == "task not found" else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return serialize_task(task)


@router.post("/tasks/{task_id}/claim", response_model=TaskOut)
def claim_workspace_task(
    task_id: str,
    payload: ClaimTaskRequest,
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
):
    workspace = get_workspace_for_user(conn, current_user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    try:
        task = claim_task(
            conn,
            workspace_id=workspace["id"],
            task_id=task_id,
            agent_id=payload.agent_id,
        )
    except ValueError as exc:
        status_code = 404 if str(exc) == "task not found" else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return serialize_task(task)


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=SendMessageResponse,
)
async def send_message(
    conversation_id: str,
    payload: SendMessageRequest,
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
):
    workspace = get_workspace_for_user(conn, current_user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    conversation = conn.execute(
        """
        SELECT * FROM conversations
        WHERE id = ? AND workspace_id = ?
        """,
        (conversation_id, workspace["id"]),
    ).fetchone()
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    reply_agents = resolve_reply_agents(conn, workspace["id"], conversation, payload)
    if not reply_agents:
        raise HTTPException(status_code=400, detail="没有可回复的智能体")
    task_owner = reply_agents[0]

    user_message = add_message(
        conn,
        conversation_id=conversation_id,
        sender_type="user",
        sender_id=current_user["id"],
        content=payload.content,
    )
    created_task = None
    created_agent = None
    recruit_intent = _recruit_intent_for_conversation(
        conversation, reply_agents, payload.content
    )
    if recruit_intent is not None:
        department = ensure_department(
            conn, workspace["id"], recruit_intent["department_name"]
        )
        created_agent_id = create_agent(
            conn,
            workspace_id=workspace["id"],
            department_id=department["id"],
            name=recruit_intent["name"],
            role=recruit_intent["role"],
            description=recruit_intent["description"],
            prompt=recruit_intent["prompt"],
            skills=recruit_intent["skills"],
            mcps=recruit_intent["mcps"],
            source="chat_factory",
        )
        create_dm_conversation(conn, workspace["id"], created_agent_id)
        created_agent = conn.execute(
            "SELECT * FROM agents WHERE id = ?", (created_agent_id,)
        ).fetchone()
        add_message(
            conn,
            conversation_id=conversation_id,
            sender_type="system",
            sender_id="",
            content=(
                f"已创建员工：{created_agent['name']}，加入"
                f"{recruit_intent['department_name']}。你可以在员工列表或私聊里继续配置他。"
            ),
        )
    # NOTE: Auto-task creation from chat intent has been removed.
    # Task creation now requires a confirmed consensus_brief (see ADR 0006).
    created_task = None
    agent_messages = []

    # Group discussion orchestration: if group chat in 'discussing' state,
    # use multi-agent discussion round instead of replying to each agent individually.
    is_group_discussing = (
        conversation["kind"] == "group"
        and (conversation.get("discussion_status") or "discussing") == "discussing"
    )

    if is_group_discussing and len(reply_agents) > 1:
        # Multi-agent discussion round (TD-02) — orchestrated by the
        # discussion layer. The route only injects how a turn executes and
        # how the moderator LLM is called, then collects the yielded messages.
        logger.info("group_discussion_round", conversation_id=conversation_id, agents=len(reply_agents))
        async def turn_executor(conn, agent_id):
            next_agent = next(
                (a for a in reply_agents if a["id"] == agent_id), None
            )
            if next_agent is None:
                return
            discussion_ctx = build_discussion_context(
                conn, conversation_id, next_agent, reply_agents
            )
            msg = await complete_agent_reply(
                conn,
                workspace=workspace,
                conversation=conversation,
                conversation_id=conversation_id,
                agent=next_agent,
                user_message=user_message,
                discussion_context=discussion_ctx,
                use_tools=False,
            )
            # Commit so the next speaker selection can see this reply.
            conn.commit()
            if msg is not None:
                yield {"type": "message", "message": msg}

        error_exc: Exception | None = None
        brief_draft: dict | None = None
        async for event in run_discussion_round(
            conn,
            workspace_id=workspace["id"],
            conversation_id=conversation_id,
            member_agents=reply_agents,
            turn_executor=turn_executor,
            llm_complete=make_speaker_selector(conn, workspace["id"]),
        ):
            if event["type"] == "message":
                agent_messages.append(event["message"])
            elif event["type"] == "brief_draft":
                brief_draft = event.get("draft")
            elif event["type"] == "error":
                error_exc = event.get("exc")

        if isinstance(error_exc, DeepSeekNotConfigured):
            raise HTTPException(status_code=503, detail=str(error_exc))
        if isinstance(error_exc, DeepSeekAPIError):
            raise HTTPException(status_code=502, detail=str(error_exc))

        # 讨论收敛 → 自动落 draft 共识 brief（BRIEF_CARD 系统消息并入响应）
        if brief_draft:
            card_message = _create_brief_from_draft(
                conn,
                workspace_id=workspace["id"],
                conversation_id=conversation_id,
                draft=brief_draft,
                reply_agents=reply_agents,
            )
            if card_message is not None:
                agent_messages.append(card_message)
    else:
        # DM or single-agent group: original behavior
        for agent in reply_agents:
            try:
                agent_messages.append(
                    await complete_agent_reply(
                        conn,
                        workspace=workspace,
                        conversation=conversation,
                        conversation_id=conversation_id,
                        agent=agent,
                        user_message=user_message,
                    )
                )
            except DeepSeekNotConfigured as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except DeepSeekAPIError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc

    serialized_agent_messages = [
        serialize_message(message) for message in agent_messages
    ]
    return {
        "user_message": serialize_message(user_message),
        "agent_message": serialized_agent_messages[0],
        "agent_messages": serialized_agent_messages,
        "created_task": serialize_task(created_task) if created_task else None,
        "created_agent": serialize_agent(created_agent) if created_agent else None,
    }


@router.post("/conversations/{conversation_id}/messages/stream")
async def send_message_stream(
    conversation_id: str,
    payload: SendMessageRequest,
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
):
    """SSE streaming version of send_message.

    Events emitted:
    - event: user_message  data: {message}
    - event: speaking      data: {agent_id, agent_name, agent_role}
    - event: chunk         data: {content}
    - event: done          data: {message}
    - event: system        data: {content}
    - event: end           data: {}
    - event: error         data: {detail}
    """
    workspace = get_workspace_for_user(conn, current_user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    conversation = conn.execute(
        "SELECT * FROM conversations WHERE id = ? AND workspace_id = ?",
        (conversation_id, workspace["id"]),
    ).fetchone()
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    reply_agents = resolve_reply_agents(conn, workspace["id"], conversation, payload)
    if not reply_agents:
        raise HTTPException(status_code=400, detail="没有可回复的智能体")

    user_message = add_message(
        conn,
        conversation_id=conversation_id,
        sender_type="user",
        sender_id=current_user["id"],
        content=payload.content,
    )
    conn.commit()

    # Handle recruit intent (same as non-streaming; 只在小秘私聊生效)
    recruit_intent = _recruit_intent_for_conversation(
        conversation, reply_agents, payload.content
    )
    if recruit_intent is not None:
        department = ensure_department(
            conn, workspace["id"], recruit_intent["department_name"]
        )
        create_agent(
            conn,
            workspace_id=workspace["id"],
            department_id=department["id"],
            name=recruit_intent["name"],
            role=recruit_intent["role"],
            description=recruit_intent["description"],
            prompt=recruit_intent["prompt"],
            skills=recruit_intent["skills"],
            mcps=recruit_intent["mcps"],
            source="chat_factory",
        )
        conn.commit()

    async def event_generator():
        # The `conn` dependency is closed by FastAPI the instant this route's
        # sync body returns — but StreamingResponse only starts iterating this
        # generator afterwards. Every DB op below must use its own connection,
        # opened here, or it hits "Cannot operate on a closed database" and the
        # stream dies silently mid-reply (root cause of agents never acting).
        conn = connect()
        try:
            async for chunk in _generate_stream_events(conn):
                yield chunk
        finally:
            conn.close()

    async def _generate_stream_events(conn):
        # Emit user message
        yield f"event: user_message\ndata: {json.dumps(serialize_message(user_message), ensure_ascii=False)}\n\n"

        is_group_discussing = (
            conversation["kind"] == "group"
            and (conversation.get("discussion_status") or "discussing") == "discussing"
        )

        if is_group_discussing and len(reply_agents) > 1:
            # Multi-agent discussion with streaming — orchestrated by the
            # discussion layer. The route injects a streaming turn executor
            # and translates the yielded events into SSE frames.
            async def turn_executor(conn, agent_id):
                next_agent = next(
                    (a for a in reply_agents if a["id"] == agent_id), None
                )
                if next_agent is None:
                    return
                discussion_ctx = build_discussion_context(
                    conn, conversation_id, next_agent, reply_agents
                )
                async for event in _stream_reply_events(
                    conn,
                    workspace=workspace,
                    conversation=conversation,
                    agent=next_agent,
                    user_message=user_message,
                    discussion_context=discussion_ctx,
                    allow_action_bridge=False,
                ):
                    yield event

            async for event in run_discussion_round(
                conn,
                workspace_id=workspace["id"],
                conversation_id=conversation_id,
                member_agents=reply_agents,
                turn_executor=turn_executor,
                llm_complete=make_speaker_selector(conn, workspace["id"]),
            ):
                etype = event["type"]
                if etype == "speaker":
                    speaker = next(
                        (a for a in reply_agents if a["id"] == event["agent_id"]),
                        None,
                    )
                    if speaker is not None:
                        yield f"event: speaking\ndata: {json.dumps({'agent_id': speaker['id'], 'agent_name': speaker['name'], 'agent_role': speaker['role']}, ensure_ascii=False)}\n\n"
                elif etype == "chunk":
                    yield f"event: chunk\ndata: {json.dumps({'content': event['content']}, ensure_ascii=False)}\n\n"
                elif etype == "message" and event.get("message"):
                    yield f"event: done\ndata: {json.dumps(serialize_message(event['message']), ensure_ascii=False)}\n\n"
                elif etype == "brief_draft":
                    # 讨论收敛 → 落 draft 共识 brief，把 BRIEF_CARD 系统消息
                    # 实时推给前端（event: system，前端直接渲染卡片不用刷新）
                    card_message = _create_brief_from_draft(
                        conn,
                        workspace_id=workspace["id"],
                        conversation_id=conversation_id,
                        draft=event.get("draft") or {},
                        reply_agents=reply_agents,
                    )
                    if card_message is not None:
                        yield f"event: system\ndata: {json.dumps(serialize_message(card_message), ensure_ascii=False)}\n\n"
                elif etype == "approval_required":
                    yield f"event: approval\ndata: {json.dumps(event.get('payload', {}), ensure_ascii=False)}\n\n"
                elif etype == "error":
                    yield f"event: error\ndata: {json.dumps({'detail': event['detail']}, ensure_ascii=False)}\n\n"
                elif (
                    etype == "end"
                    and event.get("turns_used") == 0
                    and not event.get("converged")
                ):
                    # Moderator picked nobody (NONE) on the very first turn —
                    # without this, the boss sees the group go dead silent with
                    # no visible signal that it was a deliberate pause rather
                    # than a bug. Surface it as a real system message so it
                    # persists in the transcript too, not just this one stream.
                    silence_message = add_message(
                        conn,
                        conversation_id=conversation_id,
                        sender_type="system",
                        sender_id="",
                        content="目前没有员工需要接话——可以 @ 具体同事继续，或换个说法。",
                    )
                    conn.commit()
                    yield f"event: system\ndata: {json.dumps(serialize_message(silence_message), ensure_ascii=False)}\n\n"
        else:
            # DM or single-agent: stream the reply (Hermes or DeepSeek fallback).
            for agent in reply_agents:
                yield f"event: speaking\ndata: {json.dumps({'agent_id': agent['id'], 'agent_name': agent['name'], 'agent_role': agent['role']}, ensure_ascii=False)}\n\n"
                try:
                    async for event in _stream_reply_events(
                        conn,
                        workspace=workspace,
                        conversation=conversation,
                        agent=agent,
                        user_message=user_message,
                    ):
                        etype = event["type"]
                        if etype == "chunk":
                            yield f"event: chunk\ndata: {json.dumps({'content': event['content']}, ensure_ascii=False)}\n\n"
                        elif etype == "message" and event.get("message"):
                            yield f"event: done\ndata: {json.dumps(serialize_message(event['message']), ensure_ascii=False)}\n\n"
                        elif etype == "approval_required":
                            yield f"event: approval\ndata: {json.dumps(event.get('payload', {}), ensure_ascii=False)}\n\n"
                except (DeepSeekNotConfigured, DeepSeekAPIError) as exc:
                    yield f"event: error\ndata: {json.dumps({'detail': str(exc)}, ensure_ascii=False)}\n\n"
                    break

        yield f"event: end\ndata: {{}}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


_WAITING_ON_LABELS = {
    "high_risk": "等老板批准",
    "business_tool": "等老板批准",
    "capability_upgrade": "等老板批准能力升级",
    "clarification": "等老板回答",
}


def _waiting_on_text(conn: Database, run_id: str, run_status: str) -> str | None:
    """"当前这条 run 在等谁/等什么"的一句话——service-claw-cloud 的
    playbook_matter_state.waiting_on 同款：把审批卡片里已经有的信息，
    也放进 run 列表本身，不用点进去才知道卡在哪。"""
    if run_status not in (RunStatus.WAITING_USER, RunStatus.WAITING_CLARIFY):
        return None
    approval = conn.execute(
        "SELECT type, title, description FROM approvals "
        "WHERE run_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if approval is None:
        return "等老板拍板"
    label = _WAITING_ON_LABELS.get(approval["type"], "等老板拍板")
    detail = approval["description"] or approval["title"]
    return f"{label}：{detail}" if detail else label


@router.get("/conversations/{conversation_id}/runs", response_model=list[RunOut])
def list_conversation_runs(
    conversation_id: str,
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
):
    """Run/activity trace for a conversation — the audit/timeline view
    multiple early users asked for independently (see CHANGELOG): trace
    every agent action (message, tool call, tool result, approval request)
    back to the run and message that triggered it, in order."""
    workspace = get_workspace_for_user(conn, current_user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    conversation = conn.execute(
        "SELECT id FROM conversations WHERE id = ? AND workspace_id = ?",
        (conversation_id, workspace["id"]),
    ).fetchone()
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    runs = conn.execute(
        """
        SELECT runs.*, agents.name AS agent_name
        FROM runs
        JOIN agents ON agents.id = runs.agent_id
        WHERE runs.conversation_id = ?
        ORDER BY runs.created_at
        """,
        (conversation_id,),
    ).fetchall()

    result = []
    from app.services.company_memory import get_run_context_summary

    for run in runs:
        steps = conn.execute(
            "SELECT * FROM run_steps WHERE run_id = ? ORDER BY created_at, id",
            (run["id"],),
        ).fetchall()
        context = get_run_context_summary(
            conn, workspace_id=workspace["id"], run_id=run["id"]
        )
        result.append(
            RunOut(
                id=run["id"],
                agent_id=run["agent_id"],
                agent_name=run["agent_name"],
                task_id=run["task_id"],
                status=run["status"],
                provider=run["provider"],
                model=run["model"],
                error=run["error"],
                created_at=run["created_at"],
                completed_at=run["completed_at"],
                waiting_on=_waiting_on_text(conn, run["id"], run["status"]),
                context_manifest_id=run.get("context_manifest_id"),
                context_event_ids=context["event_ids"],
                context_memory_ids=context["memory_ids"],
                steps=[
                    RunStepOut(
                        id=step["id"],
                        type=step["type"],
                        status=step["status"],
                        title=step["title"],
                        detail=step["detail"],
                        payload=json.loads(step["payload_json"] or "{}"),
                        created_at=step["created_at"],
                    )
                    for step in steps
                ],
            )
        )
    return result


@router.post("/approvals/{approval_id}/resolve")
def resolve_approval(
    approval_id: str,
    payload: ResolveApprovalRequest,
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
):
    workspace = get_workspace_for_user(conn, current_user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    approval = conn.execute(
        """
        SELECT * FROM approvals
        WHERE id = ? AND workspace_id = ?
        """,
        (approval_id, workspace["id"]),
    ).fetchone()
    if approval is None:
        raise HTTPException(status_code=404, detail="确认请求不存在")
    if approval["status"] == "expired":
        raise HTTPException(status_code=409, detail="该审批已超时自动拒绝，无法再操作")
    if approval["status"] != "pending":
        raise HTTPException(status_code=409, detail="确认请求已处理")

    resolved_at = now_iso()
    decision = payload.status
    try:
        decision_payload = json.loads(approval["payload_json"] or "{}")
    except (ValueError, TypeError):
        decision_payload = {}
    decision_payload["scope"] = payload.scope
    if approval["type"] == "business_tool":
        from app.services.business_actions import (
            BusinessToolError,
            resolve_business_approval,
        )

        try:
            resolve_business_approval(
                conn,
                approval=dict(approval),
                decision=decision,
                scope=payload.scope,
                resolved_by=current_user["id"],
            )
        except BusinessToolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    conn.execute(
        """
        UPDATE approvals
        SET status = ?, resolved_by = ?, resolved_at = ?, payload_json = ?
        WHERE id = ? AND workspace_id = ?
        """,
        (
            decision,
            current_user["id"],
            resolved_at,
            json.dumps(decision_payload, ensure_ascii=False),
            approval_id,
            workspace["id"],
        ),
    )

    from app.services.company_memory import record_company_event

    record_company_event(
        conn,
        workspace_id=workspace["id"],
        event_type="decision",
        source_id=approval_id,
        title="审批决定",
        content=f"{approval['title']}：{decision}",
        conversation_id=approval["conversation_id"],
        task_id=approval["task_id"],
        actor_agent_id=approval["agent_id"],
        actor_user_id=current_user["id"],
        importance=4.0,
        metadata={"approval_type": approval["type"], "status": decision},
    )

    # TD-11: this endpoint records the durable decision only. The suspended
    # RunService polls this row and owns all run-state transitions.
    run_id = approval["run_id"]
    if run_id and approval["type"] in (
        "high_risk",
        "capability_upgrade",
        "business_tool",
    ):
        # TD-06-T2: approving a capability upgrade installs the capability onto
        # the employee's profile (+ agent_capabilities row) before resuming.
        if approval["type"] == "capability_upgrade" and decision == "approved":
            from app.runtime.upgrade import UpgradeError, execute_upgrade

            try:
                appr_payload = json.loads(approval["payload_json"] or "{}")
            except (ValueError, TypeError):
                appr_payload = {}
            cap_key = (
                payload.approved_capability_key
                or appr_payload.get("suggested_capability_key")
            )
            try:
                execute_upgrade(
                    conn,
                    approval=dict(approval),
                    approved_capability_key=cap_key or "",
                    provisioner=build_provisioner_from_settings(),
                )
            except UpgradeError as exc:
                raise HTTPException(status_code=400, detail=f"能力升级失败：{exc}")

        task_id = approval["task_id"]
        if task_id:
            add_task_event(
                conn,
                workspace_id=workspace["id"],
                task_id=task_id,
                kind="approval_resolved",
                title="老板已确认通过" if decision == "approved" else "老板已驳回",
                content=approval["title"],
                conversation_id=approval["conversation_id"],
                agent_id=approval["agent_id"],
            )
    else:
        # Legacy approval (attached to a task, not a run): update task + capture
        # agent experience as before.
        task_id = approval["task_id"]
        if task_id:
            add_task_event(
                conn,
                workspace_id=workspace["id"],
                task_id=task_id,
                kind="approval_resolved",
                title="老板已确认" if decision == "approved" else "老板已驳回",
                content=approval["title"],
                conversation_id=approval["conversation_id"],
                agent_id=approval["agent_id"],
            )
            capture_agent_experience_from_approval(
                conn,
                workspace_id=workspace["id"],
                approval=approval,
                resolution=decision,
            )
            if decision == "approved":
                update_task(
                    conn,
                    workspace_id=workspace["id"],
                    task_id=task_id,
                    changes={"status": "已完成", "progress": 100},
                )
            else:
                update_task(
                    conn,
                    workspace_id=workspace["id"],
                    task_id=task_id,
                    changes={"status": "阻塞", "progress": 80},
                )

    updated = conn.execute(
        "SELECT * FROM approvals WHERE id = ?", (approval_id,)
    ).fetchone()
    return serialize_approval(updated)


@router.post("/approvals/{approval_id}/answer")
def answer_clarification(
    approval_id: str,
    answer: str = Body(..., embed=True),
    current_user: Row = Depends(get_current_user),
    conn: Database = Depends(get_db),
):
    """TD-03-T4: answer an employee's clarification request and resume its run.

    The answer is recorded as a chat message (so it enters conversation history)
    and the suspended RunService observes the database decision. NOTE: ACP permission responses
    carry only allow/deny, so the paused run resumes as "proceed" rather than
    receiving the answer text inline — the employee picks the answer up from
    conversation history on its next turn. Full inline injection needs a Hermes
    resume API and is tracked as a follow-up.
    """
    workspace = get_workspace_for_user(conn, current_user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    approval = conn.execute(
        "SELECT * FROM approvals WHERE id = ? AND workspace_id = ? AND type = 'clarification'",
        (approval_id, workspace["id"]),
    ).fetchone()
    if approval is None:
        raise HTTPException(status_code=404, detail="澄清请求不存在")
    if approval["status"] == "expired":
        raise HTTPException(status_code=409, detail="该请求已超时自动拒绝，无法再操作")
    if approval["status"] != "pending":
        raise HTTPException(status_code=409, detail="澄清请求已处理")

    conn.execute(
        "UPDATE approvals SET status = 'answered', resolved_by = ?, resolved_at = ? "
        "WHERE id = ?",
        (current_user["id"], now_iso(), approval_id),
    )
    from app.services.company_memory import record_company_event

    record_company_event(
        conn,
        workspace_id=workspace["id"],
        event_type="decision",
        source_id=approval_id,
        title="澄清回答",
        content=answer,
        conversation_id=approval["conversation_id"],
        task_id=approval["task_id"],
        actor_user_id=current_user["id"],
        importance=4.0,
        metadata={"approval_type": "clarification", "status": "answered"},
    )
    if approval["conversation_id"]:
        add_message(
            conn,
            conversation_id=approval["conversation_id"],
            sender_type="user",
            sender_id=current_user["id"],
            content=answer,
        )
    conn.commit()
    updated = conn.execute(
        "SELECT * FROM approvals WHERE id = ?", (approval_id,)
    ).fetchone()
    return serialize_approval(updated)


def capture_agent_experience_from_approval(
    conn: Database,
    *,
    workspace_id: str,
    approval: Row,
    resolution: str,
) -> None:
    agent_id = approval["agent_id"]
    task_id = approval["task_id"]
    if not agent_id or not task_id:
        return

    task = conn.execute(
        "SELECT * FROM tasks WHERE id = ? AND workspace_id = ?",
        (task_id, workspace_id),
    ).fetchone()
    if task is None:
        return

    latest_output = conn.execute(
        """
        SELECT * FROM task_outputs
        WHERE workspace_id = ? AND task_id = ? AND agent_id = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (workspace_id, task_id, agent_id),
    ).fetchone()
    output_excerpt = latest_output["content"][:180] if latest_output else ""
    if resolution == "approved":
        outcome = "success"
        summary = f"完成任务《{task['title']}》，老板已确认通过。"
        lessons = (
            f"可复用经验：围绕「{task['title']}」的输出已通过验收。"
            + (f" 关键产出：{output_excerpt}" if output_excerpt else "")
        )
    else:
        outcome = "lesson"
        summary = f"任务《{task['title']}》被老板驳回，需要重新推进。"
        lessons = (
            f"改进提醒：下次处理「{task['title']}」前先补充确认标准、风险和老板要拍板的问题。"
            + (f" 本次产出片段：{output_excerpt}" if output_excerpt else "")
        )

    add_agent_experience(
        conn,
        workspace_id=workspace_id,
        agent_id=agent_id,
        task_id=task_id,
        outcome=outcome,
        summary=summary,
        lessons=lessons,
    )


def _build_hermes_prompt(
    user_message: Row, discussion_context: str, cognitive_context: str = ""
) -> str:
    """Prompt fed to a Hermes employee. Persona comes from the profile's SOUL;
    this carries the situational context + the boss's message."""
    latest = user_message["content"]
    sections = []
    if cognitive_context:
        sections.append("【本次相关公司记忆】\n" + cognitive_context[:48000])
    if discussion_context:
        sections.append("【当前讨论】\n" + discussion_context)
    sections.append("【当前同事提出的信息】\n" + latest)
    sections.append("请以你的姓名和岗位身份，基于事实和专业判断简洁发言；不要提及运行时实现。")
    return "\n\n".join(sections)


async def _stream_hermes_reply(
    conn: Database,
    *,
    workspace: Row,
    conversation: Row,
    agent: Row,
    user_message: Row,
    discussion_context: str,
    profile: str,
) -> AsyncGenerator:
    """真 Hermes 执行路径（流式）——唯一能触发 ADR 0008 审批门的路径。"""
    logger.info("agent_reply_via_hermes", agent_id=agent["id"], profile=profile)
    context_manifest = _build_context_manifest(
        conn,
        workspace=workspace,
        conversation=conversation,
        agent=agent,
        current_text=user_message["content"],
        discussion_context=discussion_context,
    )
    work_root = os.path.abspath(settings.hermes_work_root or ".hermes-data")
    ctx = RunContext(
        run_id="",
        prompt=_build_hermes_prompt(
            user_message, discussion_context, context_manifest["text"]
        ),
        workdir=os.path.join(work_root, profile, "work", "runs", new_id("run")),
        profile=profile,
        agent_id=agent["id"],
        workspace_id=workspace["id"],
        conversation_id=conversation["id"],
        context_manifest_id=context_manifest["id"],
    )
    async for event in stream_agent_run(
        conn,
        ctx=ctx,
        backend=HermesBackend(hermes_bin=settings.hermes_bin),
        input_message_id=user_message["id"],
        permission_resolver=make_bridge_resolver(conn),  # TD-03-T4: suspend/resume
    ):
        yield event


async def _run_action_bridge_stream(
    conn: Database,
    *,
    workspace: Row,
    conversation: Row,
    agent: Row,
    user_message: Row,
    discussion_context: str = "",
) -> AsyncGenerator:
    """Agent Action Bridge（流式）：function-calling loop。

    逐段产出 chunk 事件，结尾把完整回复落库并产出 message 事件；
    空产出则没有 message 事件（由调用方决定是否兜底）。
    """
    deepseek = deepseek_client_for_workspace(conn, workspace["id"])
    history = load_llm_history(conn, conversation["id"])
    related_tasks, knowledge_sources, agent_experiences = load_agent_llm_context(
        conn,
        workspace_id=workspace["id"],
        conversation_id=conversation["id"],
        agent_id=agent["id"],
        query=user_message["content"],
    )
    context_manifest = _build_context_manifest(
        conn,
        workspace=workspace,
        conversation=conversation,
        agent=agent,
        current_text=user_message["content"],
        discussion_context=discussion_context,
    )
    tool_chunks: list[str] = []
    async for ev in run_function_loop(
        conn=conn,
        workspace_id=workspace["id"],
        workspace_name=workspace["name"],
        agent=agent,
        history=history,
        user_message_content=user_message["content"],
        deepseek_client=deepseek,
        related_tasks=related_tasks,
        knowledge_sources=knowledge_sources,
        agent_experiences=agent_experiences,
        cognitive_context=context_manifest["text"],
    ):
        yield ev
        if ev["type"] == "chunk":
            tool_chunks.append(ev["content"])
    # Persist the final message
    reply_text = "".join(tool_chunks).strip()
    if reply_text and conversation["id"]:
        msg = add_message(
            conn, conversation_id=conversation["id"],
            sender_type="agent", sender_id=agent["id"],
            content=reply_text,
            provider="deepseek", model=deepseek.model,
        )
        conn.commit()
        yield {"type": "message", "message": msg}


async def _stream_reply_events(
    conn: Database,
    *,
    workspace: Row,
    conversation: Row,
    agent: Row,
    user_message: Row,
    discussion_context: str = "",
    allow_action_bridge: bool = True,
) -> AsyncGenerator:
    """Produce an agent's reply as {type: chunk|message|...} events.

    Routing order matters: an employee with a real, ready Hermes profile must
    go straight to Hermes — that's the only path that can ever hit the
    approval gate (ADR 0008) for dangerous actions. Previously this tried the
    Agent Action Bridge (function_loop) FIRST for every employee regardless
    of Hermes status; since function_loop always "succeeds" (it has its own
    no-tool-needed conversational fallback), Hermes was never reached even
    for employees explicitly granted real capabilities — the approval gate
    was unreachable dead code in production. Verified live (2026-07-15):
    an employee with a granted `run_tests` capability and a real ready
    Hermes profile was still answering "I don't have file system access"
    from the Agent Action Bridge fallback instead of running for real.

    Employees without a Hermes profile keep using the Agent Action Bridge
    (create_employee/create_task/etc.), then the temporary DeepSeek path.
    Discussion turns pass ``allow_action_bridge=False`` so no participant can
    mutate the company before a consensus brief is launched.

    Outside discussion, the secretary (source == "system_secretary") tries the Agent
    Action Bridge FIRST even when she has a ready Hermes profile — her core
    job (create_employee/create_group/create_task) lives in the system tools
    that only function_loop has; sending her straight to Hermes means she can
    only ever *talk about* hiring instead of actually doing it. But a bridge
    round that produced text WITHOUT calling any of those system tools isn't
    evidence her job got done — it's just conversational filler (function_loop
    always has a no-tool-needed fallback), and the request may well have been
    a real task she now has real capabilities for. So only a bridge round
    that actually invoked a tool short-circuits here; a tool-less bridge reply
    (like a function_loop exception or empty output) still falls through to
    her Hermes profile below, so granting her a capability isn't a no-op.
    """
    profile = resolve_hermes_profile(conn, agent["id"])
    is_secretary = agent.get("source") == "system_secretary"
    if profile and (not is_secretary or not allow_action_bridge):
        async for event in _stream_hermes_reply(
            conn,
            workspace=workspace,
            conversation=conversation,
            agent=agent,
            user_message=user_message,
            discussion_context=discussion_context,
            profile=profile,
        ):
            yield event
        return

    if allow_action_bridge:
        # ── Agent Action Bridge (streaming): try function loop ──
        bridge_used_tool = False
        bridge_failed = False
        try:
            async for ev in _run_action_bridge_stream(
                conn,
                workspace=workspace,
                conversation=conversation,
                agent=agent,
                user_message=user_message,
                discussion_context=discussion_context,
            ):
                if ev["type"] == "tool_call":
                    bridge_used_tool = True
                yield ev
        except Exception as exc:
            logger.warning("function_loop_failed", agent_id=agent["id"], error=str(exc))
            bridge_failed = True
        if not bridge_failed and not is_secretary:
            # 普通员工保持原行为——function_loop 走完（哪怕空产出）也不回落。
            return
        if not bridge_failed and is_secretary and bridge_used_tool:
            # 她的本职工作（招人/建任务等系统工具）真的被调用了，到此为止。
            return

        # 小秘私聊兜底：Bridge 没有真正调用系统工具时走 Hermes。
        if profile:
            async for event in _stream_hermes_reply(
                conn,
                workspace=workspace,
                conversation=conversation,
                agent=agent,
                user_message=user_message,
                discussion_context=discussion_context,
                profile=profile,
            ):
                yield event
            return

    # Temporary DeepSeek execution layer (employees without a Hermes profile).
    logger.info("agent_reply_via_deepseek", agent_id=agent["id"])
    full_reply = ""
    async for chunk_text in await _stream_agent_reply(
        conn,
        workspace=workspace,
        conversation=conversation,
        conversation_id=conversation["id"],
        agent=agent,
        user_message=user_message,
        discussion_context=discussion_context,
    ):
        full_reply += chunk_text
        yield {"type": "chunk", "content": chunk_text}
    msg = add_message(
        conn,
        conversation_id=conversation["id"],
        sender_type="agent",
        sender_id=agent["id"],
        content=full_reply,
        provider="deepseek",
        model=settings.deepseek_model,
    )
    conn.commit()
    yield {"type": "message", "message": msg}


async def _stream_agent_reply(
    conn: Database,
    *,
    workspace: Row,
    conversation: Row,
    conversation_id: str,
    agent: Row,
    user_message: Row,
    discussion_context: str = "",
) -> AsyncGenerator:
    """Stream agent reply using DeepSeek streaming API.

    Yields text chunks. The caller is responsible for collecting and persisting.
    """
    from collections.abc import AsyncGenerator as _AG

    history = load_llm_history(conn, conversation_id)
    latest_user_content = next(
        (message.content for message in reversed(history) if message.role == "user"),
        "",
    )
    related_tasks, knowledge_sources, agent_experiences = load_agent_llm_context(
        conn,
        workspace_id=workspace["id"],
        conversation_id=conversation_id,
        agent_id=agent["id"],
        query=latest_user_content,
    )
    context_manifest = _build_context_manifest(
        conn,
        workspace=workspace,
        conversation=conversation,
        agent=agent,
        current_text=latest_user_content or user_message["content"],
        discussion_context=discussion_context,
    )

    request = LlmChatRequest(
        company_name=workspace["name"],
        conversation_title=conversation_title(conversation, agent),
        agent=LlmChatAgent(
            id=agent["id"],
            name=agent["name"],
            role=agent["role"],
            department=agent["department_name"],
            prompt=agent["prompt"],
            skills=json.loads(agent["skills_json"]),
        ),
        messages=history,
        related_tasks=related_tasks,
        knowledge_sources=knowledge_sources,
        agent_experiences=agent_experiences,
        discussion_context=discussion_context,
        cognitive_context=context_manifest["text"],
        context_manifest_id=context_manifest["id"],
    )

    return deepseek_client_for_workspace(conn, workspace["id"]).complete_stream(request)


def make_speaker_selector(conn: Database, workspace_id: str):
    """Build the moderator-LLM callback injected into run_discussion_round.

    The same callback serves speaker selection, convergence, brief drafting,
    and one repair attempt. Its user message must therefore stay neutral;
    task-specific instructions belong entirely to the injected system prompt.
    All selection logic (@mention priority, JSON parsing/validation,
    round-robin fallback) lives in orchestration/discussion.py.
    """

    async def _complete(prompt: str) -> str:
        completion = await deepseek_client_for_workspace(conn, workspace_id).complete(
            LlmChatRequest(
                agent=LlmChatAgent(
                    id="system",
                    name="主持人",
                    role="讨论主持人",
                    prompt=prompt,
                ),
                messages=[
                    LlmChatMessage(role="user", content="请严格按照系统指令完成输出")
                ],
            )
        )
        return completion.reply

    return _complete


def _create_brief_from_draft(
    conn: Database,
    *,
    workspace_id: str,
    conversation_id: str,
    draft: dict,
    reply_agents: list[Row],
) -> Row | None:
    """讨论收敛后自动落一条 draft 共识 brief，返回刚插入的 BRIEF_CARD 系统消息。

    去重：该会话已有 status='draft' 的 brief 就跳过（防止多轮讨论重复发卡）。
    任何失败静默兜底返回 None——出不了卡片绝不能让发消息接口报错。
    """
    try:
        goal = (draft.get("goal") or "").strip()
        if not goal or not reply_agents:
            return None
        existing = conn.execute(
            """SELECT id FROM consensus_briefs
            WHERE discussion_conversation_id = ? AND status = 'draft' LIMIT 1""",
            (conversation_id,),
        ).fetchone()
        if existing is not None:
            return None
        create_brief(
            conn,
            workspace_id=workspace_id,
            discussion_conversation_id=conversation_id,
            goal=goal,
            scope=draft.get("scope") or "",
            constraints=draft.get("constraints") or "",
            success_criteria=draft.get("success_criteria") or "",
            owner_agent_id=draft.get("owner_agent_id"),
            participant_agent_ids=[a["id"] for a in reply_agents],
            work_items=draft.get("work_items") or [],
            created_by_agent_id=reply_agents[0]["id"],
        )
        conn.commit()
        return conn.execute(
            """SELECT * FROM messages
            WHERE conversation_id = ? AND sender_type = 'system'
            ORDER BY created_at DESC LIMIT 1""",
            (conversation_id,),
        ).fetchone()
    except Exception as exc:
        logger.warning(
            "auto_brief_draft_failed", conversation_id=conversation_id, error=str(exc)
        )
        return None


async def _complete_hermes_reply(
    conn: Database,
    *,
    workspace: Row,
    conversation_id: str,
    agent: Row,
    user_message: Row,
    discussion_context: str,
    profile: str,
) -> Row | None:
    """真 Hermes 执行路径（非流式）——与 _stream_hermes_reply 同一条路径，
    只收集最终 message。"""
    logger.info("agent_reply_via_hermes", agent_id=agent["id"], profile=profile)
    conversation = conn.execute(
        "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    context_manifest = _build_context_manifest(
        conn,
        workspace=workspace,
        conversation=conversation,
        agent=agent,
        current_text=user_message["content"],
        discussion_context=discussion_context,
    )
    work_root = os.path.abspath(settings.hermes_work_root or ".hermes-data")
    ctx = RunContext(
        run_id="",
        prompt=_build_hermes_prompt(
            user_message, discussion_context, context_manifest["text"]
        ),
        workdir=os.path.join(work_root, profile, "work", "runs", new_id("run")),
        profile=profile,
        agent_id=agent["id"],
        workspace_id=workspace["id"],
        conversation_id=conversation_id,
        context_manifest_id=context_manifest["id"],
    )
    final_message = None
    async for event in stream_agent_run(
        conn,
        ctx=ctx,
        backend=HermesBackend(hermes_bin=settings.hermes_bin),
        input_message_id=user_message["id"],
        permission_resolver=make_bridge_resolver(conn),
    ):
        if event.get("type") == "message" and event.get("message"):
            final_message = event["message"]
    return final_message


async def complete_agent_reply(
    conn: Database,
    *,
    workspace: Row,
    conversation: Row,
    conversation_id: str,
    agent: Row,
    user_message: Row,
    discussion_context: str = "",
    use_tools: bool = True,
) -> Row:
    # Same routing-order fix as _stream_reply_events: an employee with a real
    # ready Hermes profile must go straight to Hermes, or the approval gate
    # (ADR 0008) is unreachable for this (non-streaming) path too.
    # Exception (also same as _stream_reply_events): the secretary tries the
    # Agent Action Bridge FIRST even with a ready profile — her core job lives
    # in the system tools only function_loop has.
    profile = resolve_hermes_profile(conn, agent["id"])
    is_secretary = agent.get("source") == "system_secretary"
    if profile and not is_secretary:
        return await _complete_hermes_reply(
            conn,
            workspace=workspace,
            conversation_id=conversation_id,
            agent=agent,
            user_message=user_message,
            discussion_context=discussion_context,
            profile=profile,
        )

    # ── Agent Action Bridge: function-calling loop ──
    # When use_tools is True (default for DM), the agent can call tools to
    # create employees, tasks, groups, etc. instead of just chatting.
    history = load_llm_history(conn, conversation_id)

    if use_tools:
        deepseek = deepseek_client_for_workspace(conn, workspace["id"])
        related_tasks, knowledge_sources, agent_experiences = load_agent_llm_context(
            conn,
            workspace_id=workspace["id"],
            conversation_id=conversation_id,
            agent_id=agent["id"],
            query=user_message["content"],
        )
        context_manifest = _build_context_manifest(
            conn,
            workspace=workspace,
            conversation=conversation,
            agent=agent,
            current_text=user_message["content"],
        )
        try:
            tool_chunks: list[str] = []
            async for ev in run_function_loop(
                conn=conn,
                workspace_id=workspace["id"],
                workspace_name=workspace["name"],
                agent=agent,
                history=history,
                user_message_content=user_message["content"],
                deepseek_client=deepseek,
                related_tasks=related_tasks,
                knowledge_sources=knowledge_sources,
                agent_experiences=agent_experiences,
                cognitive_context=context_manifest["text"],
            ):
                if ev["type"] == "chunk":
                    tool_chunks.append(ev["content"])
            reply_text = "".join(tool_chunks).strip()
            if reply_text:
                agent_message = add_message(
                    conn, conversation_id=conversation_id,
                    sender_type="agent", sender_id=agent["id"],
                    content=reply_text,
                    provider="deepseek", model=deepseek.model,
                )
                conn.commit()
                return agent_message
        except Exception as exc:
            logger.warning("function_loop_failed", agent_id=agent["id"], error=str(exc))
            # Fall through to Hermes (secretary) or normal completion

    # 小秘兜底：Bridge 异常/空产出才轮到她的 Hermes profile
    if profile and is_secretary:
        return await _complete_hermes_reply(
            conn,
            workspace=workspace,
            conversation_id=conversation_id,
            agent=agent,
            user_message=user_message,
            discussion_context=discussion_context,
            profile=profile,
        )
    latest_user_content = next(
        (message.content for message in reversed(history) if message.role == "user"),
        "",
    )
    related_tasks, knowledge_sources, agent_experiences = load_agent_llm_context(
        conn,
        workspace_id=workspace["id"],
        conversation_id=conversation_id,
        agent_id=agent["id"],
        query=latest_user_content,
    )
    completion = await deepseek_client_for_workspace(conn, workspace["id"]).complete(
        LlmChatRequest(
            company_name=workspace["name"],
            conversation_title=conversation_title(conversation, agent),
            agent=LlmChatAgent(
                id=agent["id"],
                name=agent["name"],
                role=agent["role"],
                department=agent["department_name"],
                prompt=agent["prompt"],
                skills=json.loads(agent["skills_json"]),
            ),
            messages=history,
            related_tasks=related_tasks,
            knowledge_sources=knowledge_sources,
            agent_experiences=agent_experiences,
            discussion_context=discussion_context,
        )
    )

    agent_message = add_message(
        conn,
        conversation_id=conversation_id,
        sender_type="agent",
        sender_id=agent["id"],
        content=completion.reply,
        provider=completion.provider,
        model=completion.model,
    )
    run_id = new_id("run")
    now = now_iso()
    conn.execute(
        """
        INSERT INTO runs (
          id, workspace_id, conversation_id, agent_id, status, input_message_id,
          output_message_id, provider, model, usage_json, created_at, completed_at
        )
        VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            workspace["id"],
            conversation_id,
            agent["id"],
            user_message["id"],
            agent_message["id"],
            completion.provider,
            completion.model,
            json.dumps(completion.usage or {}),
            now,
            now,
        ),
    )
    related_task_rows = conn.execute(
        """
        SELECT id, title FROM tasks
        WHERE workspace_id = ? AND conversation_id = ?
        ORDER BY updated_at DESC
        LIMIT 12
        """,
        (workspace["id"], conversation_id),
    ).fetchall()
    for task in related_task_rows:
        add_task_event(
            conn,
            workspace_id=workspace["id"],
            task_id=task["id"],
            kind="agent_output_generated",
            title=f"{agent['name']} 生成了产出",
            content=completion.reply[:600],
            conversation_id=conversation_id,
            agent_id=agent["id"],
        )
        add_task_output(
            conn,
            workspace_id=workspace["id"],
            task_id=task["id"],
            title=f"{agent['name']} 的回复",
            content=completion.reply,
            conversation_id=conversation_id,
            agent_id=agent["id"],
        )
    return agent_message


def _recruit_intent_for_conversation(
    conversation: Row,
    reply_agents: list[Row],
    content: str,
) -> dict | None:
    """招聘意图正则只在小秘的一对一私聊里生效。

    群聊（或其他员工的 DM）里说"招个分析师"是在和团队讨论需求，不该被
    正则截胡直接建一个无能力的纸片员工（source="chat_factory"）——真正的
    招聘走小秘 DM 或团队编译器（ADR 0009）。
    """
    if conversation["kind"] != "dm":
        return None
    if not reply_agents or reply_agents[0].get("source") != "system_secretary":
        return None
    return extract_recruit_intent(content)


def resolve_reply_agents(
    conn: Database,
    workspace_id: str,
    conversation: Row,
    payload: SendMessageRequest,
) -> list[Row]:
    if conversation["kind"] == "dm":
        if not conversation["agent_id"]:
            return []
        agent = load_agent_for_reply(conn, workspace_id, conversation["agent_id"])
        return [agent] if agent else []

    if payload.target_agent_id:
        agent = load_agent_for_reply(conn, workspace_id, payload.target_agent_id)
        if agent is None:
            return []
        member = conn.execute(
            """
            SELECT 1 FROM conversation_members
            WHERE conversation_id = ? AND agent_id = ?
            """,
            (conversation["id"], agent["id"]),
        ).fetchone()
        return [agent] if member else []

    rows = conn.execute(
        """
        SELECT agents.*, departments.name AS department_name
        FROM conversation_members
        JOIN agents ON agents.id = conversation_members.agent_id
        JOIN departments ON departments.id = agents.department_id
        WHERE conversation_members.conversation_id = ?
          AND agents.workspace_id = ?
        ORDER BY conversation_members.id
        """,
        (conversation["id"], workspace_id),
    ).fetchall()
    return rows


def load_agent_for_reply(
    conn: Database, workspace_id: str, agent_id: str
) -> Row | None:
    return conn.execute(
        """
        SELECT agents.*, departments.name AS department_name
        FROM agents
        JOIN departments ON departments.id = agents.department_id
        WHERE agents.id = ? AND agents.workspace_id = ?
        """,
        (agent_id, workspace_id),
    ).fetchone()


def load_llm_history(
    conn: Database, conversation_id: str
) -> list[LlmChatMessage]:
    rows = conn.execute(
        """
        SELECT messages.*, agents.name AS agent_name
        FROM messages
        LEFT JOIN agents ON agents.id = messages.sender_id
        WHERE conversation_id = ? AND sender_type IN ('user', 'agent')
        ORDER BY created_at DESC
        LIMIT 12
        """,
        (conversation_id,),
    ).fetchall()
    messages = []
    for row in reversed(rows):
        messages.append(
            LlmChatMessage(
                role="user" if row["sender_type"] == "user" else "assistant",
                name="老板" if row["sender_type"] == "user" else row["agent_name"],
                content=row["content"],
            )
        )
    return messages


def load_related_task_context(conn: Database, conversation_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
          tasks.id, tasks.title, tasks.description, tasks.priority,
          tasks.status, tasks.progress, agents.name AS owner_name
        FROM tasks
        LEFT JOIN agents ON agents.id = tasks.owner_agent_id
        WHERE tasks.conversation_id = ?
        ORDER BY
          CASE tasks.priority
            WHEN 'P0' THEN 0
            WHEN 'P1' THEN 1
            ELSE 2
          END,
          tasks.updated_at DESC
        LIMIT 12
        """,
        (conversation_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "description": row["description"],
            "priority": row["priority"],
            "status": row["status"],
            "progress": row["progress"],
            "owner_name": row["owner_name"],
        }
        for row in rows
    ]


def load_agent_experience_context(
    conn: Database, agent_id: str
) -> list[LlmAgentExperience]:
    rows = conn.execute(
        """
        SELECT id, task_id, outcome, summary, lessons
        FROM agent_experiences
        WHERE agent_id = ?
        ORDER BY created_at DESC
        LIMIT 6
        """,
        (agent_id,),
    ).fetchall()
    return [
        LlmAgentExperience(
            id=row["id"],
            task_id=row["task_id"],
            outcome=row["outcome"],
            summary=row["summary"],
            lessons=row["lessons"],
        )
        for row in rows
    ]


def load_agent_llm_context(
    conn: Database,
    *,
    workspace_id: str,
    conversation_id: str,
    agent_id: str,
    query: str,
) -> tuple[list[LlmTaskContext], list[LlmKnowledgeSource], list[LlmAgentExperience]]:
    """Load the same company-knowledge / related-task / experience context
    regardless of which reply path (Agent Action Bridge or plain DeepSeek
    completion) ends up using it — keeps both in sync."""
    related_tasks = [
        LlmTaskContext(**row) for row in load_related_task_context(conn, conversation_id)
    ]
    knowledge_sources = [
        LlmKnowledgeSource(
            id=source["id"],
            title=source["title"],
            category=source["category"],
            content=source["content"][:2000],
        )
        for source in load_knowledge_context(conn, workspace_id=workspace_id, query=query)
    ]
    agent_experiences = load_agent_experience_context(conn, agent_id)
    return related_tasks, knowledge_sources, agent_experiences


def conversation_title(conversation: Row, agent: Row) -> str:
    if conversation["kind"] == "dm":
        return f"私聊 · {agent['name']}"
    return f"群聊 · {conversation['name']}"
