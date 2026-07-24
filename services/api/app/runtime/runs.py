"""Run / RunStep data model + lifecycle state machine (TD-03-T1).

This is the persistence + state-transition foundation that TD-03-T2/T3 build on:
- ``HermesBackend`` (T2) produces the SSE-derived events.
- ``RunService`` (T3) consumes them, calling ``append_run_step`` and
  ``transition_run`` here, and enforces the "every Run belongs to a Task" +
  absolute-workdir invariants that this layer leaves nullable at the schema level.

Only the state machine and CRUD helpers live here — no Hermes/HTTP. See
[TD-03](../../../docs/tech-design/TD-03-hermes-execution.md) and
[DATA-MODEL §5](../../../docs/tech-design/DATA-MODEL-AND-API.md).
"""

from __future__ import annotations

import json

from app.core.database import Database
from app.services.workspace import new_id, now_iso


class RunStatus:
    """Run lifecycle states (DATA-MODEL §5.1)."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_USER = "waiting_user"       # high-risk action pending owner approval
    WAITING_CLARIFY = "waiting_clarify"  # agent paused to ask for missing context
    COMPLETED = "completed"
    FAILED = "failed"

    ALL = (QUEUED, RUNNING, WAITING_USER, WAITING_CLARIFY, COMPLETED, FAILED)
    TERMINAL = (COMPLETED, FAILED)


# Allowed transitions. Anything not listed here is rejected by transition_run so
# an out-of-order Hermes event stream can't silently corrupt run state.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.FAILED},
    RunStatus.RUNNING: {
        RunStatus.WAITING_USER,
        RunStatus.WAITING_CLARIFY,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    },
    # Resume after the owner approves / answers, or fail (rejected / stopped / timeout).
    RunStatus.WAITING_USER: {RunStatus.RUNNING, RunStatus.COMPLETED, RunStatus.FAILED},
    RunStatus.WAITING_CLARIFY: {RunStatus.RUNNING, RunStatus.COMPLETED, RunStatus.FAILED},
    RunStatus.COMPLETED: set(),
    RunStatus.FAILED: set(),
}


class RunStepType:
    """run_steps.type values — mirrors the Hermes SSE event taxonomy (§5.2)."""

    MESSAGE = "message"
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    APPROVAL_REQUIRED = "approval_required"
    STATUS = "status"
    FINAL = "final"

    ALL = (
        MESSAGE,
        THINKING,
        TOOL_CALL,
        TOOL_RESULT,
        APPROVAL_REQUIRED,
        STATUS,
        FINAL,
    )


class RunStateError(ValueError):
    """Raised on an illegal run status transition."""


def create_run(
    conn: Database,
    *,
    workspace_id: str,
    conversation_id: str,
    agent_id: str,
    input_message_id: str | None,
    task_id: str | None = None,
    hermes_profile_id: str | None = None,
    hermes_run_id: str | None = None,
    workdir: str | None = None,
    provider: str = "hermes",
    model: str = "",
    status: str = RunStatus.QUEUED,
    attempt_no: int = 1,
    lease_owner: str | None = None,
    lease_expires_at: str | None = None,
    started_at: str | None = None,
    execution_target: str = "server",
    device_id: str | None = None,
    local_project_id: str | None = None,
    runtime_status: str = "server",
    prompt_text: str = "",
) -> str:
    """Insert a new run row and return its id."""
    if status not in RunStatus.ALL:
        raise RunStateError(f"invalid initial run status: {status}")
    run_id = new_id("run")
    sql = """
        INSERT INTO runs (
          id, workspace_id, conversation_id, agent_id, task_id, status,
          input_message_id, output_message_id, hermes_profile_id, hermes_run_id,
          workdir, provider, model, usage_json, error, attempt_no, lease_owner,
          lease_expires_at, started_at, created_at, completed_at,
          execution_target, device_id, local_project_id, runtime_status, prompt_text
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, '{}', '', ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
        """
    params = (
            run_id,
            workspace_id,
            conversation_id,
            agent_id,
            task_id,
            status,
            input_message_id,
            hermes_profile_id,
            hermes_run_id,
            workdir,
            provider,
            model,
            attempt_no,
            lease_owner,
            lease_expires_at,
            started_at,
            now_iso(),
            execution_target,
            device_id,
            local_project_id,
            runtime_status,
            prompt_text,
        )
    try:
        conn.execute(sql, params)
    except Exception as exc:
        # Isolated embedders and lifecycle tests may still construct the
        # pre-TD-14 minimal runs table. Production startup migration adds the
        # columns; preserve the old create_run contract for those schemas.
        if conn.dialect != "sqlite" or "no column named execution_target" not in str(exc):
            raise
        conn.execute(
            """
            INSERT INTO runs (
              id, workspace_id, conversation_id, agent_id, task_id, status,
              input_message_id, output_message_id, hermes_profile_id, hermes_run_id,
              workdir, provider, model, usage_json, error, attempt_no, lease_owner,
              lease_expires_at, started_at, created_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, '{}', '', ?, ?, ?, ?, ?, NULL)
            """,
            params[:17],
        )
    return run_id


def get_run(conn: Database, run_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def transition_run(
    conn: Database,
    run_id: str,
    to_status: str,
    *,
    error: str | None = None,
    output_message_id: str | None = None,
    hermes_run_id: str | None = None,
) -> dict:
    """Move a run to ``to_status`` if the transition is legal.

    Raises RunStateError on an unknown run or an illegal transition. Terminal
    states (completed/failed) stamp ``completed_at``.
    """
    run = get_run(conn, run_id)
    if run is None:
        raise RunStateError(f"run not found: {run_id}")
    current = run["status"]
    if to_status not in RunStatus.ALL:
        raise RunStateError(f"invalid run status: {to_status}")
    if to_status == current:
        return run
    if to_status not in ALLOWED_TRANSITIONS.get(current, set()):
        raise RunStateError(f"illegal run transition: {current} -> {to_status}")

    completed_at = now_iso() if to_status in RunStatus.TERMINAL else None
    conn.execute(
        """
        UPDATE runs SET
          status = ?,
          error = COALESCE(?, error),
          output_message_id = COALESCE(?, output_message_id),
          hermes_run_id = COALESCE(?, hermes_run_id),
          completed_at = CASE WHEN ? IS NULL THEN completed_at ELSE ? END
        WHERE id = ?
        """,
        (
            to_status,
            error,
            output_message_id,
            hermes_run_id,
            completed_at,
            completed_at,
            run_id,
        ),
    )
    return get_run(conn, run_id)  # type: ignore[return-value]


def append_run_step(
    conn: Database,
    *,
    run_id: str,
    type: str,
    status: str = "",
    title: str = "",
    detail: str = "",
    payload: dict | None = None,
) -> str:
    """Append a run_step row and return its id."""
    if type not in RunStepType.ALL:
        raise RunStateError(f"invalid run step type: {type}")
    step_id = new_id("step")
    conn.execute(
        """
        INSERT INTO run_steps (
          id, run_id, type, status, title, detail, payload_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            step_id,
            run_id,
            type,
            status,
            title,
            detail,
            json.dumps(payload or {}, ensure_ascii=False),
            now_iso(),
        ),
    )
    return step_id


def list_run_steps(
    conn: Database, run_id: str, *, after_step_id: str | None = None
) -> list[dict]:
    """Return run steps in creation order, optionally only those after a step id.

    ``after_step_id`` supports the incremental polling contract in TD-03 (the
    desktop task detail pane pulls only new steps).
    """
    rows = conn.execute(
        "SELECT * FROM run_steps WHERE run_id = ? ORDER BY created_at, id",
        (run_id,),
    ).fetchall()
    steps = [serialize_run_step(row) for row in rows]
    if after_step_id is None:
        return steps
    for index, step in enumerate(steps):
        if step["id"] == after_step_id:
            return steps[index + 1 :]
    return steps


def serialize_run_step(row: dict) -> dict:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "type": row["type"],
        "status": row["status"],
        "title": row["title"],
        "detail": row["detail"],
        "payload": json.loads(row["payload_json"] or "{}"),
        "created_at": row["created_at"],
    }
