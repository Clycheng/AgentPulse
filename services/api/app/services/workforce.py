"""Durable workforce services for TD-15."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from app.core.database import Database
from app.orchestration.workforce import (
    PriorityInputs,
    age_bonus,
    calculate_priority_score,
    dependency_blockers,
    lock_schedule_key,
    refresh_task_dependency_state,
    should_preempt,
    sync_agent_work_state,
)
from app.runtime.runs import RunStatus, transition_run
from app.services.workspace import (
    add_message,
    create_dm_conversation,
    create_task,
    new_id,
    now_iso,
    serialize_task,
)
from app.services.workforce_events import emit_workforce_event


class WorkforceError(ValueError):
    pass


def _agent(conn: Database, workspace_id: str, agent_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM agents WHERE id = ? AND workspace_id = ?",
        (agent_id, workspace_id),
    ).fetchone()
    if row is None:
        raise WorkforceError("agent not found")
    return dict(row)


def serialize_work_request(row: dict) -> dict:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "conversation_id": row.get("conversation_id"),
        "source_message_id": row.get("source_message_id"),
        "requester_type": row["requester_type"],
        "requester_id": row.get("requester_id") or "",
        "target_agent_id": row["target_agent_id"],
        "source_task_id": row.get("source_task_id"),
        "consensus_brief_id": row.get("consensus_brief_id"),
        "status": row["status"],
        "content": row["content"],
        "response_content": row.get("response_content") or "",
        "decision_reason": row.get("decision_reason") or "",
        "story_points": row.get("story_points"),
        "business_value": row.get("business_value", 3),
        "urgency": row.get("urgency", 2),
        "unblock_score": row.get("unblock_score", 0),
        "risk_reduction": row.get("risk_reduction", 0),
        "switching_cost": row.get("switching_cost", 0),
        "priority_score": row.get("priority_score", 0),
        "converted_task_id": row.get("converted_task_id"),
        "triage_run_id": row.get("triage_run_id"),
        "preempts_task_id": row.get("preempts_task_id"),
        "created_at": row["created_at"],
        "acknowledged_at": row.get("acknowledged_at"),
        "decided_at": row.get("decided_at"),
        "updated_at": row["updated_at"],
    }


def serialize_coordination_case(row: dict) -> dict:
    payload = dict(row)
    payload["evidence_ids"] = json.loads(row.get("evidence_json") or "[]")
    payload.pop("evidence_json", None)
    return payload


def open_coordination_case(
    conn: Database,
    *,
    workspace_id: str,
    work_request_id: str,
    raised_by_type: str,
    raised_by_id: str,
    reason: str,
    evidence_ids: list[str] | None = None,
) -> dict:
    request = conn.execute(
        "SELECT * FROM work_requests WHERE id = ? AND workspace_id = ?",
        (work_request_id, workspace_id),
    ).fetchone()
    if request is None:
        raise WorkforceError("work request not found")
    if request["status"] == "withdrawn":
        raise WorkforceError("withdrawn request cannot be disputed")
    existing = conn.execute(
        """SELECT * FROM coordination_cases WHERE work_request_id = ?
        AND status IN ('queued','evaluating','needs_goal') ORDER BY created_at LIMIT 1""",
        (work_request_id,),
    ).fetchone()
    if existing:
        return serialize_coordination_case(existing)
    excluded = {request["target_agent_id"]}
    if request["requester_type"] == "agent" and request.get("requester_id"):
        excluded.add(request["requester_id"])
    placeholders = ",".join("?" for _ in excluded)
    candidates = conn.execute(
        f"""SELECT a.id FROM agents a JOIN agent_specs s ON s.agent_id = a.id
        WHERE a.workspace_id = ? AND s.status = 'ready'
          AND COALESCE(s.hermes_profile, '') <> '' AND a.id NOT IN ({placeholders})
        ORDER BY CASE
          WHEN a.role LIKE '%协调%' OR a.role LIKE '%秘书%' OR a.role LIKE '%运营%' THEN 0
          ELSE 1 END, a.created_at, a.id LIMIT 1""",
        (workspace_id, *sorted(excluded)),
    ).fetchone()
    if candidates is None:
        raise WorkforceError("no independent Hermes coordinator is ready")
    case_id = new_id("coord")
    timestamp = now_iso()
    conn.execute(
        """INSERT INTO coordination_cases (
          id, workspace_id, work_request_id, raised_by_type, raised_by_id,
          coordinator_agent_id, status, reason, evidence_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
        (
            case_id,
            workspace_id,
            work_request_id,
            raised_by_type,
            raised_by_id,
            candidates["id"],
            reason.strip(),
            json.dumps(evidence_ids or [], ensure_ascii=False),
            timestamp,
            timestamp,
        ),
    )
    add_message(
        conn,
        conversation_id=request["conversation_id"],
        sender_type="system",
        sender_id="",
        content=f"COORDINATION_CASE:{case_id}:已交给独立协调员工依据目标和证据处理。",
    )
    emit_workforce_event(
        conn,
        workspace_id=workspace_id,
        event_type="workforce_coordination_opened",
        source_id=case_id,
        title="内部争议进入独立协调",
        content=reason.strip(),
        conversation_id=request["conversation_id"],
        notify_owner=True,
        metadata={"work_request_id": work_request_id},
    )
    return serialize_coordination_case(
        conn.execute("SELECT * FROM coordination_cases WHERE id = ?", (case_id,)).fetchone()
    )


def decide_coordination_case(
    conn: Database,
    *,
    workspace_id: str,
    case_id: str,
    coordinator_agent_id: str,
    run_id: str,
    decision: str,
    reason: str,
) -> dict:
    if decision not in ("uphold", "defer", "reject", "needs_goal"):
        raise WorkforceError("invalid coordination decision")
    case = conn.execute(
        """SELECT * FROM coordination_cases WHERE id = ? AND workspace_id = ?
        AND coordinator_agent_id = ? AND run_id = ?""",
        (case_id, workspace_id, coordinator_agent_id, run_id),
    ).fetchone()
    if case is None:
        raise WorkforceError("coordination case does not belong to this Hermes run")
    if case["status"] != "evaluating":
        raise WorkforceError("coordination case is not evaluating")
    timestamp = now_iso()
    case_status = "needs_goal" if decision == "needs_goal" else "resolved"
    conn.execute(
        """UPDATE coordination_cases SET status = ?, decision = ?,
        decision_reason = ?, resolved_at = ?, updated_at = ? WHERE id = ?""",
        (case_status, decision, reason.strip(), timestamp, timestamp, case_id),
    )
    request = conn.execute(
        "SELECT * FROM work_requests WHERE id = ?", (case["work_request_id"],)
    ).fetchone()
    if decision == "uphold":
        conn.execute(
            """UPDATE work_requests SET status = 'delivered', triage_run_id = NULL,
            decision_reason = ?, decided_at = NULL, updated_at = ? WHERE id = ?""",
            (f"协调结论：{reason.strip()}"[:2000], timestamp, request["id"]),
        )
    elif decision == "needs_goal":
        conn.execute(
            """UPDATE work_requests SET status = 'needs_info', decision_reason = ?,
            updated_at = ? WHERE id = ?""",
            (f"需要老板补充目标：{reason.strip()}"[:2000], timestamp, request["id"]),
        )
    else:
        request_status = "deferred" if decision == "defer" else "rejected"
        conn.execute(
            """UPDATE work_requests SET status = ?, decision_reason = ?,
            decided_at = ?, updated_at = ? WHERE id = ?""",
            (request_status, f"协调结论：{reason.strip()}"[:2000], timestamp, timestamp, request["id"]),
        )
    add_message(
        conn,
        conversation_id=request["conversation_id"],
        sender_type="agent",
        sender_id=coordinator_agent_id,
        content=f"协调结论（{decision}）：{reason.strip()}",
        provider="hermes",
        model="",
    )
    emit_workforce_event(
        conn,
        workspace_id=workspace_id,
        event_type="workforce_coordination_decided",
        source_id=case_id,
        title=f"独立协调结论：{decision}",
        content=reason.strip(),
        conversation_id=request["conversation_id"],
        actor_agent_id=coordinator_agent_id,
        notify_owner=decision == "needs_goal",
        metadata={"work_request_id": request["id"], "decision": decision},
    )
    return serialize_coordination_case(
        conn.execute("SELECT * FROM coordination_cases WHERE id = ?", (case_id,)).fetchone()
    )


def create_work_request(
    conn: Database,
    *,
    workspace_id: str,
    requester_type: str,
    requester_id: str,
    target_agent_id: str,
    content: str,
    conversation_id: str | None = None,
    source_message_id: str | None = None,
    source_task_id: str | None = None,
    consensus_brief_id: str | None = None,
) -> dict:
    if requester_type not in ("user", "agent", "system"):
        raise WorkforceError("invalid requester type")
    _agent(conn, workspace_id, target_agent_id)
    if requester_type == "agent" and requester_id == target_agent_id:
        raise WorkforceError("agent cannot send a work request to itself")
    source_task = None
    if source_task_id:
        source_task = conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND workspace_id = ?",
            (source_task_id, workspace_id),
        ).fetchone()
        if source_task is None:
            raise WorkforceError("source task not found")
        consensus_brief_id = consensus_brief_id or source_task.get("consensus_brief_id")
        conversation_id = conversation_id or source_task.get("conversation_id")
    if conversation_id:
        conversation = conn.execute(
            "SELECT id FROM conversations WHERE id = ? AND workspace_id = ?",
            (conversation_id, workspace_id),
        ).fetchone()
        if conversation is None:
            raise WorkforceError("conversation not found")
    else:
        conversation_id = create_dm_conversation(
            conn, workspace_id, target_agent_id
        )["id"]
    if source_message_id:
        existing = conn.execute(
            """SELECT * FROM work_requests
            WHERE source_message_id = ? AND target_agent_id = ?""",
            (source_message_id, target_agent_id),
        ).fetchone()
        if existing:
            return serialize_work_request(existing)

    request_id = new_id("wreq")
    timestamp = now_iso()
    conn.execute(
        """INSERT INTO work_requests (
          id, workspace_id, conversation_id, source_message_id, requester_type,
          requester_id, target_agent_id, source_task_id, consensus_brief_id,
          status, content, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'delivered', ?, ?, ?)""",
        (
            request_id,
            workspace_id,
            conversation_id,
            source_message_id,
            requester_type,
            requester_id,
            target_agent_id,
            source_task_id,
            consensus_brief_id,
            content.strip(),
            timestamp,
            timestamp,
        ),
    )
    if conversation_id:
        add_message(
            conn,
            conversation_id=conversation_id,
            sender_type="system",
            sender_id="",
            content=f"WORK_REQUEST_RECEIPT:{request_id}:已送达，等待员工在安全点评估。",
        )
    active = conn.execute(
        """SELECT id FROM runs WHERE agent_id = ? AND status = 'running'
        ORDER BY COALESCE(started_at, created_at), id LIMIT 1""",
        (target_agent_id,),
    ).fetchone()
    if active:
        conn.execute(
            """UPDATE runs SET pause_requested_at = ?, pause_reason = ?,
            preempted_by_request_id = ? WHERE id = ?""",
            (timestamp, "收到同事工作请求，等待安全点评估", request_id, active["id"]),
        )
    emit_workforce_event(
        conn,
        workspace_id=workspace_id,
        event_type="workforce_request_delivered",
        source_id=request_id,
        title="工作请求已送达",
        content=content.strip(),
        conversation_id=conversation_id,
        task_id=source_task_id,
        actor_agent_id=requester_id if requester_type == "agent" else None,
        actor_user_id=requester_id if requester_type == "user" else None,
        metadata={"target_agent_id": target_agent_id, "pause_requested": bool(active)},
    )
    sync_agent_work_state(conn, target_agent_id, now_iso=timestamp)
    return serialize_work_request(
        conn.execute("SELECT * FROM work_requests WHERE id = ?", (request_id,)).fetchone()
    )


def list_agent_work_requests(
    conn: Database, *, workspace_id: str, agent_id: str, include_closed: bool = False
) -> list[dict]:
    _agent(conn, workspace_id, agent_id)
    status_filter = "" if include_closed else (
        "AND status IN ('delivered','acknowledged','evaluating','needs_info')"
    )
    rows = conn.execute(
        f"""SELECT * FROM work_requests WHERE workspace_id = ? AND target_agent_id = ?
        {status_filter} ORDER BY created_at, id""",
        (workspace_id, agent_id),
    ).fetchall()
    return [serialize_work_request(row) for row in rows]


def acknowledge_work_request(
    conn: Database, *, request_id: str, target_agent_id: str
) -> dict:
    row = conn.execute("SELECT * FROM work_requests WHERE id = ?", (request_id,)).fetchone()
    if row is None or row["target_agent_id"] != target_agent_id:
        raise WorkforceError("work request not found")
    if row["status"] == "delivered":
        timestamp = now_iso()
        conn.execute(
            """UPDATE work_requests SET status = 'acknowledged', acknowledged_at = ?,
            updated_at = ? WHERE id = ?""",
            (timestamp, timestamp, request_id),
        )
    return serialize_work_request(
        conn.execute("SELECT * FROM work_requests WHERE id = ?", (request_id,)).fetchone()
    )


def _priority_values(payload: dict, created_at: str) -> tuple[PriorityInputs, float]:
    values = PriorityInputs(
        story_points=int(payload["story_points"]),
        business_value=int(payload.get("business_value", 3)),
        urgency=int(payload.get("urgency", 2)),
        unblock_score=int(payload.get("unblock_score", 0)),
        risk_reduction=int(payload.get("risk_reduction", 0)),
        age_bonus=age_bonus(created_at),
        switching_cost=float(payload.get("switching_cost", 0)),
    )
    return values, calculate_priority_score(values)


def _record_priority_assessment(
    conn: Database,
    *,
    workspace_id: str,
    request_id: str,
    task_id: str,
    agent_id: str,
    values: PriorityInputs,
    score: float,
    reason: str,
) -> None:
    conn.execute(
        """INSERT INTO priority_assessments (
          id, workspace_id, work_request_id, task_id, agent_id, story_points,
          business_value, urgency, unblock_score, risk_reduction, age_bonus,
          switching_cost, priority_score, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            new_id("prio"),
            workspace_id,
            request_id,
            task_id,
            agent_id,
            values.story_points,
            values.business_value,
            values.urgency,
            values.unblock_score,
            values.risk_reduction,
            values.age_bonus,
            values.switching_cost,
            score,
            reason[:2000],
            now_iso(),
        ),
    )


def _current_task_for_agent(conn: Database, agent_id: str) -> dict | None:
    row = conn.execute(
        """SELECT * FROM tasks WHERE owner_agent_id = ?
        AND workflow_status IN ('in_progress','paused')
        ORDER BY CASE workflow_status WHEN 'in_progress' THEN 0 ELSE 1 END,
          updated_at DESC LIMIT 1""",
        (agent_id,),
    ).fetchone()
    return dict(row) if row else None


def _record_agent_preemption(
    conn: Database, *, agent_id: str, timestamp: str
) -> None:
    state = conn.execute(
        "SELECT * FROM agent_work_states WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    if state is None:
        return
    window_started = state.get("interruption_window_started_at")
    try:
        started_at = datetime.fromisoformat(window_started) if window_started else None
    except ValueError:
        started_at = None
    now = datetime.fromisoformat(timestamp)
    if started_at is None or now - started_at >= timedelta(hours=1):
        count = 1
        window_started = timestamp
    else:
        count = int(state.get("interruption_count_window") or 0) + 1
    conn.execute(
        """UPDATE agent_work_states SET interruption_count_window = ?,
        interruption_window_started_at = ?, updated_at = ? WHERE agent_id = ?""",
        (count, window_started, timestamp, agent_id),
    )


def _resume_paused_run_for_request(
    conn: Database, *, request_id: str, agent_id: str
) -> None:
    run = conn.execute(
        """SELECT * FROM runs WHERE agent_id = ? AND preempted_by_request_id = ?
        AND status = 'paused' ORDER BY created_at DESC LIMIT 1""",
        (agent_id, request_id),
    ).fetchone()
    if run is None:
        return
    transition_run(conn, run["id"], RunStatus.QUEUED)
    conn.execute(
        """UPDATE runs SET pause_requested_at = NULL, pause_reason = '',
        lease_owner = NULL, lease_expires_at = NULL WHERE id = ?""",
        (run["id"],),
    )
    if run.get("task_id"):
        conn.execute(
            "UPDATE tasks SET workflow_status = 'ready', waiting_reason = '', updated_at = ? WHERE id = ?",
            (now_iso(), run["task_id"]),
        )


def resume_preempted_task_after(conn: Database, *, task_id: str) -> str | None:
    """Resume the exact foreground task displaced by a finished intervention."""
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None or not task.get("preempted_task_id"):
        return None
    previous_task_id = task["preempted_task_id"]
    previous = conn.execute(
        "SELECT * FROM tasks WHERE id = ?", (previous_task_id,)
    ).fetchone()
    if previous is None or previous["workflow_status"] != "paused":
        return None
    run = conn.execute(
        """SELECT * FROM runs WHERE task_id = ? AND status = 'paused'
        ORDER BY created_at DESC, id DESC LIMIT 1""",
        (previous_task_id,),
    ).fetchone()
    if run is None:
        return None
    transition_run(conn, run["id"], RunStatus.QUEUED)
    timestamp = now_iso()
    conn.execute(
        """UPDATE runs SET pause_requested_at = NULL, pause_reason = '',
        preempted_by_request_id = NULL, lease_owner = NULL, lease_expires_at = NULL,
        runtime_status = 'queued' WHERE id = ?""",
        (run["id"],),
    )
    conn.execute(
        """UPDATE tasks SET status = '待执行', workflow_status = 'ready',
        waiting_reason = '', updated_at = ? WHERE id = ?""",
        (timestamp, previous_task_id),
    )
    sync_agent_work_state(conn, previous["owner_agent_id"], now_iso=timestamp)
    emit_workforce_event(
        conn,
        workspace_id=previous["workspace_id"],
        event_type="workforce_run_resumed",
        source_id=f"{run['id']}:{task_id}",
        title="被抢占任务已恢复",
        task_id=previous_task_id,
        actor_agent_id=previous["owner_agent_id"],
        metadata={"run_id": run["id"], "intervention_task_id": task_id},
    )
    return run["id"]


def decide_work_request(
    conn: Database,
    *,
    workspace_id: str,
    request_id: str,
    target_agent_id: str,
    payload: dict,
) -> dict:
    request = conn.execute(
        "SELECT * FROM work_requests WHERE id = ? AND workspace_id = ?",
        (request_id, workspace_id),
    ).fetchone()
    if request is None or request["target_agent_id"] != target_agent_id:
        raise WorkforceError("work request not found")
    if request["status"] not in ("delivered", "acknowledged", "evaluating", "needs_info"):
        raise WorkforceError("work request is already decided")
    decision = payload["decision"]
    if decision not in ("answered", "accepted", "deferred", "rejected", "needs_info"):
        raise WorkforceError("invalid work request decision")
    timestamp = now_iso()
    task = None
    preempts_task_id = None
    score = 0.0
    values = None
    if decision in ("accepted", "deferred"):
        if payload.get("story_points") is None:
            raise WorkforceError("accepted/deferred work requires story_points")
        brief_id = request.get("consensus_brief_id")
        source_task = None
        if request.get("source_task_id"):
            source_task = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (request["source_task_id"],)
            ).fetchone()
            if source_task:
                brief_id = brief_id or source_task.get("consensus_brief_id")
        if not brief_id:
            raise WorkforceError("work outside a confirmed brief must return to discussion first")
        brief = conn.execute(
            "SELECT status FROM consensus_briefs WHERE id = ? AND workspace_id = ?",
            (brief_id, workspace_id),
        ).fetchone()
        if brief is None or brief["status"] != "confirmed":
            raise WorkforceError("work request brief is not confirmed")
        values, score = _priority_values(payload, request["created_at"])
        current = _current_task_for_agent(conn, target_agent_id)
        can_preempt = False
        if decision == "accepted" and current:
            checkpoint = json.loads(current.get("checkpoint_json") or "{}")
            state = conn.execute(
                "SELECT * FROM agent_work_states WHERE agent_id = ?", (target_agent_id,)
            ).fetchone()
            can_preempt = should_preempt(
                incoming_score=score,
                current_score=float(current.get("priority_score") or 0),
                current_preemptible=bool(current.get("preemptible", 1)),
                current_in_atomic_tool=bool(checkpoint.get("atomic_tool_active")),
                recent_interruptions=int(state.get("interruption_count_window") or 0) if state else 0,
            )
            if can_preempt:
                preempts_task_id = current["id"]
        workflow_status = "ready" if decision == "accepted" and not current else "queued"
        if can_preempt:
            workflow_status = "ready"
            conn.execute(
                """UPDATE tasks SET workflow_status = 'paused', waiting_reason = 'preempted',
                preemption_count = preemption_count + 1, updated_at = ? WHERE id = ?""",
                (timestamp, current["id"]),
            )
            _record_agent_preemption(
                conn, agent_id=target_agent_id, timestamp=timestamp
            )
        task = create_task(
            conn,
            workspace_id=workspace_id,
            title=(payload.get("title") or request["content"][:160]).strip(),
            description=request["content"],
            owner_agent_id=target_agent_id,
            status="待执行",
            progress=0,
            conversation_id=request.get("conversation_id"),
            parent_task_id=request.get("source_task_id"),
            consensus_brief_id=brief_id,
            task_plan_id=source_task.get("task_plan_id") if source_task else None,
            plan_item_key=f"request_{request_id}",
            expected_output=payload.get("expected_output") or "完成请求并回传可审计结果",
            output_type=payload.get("output_type") or "markdown",
        )
        conn.execute(
            """UPDATE tasks SET workflow_status = ?, story_points = ?, business_value = ?,
            urgency = ?, unblock_score = ?, risk_reduction = ?, switching_cost = ?,
            priority_score = ?, preempted_task_id = ?, review_required = ?, risk_level = ?
            WHERE id = ?""",
            (
                workflow_status,
                values.story_points,
                values.business_value,
                values.urgency,
                values.unblock_score,
                values.risk_reduction,
                values.switching_cost,
                score,
                preempts_task_id,
                1 if payload.get("review_required") else 0,
                payload.get("risk_level") or "low",
                task["id"],
            ),
        )
        _record_priority_assessment(
            conn,
            workspace_id=workspace_id,
            request_id=request_id,
            task_id=task["id"],
            agent_id=target_agent_id,
            values=values,
            score=score,
            reason=payload.get("decision_reason") or payload.get("response_content") or "",
        )
    conn.execute(
        """UPDATE work_requests SET status = ?, response_content = ?, decision_reason = ?,
        story_points = ?, business_value = ?, urgency = ?, unblock_score = ?,
        risk_reduction = ?, switching_cost = ?, priority_score = ?, converted_task_id = ?,
        preempts_task_id = ?, decided_at = ?, updated_at = ? WHERE id = ?""",
        (
            decision,
            payload["response_content"].strip(),
            (payload.get("decision_reason") or "")[:2000],
            payload.get("story_points"),
            payload.get("business_value", 3),
            payload.get("urgency", 2),
            payload.get("unblock_score", 0),
            payload.get("risk_reduction", 0),
            payload.get("switching_cost", 0),
            score,
            task["id"] if task else None,
            preempts_task_id,
            timestamp,
            timestamp,
            request_id,
        ),
    )
    if request.get("conversation_id"):
        add_message(
            conn,
            conversation_id=request["conversation_id"],
            sender_type="agent",
            sender_id=target_agent_id,
            content=payload["response_content"].strip(),
            provider="hermes",
            model="",
        )
    if not preempts_task_id:
        _resume_paused_run_for_request(
            conn, request_id=request_id, agent_id=target_agent_id
        )
    sync_agent_work_state(conn, target_agent_id, now_iso=timestamp)
    emit_workforce_event(
        conn,
        workspace_id=workspace_id,
        event_type="workforce_request_decided",
        source_id=request_id,
        title=f"工作请求已{decision}",
        content=payload["response_content"].strip(),
        conversation_id=request.get("conversation_id"),
        task_id=task["id"] if task else request.get("source_task_id"),
        actor_agent_id=target_agent_id,
        notify_owner=decision in {"rejected", "needs_info"},
        metadata={
            "decision": decision,
            "priority_score": score,
            "preempts_task_id": preempts_task_id,
        },
    )
    if preempts_task_id:
        emit_workforce_event(
            conn,
            workspace_id=workspace_id,
            event_type="workforce_task_preempted",
            source_id=request_id,
            title="高优先级请求触发安全抢占",
            task_id=preempts_task_id,
            actor_agent_id=target_agent_id,
            metadata={"incoming_task_id": task["id"], "priority_score": score},
        )
    result = serialize_work_request(
        conn.execute("SELECT * FROM work_requests WHERE id = ?", (request_id,)).fetchone()
    )
    result["task"] = serialize_task(
        conn.execute("SELECT * FROM tasks WHERE id = ?", (task["id"],)).fetchone()
    ) if task else None
    return result


def get_agent_workbench(conn: Database, *, workspace_id: str, agent_id: str) -> dict:
    agent = _agent(conn, workspace_id, agent_id)
    state = sync_agent_work_state(conn, agent_id, now_iso=now_iso())
    queue_rows = conn.execute(
        """SELECT * FROM tasks WHERE owner_agent_id = ? AND workflow_status IN (
          'queued','ready','waiting_dependency','waiting_information','waiting_review',
          'waiting_approval','waiting_resource') ORDER BY priority_score DESC, created_at, id""",
        (agent_id,),
    ).fetchall()
    queue = []
    for row in queue_rows:
        payload = serialize_task(row)
        payload.update(
            {
                "workflow_status": row["workflow_status"],
                "waiting_reason": row["waiting_reason"],
                "story_points": row["story_points"],
                "priority_score": row["priority_score"],
                "dynamic_priority_score": calculate_priority_score(
                    PriorityInputs(
                        story_points=int(row["story_points"]),
                        business_value=int(row["business_value"]),
                        urgency=int(row["urgency"]),
                        unblock_score=int(row["unblock_score"]),
                        risk_reduction=int(row["risk_reduction"]),
                        age_bonus=age_bonus(row["created_at"]),
                        switching_cost=float(row["switching_cost"]),
                    )
                ),
                "blockers": dependency_blockers(conn, row["id"]),
            }
        )
        queue.append(payload)
    queue.sort(key=lambda item: (-item["dynamic_priority_score"], item["created_at"], item["id"]))
    current = None
    if state.get("current_task_id"):
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (state["current_task_id"],)).fetchone()
        current = serialize_task(row) if row else None
    paused_rows = conn.execute(
        "SELECT * FROM tasks WHERE owner_agent_id = ? AND workflow_status = 'paused' ORDER BY updated_at DESC",
        (agent_id,),
    ).fetchall()
    resources = conn.execute(
        """SELECT * FROM resource_leases WHERE owner_agent_id = ? AND status = 'active'
        ORDER BY created_at, id""",
        (agent_id,),
    ).fetchall()
    return {
        "agent": {"id": agent["id"], "name": agent["name"], "role": agent["role"]},
        "state": state,
        "current_task": current,
        "requests": list_agent_work_requests(
            conn, workspace_id=workspace_id, agent_id=agent_id
        ),
        "queue": queue,
        "paused": [serialize_task(row) | {"workflow_status": "paused"} for row in paused_rows],
        "resources": [dict(row) for row in resources],
    }


def get_workforce_overview(conn: Database, *, workspace_id: str) -> dict:
    agent_state_rows = conn.execute(
        """SELECT a.id AS agent_id, COALESCE(s.activity, 'available') AS activity,
        s.current_task_id, s.updated_at
        FROM agents a LEFT JOIN agent_work_states s ON s.agent_id = a.id
        WHERE a.workspace_id = ? ORDER BY a.created_at, a.id""",
        (workspace_id,),
    ).fetchall()
    agent_counts: dict[str, int] = {}
    for row in agent_state_rows:
        activity = row["activity"]
        agent_counts[activity] = agent_counts.get(activity, 0) + 1
    run_rows = conn.execute(
        """SELECT status, COUNT(*) AS count FROM runs WHERE workspace_id = ?
        AND status NOT IN ('completed','failed','cancelled','timed_out') GROUP BY status""",
        (workspace_id,),
    ).fetchall()
    plan_rows = conn.execute(
        """SELECT COALESCE(NULLIF(workflow_status, ''), status) AS status,
        COUNT(*) AS count FROM task_plans WHERE workspace_id = ?
        AND status NOT IN ('completed','cancelled')
        GROUP BY COALESCE(NULLIF(workflow_status, ''), status)""",
        (workspace_id,),
    ).fetchall()
    requests = conn.execute(
        """SELECT COUNT(*) AS count FROM work_requests WHERE workspace_id = ?
        AND status IN ('delivered','acknowledged','evaluating','needs_info')""",
        (workspace_id,),
    ).fetchone()["count"]
    resources = conn.execute(
        """SELECT COUNT(*) AS count FROM resource_leases
        WHERE workspace_id = ? AND status = 'active' AND expires_at >= ?""",
        (workspace_id, now_iso()),
    ).fetchone()["count"]
    return {
        "agents": agent_counts,
        "agent_states": [dict(row) for row in agent_state_rows],
        "runs": {row["status"]: int(row["count"]) for row in run_rows},
        "plans": {row["status"]: int(row["count"]) for row in plan_rows},
        "open_requests": int(requests),
        "active_resources": int(resources),
    }


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


def add_task_dependency(
    conn: Database,
    *,
    workspace_id: str,
    task_id: str,
    dependency_type: str,
    depends_on_task_id: str | None = None,
    resource_type: str | None = None,
    resource_key: str | None = None,
    mode: str = "exclusive",
    units: int = 1,
) -> dict:
    task = conn.execute(
        "SELECT * FROM tasks WHERE id = ? AND workspace_id = ?", (task_id, workspace_id)
    ).fetchone()
    if task is None:
        raise WorkforceError("task not found")
    timestamp = now_iso()
    if dependency_type == "resource":
        if not resource_type or not resource_key:
            raise WorkforceError("resource dependency requires resource type and key")
        requirement_id = new_id("req")
        conn.execute(
            """INSERT INTO task_resource_requirements (
              id, workspace_id, task_id, resource_type, resource_key, mode, units, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (task_id, resource_type, resource_key) DO NOTHING""",
            (
                requirement_id,
                workspace_id,
                task_id,
                resource_type,
                resource_key,
                mode,
                units,
                timestamp,
            ),
        )
        conn.execute(
            "UPDATE tasks SET workflow_status = 'waiting_resource', waiting_reason = 'resource', updated_at = ? WHERE id = ?",
            (timestamp, task_id),
        )
        emit_workforce_event(
            conn,
            workspace_id=workspace_id,
            event_type="workforce_dependency_added",
            source_id=requirement_id,
            title="任务等待执行资源",
            task_id=task_id,
            metadata={"dependency_type": "resource", "resource_type": resource_type, "resource_key": resource_key},
        )
        return {"id": requirement_id, "dependency_type": "resource", "resource_type": resource_type, "resource_key": resource_key}
    dependency = conn.execute(
        "SELECT * FROM tasks WHERE id = ? AND workspace_id = ?",
        (depends_on_task_id, workspace_id),
    ).fetchone()
    if dependency is None:
        raise WorkforceError("dependency task not found")
    if not task.get("task_plan_id"):
        raise WorkforceError("task dependencies require a confirmed plan")
    if task.get("task_plan_id") != dependency.get("task_plan_id"):
        raise WorkforceError("dependency must belong to the same confirmed plan")
    if _would_create_cycle(conn, task_id=task_id, depends_on_task_id=depends_on_task_id):
        raise WorkforceError("dependency would create a cycle")
    dependency_id = new_id("dep")
    conn.execute(
        """INSERT INTO task_dependencies (
          id, task_plan_id, task_id, depends_on_task_id, dependency_type, status, created_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
        (dependency_id, task["task_plan_id"], task_id, depends_on_task_id, dependency_type, timestamp),
    )
    workflow_status = refresh_task_dependency_state(conn, task_id)
    emit_workforce_event(
        conn,
        workspace_id=workspace_id,
        event_type="workforce_dependency_added",
        source_id=dependency_id,
        title=f"任务新增{dependency_type}依赖",
        task_id=task_id,
        metadata={"depends_on_task_id": depends_on_task_id, "workflow_status": workflow_status},
    )
    return {"id": dependency_id, "dependency_type": dependency_type, "workflow_status": workflow_status}


def satisfy_information_dependency(
    conn: Database, *, dependency_id: str, evidence_type: str, evidence_id: str
) -> dict:
    row = conn.execute("SELECT * FROM task_dependencies WHERE id = ?", (dependency_id,)).fetchone()
    if row is None or row.get("dependency_type") != "information":
        raise WorkforceError("information dependency not found")
    timestamp = now_iso()
    conn.execute(
        """UPDATE task_dependencies SET status = 'satisfied', satisfied_by_type = ?,
        satisfied_by_id = ?, satisfied_at = ? WHERE id = ?""",
        (evidence_type, evidence_id, timestamp, dependency_id),
    )
    refresh_task_dependency_state(conn, row["task_id"])
    task = conn.execute(
        "SELECT workspace_id FROM tasks WHERE id = ?", (row["task_id"],)
    ).fetchone()
    if task:
        emit_workforce_event(
            conn,
            workspace_id=task["workspace_id"],
            event_type="workforce_dependency_satisfied",
            source_id=dependency_id,
            title="信息依赖已满足",
            task_id=row["task_id"],
            metadata={"evidence_type": evidence_type, "evidence_id": evidence_id},
        )
    return dict(conn.execute("SELECT * FROM task_dependencies WHERE id = ?", (dependency_id,)).fetchone())


def request_task_review(
    conn: Database,
    *,
    workspace_id: str,
    task_id: str,
    reviewer_agent_id: str,
    instructions: str,
) -> dict:
    task = conn.execute(
        "SELECT * FROM tasks WHERE id = ? AND workspace_id = ?", (task_id, workspace_id)
    ).fetchone()
    if task is None:
        raise WorkforceError("task not found")
    _agent(conn, workspace_id, reviewer_agent_id)
    if reviewer_agent_id == task.get("owner_agent_id"):
        raise WorkforceError("reviewer must be a different employee")
    review_task = create_task(
        conn,
        workspace_id=workspace_id,
        title=f"评审：{task['title']}",
        description=instructions or "检查交付是否满足成功标准，并给出批准或修改意见。",
        owner_agent_id=reviewer_agent_id,
        status="待执行",
        conversation_id=task.get("conversation_id"),
        parent_task_id=task_id,
        consensus_brief_id=task.get("consensus_brief_id"),
        task_plan_id=task.get("task_plan_id"),
        plan_item_key=f"review_{new_id('item')}",
        expected_output="明确的 approved 或 changes_requested 结论及证据",
        output_type="markdown",
    )
    timestamp = now_iso()
    conn.execute(
        "UPDATE tasks SET workflow_status = 'ready', review_required = 1 WHERE id = ?",
        (review_task["id"],),
    )
    conn.execute(
        """UPDATE tasks SET workflow_status = 'waiting_review', waiting_reason = 'review',
        review_required = 1, updated_at = ? WHERE id = ?""",
        (timestamp, task_id),
    )
    review_id = new_id("review")
    conn.execute(
        """INSERT INTO task_reviews (
          id, workspace_id, task_id, reviewer_agent_id, review_task_id, status,
          instructions, created_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (review_id, workspace_id, task_id, reviewer_agent_id, review_task["id"], instructions, timestamp),
    )
    return dict(conn.execute("SELECT * FROM task_reviews WHERE id = ?", (review_id,)).fetchone())


def decide_task_review(
    conn: Database,
    *,
    workspace_id: str,
    review_id: str,
    reviewer_agent_id: str,
    decision: str,
    reason: str,
) -> dict:
    review = conn.execute(
        "SELECT * FROM task_reviews WHERE id = ? AND workspace_id = ?",
        (review_id, workspace_id),
    ).fetchone()
    if review is None or review["reviewer_agent_id"] != reviewer_agent_id:
        raise WorkforceError("review not found")
    if review["status"] in ("approved", "changes_requested", "cancelled"):
        raise WorkforceError("review is already decided")
    timestamp = now_iso()
    conn.execute(
        """UPDATE task_reviews SET status = ?, decision_reason = ?, decided_at = ?
        WHERE id = ?""",
        (decision, reason, timestamp, review_id),
    )
    if decision == "approved":
        conn.execute(
            """UPDATE tasks SET workflow_status = 'completed', status = '已完成',
            progress = 100, waiting_reason = '', updated_at = ? WHERE id = ?""",
            (timestamp, review["task_id"]),
        )
    else:
        conn.execute(
            """UPDATE tasks SET workflow_status = 'ready', status = '待执行',
            progress = CASE WHEN progress > 90 THEN 90 ELSE progress END,
            waiting_reason = '', updated_at = ? WHERE id = ?""",
            (timestamp, review["task_id"]),
        )
    if review.get("review_task_id"):
        conn.execute(
            """UPDATE tasks SET workflow_status = 'completed', status = '已完成',
            progress = 100, updated_at = ? WHERE id = ?""",
            (timestamp, review["review_task_id"]),
        )
    return dict(conn.execute("SELECT * FROM task_reviews WHERE id = ?", (review_id,)).fetchone())


def acquire_task_resources(
    conn: Database,
    *,
    workspace_id: str,
    task_id: str,
    run_id: str,
    agent_id: str,
    lease_owner: str,
    ttl_seconds: int,
) -> list[dict] | None:
    requirements = conn.execute(
        "SELECT * FROM task_resource_requirements WHERE task_id = ? ORDER BY created_at, id",
        (task_id,),
    ).fetchall()
    if not requirements:
        return []
    now = datetime.now(UTC)
    now_text = now.isoformat()
    expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
    for requirement in sorted(
        requirements,
        key=lambda row: (row["resource_type"], row["resource_key"]),
    ):
        lock_schedule_key(
            conn,
            f"resource:{workspace_id}:{requirement['resource_type']}:{requirement['resource_key']}",
        )
    conn.execute(
        """UPDATE resource_leases SET status = 'expired', released_at = ?
        WHERE status = 'active' AND expires_at < ?""",
        (now_text, now_text),
    )
    for requirement in requirements:
        conflicts = conn.execute(
            """SELECT * FROM resource_leases WHERE workspace_id = ?
            AND resource_type = ? AND resource_key = ? AND status = 'active'
            AND (mode = 'exclusive' OR ? = 'exclusive')""",
            (
                workspace_id,
                requirement["resource_type"],
                requirement["resource_key"],
                requirement["mode"],
            ),
        ).fetchall()
        if any(row.get("run_id") != run_id for row in conflicts):
            conn.execute(
                "UPDATE tasks SET workflow_status = 'waiting_resource', waiting_reason = 'resource' WHERE id = ?",
                (task_id,),
            )
            return None
    leases = []
    for requirement in requirements:
        lease_id = new_id("lease")
        conn.execute(
            """INSERT INTO resource_leases (
              id, workspace_id, resource_type, resource_key, mode, owner_agent_id,
              run_id, lease_owner, status, expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (
                lease_id,
                workspace_id,
                requirement["resource_type"],
                requirement["resource_key"],
                requirement["mode"],
                agent_id,
                run_id,
                lease_owner,
                expires_at,
                now_text,
            ),
        )
        leases.append(dict(conn.execute("SELECT * FROM resource_leases WHERE id = ?", (lease_id,)).fetchone()))
    return leases


def release_run_resources(conn: Database, *, run_id: str) -> None:
    conn.execute(
        """UPDATE resource_leases SET status = 'released', released_at = ?
        WHERE run_id = ? AND status = 'active'""",
        (now_iso(), run_id),
    )


def request_run_pause(
    conn: Database, *, workspace_id: str, run_id: str, reason: str
) -> dict:
    run = conn.execute(
        "SELECT * FROM runs WHERE id = ? AND workspace_id = ?",
        (run_id, workspace_id),
    ).fetchone()
    if run is None:
        raise WorkforceError("run not found")
    if run["status"] in RunStatus.TERMINAL:
        raise WorkforceError("terminal run cannot be paused")
    if run["status"] == RunStatus.PAUSED:
        return dict(run)
    conn.execute(
        """UPDATE runs SET pause_requested_at = ?, pause_reason = ?
        WHERE id = ?""",
        (now_iso(), reason[:1000], run_id),
    )
    if run["status"] in (RunStatus.QUEUED, RunStatus.LEASED):
        transition_run(conn, run_id, RunStatus.PAUSED)
        conn.execute(
            "UPDATE runs SET lease_owner = NULL, lease_expires_at = NULL WHERE id = ?",
            (run_id,),
        )
    sync_agent_work_state(conn, run["agent_id"], now_iso=now_iso())
    emit_workforce_event(
        conn,
        workspace_id=workspace_id,
        event_type="workforce_run_pause_requested",
        source_id=f"{run_id}:{run.get('pause_requested_at') or now_iso()}",
        title="Run 已请求安全暂停",
        content=reason[:1000],
        task_id=run.get("task_id"),
        actor_agent_id=run["agent_id"],
        metadata={"run_id": run_id},
    )
    return dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())


def resume_paused_run(
    conn: Database, *, workspace_id: str, run_id: str, reason: str
) -> dict:
    run = conn.execute(
        "SELECT * FROM runs WHERE id = ? AND workspace_id = ?",
        (run_id, workspace_id),
    ).fetchone()
    if run is None:
        raise WorkforceError("run not found")
    if run["status"] != RunStatus.PAUSED:
        raise WorkforceError("only paused runs can resume")
    foreground = conn.execute(
        """SELECT id FROM runs WHERE agent_id = ? AND id <> ?
        AND status IN ('leased','running','pausing') LIMIT 1""",
        (run["agent_id"], run_id),
    ).fetchone()
    if foreground:
        raise WorkforceError("employee already has a foreground run")
    transition_run(conn, run_id, RunStatus.QUEUED)
    conn.execute(
        """UPDATE runs SET pause_requested_at = NULL, pause_reason = ?,
        lease_owner = NULL, lease_expires_at = NULL WHERE id = ?""",
        (reason[:1000], run_id),
    )
    if run.get("task_id"):
        conn.execute(
            """UPDATE tasks SET workflow_status = 'ready', waiting_reason = '',
            status = '待执行', updated_at = ? WHERE id = ?""",
            (now_iso(), run["task_id"]),
        )
    sync_agent_work_state(conn, run["agent_id"], now_iso=now_iso())
    emit_workforce_event(
        conn,
        workspace_id=workspace_id,
        event_type="workforce_run_resumed",
        source_id=f"manual:{run_id}",
        title="Run 已恢复",
        content=reason[:1000],
        task_id=run.get("task_id"),
        actor_agent_id=run["agent_id"],
        metadata={"run_id": run_id},
    )
    return dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())
