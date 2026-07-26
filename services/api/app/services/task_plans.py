"""Durable task-plan creation and read APIs for TD-11."""

from __future__ import annotations

import json
import os

from app.core.config import settings
from app.core.database import Database
from app.orchestration.brief import BriefStatus, confirm_brief, validate_work_items
from app.runtime.runner import resolve_hermes_profile
from app.runtime.runs import RunStatus, create_run, list_run_steps
from app.services.workspace import (
    add_message,
    create_task,
    new_id,
    now_iso,
    serialize_approval,
)
from app.services.business_actions import list_actions


class TaskPlanError(ValueError):
    pass


def _legacy_work_items(brief: dict) -> list[dict]:
    owner = brief.get("owner_agent_id")
    if not owner:
        raise TaskPlanError("legacy confirmed brief has no owner and cannot be launched")
    return [
        {
            "key": "research",
            "title": "补齐内容依据",
            "description": "检索公司资料与必要的公开信息，整理可引用的事实和未知项。",
            "owner_agent_id": owner,
            "expected_output": "带资料 ID 或 URL 的研究摘要",
            "output_type": "markdown",
            "depends_on_keys": [],
            "final_delivery": False,
        },
        {
            "key": "draft",
            "title": "形成内容草稿",
            "description": "依据研究摘要完成内容草稿和排期建议。",
            "owner_agent_id": owner,
            "expected_output": "可审阅的内容草稿",
            "output_type": "markdown",
            "depends_on_keys": ["research"],
            "final_delivery": False,
        },
        {
            "key": "package",
            "title": "组装待发布内容包",
            "description": "把草稿整理为结构化待发布内容包，不执行真实发布。",
            "owner_agent_id": owner,
            "expected_output": "合法的 content_package_v1",
            "output_type": "content_package_v1",
            "depends_on_keys": ["draft"],
            "final_delivery": True,
        },
    ]


def _brief_work_items(brief: dict, allowed_agent_ids: set[str]) -> list[dict]:
    raw = json.loads(brief.get("work_items_json") or "[]")
    if not raw and brief["status"] == BriefStatus.CONFIRMED:
        raw = _legacy_work_items(brief)
    try:
        return validate_work_items(raw, allowed_agent_ids=allowed_agent_ids)
    except ValueError as exc:
        raise TaskPlanError(str(exc)) from exc


def _ready_profiles(conn: Database, owner_ids: set[str]) -> dict[str, str]:
    if not owner_ids:
        return {}
    missing: list[str] = []
    profiles: dict[str, str] = {}
    for agent_id in owner_ids:
        profile = resolve_hermes_profile(conn, agent_id)
        if not profile:
            row = conn.execute(
                "SELECT status FROM agent_specs WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            agent = conn.execute(
                "SELECT name FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            name = agent["name"] if agent else agent_id
            status = row["status"] if row else "not_provisioned"
            missing.append(f"{name} ({status})")
        else:
            profiles[agent_id] = profile
    if missing:
        raise TaskPlanError("employees are not ready: " + ", ".join(missing))
    return profiles


def _workdir(profile: str, run_id: str) -> str:
    root = os.path.abspath(settings.hermes_work_root or ".hermes-data")
    return os.path.join(root, profile, "work", "runs", run_id)


def enqueue_task_run(
    conn: Database,
    *,
    task: dict,
    profile: str,
    attempt_no: int,
    input_message_id: str | None = None,
) -> str:
    run_id = create_run(
        conn,
        workspace_id=task["workspace_id"],
        conversation_id=task["conversation_id"],
        agent_id=task["owner_agent_id"],
        task_id=task["id"],
        input_message_id=input_message_id,
        hermes_profile_id=profile,
        workdir="",
        status=RunStatus.QUEUED,
        attempt_no=attempt_no,
    )
    conn.execute("UPDATE runs SET workdir = ? WHERE id = ?", (_workdir(profile, run_id), run_id))
    return run_id


def launch_brief(
    conn: Database,
    *,
    workspace_id: str,
    brief_id: str,
    confirmed_by_user_id: str,
) -> dict:
    """Atomically confirm a brief and materialize its durable execution plan."""
    existing = conn.execute(
        "SELECT id FROM task_plans WHERE brief_id = ? AND workspace_id = ?",
        (brief_id, workspace_id),
    ).fetchone()
    if existing:
        return get_task_plan(conn, existing["id"], workspace_id=workspace_id)

    brief = conn.execute(
        "SELECT * FROM consensus_briefs WHERE id = ? AND workspace_id = ?",
        (brief_id, workspace_id),
    ).fetchone()
    if brief is None:
        raise TaskPlanError("brief not found")
    if brief["status"] in (BriefStatus.REJECTED, BriefStatus.SUPERSEDED):
        raise TaskPlanError(f"{brief['status']} brief cannot be launched")
    if brief["status"] not in (BriefStatus.DRAFT, BriefStatus.CONFIRMED):
        raise TaskPlanError(f"brief cannot be launched from status {brief['status']}")

    members = conn.execute(
        "SELECT agent_id FROM conversation_members WHERE conversation_id = ?",
        (brief["discussion_conversation_id"],),
    ).fetchall()
    allowed_agent_ids = {row["agent_id"] for row in members}
    work_items = _brief_work_items(brief, allowed_agent_ids)
    profiles = _ready_profiles(
        conn, {item["owner_agent_id"] for item in work_items}
    )

    user = conn.execute(
        "SELECT id FROM users WHERE id = ?", (confirmed_by_user_id,)
    ).fetchone()
    if user is None:
        raise TaskPlanError("user not found")

    plan_id = new_id("plan")
    timestamp = now_iso()
    conn.execute(
        """INSERT INTO task_plans (
          id, workspace_id, brief_id, root_task_id, status, revision_count,
          blocked_reason, created_at, updated_at, completed_at
        ) VALUES (?, ?, ?, NULL, 'launching', 0, '', ?, ?, NULL)
        ON CONFLICT (brief_id) DO NOTHING""",
        (plan_id, workspace_id, brief_id, timestamp, timestamp),
    )
    claimed = conn.execute(
        "SELECT * FROM task_plans WHERE brief_id = ?", (brief_id,)
    ).fetchone()
    if claimed is None:
        raise TaskPlanError("failed to create task plan")
    if claimed["id"] != plan_id:
        return get_task_plan(conn, claimed["id"], workspace_id=workspace_id)

    if brief["status"] == BriefStatus.DRAFT:
        confirm_brief(
            conn,
            workspace_id=workspace_id,
            brief_id=brief_id,
            confirmed_by_user_id=confirmed_by_user_id,
        )

    final_item = next(item for item in work_items if item["final_delivery"])
    root_task = create_task(
        conn,
        workspace_id=workspace_id,
        title=brief["goal"],
        description="共识 brief 自动执行计划",
        owner_agent_id=brief["owner_agent_id"] or final_item["owner_agent_id"],
        status="进行中",
        progress=0,
        conversation_id=brief["discussion_conversation_id"],
        consensus_brief_id=brief_id,
        task_plan_id=plan_id,
        plan_item_key="__root__",
        expected_output="全部子任务完成并归档",
        output_type="plan_summary",
    )
    task_by_key: dict[str, dict] = {}
    for item in work_items:
        task = create_task(
            conn,
            workspace_id=workspace_id,
            title=item["title"],
            description=item["description"],
            owner_agent_id=item["owner_agent_id"],
            status="待执行",
            progress=0,
            conversation_id=brief["discussion_conversation_id"],
            parent_task_id=root_task["id"],
            consensus_brief_id=brief_id,
            task_plan_id=plan_id,
            plan_item_key=item["key"],
            expected_output=item["expected_output"],
            output_type=item["output_type"],
        )
        task_by_key[item["key"]] = task

    for item in work_items:
        for dependency_key in item["depends_on_keys"]:
            conn.execute(
                """INSERT INTO task_dependencies (
                  id, task_plan_id, task_id, depends_on_task_id, created_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    new_id("dep"),
                    plan_id,
                    task_by_key[item["key"]]["id"],
                    task_by_key[dependency_key]["id"],
                    timestamp,
                ),
            )

    for item in work_items:
        if not item["depends_on_keys"]:
            enqueue_task_run(
                conn,
                task=task_by_key[item["key"]],
                profile=profiles[item["owner_agent_id"]],
                attempt_no=1,
            )

    conn.execute(
        """UPDATE task_plans SET root_task_id = ?, status = 'active',
        workflow_status = 'active', updated_at = ?
        WHERE id = ?""",
        (root_task["id"], now_iso(), plan_id),
    )
    return get_task_plan(conn, plan_id, workspace_id=workspace_id)


def _serialize_output(row: dict) -> dict:
    content: object = row["content"]
    if row["output_type"] == "content_package_v1":
        try:
            content = json.loads(row["content"])
        except (TypeError, json.JSONDecodeError):
            pass
    return {
        "id": row["id"],
        "title": row["title"],
        "output_type": row["output_type"],
        "content": content,
        "created_at": row["created_at"],
    }


def get_task_plan(conn: Database, plan_id: str, *, workspace_id: str) -> dict:
    plan = conn.execute(
        "SELECT * FROM task_plans WHERE id = ? AND workspace_id = ?",
        (plan_id, workspace_id),
    ).fetchone()
    if plan is None:
        raise TaskPlanError("task plan not found")
    tasks = conn.execute(
        """SELECT * FROM tasks WHERE task_plan_id = ?
        ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END, created_at, id""",
        (plan_id, plan["root_task_id"]),
    ).fetchall()
    task_payloads: list[dict] = []
    for task in tasks:
        outputs = conn.execute(
            "SELECT * FROM task_outputs WHERE task_id = ? ORDER BY created_at, id",
            (task["id"],),
        ).fetchall()
        approvals = conn.execute(
            "SELECT * FROM approvals WHERE task_id = ? ORDER BY created_at, id",
            (task["id"],),
        ).fetchall()
        runs = conn.execute(
            """SELECT id, status, attempt_no, error, created_at, started_at,
            completed_at FROM runs WHERE task_id = ? ORDER BY attempt_no""",
            (task["id"],),
        ).fetchall()
        blocked_event = conn.execute(
            """SELECT content FROM task_events WHERE task_id = ? AND kind = 'task_blocked'
            ORDER BY created_at DESC LIMIT 1""",
            (task["id"],),
        ).fetchone()
        serialized_runs = []
        for run in runs:
            run_payload = dict(run)
            from app.services.company_memory import get_run_context_summary

            run_payload.update(
                get_run_context_summary(
                    conn, workspace_id=workspace_id, run_id=run["id"]
                )
            )
            serialized_runs.append(run_payload)
        task_payloads.append(
            {
                "id": task["id"],
                "title": task["title"],
                "description": task["description"],
                "owner_agent_id": task["owner_agent_id"],
                "status": task["status"],
                "progress": task["progress"],
                "parent_task_id": task["parent_task_id"],
                "plan_item_key": task["plan_item_key"],
                "expected_output": task["expected_output"] or "",
                "output_type": task["output_type"] or "markdown",
                "blocked_reason": blocked_event["content"] if blocked_event else "",
                "outputs": [_serialize_output(row) for row in outputs],
                "approvals": [serialize_approval(row) for row in approvals],
                "runs": serialized_runs,
                "business_actions": list_actions(
                    conn, workspace_id=plan["workspace_id"], task_id=task["id"]
                ),
            }
        )
    dependencies = conn.execute(
        """SELECT task_id, depends_on_task_id FROM task_dependencies
        WHERE task_plan_id = ? ORDER BY created_at, id""",
        (plan_id,),
    ).fetchall()
    return {
        "id": plan["id"],
        "workspace_id": plan["workspace_id"],
        "brief_id": plan["brief_id"],
        "root_task_id": plan["root_task_id"],
        "status": plan["status"],
        "revision_count": plan["revision_count"],
        "blocked_reason": plan["blocked_reason"] or "",
        "created_at": plan["created_at"],
        "updated_at": plan["updated_at"],
        "completed_at": plan["completed_at"],
        "tasks": task_payloads,
        "dependencies": [dict(row) for row in dependencies],
    }


def list_task_runs(conn: Database, *, workspace_id: str, task_id: str) -> list[dict]:
    task = conn.execute(
        "SELECT id FROM tasks WHERE id = ? AND workspace_id = ?",
        (task_id, workspace_id),
    ).fetchone()
    if task is None:
        raise TaskPlanError("task not found")
    runs = conn.execute(
        "SELECT * FROM runs WHERE task_id = ? ORDER BY attempt_no, created_at",
        (task_id,),
    ).fetchall()
    result = []
    from app.services.company_memory import get_run_context_summary

    for row in runs:
        context = get_run_context_summary(
            conn, workspace_id=workspace_id, run_id=row["id"]
        )
        result.append(
            {
                "id": row["id"],
                "task_id": row["task_id"],
                "status": row["status"],
                "attempt_no": row["attempt_no"],
                "error": row["error"] or "",
                "lease_owner": row["lease_owner"],
                "lease_expires_at": row["lease_expires_at"],
                "started_at": row["started_at"],
                "created_at": row["created_at"],
                "completed_at": row["completed_at"],
                "steps": list_run_steps(conn, row["id"]),
                "business_actions": list_actions(
                    conn, workspace_id=workspace_id, run_id=row["id"]
                ),
                **context,
            }
        )
    return result


def resume_task(
    conn: Database, *, workspace_id: str, task_id: str, message: str
) -> dict:
    task = conn.execute(
        "SELECT * FROM tasks WHERE id = ? AND workspace_id = ?",
        (task_id, workspace_id),
    ).fetchone()
    if task is None:
        raise TaskPlanError("task not found")
    if not task["task_plan_id"] or task["plan_item_key"] == "__root__":
        raise TaskPlanError("only task-plan work items can be resumed")
    if task["status"] != "阻塞":
        raise TaskPlanError("only a blocked task can be resumed")
    active = conn.execute(
        """SELECT id FROM runs WHERE task_id = ?
        AND status IN ('queued','running','waiting_user','waiting_clarify') LIMIT 1""",
        (task_id,),
    ).fetchone()
    if active:
        raise TaskPlanError("task already has an active run")
    profile = resolve_hermes_profile(conn, task["owner_agent_id"])
    if not profile:
        raise TaskPlanError("task owner is not ready")
    last = conn.execute(
        "SELECT COALESCE(MAX(attempt_no), 0) AS attempt_no FROM runs WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    input_message = add_message(
        conn,
        conversation_id=task["conversation_id"],
        sender_type="user",
        sender_id="",
        content=message.strip(),
    )
    enqueue_task_run(
        conn,
        task=task,
        profile=profile,
        attempt_no=int(last["attempt_no"]) + 1,
        input_message_id=input_message["id"],
    )
    conn.execute(
        """UPDATE tasks SET status = '待执行', workflow_status = 'ready',
        waiting_reason = '', progress = 0, updated_at = ? WHERE id = ?""",
        (now_iso(), task_id),
    )
    if task["task_plan_id"]:
        conn.execute(
            """UPDATE task_plans SET status = 'active', workflow_status = 'active',
            blocked_reason = '', updated_at = ?
            WHERE id = ?""",
            (now_iso(), task["task_plan_id"]),
        )
    return get_task_plan(conn, task["task_plan_id"], workspace_id=workspace_id)
