"""Validated AgentPulse company operations exposed to Hermes over MCP."""

from __future__ import annotations

import json

from app.core.database import Database
from app.services.content_packages import parse_content_package
from app.services.workspace import add_task_event, add_task_output, create_task, new_id, now_iso


class CompanyToolError(ValueError):
    pass


def _assert_colleague(conn: Database, workspace_id: str, agent_id: str) -> dict:
    row = conn.execute(
        "SELECT id, name, role, description FROM agents WHERE id = ? AND workspace_id = ?",
        (agent_id, workspace_id),
    ).fetchone()
    if row is None:
        raise CompanyToolError("同事不存在或不属于当前公司")
    return dict(row)


def _resolve_colleague_name(conn: Database, workspace_id: str, name: str) -> dict:
    rows = conn.execute(
        """SELECT id, name, role, description FROM agents
        WHERE workspace_id = ? AND name = ?""",
        (workspace_id, name.strip()),
    ).fetchall()
    if len(rows) != 1:
        raise CompanyToolError("请使用唯一的同事姓名；找不到或存在重名")
    return dict(rows[0])


def authorize_run(conn: Database, claims: dict) -> dict:
    if claims.get("run_kind") in {"chat", "triage", "idle", "coordination"}:
        row = conn.execute(
            """SELECT * FROM runs
            WHERE id = ? AND task_id IS NULL""",
            (claims["run_id"],),
        ).fetchone()
        if row is None:
            raise CompanyToolError("run is not an active chat run")
        expected = {
            "workspace_id": row["workspace_id"],
            "conversation_id": row["conversation_id"],
            "run_id": row["id"],
            "agent_id": row["agent_id"],
        }
        if any(claims.get(key) != value for key, value in expected.items()):
            raise CompanyToolError("company tool token does not match chat run ownership")
        if row["status"] not in (
            "running", "pausing", "waiting_user", "waiting_clarify",
            "waiting_information", "waiting_colleague",
        ):
            raise CompanyToolError("run is not active")
        return dict(row)

    row = conn.execute(
        """SELECT r.*, t.task_plan_id, t.owner_agent_id, t.workspace_id AS task_workspace_id,
        t.conversation_id, t.status AS task_status, t.output_type
        FROM runs r JOIN tasks t ON t.id = r.task_id
        WHERE r.id = ? AND r.task_id = ?""",
        (claims["run_id"], claims["task_id"]),
    ).fetchone()
    if row is None:
        raise CompanyToolError("run is not bound to this task")
    expected = {
        "workspace_id": row["workspace_id"],
        "plan_id": row["task_plan_id"],
        "task_id": row["task_id"],
        "run_id": row["id"],
        "agent_id": row["agent_id"],
    }
    if any(claims.get(key) != value for key, value in expected.items()):
        raise CompanyToolError("company tool token does not match run ownership")
    if row["owner_agent_id"] != claims["agent_id"]:
        raise CompanyToolError("only the current task owner may use company tools")
    if row["status"] not in (
        "running", "pausing", "waiting_user", "waiting_clarify",
        "waiting_approval", "waiting_information", "waiting_colleague",
    ):
        raise CompanyToolError("run is not active")
    return dict(row)


def require_task_run(conn: Database, claims: dict) -> dict:
    """Reject plan-mutating tools from a free-form conversation Run."""
    # Older service tests omit run_kind; a present non-task scope is rejected.
    if claims.get("run_kind") not in (None, "task", "review"):
        raise CompanyToolError("该操作必须在已确认 brief 的任务 Run 中执行")
    return authorize_run(conn, claims)


def search_company_knowledge(
    conn: Database, claims: dict, *, query: str, limit: int = 5
) -> list[dict]:
    authorize_run(conn, claims)
    bounded = max(1, min(10, limit))
    needle = f"%{query.strip().lower()}%"
    rows = conn.execute(
        """SELECT id, title, category, content FROM knowledge_sources
        WHERE workspace_id = ? AND (
          LOWER(title) LIKE ? OR LOWER(category) LIKE ? OR LOWER(content) LIKE ?
        ) ORDER BY updated_at DESC LIMIT ?""",
        (claims["workspace_id"], needle, needle, needle, bounded),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "category": row["category"],
            "snippet": row["content"][:1200],
        }
        for row in rows
    ]


def list_colleagues(conn: Database, claims: dict) -> list[dict]:
    authorize_run(conn, claims)
    rows = conn.execute(
        """SELECT id, name, role, description, status_label FROM agents
        WHERE workspace_id = ? AND id <> ? ORDER BY created_at, id""",
        (claims["workspace_id"], claims["agent_id"]),
    ).fetchall()
    return [
        {
            "name": row["name"],
            "role": row["role"],
            "description": row["description"],
            "status": row["status_label"],
        }
        for row in rows
    ]


def search_company_memory_for_run(
    conn: Database, claims: dict, *, query: str, limit: int = 8
) -> list[dict]:
    authorize_run(conn, claims)
    from app.services.company_memory import search_company_memory

    selected = search_company_memory(
        conn,
        workspace_id=claims["workspace_id"],
        query=query,
        agent_id=claims["agent_id"],
        limit=limit,
    )
    hidden = {"workspace_id", "agent_id", "actor_agent_id", "actor_user_id"}
    return [{key: value for key, value in item.items() if key not in hidden} for item in selected]


def ping_colleague(
    conn: Database,
    claims: dict,
    *,
    to_agent_id: str,
    content: str,
) -> dict:
    authorize_run(conn, claims)
    _assert_colleague(conn, claims["workspace_id"], to_agent_id)
    from app.services.company_memory import send_internal_ping

    return send_internal_ping(
        conn,
        workspace_id=claims["workspace_id"],
        from_agent_id=claims["agent_id"],
        to_agent_id=to_agent_id,
        content=content,
        run_id=claims["run_id"],
    )


def ping_colleague_by_name(
    conn: Database, claims: dict, *, to_colleague_name: str, content: str
) -> dict:
    colleague = _resolve_colleague_name(
        conn, claims["workspace_id"], to_colleague_name
    )
    result = ping_colleague(
        conn,
        claims,
        to_agent_id=colleague["id"],
        content=content,
    )
    return {
        "ok": True,
        "delivered_to": colleague["name"],
        "message_id": result["message_id"],
        "work_request_id": result.get("work_request_id"),
        "delivery_status": "delivered",
    }


def get_my_workbench(conn: Database, claims: dict) -> dict:
    authorize_run(conn, claims)
    from app.services.workforce import get_agent_workbench

    return get_agent_workbench(
        conn,
        workspace_id=claims["workspace_id"],
        agent_id=claims["agent_id"],
    )


def decide_my_work_request(
    conn: Database,
    claims: dict,
    *,
    request_id: str,
    decision: str,
    response_content: str,
    decision_reason: str = "",
    title: str | None = None,
    expected_output: str = "",
    output_type: str = "markdown",
    story_points: int | None = None,
    business_value: int = 3,
    urgency: int = 2,
    unblock_score: int = 0,
    risk_reduction: int = 0,
    switching_cost: float = 0,
    risk_level: str = "low",
    review_required: bool = False,
) -> dict:
    run = authorize_run(conn, claims)
    if claims.get("run_kind") != "triage":
        raise CompanyToolError("仅工作请求评估 Run 可以调用此工具")
    request = conn.execute(
        "SELECT * FROM work_requests WHERE id = ?", (request_id,)
    ).fetchone()
    if (
        request is None
        or request["target_agent_id"] != claims["agent_id"]
        or request.get("triage_run_id") != run["id"]
    ):
        raise CompanyToolError("工作请求不属于当前评估 Run")
    from app.services.workforce import WorkforceError, decide_work_request

    try:
        return decide_work_request(
            conn,
            workspace_id=claims["workspace_id"],
            request_id=request_id,
            target_agent_id=claims["agent_id"],
            payload={
                "decision": decision,
                "response_content": response_content,
                "decision_reason": decision_reason,
                "title": title,
                "expected_output": expected_output,
                "output_type": output_type,
                "story_points": story_points,
                "business_value": business_value,
                "urgency": urgency,
                "unblock_score": unblock_score,
                "risk_reduction": risk_reduction,
                "switching_cost": switching_cost,
                "risk_level": risk_level,
                "review_required": review_required,
            },
        )
    except WorkforceError as exc:
        raise CompanyToolError(str(exc)) from exc


def decide_my_coordination_case(
    conn: Database,
    claims: dict,
    *,
    case_id: str,
    decision: str,
    reason: str,
) -> dict:
    run = authorize_run(conn, claims)
    if claims.get("run_kind") != "coordination":
        raise CompanyToolError("仅独立协调 Hermes Run 可以提交协调结论")
    from app.services.workforce import WorkforceError, decide_coordination_case

    try:
        return decide_coordination_case(
            conn,
            workspace_id=claims["workspace_id"],
            case_id=case_id,
            coordinator_agent_id=claims["agent_id"],
            run_id=run["id"],
            decision=decision,
            reason=reason,
        )
    except WorkforceError as exc:
        raise CompanyToolError(str(exc)) from exc


def record_observation(
    conn: Database, claims: dict, *, title: str, content: str, promoted: bool = False
) -> dict:
    authorize_run(conn, claims)
    from app.services.company_memory import record_company_event, record_memory

    event = record_company_event(
        conn,
        workspace_id=claims["workspace_id"],
        event_type="agent_observation",
        source_id=f"{claims['run_id']}:{new_id('observation')}",
        title=title,
        content=content,
        task_id=claims["task_id"],
        actor_agent_id=claims["agent_id"],
        importance=2.0,
        metadata={"run_id": claims["run_id"]},
    )
    memory = record_memory(
        conn,
        workspace_id=claims["workspace_id"],
        agent_id=claims["agent_id"],
        memory_type="observation",
        title=title,
        content=content,
        evidence_event_ids=[event["id"]],
        importance=2.0,
        confidence=0.8,
        is_private=not promoted,
        promoted=promoted,
    )
    return {"ok": True, "event_id": event["id"], "memory_id": memory["id"]}


def report_relationship_fact(
    conn: Database,
    claims: dict,
    *,
    colleague_agent_id: str,
    fact: str,
) -> dict:
    authorize_run(conn, claims)
    colleague = _assert_colleague(conn, claims["workspace_id"], colleague_agent_id)
    from app.services.company_memory import record_company_event, record_memory

    event = record_company_event(
        conn,
        workspace_id=claims["workspace_id"],
        event_type="relationship_fact",
        source_id=f"{claims['run_id']}:{colleague_agent_id}:{new_id('fact')}",
        title=f"与 {colleague['name']} 的协作事实",
        content=fact,
        task_id=claims["task_id"],
        actor_agent_id=claims["agent_id"],
        importance=2.0,
        metadata={"colleague_agent_id": colleague_agent_id},
    )
    memory = record_memory(
        conn,
        workspace_id=claims["workspace_id"],
        agent_id=claims["agent_id"],
        memory_type="relationship",
        title=f"与 {colleague['name']} 的合作经验",
        content=fact,
        evidence_event_ids=[event["id"]],
        importance=2.5,
        confidence=0.75,
    )
    existing = conn.execute(
        """SELECT * FROM agent_relationships
        WHERE workspace_id = ? AND agent_id = ? AND colleague_agent_id = ?""",
        (claims["workspace_id"], claims["agent_id"], colleague_agent_id),
    ).fetchone()
    if existing is None:
        conn.execute(
            """INSERT INTO agent_relationships (
              id, workspace_id, agent_id, colleague_agent_id, summary,
              trust_score, interaction_count, evidence_event_ids_json,
              last_interacted_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0.6, 1, ?, ?, ?)""",
            (
                new_id("rel"),
                claims["workspace_id"],
                claims["agent_id"],
                colleague_agent_id,
                fact[:500],
                json.dumps([event["id"]], ensure_ascii=False),
                now_iso(),
                now_iso(),
            ),
        )
    else:
        try:
            evidence = json.loads(existing["evidence_event_ids_json"] or "[]")
        except (TypeError, ValueError):
            evidence = []
        conn.execute(
            """UPDATE agent_relationships SET summary = ?,
            interaction_count = interaction_count + 1,
            evidence_event_ids_json = ?, last_interacted_at = ?, updated_at = ?
            WHERE id = ?""",
            (
                fact[:500],
                json.dumps((evidence + [event["id"]])[-20:], ensure_ascii=False),
                now_iso(),
                now_iso(),
                existing["id"],
            ),
        )
    return {"ok": True, "event_id": event["id"], "memory_id": memory["id"]}


def report_relationship_fact_by_name(
    conn: Database, claims: dict, *, colleague_name: str, fact: str
) -> dict:
    colleague = _resolve_colleague_name(conn, claims["workspace_id"], colleague_name)
    return report_relationship_fact(
        conn,
        claims,
        colleague_agent_id=colleague["id"],
        fact=fact,
    )


def propose_internal_task(
    conn: Database,
    claims: dict,
    *,
    title: str,
    description: str,
    owner_agent_id: str,
    expected_output: str,
) -> dict:
    require_task_run(conn, claims)
    _assert_colleague(conn, claims["workspace_id"], owner_agent_id)
    return create_subtask(
        conn,
        claims,
        title=title,
        description=description,
        owner_agent_id=owner_agent_id,
        expected_output=expected_output,
        output_type="markdown",
    )


def propose_internal_task_by_name(
    conn: Database,
    claims: dict,
    *,
    title: str,
    description: str,
    owner_colleague_name: str,
    expected_output: str,
) -> dict:
    colleague = _resolve_colleague_name(
        conn, claims["workspace_id"], owner_colleague_name
    )
    return propose_internal_task(
        conn,
        claims,
        title=title,
        description=description,
        owner_agent_id=colleague["id"],
        expected_output=expected_output,
    )


def request_support_by_name(
    conn: Database,
    claims: dict,
    *,
    colleague_name: str,
    request: str,
    expected_output: str,
) -> dict:
    colleague = _resolve_colleague_name(conn, claims["workspace_id"], colleague_name)
    return request_support(
        conn,
        claims,
        agent_id=colleague["id"],
        request=request,
        expected_output=expected_output,
    )


def create_subtask_by_name(
    conn: Database,
    claims: dict,
    *,
    title: str,
    description: str,
    owner_colleague_name: str,
    expected_output: str,
    output_type: str = "markdown",
    depends_on_task_ids: list[str] | None = None,
) -> dict:
    colleague = _resolve_colleague_name(
        conn, claims["workspace_id"], owner_colleague_name
    )
    return create_subtask(
        conn,
        claims,
        title=title,
        description=description,
        owner_agent_id=colleague["id"],
        expected_output=expected_output,
        output_type=output_type,
        depends_on_task_ids=depends_on_task_ids,
    )


def report_progress(
    conn: Database, claims: dict, *, progress: int, summary: str
) -> dict:
    run = require_task_run(conn, claims)
    bounded = max(1, min(95, progress))
    conn.execute(
        "UPDATE tasks SET status = '进行中', progress = ?, updated_at = ? WHERE id = ?",
        (bounded, now_iso(), claims["task_id"]),
    )
    add_task_event(
        conn,
        workspace_id=claims["workspace_id"],
        task_id=claims["task_id"],
        conversation_id=run["conversation_id"],
        agent_id=claims["agent_id"],
        kind="progress_reported",
        title=f"进度更新 {bounded}%",
        content=summary[:2000],
    )
    return {"ok": True, "progress": bounded}


def submit_output(
    conn: Database,
    claims: dict,
    *,
    title: str,
    output_type: str,
    content: object,
) -> dict:
    run = require_task_run(conn, claims)
    if output_type != run["output_type"]:
        raise CompanyToolError(
            f"task requires output_type={run['output_type']}, got {output_type}"
        )
    if output_type == "content_package_v1":
        package = parse_content_package(content)
        serialized = package.model_dump_json()
    elif output_type == "markdown":
        serialized = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        if not serialized.strip():
            raise CompanyToolError("markdown output cannot be empty")
    else:
        serialized = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    output = add_task_output(
        conn,
        workspace_id=claims["workspace_id"],
        task_id=claims["task_id"],
        conversation_id=run["conversation_id"],
        agent_id=claims["agent_id"],
        title=title[:160],
        output_type=output_type,
        content=serialized,
    )
    add_task_event(
        conn,
        workspace_id=claims["workspace_id"],
        task_id=claims["task_id"],
        conversation_id=run["conversation_id"],
        agent_id=claims["agent_id"],
        kind="output_submitted",
        title="员工提交产出",
        content=title[:160],
    )
    return {"ok": True, "output_id": output["id"], "output_type": output_type}


def _consume_revision(conn: Database, plan_id: str) -> int:
    plan = conn.execute(
        "SELECT revision_count FROM task_plans WHERE id = ?", (plan_id,)
    ).fetchone()
    if plan is None:
        raise CompanyToolError("task plan not found")
    if int(plan["revision_count"]) >= 2:
        raise CompanyToolError("automatic plan adjustment limit reached")
    next_count = int(plan["revision_count"]) + 1
    conn.execute(
        "UPDATE task_plans SET revision_count = ?, updated_at = ? WHERE id = ?",
        (next_count, now_iso(), plan_id),
    )
    return next_count


def _would_create_cycle(
    conn: Database, *, task_id: str, depends_on_task_id: str
) -> bool:
    frontier = [depends_on_task_id]
    visited: set[str] = set()
    while frontier:
        current = frontier.pop()
        if current == task_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        rows = conn.execute(
            "SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ?",
            (current,),
        ).fetchall()
        frontier.extend(row["depends_on_task_id"] for row in rows)
    return False


def create_subtask(
    conn: Database,
    claims: dict,
    *,
    title: str,
    description: str,
    owner_agent_id: str,
    expected_output: str,
    output_type: str = "markdown",
    depends_on_task_ids: list[str] | None = None,
) -> dict:
    run = require_task_run(conn, claims)
    plan = conn.execute(
        """SELECT p.*, b.participant_agent_ids_json, b.work_items_json
        FROM task_plans p JOIN consensus_briefs b ON b.id = p.brief_id
        WHERE p.id = ?""",
        (claims["plan_id"],),
    ).fetchone()
    participants = set(json.loads(plan["participant_agent_ids_json"] or "[]"))
    if owner_agent_id not in participants:
        raise CompanyToolError("subtask owner must be a brief participant")
    if output_type == "content_package_v1":
        raise CompanyToolError("subtasks cannot add another final delivery")
    _consume_revision(conn, claims["plan_id"])
    task = create_task(
        conn,
        workspace_id=claims["workspace_id"],
        title=title[:160],
        description=description[:2000],
        owner_agent_id=owner_agent_id,
        status="待执行",
        conversation_id=run["conversation_id"],
        parent_task_id=plan["root_task_id"],
        consensus_brief_id=plan["brief_id"],
        task_plan_id=claims["plan_id"],
        plan_item_key=f"adjustment_{new_id('item')}",
        expected_output=expected_output[:2000],
        output_type=output_type,
    )
    dependencies = depends_on_task_ids or []
    for dependency_id in dependencies:
        dependency = conn.execute(
            "SELECT id FROM tasks WHERE id = ? AND task_plan_id = ?",
            (dependency_id, claims["plan_id"]),
        ).fetchone()
        if not dependency:
            raise CompanyToolError("dependency must belong to the same plan")
        if _would_create_cycle(conn, task_id=task["id"], depends_on_task_id=dependency_id):
            raise CompanyToolError("dependency would create a cycle")
        conn.execute(
            """INSERT INTO task_dependencies (
              id, task_plan_id, task_id, depends_on_task_id, created_at
            ) VALUES (?, ?, ?, ?, ?)""",
            (new_id("dep"), claims["plan_id"], task["id"], dependency_id, now_iso()),
        )
    return {"ok": True, "task_id": task["id"]}


def request_support(
    conn: Database,
    claims: dict,
    *,
    agent_id: str,
    request: str,
    expected_output: str,
) -> dict:
    result = create_subtask(
        conn,
        claims,
        title="协作支援",
        description=request,
        owner_agent_id=agent_id,
        expected_output=expected_output,
        output_type="markdown",
    )
    support_task_id = result["task_id"]
    if _would_create_cycle(
        conn, task_id=claims["task_id"], depends_on_task_id=support_task_id
    ):
        raise CompanyToolError("support dependency would create a cycle")
    conn.execute(
        """INSERT INTO task_dependencies (
          id, task_plan_id, task_id, depends_on_task_id, created_at
        ) VALUES (?, ?, ?, ?, ?)""",
        (new_id("dep"), claims["plan_id"], claims["task_id"], support_task_id, now_iso()),
    )
    return result


def block_task(conn: Database, claims: dict, *, reason: str) -> dict:
    run = require_task_run(conn, claims)
    conn.execute(
        "UPDATE tasks SET status = '阻塞', updated_at = ? WHERE id = ?",
        (now_iso(), claims["task_id"]),
    )
    conn.execute(
        """UPDATE task_plans SET status = 'blocked', blocked_reason = ?, updated_at = ?
        WHERE id = ?""",
        (reason[:2000], now_iso(), claims["plan_id"]),
    )
    add_task_event(
        conn,
        workspace_id=claims["workspace_id"],
        task_id=claims["task_id"],
        conversation_id=run["conversation_id"],
        agent_id=claims["agent_id"],
        kind="task_blocked",
        title="任务需要老板补充信息",
        content=reason[:2000],
    )
    return {"ok": True, "status": "blocked"}
