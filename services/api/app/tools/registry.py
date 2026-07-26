"""Tool registry — schemas + handlers the agent can call.

Each tool is a dict with:
- schema: OpenAI function-calling schema (name, description, parameters)
- handler: async callable(conn, workspace, agent, args) -> str (result text)

The handler calls internal service functions directly (no HTTP layer), so it
shares the same DB connection as the calling request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from app.core.database import Database, Row
from app.orchestration.capability_catalog import CATALOG
from app.runtime.runtime_guard import bounded_tool_output
from app.services.execution_receipts import begin_receipt, finish_receipt
from app.services.workspace import (
    add_message,
    claim_task,
    create_agent,
    create_dm_conversation,
    create_task,
    ensure_department,
    get_bootstrap,
    new_id,
    now_iso,
    provision_new_agent,
    serialize_agent,
    serialize_task,
    update_task,
)


# ---------------------------------------------------------------------------
# Tool result types
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    """A parsed tool call from the LLM response."""
    id: str
    name: str
    arguments: dict = field(default_factory=dict)
    parse_error: str = ""


@dataclass
class ToolResult:
    """Result of executing a tool call, to be fed back to the LLM."""
    tool_call_id: str
    name: str
    content: str  # JSON string or human-readable text
    ok: bool = False
    execution_receipt_id: str | None = None
    result: Any = None


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling schema)
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "create_employee",
            "description": (
                "创建一个新的员工。用自然语言描述你想要什么样的员工，"
                "系统会分配角色和技能。你可以一次创建多个员工来组建团队。"
                "创建后员工需要经过配置才能开始工作，但可以立即和ta聊天。"
                "如果公司参与者描述的职责涉及真实操作（读写文件、跑代码、查数据、"
                "分析统计等），先调用 list_capabilities 看目录里有没有对应的"
                "能力，把匹配的 key 填进 capability_keys——不填的话这个员工"
                "只能聊天，干不了实际工作。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "员工的名字，例如：小明、市场分析师小王",
                    },
                    "role": {
                        "type": "string",
                        "description": "岗位名称，例如：内容策划、全栈工程师、客服专员",
                    },
                    "description": {
                        "type": "string",
                        "description": "员工的职责描述，帮助公司推进什么",
                    },
                    "department": {
                        "type": "string",
                        "description": "所属部门，例如：市场部、技术部、运营部。不提供则自动归入合适部门",
                    },
                    "skills": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "技能列表，例如：[\"公众号文案\", \"SEO优化\"]",
                    },
                    "responsibilities": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "从当前发起人的描述里拆出来的具体职责清单，每条一句话，"
                            "例如：[\"逐一核查上门打卡照片，核查服务真实性\", "
                            "\"制作项目台账、工程量统计表\"]。写不清楚就写老板"
                            "原话里那一段，不要替发起人编造没提过的内容。"
                        ),
                    },
                    "capability_keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "先调 list_capabilities 看目录，再从里面选这个员工"
                            "真正需要的能力 key（比如 write_code、data_analysis、"
                            "customer_service）。目录里找不到贴切的就先不填——"
                            "不要瞎编一个不存在的 key。"
                        ),
                    },
                },
                "required": ["name", "role", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_capabilities",
            "description": (
                "列出系统里所有可授予员工的能力目录（key、说明、是否需要"
                "需要配置业务凭证）。创建员工前想给她真实工作能力时，先调这个看"
                "有哪些 key 可选，再把匹配的填进 create_employee 的"
                "capability_keys。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": (
                "创建一个新任务。公司参与者直接提出的任务不需要 brief 确认。"
                "分配给指定员工。设置 bypass_gate=true 跳过共识门控。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "任务标题，简明扼要",
                    },
                    "description": {
                        "type": "string",
                        "description": "任务详细描述，包括目标、范围、验收标准",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["P0", "P1", "P2"],
                        "description": "优先级：P0最紧急，P2最低",
                    },
                    "owner_agent_id": {
                        "type": "string",
                        "description": "分配给哪个员工的ID。先调 list_agents 查到员工ID再分配",
                    },
                    "conversation_id": {
                        "type": "string",
                        "description": "关联的会话ID（可选）",
                    },
                    "bypass_gate": {
                        "type": "boolean",
                        "description": "公司参与者直接提出的任务设为true，跳过共识brief要求",
                        "default": True,
                    },
                },
                "required": ["title", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_group",
            "description": (
                "创建一个群聊。把多个员工拉到一个群里讨论协作。"
                "建群后可以用 add_group_member 加更多成员。"
                "⚠️ 建群前必须先调 list_groups 检查是否已经有相同目的的群——"
                "已经有就用 send_group_message 或 add_group_member，不要重复建群。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "群聊名称，例如：新产品讨论组",
                    },
                    "member_agent_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "初始成员（员工ID列表）。至少1个",
                    },
                },
                "required": ["name", "member_agent_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_group_member",
            "description": "把员工拉进已有群聊。",
            "parameters": {
                "type": "object",
                "properties": {
                    "conversation_id": {
                        "type": "string",
                        "description": "群聊ID",
                    },
                    "agent_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要加入的员工ID列表",
                    },
                },
                "required": ["conversation_id", "agent_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_groups",
            "description": (
                "列出公司当前所有群聊，包括群聊ID、名称和成员。"
                "建群前必须先查这里，避免为同一个目的重复创建群聊。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_agents",
            "description": (
                "列出公司当前所有员工，获取他们的ID、名字、岗位、部门等信息。"
                "在分配任务或拉群前先查这里获取员工ID。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "列出公司当前所有任务，知道有哪些需要做。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "按状态筛选：待认领、进行中、待确认、阻塞、已完成。不填则列全部",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "claim_task",
            "description": "认领一个待办任务，表示这个员工要开始执行它了。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "要认领的任务ID",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "认领的员工ID（通常就是操作者自己）",
                    },
                },
                "required": ["task_id", "agent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_task",
            "description": "更新任务状态或信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "任务ID",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["进行中", "待确认", "阻塞", "已完成"],
                        "description": "新状态",
                    },
                    "progress": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "进度百分比",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_group_message",
            "description": (
                "在群聊里发消息。当你需要通知多个员工、发起讨论、"
                "或者传达已经确认的公司决定时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "conversation_id": {
                        "type": "string",
                        "description": "群聊ID",
                    },
                    "content": {
                        "type": "string",
                        "description": "消息内容",
                    },
                },
                "required": ["conversation_id", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "no_action_needed",
            "description": (
                "当前不需要执行任何操作，只是一个普通回复。"
                "这是默认行为——只有当你确定需要创建、修改、查询东西时才调其他工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

async def _handle_create_employee(
    conn: Database,
    workspace_id: str,
    agent: Row,
    args: dict,
) -> str:
    """Create one employee — and, if capability_keys are given, a real one.

    Without capability_keys this still creates an employee record, but the
    employee remains blocked until a Hermes profile is provisioned. With
    capabilities it runs through the same provision_new_agent() every other
    hiring path uses (Talent Market, the team compiler), so an employee the
    boss describes to the secretary in chat can come out just as real as
    one hand-configured through a form.
    """
    name = str(args.get("name", "")).strip()
    role = str(args.get("role", "")).strip()
    description = str(args.get("description", "")).strip()
    department_name = str(args.get("department", "新员工部")).strip()
    skills: list[str] = args.get("skills") or []
    responsibilities: list[str] = [
        str(item) for item in (args.get("responsibilities") or [])
    ]
    capability_keys: list[str] = [
        str(item) for item in (args.get("capability_keys") or [])
    ]

    if not name or not role:
        return json.dumps({"error": "员工名字和岗位不能为空"})

    if len(name) > 24:
        name = name[:24]
    if len(role) > 24:
        role = role[:24]

    department = ensure_department(conn, workspace_id, department_name)
    agent_id = create_agent(
        conn,
        workspace_id=workspace_id,
        department_id=department["id"],
        name=name,
        role=role,
        description=description or f"由 {agent['name']} 创建的{role}",
        prompt=f"你是一名{role}。{description}",
        skills=skills,
        mcps=[],
        source="custom",
    )
    create_dm_conversation(conn, workspace_id, agent_id)
    if capability_keys:
        provision_new_agent(
            conn,
            agent_id=agent_id,
            workspace_id=workspace_id,
            role_name=role,
            source_request=f"由 {agent['name']} 在对话中创建",
            responsibilities=responsibilities,
            capability_keys=capability_keys,
        )
    new_agent = conn.execute(
        "SELECT * FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()

    return json.dumps(
        {
            "success": True,
            "agent": {
                "id": agent_id,
                "name": name,
                "role": role,
                "department": department_name,
            },
        },
        ensure_ascii=False,
    )


async def _handle_list_capabilities(
    conn: Database,
    workspace_id: str,
    agent: Row,
    args: dict,
) -> str:
    """List the capability catalog so the caller can pick real keys instead
    of guessing — same source as GET /api/capabilities."""
    return json.dumps(
        {
            "capabilities": [
                {
                    "key": key,
                    "description": cap.description,
                    "risk_gate": cap.risk_gate,
                    "needs_credentials": bool(cap.required_credentials),
                }
                for key, cap in CATALOG.items()
            ],
        },
        ensure_ascii=False,
    )


async def _handle_create_task(
    conn: Database,
    workspace_id: str,
    agent: Row,
    args: dict,
) -> str:
    title = str(args.get("title", "")).strip()
    description = str(args.get("description", "")).strip()
    priority = str(args.get("priority", "P2")).strip()
    owner_agent_id = str(args.get("owner_agent_id", "")).strip() or None
    conversation_id = str(args.get("conversation_id", "")).strip() or None

    if not title:
        return json.dumps({"error": "任务标题不能为空"})

    try:
        bypass = args.get("bypass_gate", True)
        task = create_task(
            conn,
            workspace_id=workspace_id,
            title=title[:160],
            description=description[:2000] if description else "",
            priority=priority if priority in ("P0", "P1", "P2") else "P2",
            owner_agent_id=owner_agent_id,
            status="待认领",
            conversation_id=conversation_id,
            bypass_gate=bypass,
        )
        return json.dumps(
            {
                "success": True,
                "task": {
                    "id": task["id"],
                    "title": task["title"],
                    "priority": task["priority"],
                    "status": task["status"],
                },
            },
            ensure_ascii=False,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})


def _normalize_group_name(name: str) -> str:
    """Strip a leading emoji/symbol run and surrounding whitespace so
    "运营团队" and "📈 运营团队" are recognized as the same group name."""
    stripped = name.strip()
    while stripped and not (stripped[0].isalnum() or "一" <= stripped[0] <= "鿿"):
        stripped = stripped[1:].lstrip()
    return stripped.lower()


async def _handle_create_group(
    conn: Database,
    workspace_id: str,
    agent: Row,
    args: dict,
) -> str:
    name = str(args.get("name", "")).strip()
    member_ids: list[str] = args.get("member_agent_ids") or []

    if not name:
        return json.dumps({"error": "群聊名称不能为空"})
    if not member_ids:
        return json.dumps({"error": "至少需要1个成员"})

    # Defense in depth against duplicate groups: even if the model skipped
    # list_groups, don't create a second group for a name that (ignoring a
    # leading emoji/symbol) already exists — add any missing members to the
    # existing one instead.
    normalized = _normalize_group_name(name)
    existing_groups = conn.execute(
        "SELECT id, name FROM conversations WHERE workspace_id = ? AND kind = 'group'",
        (workspace_id,),
    ).fetchall()
    existing = next(
        (g for g in existing_groups if _normalize_group_name(g["name"]) == normalized),
        None,
    )
    if existing is not None:
        conversation_id = existing["id"]
        current_members = {
            row["agent_id"]
            for row in conn.execute(
                "SELECT agent_id FROM conversation_members WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchall()
        }
        added = 0
        for mid in member_ids:
            if mid not in current_members:
                conn.execute(
                    "INSERT INTO conversation_members (conversation_id, agent_id) VALUES (?, ?)",
                    (conversation_id, mid),
                )
                added += 1
        conn.commit()
        return json.dumps(
            {
                "success": True,
                "group": {"id": conversation_id, "name": existing["name"], "reused": True},
                "added_members": added,
            },
            ensure_ascii=False,
        )

    conversation_id = new_id("conv")
    created_at = now_iso()
    conn.execute(
        """
        INSERT INTO conversations (id, workspace_id, kind, name, unread, created_at, updated_at)
        VALUES (?, ?, 'group', ?, 0, ?, ?)
        """,
        (conversation_id, workspace_id, name, created_at, created_at),
    )
    for mid in member_ids:
        conn.execute(
            "INSERT INTO conversation_members (conversation_id, agent_id) VALUES (?, ?)",
            (conversation_id, mid),
        )
    conn.commit()

    # Also send a welcome message
    add_message(
        conn,
        conversation_id=conversation_id,
        sender_type="system",
        sender_id="",
        content=f"群聊「{name}」已创建，{len(member_ids)} 位成员已加入。",
    )
    conn.commit()

    return json.dumps(
        {
            "success": True,
            "group": {
                "id": conversation_id,
                "name": name,
                "member_count": len(member_ids),
            },
        },
        ensure_ascii=False,
    )


async def _handle_add_group_member(
    conn: Database,
    workspace_id: str,
    agent: Row,
    args: dict,
) -> str:
    conversation_id = str(args.get("conversation_id", "")).strip()
    agent_ids: list[str] = args.get("agent_ids") or []

    if not conversation_id or not agent_ids:
        return json.dumps({"error": "缺少群聊ID或员工ID"})

    conv = conn.execute(
        "SELECT id FROM conversations WHERE id = ? AND workspace_id = ?",
        (conversation_id, workspace_id),
    ).fetchone()
    if conv is None:
        return json.dumps({"error": "群聊不存在"})

    added = 0
    for aid in agent_ids:
        existing = conn.execute(
            "SELECT 1 FROM conversation_members WHERE conversation_id = ? AND agent_id = ?",
            (conversation_id, aid),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO conversation_members (conversation_id, agent_id) VALUES (?, ?)",
                (conversation_id, aid),
            )
            added += 1

    conn.commit()

    add_message(
        conn,
        conversation_id=conversation_id,
        sender_type="system",
        sender_id="",
        content=f"{agent['name']} 邀请了 {added} 位新成员加入群聊。",
    )
    conn.commit()

    return json.dumps({"success": True, "added": added}, ensure_ascii=False)


async def _handle_list_groups(
    conn: Database,
    workspace_id: str,
    agent: Row,
    args: dict,
) -> str:
    rows = conn.execute(
        """
        SELECT id, name FROM conversations
        WHERE workspace_id = ? AND kind = 'group'
        ORDER BY created_at
        """,
        (workspace_id,),
    ).fetchall()

    groups_list = []
    for r in rows:
        members = conn.execute(
            """
            SELECT a.name FROM conversation_members cm
            JOIN agents a ON a.id = cm.agent_id
            WHERE cm.conversation_id = ?
            """,
            (r["id"],),
        ).fetchall()
        groups_list.append(
            {
                "id": r["id"],
                "name": r["name"],
                "members": [m["name"] for m in members],
            }
        )
    return json.dumps({"groups": groups_list, "total": len(groups_list)}, ensure_ascii=False)


async def _handle_list_agents(
    conn: Database,
    workspace_id: str,
    agent: Row,
    args: dict,
) -> str:
    rows = conn.execute(
        """
        SELECT a.id, a.name, a.role, a.description, d.name AS department, a.status_kind
        FROM agents a
        JOIN departments d ON d.id = a.department_id
        WHERE a.workspace_id = ?
        ORDER BY a.created_at
        """,
        (workspace_id,),
    ).fetchall()

    agents_list = [
        {
            "id": r["id"],
            "name": r["name"],
            "role": r["role"],
            "department": r["department"],
            "status": r["status_kind"],
        }
        for r in rows
    ]
    return json.dumps({"agents": agents_list, "total": len(agents_list)}, ensure_ascii=False)


async def _handle_list_tasks(
    conn: Database,
    workspace_id: str,
    agent: Row,
    args: dict,
) -> str:
    status_filter = str(args.get("status", "")).strip()
    if status_filter:
        rows = conn.execute(
            """
            SELECT id, title, priority, status, progress, owner_agent_id
            FROM tasks WHERE workspace_id = ? AND status = ?
            ORDER BY
              CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 ELSE 2 END,
              updated_at DESC
            """,
            (workspace_id, status_filter),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, title, priority, status, progress, owner_agent_id
            FROM tasks WHERE workspace_id = ?
            ORDER BY
              CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 ELSE 2 END,
              updated_at DESC
            """,
            (workspace_id,),
        ).fetchall()

    tasks_list = [
        {
            "id": r["id"],
            "title": r["title"],
            "priority": r["priority"],
            "status": r["status"],
            "progress": r["progress"],
            "owner_id": r["owner_agent_id"],
        }
        for r in rows
    ]
    return json.dumps({"tasks": tasks_list, "total": len(tasks_list)}, ensure_ascii=False)


async def _handle_claim_task(
    conn: Database,
    workspace_id: str,
    agent: Row,
    args: dict,
) -> str:
    task_id = str(args.get("task_id", "")).strip()
    claimant_agent_id = str(args.get("agent_id", "")).strip()

    if not task_id or not claimant_agent_id:
        return json.dumps({"error": "缺少任务ID或员工ID"})

    try:
        task = claim_task(
            conn,
            workspace_id=workspace_id,
            task_id=task_id,
            agent_id=claimant_agent_id,
        )
        return json.dumps(
            {"success": True, "task": {"id": task["id"], "status": task["status"]}},
            ensure_ascii=False,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})


async def _handle_update_task(
    conn: Database,
    workspace_id: str,
    agent: Row,
    args: dict,
) -> str:
    task_id = str(args.get("task_id", "")).strip()
    changes: dict[str, Any] = {}
    if "status" in args:
        changes["status"] = args["status"]
    if "progress" in args:
        changes["progress"] = int(args["progress"])

    if not task_id or not changes:
        return json.dumps({"error": "缺少任务ID或更新内容"})

    try:
        task = update_task(conn, workspace_id=workspace_id, task_id=task_id, changes=changes)
        return json.dumps(
            {"success": True, "task": {"id": task["id"], "status": task["status"], "progress": task["progress"]}},
            ensure_ascii=False,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})


async def _handle_send_group_message(
    conn: Database,
    workspace_id: str,
    agent: Row,
    args: dict,
) -> str:
    conversation_id = str(args.get("conversation_id", "")).strip()
    content = str(args.get("content", "")).strip()

    if not conversation_id or not content:
        return json.dumps({"error": "缺少群聊ID或消息内容"})

    add_message(
        conn,
        conversation_id=conversation_id,
        sender_type="agent",
        sender_id=agent["id"],
        content=content,
    )
    conn.commit()
    return json.dumps({"success": True})


async def _handle_no_action(_conn, _ws, _agent, _args) -> str:
    return json.dumps({"action": "none"})


# ---------------------------------------------------------------------------
# Handler dispatch map
# ---------------------------------------------------------------------------

HANDLER_MAP: dict[str, Callable] = {
    "create_employee": _handle_create_employee,
    "list_capabilities": _handle_list_capabilities,
    "create_task": _handle_create_task,
    "create_group": _handle_create_group,
    "add_group_member": _handle_add_group_member,
    "list_groups": _handle_list_groups,
    "list_agents": _handle_list_agents,
    "list_tasks": _handle_list_tasks,
    "claim_task": _handle_claim_task,
    "update_task": _handle_update_task,
    "send_group_message": _handle_send_group_message,
    "no_action_needed": _handle_no_action,
}


async def execute_tool(
    conn: Database,
    workspace_id: str,
    agent: Row,
    tool_call: ToolCall,
    *,
    run_id: str | None = None,
) -> ToolResult:
    """Execute a single tool call with a durable truth-bearing receipt."""
    receipt_id = begin_receipt(
        conn,
        workspace_id=workspace_id,
        agent_id=agent.get("id"),
        tool_name=tool_call.name,
        arguments=tool_call.arguments,
        run_id=run_id,
    )

    validation_error = _validate_tool_call(tool_call)
    if validation_error:
        finish_receipt(
            conn,
            receipt_id,
            status="rejected",
            error=validation_error,
        )
        return _tool_result(
            tool_call,
            ok=False,
            result=None,
            receipt_id=receipt_id,
            error=validation_error,
        )

    handler = HANDLER_MAP.get(tool_call.name)
    if handler is None:
        error = f"未知工具: {tool_call.name}"
        finish_receipt(conn, receipt_id, status="rejected", error=error)
        return _tool_result(
            tool_call,
            ok=False,
            result=None,
            receipt_id=receipt_id,
            error=error,
        )

    try:
        raw_content = await handler(conn, workspace_id, agent, tool_call.arguments)
        try:
            result = json.loads(raw_content)
        except (TypeError, json.JSONDecodeError):
            result = {"text": str(raw_content)}
        error = ""
        if isinstance(result, dict) and result.get("error"):
            error = str(result["error"])
        ok = not error and not (
            isinstance(result, dict) and result.get("success") is False
        )
        finish_receipt(
            conn,
            receipt_id,
            status="succeeded" if ok else "failed",
            result=result,
            error=error,
        )
        return _tool_result(
            tool_call,
            ok=ok,
            result=result if ok else None,
            receipt_id=receipt_id,
            error=error,
        )
    except Exception as exc:
        error = f"工具执行失败: {exc}"
        finish_receipt(conn, receipt_id, status="failed", error=error)
        return _tool_result(
            tool_call,
            ok=False,
            result=None,
            receipt_id=receipt_id,
            error=error,
        )


def _tool_result(
    tool_call: ToolCall,
    *,
    ok: bool,
    result: Any,
    receipt_id: str,
    error: str = "",
) -> ToolResult:
    envelope: dict[str, Any] = {
        "ok": ok,
        "tool": tool_call.name,
        "result": result,
        "execution_receipt_id": receipt_id,
    }
    if error:
        envelope["error"] = error
    # Preserve the old top-level fields for existing MCP/bridge consumers while
    # making the new envelope authoritative.
    if isinstance(result, dict):
        envelope.update(result)
    encoded = json.dumps(envelope, ensure_ascii=False)
    if len(encoded) > 20_000:
        envelope["result"] = {
            "truncated": True,
            "preview": bounded_tool_output(result),
        }
    return ToolResult(
        tool_call_id=tool_call.id,
        name=tool_call.name,
        content=json.dumps(envelope, ensure_ascii=False),
        ok=ok,
        execution_receipt_id=receipt_id,
        result=result,
    )


def _validate_tool_call(tool_call: ToolCall) -> str | None:
    if tool_call.parse_error:
        return f"工具调用参数解析失败，未执行：{tool_call.parse_error}"
    if not tool_call.name:
        return "工具名称为空，未执行"
    if not isinstance(tool_call.arguments, dict):
        return "工具参数必须是 JSON 对象，未执行"
    definition = next(
        (
            item["function"]
            for item in TOOLS
            if item.get("function", {}).get("name") == tool_call.name
        ),
        None,
    )
    if definition is None:
        return f"未知工具: {tool_call.name}"
    schema = definition.get("parameters") or {}
    properties = schema.get("properties") or {}
    required_messages = {
        "name": "员工名字不能为空",
        "role": "岗位名称不能为空",
        "title": "任务标题不能为空",
        "description": "任务描述不能为空",
    }
    for required in schema.get("required") or []:
        if required not in tool_call.arguments:
            return required_messages.get(required, f"缺少必填参数: {required}")
    for name, value in tool_call.arguments.items():
        spec = properties.get(name)
        if not spec:
            continue
        expected = spec.get("type")
        valid = (
            expected == "string" and isinstance(value, str)
        ) or (
            expected == "boolean" and isinstance(value, bool)
        ) or (
            expected == "array" and isinstance(value, list)
        ) or (
            expected == "object" and isinstance(value, dict)
        ) or (
            expected == "integer" and isinstance(value, int) and not isinstance(value, bool)
        )
        if expected and not valid:
            return f"参数 {name} 类型错误，应为 {expected}"
        if spec.get("enum") and value not in spec["enum"]:
            return f"参数 {name} 不是允许的值"
    return None


def system_prompt_for_operator(
    workspace_name: str,
    agent_name: str,
    agent_role: str,
    *,
    related_tasks: list | None = None,
    knowledge_sources: list | None = None,
    agent_experiences: list | None = None,
    cognitive_context: str = "",
) -> str:
    """System prompt that tells the agent it can operate the system.

    Also carries company knowledge, related tasks, and employee experience for
    the legacy company-tool handler. Employee execution itself uses Hermes ACP.
    """
    from app.runtime.hermes_prompts import (
        format_agent_experiences,
        format_knowledge_sources,
        format_related_tasks,
    )

    return (
        f"你是 {workspace_name} 中的同事「{agent_name}」，岗位是{agent_role}。\n"
        "你基于公司目标和专业判断推进工作，不是某一个人的附属助手。\n\n"
        "你不仅能聊天，还能**直接操作公司系统**。你可以调用工具来：\n"
        "- 创建新员工（帮你招人、组建团队）\n"
        "- 创建任务（把工作分配给员工）\n"
        "- 创建群聊和拉人进群（组织协作）\n"
        "- 查看员工列表和任务列表\n"
        "- 认领任务、更新任务状态\n\n"
        f"{format_related_tasks(related_tasks or [])}\n"
        f"{format_knowledge_sources(knowledge_sources or [])}\n"
        f"{format_agent_experiences(agent_experiences or [])}\n\n"
        f"【本次相关公司记忆】\n{cognitive_context or '暂无额外记忆'}\n\n"
        "**重要规则**：\n"
        "1. 当公司参与者明确要求招募、建团队或创建任务时，你必须调用对应工具执行，"
        "不要只说\"好的我帮你做\"然后不做。\n"
        "2. 公司参与者说\"帮我建一个完整的创业团队\"时，根据团队类型一口气创建多个员工。\n"
        "3. 创建完员工后，主动告诉相关同事接下来可以做什么。\n"
        "4. 普通聊天时不需要调工具，用 no_action_needed。\n"
        "5. 操作完成后用中文简洁汇报结果，让当前对话参与者知道已经执行成功了。\n"
        "6. 调工具前如果需要查员工ID，先调 list_agents。\n"
        "7. 所有回复用中文，专业直接。\n"
        "8. 如果有公司资料库上下文，优先结合资料里的品牌、业务、客户、流程等事实，不要编造资料中没有的公司事实。\n"
        "9. 如果有个人经验记忆，优先复用成功经验，避开复盘教训里已经暴露的问题。\n"
        "10. 创建群聊前必须先调 list_groups 检查是否已有相同目的的群——"
        "已存在就用 add_group_member/send_group_message，绝对不要为同一件事重复建群。"
    )
