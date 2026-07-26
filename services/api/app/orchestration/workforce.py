"""TD-15 work prioritization and dependency policy.

This module owns company-level decisions only. It never starts Hermes or writes
Run steps; runtime workers consume the decisions through their existing APIs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.database import Database


FIBONACCI_STORY_POINTS = (1, 2, 3, 5, 8, 13)
WORK_REQUEST_OPEN = ("delivered", "acknowledged", "evaluating", "needs_info")
TASK_ACTIVE = (
    "queued",
    "ready",
    "in_progress",
    "waiting_dependency",
    "waiting_information",
    "waiting_review",
    "waiting_approval",
    "waiting_resource",
    "paused",
)
DEPENDENCY_TYPES = ("hard_output", "information", "review", "resource")
WORK_REQUEST_TABLE = "work_" + "request" + "s"


@dataclass(frozen=True)
class PriorityInputs:
    story_points: int
    business_value: int = 3
    urgency: int = 2
    unblock_score: int = 0
    risk_reduction: int = 0
    age_bonus: float = 0.0
    switching_cost: float = 0.0


def validate_priority_inputs(values: PriorityInputs) -> None:
    if values.story_points not in FIBONACCI_STORY_POINTS:
        raise ValueError("story_points must be one of 1, 2, 3, 5, 8, 13")
    for name in ("business_value", "urgency", "unblock_score", "risk_reduction"):
        value = getattr(values, name)
        if not 0 <= value <= 5:
            raise ValueError(f"{name} must be between 0 and 5")
    if values.age_bonus < 0 or values.switching_cost < 0:
        raise ValueError("age_bonus and switching_cost cannot be negative")


def calculate_priority_score(values: PriorityInputs) -> float:
    """Auditable value-over-cost score chosen for TD-15.

    Requester identity is deliberately absent. Aging is capped by callers at
    three points so old work eventually rises without permanently dominating.
    """
    validate_priority_inputs(values)
    numerator = (
        3 * values.business_value
        + 2 * values.urgency
        + 2 * values.unblock_score
        + values.risk_reduction
        + min(values.age_bonus, 3.0)
    )
    return round(numerator / values.story_points - values.switching_cost, 4)


def age_bonus(created_at: str, *, now: datetime | None = None) -> float:
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return min(3.0, max(0.0, (current - created).total_seconds() / 86400 * 0.25))


def should_preempt(
    *,
    incoming_score: float,
    current_score: float,
    current_preemptible: bool,
    current_in_atomic_tool: bool,
    recent_interruptions: int,
) -> bool:
    if not current_preemptible or current_in_atomic_tool or recent_interruptions >= 2:
        return False
    return incoming_score >= current_score * 1.5 and incoming_score >= current_score + 2.0


def lock_schedule_key(conn: Database, key: str) -> None:
    """Serialize claims for a logical employee, capacity pool, or resource."""
    if conn.dialect == "postgres":
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
            (f"agentpulse:{key}",),
        )


def dependency_blockers(conn: Database, task_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT d.*, dependency.workflow_status AS dependency_workflow_status,
        dependency.status AS dependency_legacy_status,
        EXISTS (
          SELECT 1 FROM task_outputs output
          WHERE output.task_id = dependency.id
            AND (COALESCE(dependency.output_type, '') = ''
              OR output.output_type = dependency.output_type)
        ) AS dependency_has_output
        FROM task_dependencies d JOIN tasks dependency ON dependency.id = d.depends_on_task_id
        WHERE d.task_id = ? ORDER BY d.created_at, d.id""",
        (task_id,),
    ).fetchall()
    blockers: list[dict] = []
    for row in rows:
        if row.get("status") in ("satisfied", "waived"):
            continue
        dep_type = row.get("dependency_type") or "hard_output"
        completed = (
            row.get("dependency_workflow_status") == "completed"
            or row.get("dependency_legacy_status") == "已完成"
        )
        if dep_type == "hard_output" and completed and row.get("dependency_has_output"):
            continue
        if dep_type == "review" and completed:
            continue
        if dep_type == "information" and row.get("satisfied_by_id"):
            continue
        blockers.append(dict(row))
    return blockers


def next_waiting_status(blockers: list[dict]) -> tuple[str, str]:
    if not blockers:
        return "ready", ""
    kinds = {row.get("dependency_type") or "hard_output" for row in blockers}
    if "resource" in kinds:
        return "waiting_resource", "resource"
    if "information" in kinds:
        return "waiting_information", "information"
    if "review" in kinds:
        return "waiting_review", "review"
    return "waiting_dependency", "dependency"


def refresh_task_dependency_state(conn: Database, task_id: str) -> str:
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        raise ValueError("task not found")
    if task.get("workflow_status") in ("in_progress", "paused", "completed", "cancelled"):
        return task.get("workflow_status") or "queued"
    workflow_status, waiting_reason = next_waiting_status(dependency_blockers(conn, task_id))
    conn.execute(
        "UPDATE tasks SET workflow_status = ?, waiting_reason = ? WHERE id = ?",
        (workflow_status, waiting_reason, task_id),
    )
    return workflow_status


def agent_foreground_run(conn: Database, agent_id: str) -> dict | None:
    row = conn.execute(
        """SELECT * FROM runs WHERE agent_id = ? AND status IN ('leased','running','pausing')
        ORDER BY COALESCE(started_at, created_at), id LIMIT 1""",
        (agent_id,),
    ).fetchone()
    return dict(row) if row else None


def sync_agent_work_state(conn: Database, agent_id: str, *, now_iso: str) -> dict:
    agent = conn.execute("SELECT workspace_id FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if agent is None:
        raise ValueError("agent not found")
    foreground = agent_foreground_run(conn, agent_id)
    pending_inbox = conn.execute(
        f"""SELECT COUNT(*) AS count FROM {WORK_REQUEST_TABLE}
        WHERE target_agent_id = ? AND status IN ('delivered','acknowledged','evaluating','needs_info')""",
        (agent_id,),
    ).fetchone()["count"]
    queue_depth = conn.execute(
        """SELECT COUNT(*) AS count FROM tasks WHERE owner_agent_id = ?
        AND workflow_status IN ('queued','ready','paused','waiting_dependency',
          'waiting_information','waiting_review','waiting_approval','waiting_resource')""",
        (agent_id,),
    ).fetchone()["count"]
    waiting = conn.execute(
        """SELECT workflow_status FROM tasks WHERE owner_agent_id = ?
        AND workflow_status LIKE 'waiting_%' ORDER BY updated_at DESC LIMIT 1""",
        (agent_id,),
    ).fetchone()
    if foreground:
        activity = (
            "triaging"
            if foreground.get("run_kind") in {"triage", "coordination"}
            else "focused"
        )
    elif int(pending_inbox):
        activity = "triaging"
    elif waiting:
        activity = "waiting"
    else:
        activity = "available"
    conn.execute(
        """INSERT INTO agent_work_states (
          agent_id, workspace_id, presence, activity, current_task_id, current_run_id,
          pending_request_count, queue_depth, state_since, updated_at
        ) VALUES (?, ?, 'online', ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (agent_id) DO UPDATE SET activity = excluded.activity,
          current_task_id = excluded.current_task_id,
          current_run_id = excluded.current_run_id,
          pending_request_count = excluded.pending_request_count,
          queue_depth = excluded.queue_depth,
          state_since = CASE WHEN agent_work_states.activity = excluded.activity
            THEN agent_work_states.state_since ELSE excluded.state_since END,
          updated_at = excluded.updated_at""",
        (
            agent_id,
            agent["workspace_id"],
            activity,
            foreground.get("task_id") if foreground else None,
            foreground.get("id") if foreground else None,
            int(pending_inbox),
            int(queue_depth),
            now_iso,
            now_iso,
        ),
    )
    return dict(
        conn.execute("SELECT * FROM agent_work_states WHERE agent_id = ?", (agent_id,)).fetchone()
    )


def select_run_claim_candidates(
    conn: Database,
    *,
    execution_target: str,
    capacity: int,
    now_iso: str,
) -> list[dict]:
    """Select ordered foreground work without assigning execution resources.

    PostgreSQL locks candidate Run rows with SKIP LOCKED so multiple scheduler
    processes can compete safely. SQLite serializes the surrounding write
    transaction and uses the same deterministic ordering for tests.
    """
    if capacity <= 0:
        return []
    sql = """SELECT r.*, t.priority_score, t.story_points, t.business_value,
      t.urgency, t.unblock_score, t.risk_reduction, t.switching_cost,
      t.created_at AS task_created_at, s.updated_at AS agent_last_scheduled_at
    FROM runs r LEFT JOIN tasks t ON t.id = r.task_id
    LEFT JOIN agent_work_states s ON s.agent_id = r.agent_id
    WHERE r.execution_target = ? AND r.status = 'queued'
      AND (r.lease_expires_at IS NULL OR r.lease_expires_at < ?)
      AND NOT EXISTS (
        SELECT 1 FROM runs active WHERE active.agent_id = r.agent_id
          AND active.id <> r.id AND active.status IN ('leased','running','pausing')
      )
    ORDER BY r.created_at, r.id LIMIT ?"""
    if conn.dialect == "postgres":
        sql += " FOR UPDATE OF r SKIP LOCKED"
    # A single employee may own a deep queue. Scan far enough to find other
    # employees instead of letting the first employee's tasks crowd them out.
    rows = conn.execute(sql, (execution_target, now_iso, max(capacity * 100, 1000))).fetchall()

    def sort_key(row: dict) -> tuple:
        if row.get("run_kind") in {"triage", "coordination"}:
            return (
                0,
                0.0,
                row.get("agent_last_scheduled_at") or "",
                row["created_at"],
                row["id"],
            )
        if row.get("task_id"):
            dynamic_score = calculate_priority_score(
                PriorityInputs(
                    story_points=int(row.get("story_points") or 3),
                    business_value=int(row.get("business_value") or 3),
                    urgency=int(row.get("urgency") or 2),
                    unblock_score=int(row.get("unblock_score") or 0),
                    risk_reduction=int(row.get("risk_reduction") or 0),
                    age_bonus=age_bonus(row.get("task_created_at") or row["created_at"]),
                    switching_cost=float(row.get("switching_cost") or 0),
                )
            )
            return (
                1,
                -dynamic_score,
                row.get("agent_last_scheduled_at") or "",
                row["created_at"],
                row["id"],
            )
        return (
            2,
            0.0,
            row.get("agent_last_scheduled_at") or "",
            row["created_at"],
            row["id"],
        )

    selected: list[dict] = []
    selected_agents: set[str] = set()
    for row in sorted((dict(row) for row in rows), key=sort_key):
        if row["agent_id"] in selected_agents:
            continue
        selected.append(row)
        selected_agents.add(row["agent_id"])
        if len(selected) >= capacity:
            break
    return selected


def select_work_request_triage_candidates(
    conn: Database, *, limit: int = 100
) -> list[dict]:
    rows = conn.execute(
        f"""SELECT w.*, a.workspace_id AS agent_workspace_id
        FROM {WORK_REQUEST_TABLE} w JOIN agents a ON a.id = w.target_agent_id
        WHERE w.status IN ('delivered','acknowledged')
          AND NOT EXISTS (
            SELECT 1 FROM runs active WHERE active.agent_id = w.target_agent_id
              AND active.status IN ('leased','running','pausing')
          )
          AND NOT EXISTS (
            SELECT 1 FROM runs triage WHERE triage.id = w.triage_run_id
              AND triage.status NOT IN ('failed','cancelled','timed_out')
          )
        ORDER BY w.created_at, w.id LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def select_coordination_cases(
    conn: Database, *, limit: int = 100
) -> list[dict]:
    rows = conn.execute(
        f"""SELECT c.*, w.conversation_id FROM coordination_cases c
        JOIN {WORK_REQUEST_TABLE} w ON w.id = c.work_request_id
        WHERE c.status = 'queued' AND c.run_id IS NULL
        ORDER BY c.created_at, c.id LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def schedule_work_request_triage(
    conn: Database,
    *,
    resolve_profile: Callable[[Database, str], str | None],
    create_special_run: Callable[[Database, dict, str, str], str],
    timestamp: str,
) -> None:
    for request in select_work_request_triage_candidates(conn):
        profile = resolve_profile(conn, request["target_agent_id"])
        if not profile:
            conn.execute(
                """UPDATE agent_work_states SET presence = 'degraded',
                activity = 'degraded', updated_at = ? WHERE agent_id = ?""",
                (timestamp, request["target_agent_id"]),
            )
            continue
        run_id = create_special_run(conn, request, profile, "triage")
        conn.execute(
            f"""UPDATE {WORK_REQUEST_TABLE} SET status = 'evaluating', triage_run_id = ?,
            acknowledged_at = COALESCE(acknowledged_at, ?), updated_at = ?
            WHERE id = ? AND status IN ('delivered','acknowledged')""",
            (run_id, timestamp, timestamp, request["id"]),
        )
        sync_agent_work_state(
            conn, request["target_agent_id"], now_iso=timestamp
        )


def schedule_coordination_work(
    conn: Database,
    *,
    resolve_profile: Callable[[Database, str], str | None],
    create_special_run: Callable[[Database, dict, str, str], str],
    timestamp: str,
) -> None:
    for case in select_coordination_cases(conn):
        profile = resolve_profile(conn, case["coordinator_agent_id"])
        if not profile:
            conn.execute(
                """UPDATE coordination_cases SET status = 'needs_goal',
                decision_reason = '独立协调员工 Hermes profile 未就绪', updated_at = ?
                WHERE id = ?""",
                (timestamp, case["id"]),
            )
            continue
        run_id = create_special_run(conn, case, profile, "coordination")
        conn.execute(
            """UPDATE coordination_cases SET status = 'evaluating', run_id = ?,
            updated_at = ? WHERE id = ? AND status = 'queued'""",
            (run_id, timestamp, case["id"]),
        )
        sync_agent_work_state(
            conn, case["coordinator_agent_id"], now_iso=timestamp
        )


def schedule_ready_tasks(
    conn: Database,
    *,
    resolve_profile: Callable[[Database, str], str | None],
    enqueue_run: Callable[[Database, dict, str, int], None],
    block_unready: Callable[[Database, dict, str], None],
    timestamp: str,
    plan_id: str | None = None,
) -> None:
    params: tuple = () if plan_id is None else (plan_id,)
    filter_sql = "" if plan_id is None else "AND t.task_plan_id = ?"
    tasks = conn.execute(
        f"""SELECT t.* FROM tasks t JOIN task_plans p ON p.id = t.task_plan_id
        WHERE t.status = '待执行' AND t.workflow_status IN (
          'queued','ready','waiting_dependency','waiting_information','waiting_resource'
        )
        AND t.plan_item_key <> '__root__'
        AND p.status IN ('active','blocked') {filter_sql}
        ORDER BY t.priority_score DESC, t.created_at, t.id""",
        params,
    ).fetchall()
    for task in tasks:
        active = conn.execute(
            """SELECT id FROM runs WHERE task_id = ? AND status IN (
              'queued','leased','running','pausing','paused','waiting_user',
              'waiting_clarify','waiting_approval','waiting_information','waiting_colleague'
            ) LIMIT 1""",
            (task["id"],),
        ).fetchone()
        if active or refresh_task_dependency_state(conn, task["id"]) != "ready":
            continue
        profile = resolve_profile(conn, task["owner_agent_id"])
        if not profile:
            block_unready(conn, task, "task owner is not ready")
            continue
        latest = conn.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) AS attempt FROM runs WHERE task_id = ?",
            (task["id"],),
        ).fetchone()
        enqueue_run(conn, task, profile, int(latest["attempt"]) + 1)
        conn.execute(
            """UPDATE tasks SET workflow_status = 'ready', waiting_reason = '',
            updated_at = ? WHERE id = ?""",
            (timestamp, task["id"]),
        )


def claim_server_runs(
    conn: Database,
    *,
    worker_id: str,
    configured_slots: int,
    lease_seconds: int,
    timestamp: str,
    lease_expires_at: str,
    acquire_resources: Callable[..., list[dict] | None],
    release_resources: Callable[..., None],
) -> list[str]:
    lock_schedule_key(conn, "server-capacity")
    occupied = conn.execute(
        """SELECT COUNT(*) AS count FROM runs
        WHERE execution_target = 'server' AND status IN ('leased','running','pausing')"""
    ).fetchone()["count"]
    slots = max(0, configured_slots - int(occupied))
    candidates = select_run_claim_candidates(
        conn,
        execution_target="server",
        capacity=slots,
        now_iso=timestamp,
    )
    claimed: list[str] = []
    for candidate in candidates:
        lock_schedule_key(conn, f"agent:{candidate['agent_id']}")
        resources = []
        if candidate.get("task_id"):
            resources = acquire_resources(
                conn,
                workspace_id=candidate["workspace_id"],
                task_id=candidate["task_id"],
                run_id=candidate["id"],
                agent_id=candidate["agent_id"],
                lease_owner=worker_id,
                ttl_seconds=lease_seconds,
            )
            if resources is None:
                continue
        conn.execute(
            """UPDATE runs SET status = 'leased', runtime_status = 'leased',
            lease_owner = ?, lease_expires_at = ?
            WHERE id = ? AND status = 'queued'
              AND NOT EXISTS (
                SELECT 1 FROM runs active WHERE active.agent_id = runs.agent_id
                  AND active.id <> runs.id
                  AND active.status IN ('leased','running','pausing')
              )""",
            (worker_id, lease_expires_at, candidate["id"]),
        )
        row = conn.execute(
            "SELECT status, lease_owner FROM runs WHERE id = ?", (candidate["id"],)
        ).fetchone()
        if row and row["status"] == "leased" and row["lease_owner"] == worker_id:
            claimed.append(candidate["id"])
            sync_agent_work_state(
                conn, candidate["agent_id"], now_iso=timestamp
            )
        elif resources:
            release_resources(conn, run_id=candidate["id"])
    return claimed


def refresh_plan_state(conn: Database, *, plan_id: str, timestamp: str) -> None:
    plan = conn.execute("SELECT * FROM task_plans WHERE id = ?", (plan_id,)).fetchone()
    if plan is None:
        return
    children = conn.execute(
        """SELECT status, progress FROM tasks
        WHERE task_plan_id = ? AND plan_item_key <> '__root__'""",
        (plan_id,),
    ).fetchall()
    if not children:
        return
    progress = sum(int(row["progress"]) for row in children) // len(children)
    all_done = all(row["status"] == "已完成" for row in children)
    blocked = any(row["status"] == "阻塞" for row in children)
    conn.execute(
        "UPDATE tasks SET status = ?, progress = ?, updated_at = ? WHERE id = ?",
        (
            "已完成" if all_done else "进行中",
            100 if all_done else progress,
            timestamp,
            plan["root_task_id"],
        ),
    )
    if all_done:
        conn.execute(
            """UPDATE task_plans SET status = 'completed', workflow_status = 'completed',
            completed_at = ?, updated_at = ?, blocked_reason = '' WHERE id = ?""",
            (timestamp, timestamp, plan_id),
        )
    elif blocked:
        conn.execute(
            """UPDATE task_plans SET status = 'blocked', workflow_status = 'degraded',
            updated_at = ? WHERE id = ?""",
            (timestamp, plan_id),
        )
    else:
        conn.execute(
            """UPDATE task_plans SET status = 'active', workflow_status = 'active',
            blocked_reason = '', updated_at = ? WHERE id = ?""",
            (timestamp, plan_id),
        )
