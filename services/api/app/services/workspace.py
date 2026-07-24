from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import re
from uuid import uuid4

from app.core.database import Database, Row
from app.orchestration.capability_catalog import (
    CATALOG,
    get_role_bundle,
    split_by_credentials,
    validate_capability_keys,
)
from app.services.templates import get_template, list_agent_templates, list_talent_categories


DEFAULT_DEPARTMENTS = ["老板办公室", "内容经营部"]
HUES = [262, 230, 300, 140, 55, 20, 330]
GLYPHS = ["✦", "▲", "◆", "◗", "✱", "◍", "◉"]

# NOTE: TASK_INTENT_PATTERNS and extract_task_intent have been removed.
# Task creation now requires a confirmed consensus_brief (see ADR 0006).
# Auto-task creation from chat intent is no longer supported.

RECRUIT_INTENT_PATTERNS = [
    r"(?:帮我|给我|请你|麻烦)?(?:招|招聘|创建|新建|配置|生成)(?:一个|一名|个)?(?P<role>[^，。！？\n]{2,30}?)(?:员工|智能体|agent|Agent)?$",
    r"(?:我想|我要|需要)(?:一个|一名|个)?(?P<role>[^，。！？\n]{2,30}?)(?:员工|智能体|agent|Agent)$",
]

ROLE_PRESETS = [
    {
        "keywords": ["市场", "竞品", "行业"],
        "department": "市场部",
        "skills": ["竞品分析", "数据报表"],
        "prompt": "你是一名市场分析师。负责竞品监控、行业信息整理和市场机会分析，输出可执行的洞察和建议。",
    },
    {
        "keywords": ["运营", "增长", "投放"],
        "department": "运营部",
        "skills": ["数据报表", "投放策略"],
        "prompt": "你是一名运营增长专家。负责目标拆解、渠道策略、投放节奏和经营复盘，输出可直接执行的增长方案。",
    },
    {
        "keywords": ["内容", "文案", "公众号", "短视频"],
        "department": "内容部",
        "skills": ["公众号文案", "SEO 优化"],
        "prompt": "你是一名内容策划。负责选题、文案、脚本和内容分发计划，输出清晰的内容方案和可发布草稿。",
    },
    {
        "keywords": ["销售", "客户", "客服", "线索"],
        "department": "增长与客户",
        "skills": ["客服话术", "数据报表"],
        "prompt": "你是一名销售客户专员。负责线索跟进、客户问题整理、报价支持和成交卡点复盘，关键承诺必须请老板确认。",
    },
    {
        "keywords": ["财务", "行政", "报表", "记账"],
        "department": "财务行政",
        "skills": ["数据报表"],
        "prompt": "你是一名财务行政助理。负责记账、对账、报表整理和行政事项提醒，异常支出和风险事项要及时上报。",
    },
]


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _project_company_event(conn: Database, callback) -> None:
    """Project a legacy write only when the TD-13 ledger exists.

    A few low-level unit tests intentionally build a minimal pre-TD-13 schema.
    Production databases always run the startup migration, so missing the table
    is the only condition that is safe to ignore here; real projection errors
    must still abort the surrounding transaction.
    """
    try:
        conn.execute("SELECT 1 FROM company_events LIMIT 1")
    except Exception as exc:
        message = str(exc).lower()
        if "company_events" in message and (
            "no such table" in message
            or "does not exist" in message
            or "undefined table" in message
        ):
            return
        raise
    callback()


def create_workspace_for_user(
    conn: Database,
    user_id: str,
    workspace_name: str,
) -> Row:
    created_at = now_iso()
    workspace_id = new_id("ws")
    conn.execute(
        """
        INSERT INTO workspaces (id, owner_user_id, name, onboarding_completed, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (workspace_id, user_id, workspace_name, False, created_at),
    )

    department_ids: dict[str, str] = {}
    for index, name in enumerate(DEFAULT_DEPARTMENTS):
        department_id = new_id("dept")
        department_ids[name] = department_id
        conn.execute(
            """
            INSERT INTO departments (id, workspace_id, name, sort_order, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (department_id, workspace_id, name, index, created_at),
        )

    secretary_id = create_agent(
        conn,
        workspace_id=workspace_id,
        department_id=department_ids["老板办公室"],
        name="小秘",
        role="老板秘书",
        description="负责接收老板想法、拆任务、拉群协作和提醒拍板",
        prompt="你是老板的贴身秘书兼幕僚长。接收老板的任何想法，转化为任务、招聘建议或群组讨论；跟踪全公司任务进度；需要老板拍板时，整理好决策要点再去打扰他。",
        skills=["任务拆解", "会议纪要", "信息检索"],
        mcps=[],
        hue=262,
        glyph="✦",
        source="system_secretary",
        joined="系统内置",
    )
    conversation_id = new_id("conv")
    conn.execute(
        """
        INSERT INTO conversations (id, workspace_id, kind, name, agent_id, unread, created_at, updated_at)
        VALUES (?, ?, 'dm', '', ?, 0, ?, ?)
        """,
        (conversation_id, workspace_id, secretary_id, created_at, created_at),
    )
    add_message(
        conn,
        conversation_id=conversation_id,
        sender_type="agent",
        sender_id=secretary_id,
        content="欢迎老板。我是小秘。你可以直接把想法丢给我，我会帮你拆任务、建议招募谁、或者拉群推进。",
    )
    _bootstrap_secretary_capabilities(conn, secretary_id, workspace_id)

    starter_agents = [
        {
            "name": "内容策划",
            "role": "内容策划",
            "description": "负责平台研究、受众洞察、选题规划和内容排期",
            "prompt": "你是内容策划。先核实平台、定位、受众、周期、数量和目标，再用可引用的资料制定选题与排期；不确定的事实必须标成假设。",
            "capabilities": ["web_scraping", "seo_content", "data_analysis"],
        },
        {
            "name": "内容主笔",
            "role": "内容主笔",
            "description": "负责标题、开场钩子、正文或脚本和行动号召",
            "prompt": "你是内容主笔。依据已确认的策略和来源写可直接审阅的内容，保持平台语气一致，事实性表述必须保留引用。",
            "capabilities": ["content_writing", "seo_content"],
        },
        {
            "name": "运营执行",
            "role": "运营执行",
            "description": "负责发布排期、素材建议、内容包组装和交付检查",
            "prompt": "你是运营执行。把已完成内容整理成结构化待发布内容包，检查排期、CTA、素材建议、来源和未知项；首版不得真实发布。",
            "capabilities": ["content_writing", "image_creation", "report_generation"],
        },
    ]
    starter_agent_ids = [secretary_id]
    for definition in starter_agents:
        agent_id = create_agent(
            conn,
            workspace_id=workspace_id,
            department_id=department_ids["内容经营部"],
            name=definition["name"],
            role=definition["role"],
            description=definition["description"],
            prompt=definition["prompt"],
            skills=[],
            mcps=[],
            source="system_content_team",
            joined="系统内置",
        )
        starter_agent_ids.append(agent_id)
        create_dm_conversation(conn, workspace_id, agent_id)
        provision_new_agent(
            conn,
            agent_id=agent_id,
            workspace_id=workspace_id,
            role_name=definition["role"],
            source_request="系统默认配置：自媒体内容经营团队",
            responsibilities=[definition["description"]],
            capability_keys=definition["capabilities"],
        )

    group_id = new_id("conv")
    group_created_at = now_iso()
    conn.execute(
        """INSERT INTO conversations (
          id, workspace_id, kind, name, agent_id, unread, created_at, updated_at
        ) VALUES (?, ?, 'group', '内容经营群', NULL, 0, ?, ?)""",
        (group_id, workspace_id, group_created_at, group_created_at),
    )
    for agent_id in starter_agent_ids:
        conn.execute(
            "INSERT INTO conversation_members (conversation_id, agent_id) VALUES (?, ?)",
            (group_id, agent_id),
        )
    add_message(
        conn,
        conversation_id=group_id,
        sender_type="system",
        sender_id="",
        content="内容经营团队已就位。先把平台、定位、受众、周期、数量、目标和约束讨论清楚，再提交完整分工给老板确认。",
    )
    return get_workspace_by_id(conn, workspace_id)


# Default capabilities every new secretary gets — deliberately restricted to
# risk_gate='auto' entries with zero required_credentials (see
# capability_catalog.CATALOG). provision() is all-or-nothing: a single
# credential-missing capability blocks the whole spec at
# 'blocked_on_credentials' with no real Hermes profile at all (see ADR 0008 /
# supply.provision docstring), so anything needing a token the owner hasn't
# configured would leave the secretary worse off than having no spec at all.
SECRETARY_DEFAULT_CAPABILITIES = [
    "task_delegation",
    "data_analysis",
    "report_generation",
]


def _bootstrap_secretary_capabilities(
    conn: Database, secretary_id: str, workspace_id: str
) -> None:
    """Give the default secretary a real, ready Hermes profile on day one.

    Thin wrapper around the shared provision_new_agent — see its docstring
    for the settings.hermes_provisioning gate and the credential-split
    behavior (moot here since every default capability is credential-free,
    but going through the one shared function keeps this consistent with
    every other hiring path instead of re-deriving the same dance).
    """
    provision_new_agent(
        conn,
        agent_id=secretary_id,
        workspace_id=workspace_id,
        role_name="老板秘书",
        source_request="系统默认配置：秘书上岗即具备基础助理能力",
        responsibilities=[],
        capability_keys=SECRETARY_DEFAULT_CAPABILITIES,
    )


def create_agent(
    conn: Database,
    *,
    workspace_id: str,
    department_id: str,
    name: str,
    role: str,
    description: str,
    prompt: str,
    skills: list[str],
    mcps: list[str],
    hue: int | None = None,
    glyph: str | None = None,
    source: str = "custom",
    joined: str = "今天入职",
) -> str:
    agent_count = conn.execute(
        "SELECT COUNT(*) AS count FROM agents WHERE workspace_id = ?", (workspace_id,)
    ).fetchone()["count"]
    agent_id = new_id("agent")
    conn.execute(
        """
        INSERT INTO agents (
          id, workspace_id, department_id, name, role, description, prompt,
          hue, glyph, status_kind, status_label, joined, source,
          skills_json, mcps_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'idle', '在线待命', ?, ?, ?, ?, ?)
        """,
        (
            agent_id,
            workspace_id,
            department_id,
            name,
            role,
            description,
            prompt,
            hue if hue is not None else HUES[agent_count % len(HUES)],
            glyph if glyph is not None else GLYPHS[agent_count % len(GLYPHS)],
            joined,
            source,
            json.dumps(skills, ensure_ascii=False),
            json.dumps(mcps, ensure_ascii=False),
            now_iso(),
        ),
    )
    return agent_id


def get_workspace_for_user(conn: Database, user_id: str) -> Row | None:
    return conn.execute(
        """
        SELECT * FROM workspaces
        WHERE owner_user_id = ?
        ORDER BY created_at ASC
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()


def get_workspace_by_id(conn: Database, workspace_id: str) -> Row:
    row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    if row is None:
        raise ValueError("workspace not found")
    return row


def ensure_department(
    conn: Database, workspace_id: str, department_name: str
) -> Row:
    """Find-or-create a department, returning the leaf.

    Accepts a "/"-separated path (e.g. "技术部/后端研发中心/数据组") and
    walks/creates each level as a real parent-child chain — a multi-level
    org chart, not a flat list with a slash-joined display string baked
    into `name` (which is what silently happened before this existed:
    every level collapsed into one department row at the top).
    """
    segments = [s.strip() for s in department_name.split("/") if s.strip()]
    if not segments:
        segments = [department_name]

    parent_id: str | None = None
    department: Row | None = None
    for segment in segments:
        department = _ensure_department_level(conn, workspace_id, parent_id, segment)
        parent_id = department["id"]
    return department


def _ensure_department_level(
    conn: Database, workspace_id: str, parent_id: str | None, name: str
) -> Row:
    if parent_id is None:
        row = conn.execute(
            "SELECT * FROM departments WHERE workspace_id = ? AND parent_id IS NULL AND name = ?",
            (workspace_id, name),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM departments WHERE workspace_id = ? AND parent_id = ? AND name = ?",
            (workspace_id, parent_id, name),
        ).fetchone()
    if row is not None:
        return row

    count = conn.execute(
        "SELECT COUNT(*) AS count FROM departments WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()["count"]
    department_id = new_id("dept")
    conn.execute(
        """
        INSERT INTO departments (id, workspace_id, name, parent_id, sort_order, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (department_id, workspace_id, name, parent_id, count, now_iso()),
    )
    return conn.execute("SELECT * FROM departments WHERE id = ?", (department_id,)).fetchone()


def add_message(
    conn: Database,
    *,
    conversation_id: str,
    sender_type: str,
    sender_id: str,
    content: str,
    provider: str | None = None,
    model: str | None = None,
) -> Row:
    message_id = new_id("msg")
    created_at = now_iso()
    conn.execute(
        """
        INSERT INTO messages (
          id, conversation_id, sender_type, sender_id, content,
          provider, model, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            conversation_id,
            sender_type,
            sender_id,
            content,
            provider,
            model,
            created_at,
        ),
    )
    conn.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?",
        (created_at, conversation_id),
    )
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    _project_company_event(
        conn,
        lambda: __import__(
            "app.services.company_memory", fromlist=["record_message_event"]
        ).record_message_event(conn, row),
    )
    return row


def serialize_workspace(row: Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "onboarding_completed": bool(row["onboarding_completed"]),
    }


def serialize_user(row: Row) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
    }


def serialize_department(row: Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "parent_id": row["parent_id"],
        "sort_order": row["sort_order"],
    }


def serialize_agent(row: Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "role": row["role"],
        "description": row["description"],
        "department_id": row["department_id"],
        "prompt": row["prompt"],
        "hue": row["hue"],
        "glyph": row["glyph"],
        "status_kind": row["status_kind"],
        "status_label": row["status_label"],
        "joined": row["joined"],
        "source": row["source"],
        "skills": json.loads(row["skills_json"]),
        "mcps": json.loads(row["mcps_json"]),
    }


def serialize_conversation(row: Row, member_ids: list[str]) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "name": row["name"],
        "agent_id": row["agent_id"],
        "member_ids": member_ids,
        "unread": row["unread"],
        "updated_at": row["updated_at"],
        "discussion_status": row["discussion_status"] or "discussing",
    }


def serialize_message(row: Row) -> dict:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "sender_type": row["sender_type"],
        "sender_id": row["sender_id"],
        "content": row["content"],
        "provider": row["provider"],
        "model": row["model"],
        "created_at": row["created_at"],
    }


def serialize_task(row: Row, suggestion: dict | None = None) -> dict:
    payload = {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "priority": row["priority"],
        "owner_agent_id": row["owner_agent_id"],
        "suggested_agent_id": None,
        "suggested_agent_reason": "",
        "status": row["status"],
        "progress": row["progress"],
        "conversation_id": row["conversation_id"],
        "due_date": row["due_date"],
        "parent_task_id": row["parent_task_id"],
        "consensus_brief_id": row["consensus_brief_id"],
        "task_plan_id": row.get("task_plan_id"),
        "plan_item_key": row.get("plan_item_key"),
        "expected_output": row.get("expected_output") or "",
        "output_type": row.get("output_type") or "markdown",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if suggestion:
        payload["suggested_agent_id"] = suggestion["agent_id"]
        payload["suggested_agent_reason"] = suggestion["reason"]
    return payload


def serialize_knowledge_source(row: Row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "category": row["category"],
        "content": row["content"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "origin": row.get("origin") or "manual",
        "source_ref": row.get("source_ref") or "",
    }


def serialize_task_event(row: Row) -> dict:
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "conversation_id": row["conversation_id"],
        "agent_id": row["agent_id"],
        "kind": row["kind"],
        "title": row["title"],
        "content": row["content"],
        "created_at": row["created_at"],
    }


def serialize_task_output(row: Row) -> dict:
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "conversation_id": row["conversation_id"],
        "agent_id": row["agent_id"],
        "title": row["title"],
        "output_type": row["output_type"],
        "content": row["content"],
        "created_at": row["created_at"],
    }


def serialize_approval(row: Row) -> dict:
    try:
        payload = json.loads(row.get("payload_json") or "{}")
    except (TypeError, ValueError):
        payload = {}
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "conversation_id": row["conversation_id"],
        "agent_id": row["agent_id"],
        "title": row["title"],
        "description": row["description"],
        "status": row["status"],
        "risk_level": row["risk_level"],
        "type": row.get("type") or "high_risk",
        "payload": payload,
        "resolved_by": row["resolved_by"],
        "resolved_at": row["resolved_at"],
        "created_at": row["created_at"],
    }


def serialize_agent_experience(row: Row) -> dict:
    return {
        "id": row["id"],
        "agent_id": row["agent_id"],
        "task_id": row["task_id"],
        "outcome": row["outcome"],
        "summary": row["summary"],
        "lessons": row["lessons"],
        "created_at": row["created_at"],
    }


def add_task_event(
    conn: Database,
    *,
    workspace_id: str,
    task_id: str,
    kind: str,
    title: str,
    content: str = "",
    conversation_id: str | None = None,
    agent_id: str | None = None,
) -> Row:
    event_id = new_id("event")
    created_at = now_iso()
    conn.execute(
        """
        INSERT INTO task_events (
          id, workspace_id, task_id, conversation_id, agent_id,
          kind, title, content, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            workspace_id,
            task_id,
            conversation_id,
            agent_id,
            kind,
            title,
            content,
            created_at,
        ),
    )
    row = conn.execute("SELECT * FROM task_events WHERE id = ?", (event_id,)).fetchone()
    _project_company_event(
        conn,
        lambda: __import__(
            "app.services.company_memory", fromlist=["record_task_event_projection"]
        ).record_task_event_projection(conn, row),
    )
    return row


def add_task_output(
    conn: Database,
    *,
    workspace_id: str,
    task_id: str,
    title: str,
    content: str,
    conversation_id: str | None = None,
    agent_id: str | None = None,
    output_type: str = "markdown",
) -> Row:
    output_id = new_id("output")
    created_at = now_iso()
    conn.execute(
        """
        INSERT INTO task_outputs (
          id, workspace_id, task_id, conversation_id, agent_id,
          title, output_type, content, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            output_id,
            workspace_id,
            task_id,
            conversation_id,
            agent_id,
            title,
            output_type,
            content,
            created_at,
        ),
    )
    row = conn.execute("SELECT * FROM task_outputs WHERE id = ?", (output_id,)).fetchone()
    _project_company_event(
        conn,
        lambda: __import__(
            "app.services.company_memory", fromlist=["record_task_event_projection"]
        ).record_task_event_projection(conn, row, event_type="task_output"),
    )
    return row


def add_agent_experience(
    conn: Database,
    *,
    workspace_id: str,
    agent_id: str,
    task_id: str | None,
    outcome: str,
    summary: str,
    lessons: str = "",
) -> Row:
    experience_id = new_id("exp")
    created_at = now_iso()
    conn.execute(
        """
        INSERT INTO agent_experiences (
          id, workspace_id, agent_id, task_id, outcome,
          summary, lessons, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            experience_id,
            workspace_id,
            agent_id,
            task_id,
            outcome,
            summary,
            lessons,
            created_at,
        ),
    )
    row = conn.execute(
        "SELECT * FROM agent_experiences WHERE id = ?", (experience_id,)
    ).fetchone()
    def project() -> None:
        from app.services.company_memory import record_company_event, record_memory

        event = record_company_event(
            conn,
            workspace_id=workspace_id,
            event_type="agent_experience",
            source_id=experience_id,
            title="员工工作经验",
            content=f"{summary}\n{lessons}".strip(),
            task_id=task_id,
            actor_agent_id=agent_id,
            importance=3.0,
            metadata={"outcome": outcome},
        )
        record_memory(
            conn,
            workspace_id=workspace_id,
            agent_id=agent_id,
            memory_type="lesson" if outcome != "success" else "episode",
            title="成功经验" if outcome == "success" else "复盘教训",
            content=f"{summary}\n{lessons}".strip(),
            evidence_event_ids=[event["id"]],
            importance=3.0,
            confidence=0.9,
        )
    _project_company_event(conn, project)
    return row


def create_knowledge_source(
    conn: Database,
    *,
    workspace_id: str,
    title: str,
    category: str,
    content: str,
    created_by: str,
    origin: str = "manual",
    source_ref: str = "",
    source_hash: str = "",
) -> Row:
    source_id = new_id("ks")
    created_at = now_iso()
    normalized_category = category.strip() or "通用资料"
    conn.execute(
        """
        INSERT INTO knowledge_sources (
          id, workspace_id, title, category, content, created_by, origin,
          source_ref, source_hash, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            workspace_id,
            title.strip(),
            normalized_category,
            content.strip(),
            created_by,
            origin,
            source_ref,
            source_hash,
            created_at,
            created_at,
        ),
    )
    row = conn.execute(
        "SELECT * FROM knowledge_sources WHERE id = ?", (source_id,)
    ).fetchone()
    _project_company_event(
        conn,
        lambda: __import__(
            "app.services.company_memory", fromlist=["record_company_event"]
        ).record_company_event(
            conn,
            workspace_id=workspace_id,
            event_type="knowledge",
            source_id=source_id,
            title=title.strip(),
            content=content.strip(),
            occurred_at=created_at,
            importance=3.0,
            metadata={
                "category": normalized_category,
                "source_id": source_id,
                "origin": origin,
                "source_ref": source_ref,
                "source_hash": source_hash,
            },
        ),
    )
    return row


def sync_obsidian_documents(
    conn: Database,
    *,
    workspace_id: str,
    created_by: str,
    documents: list[dict],
) -> dict:
    """Upsert only the desktop's explicitly managed Obsidian Markdown area."""
    created = 0
    updated = 0
    unchanged = 0
    for document in documents:
        relative_path = str(document.get("relative_path") or "").strip().replace("\\", "/")
        if not relative_path or relative_path.startswith("/") or ".." in relative_path.split("/"):
            raise ValueError("Obsidian 文档路径必须是 managed 区域内的相对路径")
        content = str(document.get("content") or "").strip()
        title = str(document.get("title") or relative_path.rsplit("/", 1)[-1]).strip()
        if not content or not title:
            continue
        source_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        existing = conn.execute(
            "SELECT * FROM knowledge_sources WHERE workspace_id = ? "
            "AND origin = 'obsidian' AND source_ref = ?",
            (workspace_id, relative_path),
        ).fetchone()
        timestamp = now_iso()
        if existing is None:
            create_knowledge_source(
                conn,
                workspace_id=workspace_id,
                title=title,
                category="Obsidian 资料",
                content=content,
                created_by=created_by,
                origin="obsidian",
                source_ref=relative_path,
                source_hash=source_hash,
            )
            created += 1
        elif existing.get("source_hash") == source_hash:
            unchanged += 1
        else:
            conn.execute(
                "UPDATE knowledge_sources SET title = ?, content = ?, source_hash = ?, "
                "updated_at = ? WHERE id = ? AND workspace_id = ?",
                (title, content, source_hash, timestamp, existing["id"], workspace_id),
            )
            from app.services.company_memory import record_company_event

            record_company_event(
                conn,
                workspace_id=workspace_id,
                event_type="knowledge_update",
                source_id=f"{existing['id']}:{source_hash}",
                title=title,
                content=content,
                occurred_at=timestamp,
                importance=3.0,
                metadata={
                    "source_id": existing["id"],
                    "origin": "obsidian",
                    "source_ref": relative_path,
                },
            )
            updated += 1
    return {"created": created, "updated": updated, "unchanged": unchanged}


def load_knowledge_context(
    conn: Database,
    *,
    workspace_id: str,
    query: str,
    limit: int = 5,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM knowledge_sources
        WHERE workspace_id = ?
        ORDER BY updated_at DESC
        LIMIT 24
        """,
        (workspace_id,),
    ).fetchall()
    if not rows:
        return []

    keywords = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", query.lower()))

    def score(row: Row) -> tuple[int, str]:
        haystack = f"{row['title']} {row['category']} {row['content']}".lower()
        matched = sum(1 for keyword in keywords if keyword in haystack)
        return (matched, row["updated_at"])

    ranked = sorted(rows, key=score, reverse=True)
    return [serialize_knowledge_source(row) for row in ranked[:limit]]


def match_tokens(value: str) -> set[str]:
    tokens = set(re.findall(r"[a-zA-Z0-9_]{2,}", value.lower()))
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", value):
        for size in (2, 3, 4):
            for index in range(0, len(chunk) - size + 1):
                token = chunk[index : index + size]
                if token not in {"负责", "输出", "一个", "一版", "任务"}:
                    tokens.add(token)
    return tokens


def add_approval(
    conn: Database,
    *,
    workspace_id: str,
    title: str,
    description: str,
    task_id: str | None = None,
    conversation_id: str | None = None,
    agent_id: str | None = None,
    risk_level: str = "medium",
) -> Row:
    approval_id = new_id("approval")
    created_at = now_iso()
    conn.execute(
        """
        INSERT INTO approvals (
          id, workspace_id, task_id, conversation_id, agent_id,
          title, description, status, risk_level, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            approval_id,
            workspace_id,
            task_id,
            conversation_id,
            agent_id,
            title,
            description,
            risk_level,
            created_at,
        ),
    )
    return conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()


def create_task(
    conn: Database,
    *,
    workspace_id: str,
    title: str,
    description: str = "",
    priority: str = "P2",
    owner_agent_id: str | None = None,
    status: str = "进行中",
    progress: int = 0,
    conversation_id: str | None = None,
    due_date: str | None = None,
    parent_task_id: str | None = None,
    consensus_brief_id: str | None = None,  # Gate condition
    task_plan_id: str | None = None,
    plan_item_key: str | None = None,
    expected_output: str = "",
    output_type: str = "markdown",
    bypass_gate: bool = False,  # Agent action tools bypass the brief requirement
) -> Row:
    from app.orchestration.gate import (
        validate_task_creation_gate,
        TaskCreationGateError,
    )

    # Gate validation: must have confirmed brief (unless sub-task inherits from parent)
    try:
        validate_task_creation_gate(
            conn,
            workspace_id=workspace_id,
            consensus_brief_id=consensus_brief_id,
            parent_task_id=parent_task_id,
            bypass_gate=bypass_gate,
        )
    except TaskCreationGateError as e:
        raise ValueError(str(e))

    normalized_status = normalize_task_status(status)
    if not owner_agent_id and status == "进行中":
        normalized_status = "待认领"
    validate_task_links(
        conn,
        workspace_id=workspace_id,
        owner_agent_id=owner_agent_id,
        conversation_id=conversation_id,
        parent_task_id=parent_task_id,
    )
    task_id = new_id("task")
    created_at = now_iso()
    conn.execute(
        """
        INSERT INTO tasks (
          id, workspace_id, title, description, priority, owner_agent_id,
          status, progress, conversation_id, due_date, parent_task_id,
          consensus_brief_id, task_plan_id, plan_item_key, expected_output,
          output_type, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            workspace_id,
            title,
            description,
            normalize_priority(priority),
            owner_agent_id,
            normalized_status,
            progress_for_status(normalized_status, progress),
            conversation_id,
            due_date,
            parent_task_id,
            consensus_brief_id,
            task_plan_id,
            plan_item_key,
            expected_output,
            output_type,
            created_at,
            created_at,
        ),
    )
    add_task_event(
        conn,
        workspace_id=workspace_id,
        task_id=task_id,
        kind="task_created",
        title="任务已创建",
        content=description or "任务已进入执行队列。",
        conversation_id=conversation_id,
        agent_id=owner_agent_id,
    )
    if owner_agent_id:
        add_task_event(
            conn,
            workspace_id=workspace_id,
            task_id=task_id,
            kind="task_assigned",
            title="已分配负责人",
            content="任务已分配给员工处理。",
            conversation_id=conversation_id,
            agent_id=owner_agent_id,
        )
    if conversation_id:
        add_message(
            conn,
            conversation_id=conversation_id,
            sender_type="system",
            sender_id="",
            content=f"已创建任务：{title}",
        )
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def update_task(
    conn: Database,
    *,
    workspace_id: str,
    task_id: str,
    changes: dict,
) -> Row:
    existing = conn.execute(
        "SELECT * FROM tasks WHERE id = ? AND workspace_id = ?",
        (task_id, workspace_id),
    ).fetchone()
    if existing is None:
        raise ValueError("task not found")

    next_values = {
        "title": changes.get("title", existing["title"]),
        "description": changes.get("description", existing["description"]),
        "priority": normalize_priority(changes.get("priority", existing["priority"])),
        "owner_agent_id": changes.get("owner_agent_id", existing["owner_agent_id"]),
        "status": normalize_task_status(changes.get("status", existing["status"])),
        "progress": changes.get("progress", existing["progress"]),
        "conversation_id": changes.get("conversation_id", existing["conversation_id"]),
        "due_date": changes.get("due_date", existing["due_date"]),
        "parent_task_id": changes.get("parent_task_id", existing["parent_task_id"]),
    }
    next_values["progress"] = progress_for_status(
        next_values["status"], int(next_values["progress"])
    )
    if next_values["parent_task_id"] == task_id:
        raise ValueError("task cannot be its own parent")
    validate_task_links(
        conn,
        workspace_id=workspace_id,
        owner_agent_id=next_values["owner_agent_id"],
        conversation_id=next_values["conversation_id"],
        parent_task_id=next_values["parent_task_id"],
    )
    updated_at = now_iso()
    conn.execute(
        """
        UPDATE tasks
        SET title = ?, description = ?, priority = ?, owner_agent_id = ?,
            status = ?, progress = ?, conversation_id = ?, due_date = ?,
            parent_task_id = ?, updated_at = ?
        WHERE id = ? AND workspace_id = ?
        """,
        (
            next_values["title"],
            next_values["description"],
            next_values["priority"],
            next_values["owner_agent_id"],
            next_values["status"],
            next_values["progress"],
            next_values["conversation_id"],
            next_values["due_date"],
            next_values["parent_task_id"],
            updated_at,
            task_id,
            workspace_id,
        ),
    )
    if next_values["conversation_id"]:
        add_message(
            conn,
            conversation_id=next_values["conversation_id"],
            sender_type="system",
            sender_id="",
            content=f"任务更新：{next_values['title']} · {next_values['status']}",
        )
    event_kind = (
        "task_status_changed"
        if next_values["status"] != existing["status"]
        else "task_updated"
    )
    add_task_event(
        conn,
        workspace_id=workspace_id,
        task_id=task_id,
        kind=event_kind,
        title=(
            f"状态更新为 {next_values['status']}"
            if event_kind == "task_status_changed"
            else "任务信息已更新"
        ),
        content=f"当前进度 {next_values['progress']}%。",
        conversation_id=next_values["conversation_id"],
        agent_id=next_values["owner_agent_id"],
    )
    if next_values["status"] == "待确认":
        existing_pending = conn.execute(
            """
            SELECT id FROM approvals
            WHERE workspace_id = ? AND task_id = ? AND status = 'pending'
            LIMIT 1
            """,
            (workspace_id, task_id),
        ).fetchone()
        if existing_pending is None:
            add_approval(
                conn,
                workspace_id=workspace_id,
                task_id=task_id,
                conversation_id=next_values["conversation_id"],
                agent_id=next_values["owner_agent_id"],
                title=f"确认任务产出：{next_values['title']}",
                description="任务已进入待确认状态，请老板确认结果是否可以归档完成。",
                risk_level="low",
            )
            add_task_event(
                conn,
                workspace_id=workspace_id,
                task_id=task_id,
                kind="approval_requested",
                title="等待老板确认",
                content="员工已提交阶段性结果，需要老板拍板。",
                conversation_id=next_values["conversation_id"],
                agent_id=next_values["owner_agent_id"],
            )
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def claim_task(
    conn: Database,
    *,
    workspace_id: str,
    task_id: str,
    agent_id: str,
) -> Row:
    task = conn.execute(
        "SELECT * FROM tasks WHERE id = ? AND workspace_id = ?",
        (task_id, workspace_id),
    ).fetchone()
    if task is None:
        raise ValueError("task not found")
    validate_task_links(
        conn,
        workspace_id=workspace_id,
        owner_agent_id=agent_id,
        conversation_id=task["conversation_id"],
        parent_task_id=task["parent_task_id"],
    )
    updated_at = now_iso()
    next_progress = max(int(task["progress"]), 20)
    conn.execute(
        """
        UPDATE tasks
        SET owner_agent_id = ?, status = '进行中', progress = ?, updated_at = ?
        WHERE id = ? AND workspace_id = ?
        """,
        (agent_id, next_progress, updated_at, task_id, workspace_id),
    )
    add_task_event(
        conn,
        workspace_id=workspace_id,
        task_id=task_id,
        kind="task_claimed",
        title="员工已认领任务",
        content="任务已从任务池进入执行状态。",
        conversation_id=task["conversation_id"],
        agent_id=agent_id,
    )
    if task["conversation_id"]:
        add_message(
            conn,
            conversation_id=task["conversation_id"],
            sender_type="system",
            sender_id="",
            content=f"任务已被认领：{task['title']}",
        )
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def suggest_task_agent(task: Row, agents: list[Row], departments_by_id: dict[str, Row]) -> dict | None:
    if task["owner_agent_id"] or task["status"] != "待认领":
        return None
    task_text = f"{task['title']} {task['description']}".lower()
    task_tokens = match_tokens(task_text)
    candidates: list[tuple[int, Row, list[str]]] = []
    for agent in agents:
        if agent["source"] == "system_secretary":
            continue
        skills = json.loads(agent["skills_json"])
        department = departments_by_id.get(agent["department_id"])
        fields = [
            agent["name"],
            agent["role"],
            agent["description"],
            agent["prompt"],
            department["name"] if department else "",
            " ".join(skills),
        ]
        profile_text = " ".join(fields).lower()
        profile_tokens = match_tokens(profile_text)
        score = 0
        reasons: list[str] = []
        for skill in skills:
            if skill and skill.lower() in task_text:
                score += 8
                reasons.append(f"技能匹配「{skill}」")
        for field in [agent["role"], agent["description"], agent["name"]]:
            if field and field.lower() in task_text:
                score += 6
        for keyword in re.findall(r"[\w\u4e00-\u9fff]{2,}", task_text):
            if keyword in profile_text:
                score += 1
        common_tokens = sorted(task_tokens & profile_tokens, key=len, reverse=True)
        if common_tokens:
            score += min(10, len(common_tokens) * 2)
            reasons.append(f"关键词匹配「{'、'.join(common_tokens[:3])}」")
        if department and department["name"] and department["name"].lower() in task_text:
            score += 4
            reasons.append(f"部门匹配「{department['name']}」")
        if score > 0:
            if not reasons:
                reasons.append("岗位说明与任务关键词匹配")
            candidates.append((score, agent, reasons[:2]))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]["created_at"]), reverse=True)
    score, agent, reasons = candidates[0]
    reason = "，".join(reasons)
    return {
        "agent_id": agent["id"],
        "reason": f"{reason}，匹配度 {score}",
    }


def validate_task_links(
    conn: Database,
    *,
    workspace_id: str,
    owner_agent_id: str | None,
    conversation_id: str | None,
    parent_task_id: str | None,
) -> None:
    if owner_agent_id:
        owner = conn.execute(
            "SELECT id FROM agents WHERE id = ? AND workspace_id = ?",
            (owner_agent_id, workspace_id),
        ).fetchone()
        if owner is None:
            raise ValueError("owner agent not found")

    if conversation_id:
        conversation = conn.execute(
            "SELECT id FROM conversations WHERE id = ? AND workspace_id = ?",
            (conversation_id, workspace_id),
        ).fetchone()
        if conversation is None:
            raise ValueError("conversation not found")

    if parent_task_id:
        parent = conn.execute(
            "SELECT id FROM tasks WHERE id = ? AND workspace_id = ?",
            (parent_task_id, workspace_id),
        ).fetchone()
        if parent is None:
            raise ValueError("parent task not found")


def normalize_priority(value: str) -> str:
    return value if value in {"P0", "P1", "P2"} else "P2"


def normalize_task_status(value: str) -> str:
    if value == "卡住":
        return "阻塞"
    return value if value in {"待执行", "待认领", "进行中", "待确认", "阻塞", "已完成"} else "进行中"


def progress_for_status(status: str, progress: int) -> int:
    bounded = max(0, min(100, progress))
    if status == "已完成":
        return 100
    if status == "待认领":
        return 0
    if status == "待执行":
        return 0
    if status == "进行中" and bounded == 0:
        return 10
    return bounded


# NOTE: extract_task_intent has been removed (see ADR 0006).
# Task creation now requires a confirmed consensus_brief.

def extract_recruit_intent(content: str) -> dict | None:
    text = content.strip()
    action_words = ["招", "招聘", "创建", "新建", "配置", "生成", "需要", "想要"]
    role_words = [
        "员工",
        "智能体",
        "agent",
        "Agent",
        "专家",
        "分析师",
        "助理",
        "顾问",
        "策划",
    ]
    if not any(word in text for word in action_words):
        return None
    if not any(word in text for word in role_words):
        return None

    role = ""
    for pattern in RECRUIT_INTENT_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            role = match.group("role").strip(" ：:，,。")
            break
    if not role:
        return None

    role = re.sub(r"^(帮我|给我|请你|麻烦|招|招聘|创建|新建|配置|生成|需要|我想|我要)", "", role)
    role = role.strip(" ：:，,。")
    role = re.sub(r"(员工|智能体|agent|Agent)$", "", role).strip()
    if len(role) < 2:
        return None
    if len(role) > 24:
        role = role[:24]

    preset = next(
        (
            item
            for item in ROLE_PRESETS
            if any(keyword in role for keyword in item["keywords"])
        ),
        None,
    )
    department = preset["department"] if preset else "新员工部"
    skills = preset["skills"] if preset else []
    prompt = (
        preset["prompt"]
        if preset
        else f"你是一名{role}。负责围绕岗位目标拆解任务、输出可执行方案，并在需要老板确认时明确列出决策点。"
    )
    description = f"通过小秘对话创建的{role}"
    return {
        "name": role,
        "role": role,
        "description": description,
        "department_name": department,
        "prompt": prompt,
        "skills": skills,
        "mcps": [],
    }


# NOTE: task_intent_payload has been removed (see ADR 0006).


def get_bootstrap(conn: Database, workspace_id: str) -> dict:
    workspace = get_workspace_by_id(conn, workspace_id)
    departments = conn.execute(
        "SELECT * FROM departments WHERE workspace_id = ? ORDER BY sort_order, created_at",
        (workspace_id,),
    ).fetchall()
    departments_by_id = {department["id"]: department for department in departments}
    agents = conn.execute(
        "SELECT * FROM agents WHERE workspace_id = ? ORDER BY created_at", (workspace_id,)
    ).fetchall()
    conversations = conn.execute(
        """SELECT * FROM conversations WHERE workspace_id = ?
        ORDER BY CASE kind WHEN 'dm' THEN 0 ELSE 1 END, created_at, id""",
        (workspace_id,),
    ).fetchall()
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE workspace_id = ? ORDER BY updated_at DESC",
        (workspace_id,),
    ).fetchall()
    knowledge_sources = conn.execute(
        """
        SELECT * FROM knowledge_sources
        WHERE workspace_id = ?
        ORDER BY updated_at DESC
        """,
        (workspace_id,),
    ).fetchall()
    agent_ids = [agent["id"] for agent in agents]
    agent_experiences_by_agent = {agent_id: [] for agent_id in agent_ids}
    if agent_ids:
        placeholders = ",".join("?" for _ in agent_ids)
        experiences = conn.execute(
            f"""
            SELECT * FROM agent_experiences
            WHERE workspace_id = ? AND agent_id IN ({placeholders})
            ORDER BY created_at DESC
            """,
            (workspace_id, *agent_ids),
        ).fetchall()
        for experience in experiences:
            agent_experiences_by_agent.setdefault(experience["agent_id"], []).append(
                serialize_agent_experience(experience)
            )
    task_ids = [task["id"] for task in tasks]
    task_events_by_task = {task_id: [] for task_id in task_ids}
    task_outputs_by_task = {task_id: [] for task_id in task_ids}
    approvals_by_task = {task_id: [] for task_id in task_ids}
    if task_ids:
        placeholders = ",".join("?" for _ in task_ids)
        events = conn.execute(
            f"""
            SELECT * FROM task_events
            WHERE workspace_id = ? AND task_id IN ({placeholders})
            ORDER BY created_at ASC
            """,
            (workspace_id, *task_ids),
        ).fetchall()
        for event in events:
            task_events_by_task.setdefault(event["task_id"], []).append(
                serialize_task_event(event)
            )

        outputs = conn.execute(
            f"""
            SELECT * FROM task_outputs
            WHERE workspace_id = ? AND task_id IN ({placeholders})
            ORDER BY created_at DESC
            """,
            (workspace_id, *task_ids),
        ).fetchall()
        for output in outputs:
            task_outputs_by_task.setdefault(output["task_id"], []).append(
                serialize_task_output(output)
            )

        approvals = conn.execute(
            f"""
            SELECT * FROM approvals
            WHERE workspace_id = ? AND task_id IN ({placeholders})
            ORDER BY created_at DESC
            """,
            (workspace_id, *task_ids),
        ).fetchall()
        for approval in approvals:
            approvals_by_task.setdefault(approval["task_id"], []).append(
                serialize_approval(approval)
            )
    messages_by_conversation = {}
    serialized_conversations = []
    for conversation in conversations:
        members = conn.execute(
            """
            SELECT agent_id FROM conversation_members
            WHERE conversation_id = ?
            ORDER BY id
            """,
            (conversation["id"],),
        ).fetchall()
        member_ids = [member["agent_id"] for member in members]
        if conversation["kind"] == "dm" and conversation["agent_id"]:
            member_ids = [conversation["agent_id"]]
        serialized_conversations.append(serialize_conversation(conversation, member_ids))
        messages = conn.execute(
            """
            SELECT * FROM messages
            WHERE conversation_id = ?
            ORDER BY created_at ASC
            """,
            (conversation["id"],),
        ).fetchall()
        messages_by_conversation[conversation["id"]] = [
            serialize_message(message) for message in messages
        ]

    return {
        "workspace": serialize_workspace(workspace),
        "departments": [serialize_department(dept) for dept in departments],
        "agents": [serialize_agent(agent) for agent in agents],
        "conversations": serialized_conversations,
        "messages_by_conversation": messages_by_conversation,
        "tasks": [
            serialize_task(task, suggest_task_agent(task, agents, departments_by_id))
            for task in tasks
        ],
        "knowledge_sources": [
            serialize_knowledge_source(source) for source in knowledge_sources
        ],
        "task_events_by_task": task_events_by_task,
        "task_outputs_by_task": task_outputs_by_task,
        "approvals_by_task": approvals_by_task,
        "agent_experiences_by_agent": agent_experiences_by_agent,
        "agent_template_categories": list_talent_categories(conn),
        "agent_templates": list_agent_templates(conn),
        "anomaly_count_24h": count_anomalies_24h(conn, workspace_id),
    }


def count_anomalies_24h(conn: Database, workspace_id: str) -> int:
    """Failed runs + expired (timed-out, unanswered) approvals in the last
    24h — a boss-facing "did anything actually go wrong" signal.

    Borrowed from service-claw-cloud's playbook_runs.anomaly_count_24h: our
    equivalent of its `anomaly_fired` activity-log events. Deliberately
    excludes rejected approvals — a rejection is the owner's own considered
    call, not something that went wrong.
    """
    cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    failed_runs = conn.execute(
        "SELECT COUNT(*) AS c FROM runs "
        "WHERE workspace_id = ? AND status = 'failed' AND completed_at >= ?",
        (workspace_id, cutoff),
    ).fetchone()["c"]
    expired_approvals = conn.execute(
        "SELECT COUNT(*) AS c FROM approvals "
        "WHERE workspace_id = ? AND status = 'expired' AND resolved_at >= ?",
        (workspace_id, cutoff),
    ).fetchone()["c"]
    return failed_runs + expired_approvals


def recruit_from_template(
    conn: Database,
    *,
    workspace_id: str,
    template_id: str,
    department_name: str | None,
) -> Row:
    template = get_template(conn, template_id)
    if template is None:
        raise ValueError("template not found")

    department = ensure_department(
        conn, workspace_id, department_name or template["department"]
    )
    agent_id = create_agent(
        conn,
        workspace_id=workspace_id,
        department_id=department["id"],
        name=template["name"],
        role=template["name"],
        description=template["description"],
        prompt=template["prompt"],
        skills=template["skills"],
        mcps=template["mcps"],
        source=f"template:{template['id']}",
    )
    create_dm_conversation(conn, workspace_id, agent_id)
    try:
        capability_keys = get_role_bundle(template["name"])
    except ValueError:
        capability_keys = []  # custom/community template with no matching bundle
    provision_new_agent(
        conn,
        agent_id=agent_id,
        workspace_id=workspace_id,
        role_name=template["name"],
        source_request=f"通过人才市场招募官方模板「{template['name']}」",
        responsibilities=[template["description"]] if template["description"] else [],
        capability_keys=capability_keys,
    )
    return conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()


def provision_new_agent(
    conn: Database,
    *,
    agent_id: str,
    workspace_id: str,
    role_name: str,
    source_request: str,
    responsibilities: list[str],
    capability_keys: list[str],
) -> None:
    """Give a freshly-created agent a real Hermes profile, not just a name tag.

    The shared "compile a role description into a real, working employee"
    step — every hiring path (Talent Market template, the natural-language
    team compiler, the create_employee tool an agent calls on the owner's
    behalf) funnels through this one function so there's exactly one place
    that knows how to turn (role_name, responsibilities, capability_keys)
    into an actual provisioned Hermes profile, instead of each caller
    reimplementing the same dance.

    Only runs when settings.hermes_provisioning is on
    (build_provisioner_from_settings is a no-op RecordOnlyProvisioner
    otherwise) — so the test suite's DeepSeek-fallback assumptions for
    agents created without this flag aren't disturbed.

    Credential-needing capabilities remain visible as pending while the
    employee's credential-free capabilities receive a working profile.
    """
    from app.core.config import settings

    if not settings.hermes_provisioning:
        return
    from app.orchestration.supply import (
        ProvisioningError,
        build_provisioner_from_settings,
        create_agent_spec,
        provision,
    )
    try:
        validate_capability_keys(capability_keys)
    except ValueError:
        capability_keys = [k for k in capability_keys if k in CATALOG]

    try:
        from app.services.model_credentials import has_workspace_model_credential

        if settings.model_byok_required and not has_workspace_model_credential(
            conn, workspace_id
        ):
            create_agent_spec(
                conn,
                agent_id=agent_id,
                workspace_id=workspace_id,
                role_name=role_name,
                source_request=source_request,
                responsibilities=responsibilities,
                capability_keys=capability_keys,
            )
            conn.execute(
                "UPDATE agent_specs SET status = 'blocked_on_credentials', updated_at = ? "
                "WHERE agent_id = ?",
                (now_iso(), agent_id),
            )
            return

        ready_keys, pending_keys = split_by_credentials(capability_keys)
        provisioner = build_provisioner_from_settings()
        create_agent_spec(
            conn,
            agent_id=agent_id,
            workspace_id=workspace_id,
            role_name=role_name,
            source_request=source_request,
            responsibilities=responsibilities,
            capability_keys=ready_keys,
        )
        provision(conn, agent_id, provisioner)
        from app.runtime.upgrade import UpgradeError, execute_upgrade

        for key in pending_keys:
            try:
                execute_upgrade(
                    conn,
                    approval={"agent_id": agent_id, "workspace_id": workspace_id},
                    approved_capability_key=key,
                    provisioner=provisioner,
                )
            except UpgradeError:
                pass
    except ProvisioningError:
        pass  # non-fatal — the employee is still usable via the DeepSeek fallback


def create_dm_conversation(
    conn: Database, workspace_id: str, agent_id: str
) -> Row:
    existing = conn.execute(
        """
        SELECT * FROM conversations
        WHERE workspace_id = ? AND kind = 'dm' AND agent_id = ?
        """,
        (workspace_id, agent_id),
    ).fetchone()
    if existing is not None:
        return existing

    conversation_id = new_id("conv")
    created_at = now_iso()
    conn.execute(
        """
        INSERT INTO conversations (id, workspace_id, kind, name, agent_id, unread, created_at, updated_at)
        VALUES (?, ?, 'dm', '', ?, 0, ?, ?)
        """,
        (conversation_id, workspace_id, agent_id, created_at, created_at),
    )
    return conn.execute(
        "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
