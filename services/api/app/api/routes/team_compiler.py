"""Team compiler endpoints — one paragraph in, a real team out.

POST /agents/draft-team: parse a free-text org description into N role
drafts (no side effects — nothing is created yet, the owner reviews/edits
first).
POST /agents/create-team: take the (possibly edited) draft list and actually
create every member for real, through the same provision_new_agent() path
every other hiring flow uses, then drop them all into one new group
conversation (they were just hired together as one team).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_current_user, get_workspace_id
from app.core.database import Database, Row, get_db
from app.orchestration.team_compiler import (
    TeamDraftError,
    build_team_draft_prompt,
    parse_team_draft,
)
from app.runtime.hermes_control import HermesControlUnavailable, run_hermes_control
from app.schemas.agent_spec import (
    CreateTeamMemberOut,
    CreateTeamRequest,
    CreateTeamResponse,
    DraftTeamRequest,
    DraftTeamResponse,
    TeamMemberDraft,
)
from app.services.workspace import (
    add_message,
    create_agent,
    create_dm_conversation,
    ensure_department,
    new_id,
    now_iso,
    provision_new_agent,
)

router = APIRouter(tags=["team-compiler"])


@router.post("/agents/draft-team", response_model=DraftTeamResponse)
async def draft_team(
    payload: DraftTeamRequest,
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> DraftTeamResponse:
    """Parse the description into role drafts. Nothing is created here —
    this is the "预览" step: the owner reviews/edits the drafts before
    POST /agents/create-team actually provisions anyone."""
    operator = conn.execute(
        """SELECT agents.id, conversations.id AS conversation_id
        FROM agents
        JOIN agent_specs ON agent_specs.agent_id = agents.id
        LEFT JOIN conversations ON conversations.agent_id = agents.id
          AND conversations.workspace_id = agents.workspace_id
          AND conversations.kind = 'dm'
        WHERE agents.workspace_id = ? AND agents.source = 'system_secretary'
        ORDER BY conversations.created_at LIMIT 1""",
        (workspace_id,),
    ).fetchone()
    if operator is None:
        raise HTTPException(status_code=503, detail="团队编译器 Hermes 员工尚未就绪")
    try:
        draft_text = await run_hermes_control(
            conn,
            workspace_id=workspace_id,
            agent_id=operator["id"],
            conversation_id=operator["conversation_id"],
            prompt=(
                build_team_draft_prompt()
                + "\n\n老板给出的团队需求：\n"
                + payload.description
                + "\n\n只输出符合上述约束的 JSON。"
            ),
            purpose="team-draft",
        )
    except HermesControlUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    try:
        members = parse_team_draft(draft_text, source_request=payload.description)
    except TeamDraftError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return DraftTeamResponse(members=[TeamMemberDraft(**m) for m in members])


@router.post("/agents/create-team", response_model=CreateTeamResponse)
def create_team(
    payload: CreateTeamRequest,
    current_user: Row = Depends(get_current_user),
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> CreateTeamResponse:
    """Actually create every drafted member (real create_agent + real
    provisioning when settings.hermes_provisioning is on), then drop them
    all into one new group conversation — they were just hired together as
    one team, so they start in one shared room; splitting off focused
    sub-groups for a specific task stays the normal manual "拉群" action."""
    created: list[CreateTeamMemberOut] = []
    agent_ids: list[str] = []
    for member in payload.members:
        department = ensure_department(conn, workspace_id, member.department or member.role)
        agent_id = create_agent(
            conn,
            workspace_id=workspace_id,
            department_id=department["id"],
            name=member.name,
            role=member.role,
            description=member.description,
            prompt=f"你是一名{member.role}。{member.description}",
            skills=[],
            mcps=[],
            source="team_compiler",
        )
        create_dm_conversation(conn, workspace_id, agent_id)
        provision_new_agent(
            conn,
            agent_id=agent_id,
            workspace_id=workspace_id,
            role_name=member.role,
            source_request=f"团队编译器批量创建：{member.description}",
            responsibilities=member.responsibilities,
            capability_keys=member.capability_keys,
        )
        agent_ids.append(agent_id)
        created.append(
            CreateTeamMemberOut(
                id=agent_id, name=member.name, role=member.role,
                department=member.department or member.role,
            )
        )

    conversation_id = None
    if len(agent_ids) > 1:
        conversation_id = new_id("conv")
        created_at = now_iso()
        group_name = payload.group_name or "新团队"
        conn.execute(
            """
            INSERT INTO conversations (id, workspace_id, kind, name, unread, created_at, updated_at)
            VALUES (?, ?, 'group', ?, 0, ?, ?)
            """,
            (conversation_id, workspace_id, group_name, created_at, created_at),
        )
        for agent_id in agent_ids:
            conn.execute(
                "INSERT INTO conversation_members (conversation_id, agent_id) VALUES (?, ?)",
                (conversation_id, agent_id),
            )
        names = "、".join(m.name for m in payload.members)
        add_message(
            conn,
            conversation_id=conversation_id,
            sender_type="system",
            sender_id="",
            content=f"团队编译完成，{names} 已加入这个群。",
        )

    conn.commit()
    return CreateTeamResponse(agents=created, conversation_id=conversation_id)
