"""TD-15 workforce scheduler and durable work requests.

Revision ID: 15a100c0f001
Revises: 99f23f8da74b
Create Date: 2026-07-26
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "15a100c0f001"
down_revision: str | Sequence[str] | None = "99f23f8da74b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _add_columns(table: str, columns: list[sa.Column]) -> None:
    existing = _column_names(table)
    for column in columns:
        if column.name not in existing:
            op.add_column(table, column)


def upgrade() -> None:
    tables = _table_names()
    if "work_requests" not in tables:
        op.create_table(
            "work_requests",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
            sa.Column("conversation_id", sa.Text(), sa.ForeignKey("conversations.id", ondelete="SET NULL")),
            sa.Column("source_message_id", sa.Text(), sa.ForeignKey("messages.id", ondelete="SET NULL")),
            sa.Column("requester_type", sa.Text(), nullable=False),
            sa.Column("requester_id", sa.Text(), nullable=False, server_default=""),
            sa.Column("target_agent_id", sa.Text(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source_task_id", sa.Text(), sa.ForeignKey("tasks.id", ondelete="SET NULL")),
            sa.Column("consensus_brief_id", sa.Text(), sa.ForeignKey("consensus_briefs.id", ondelete="SET NULL")),
            sa.Column("status", sa.Text(), nullable=False, server_default="delivered"),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("response_content", sa.Text(), nullable=False, server_default=""),
            sa.Column("decision_reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("story_points", sa.Integer()),
            sa.Column("business_value", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("urgency", sa.Integer(), nullable=False, server_default="2"),
            sa.Column("unblock_score", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("risk_reduction", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("switching_cost", sa.Float(), nullable=False, server_default="0"),
            sa.Column("priority_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("converted_task_id", sa.Text(), sa.ForeignKey("tasks.id", ondelete="SET NULL")),
            sa.Column("triage_run_id", sa.Text(), sa.ForeignKey("runs.id", ondelete="SET NULL")),
            sa.Column("preempts_task_id", sa.Text(), sa.ForeignKey("tasks.id", ondelete="SET NULL")),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("acknowledged_at", sa.Text()),
            sa.Column("decided_at", sa.Text()),
            sa.Column("updated_at", sa.Text(), nullable=False),
            sa.CheckConstraint("requester_type IN ('user','agent','system')", name="ck_work_request_requester"),
            sa.CheckConstraint(
                "status IN ('delivered','acknowledged','evaluating','answered','accepted','deferred','rejected','needs_info','withdrawn')",
                name="ck_work_request_status",
            ),
            sa.CheckConstraint("story_points IS NULL OR story_points IN (1,2,3,5,8,13)", name="ck_work_request_story_points"),
        )

    if "priority_assessments" not in tables:
        op.create_table(
            "priority_assessments",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
            sa.Column("work_request_id", sa.Text(), sa.ForeignKey("work_requests.id", ondelete="CASCADE")),
            sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.id", ondelete="CASCADE")),
            sa.Column("agent_id", sa.Text(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
            sa.Column("story_points", sa.Integer(), nullable=False),
            sa.Column("business_value", sa.Integer(), nullable=False),
            sa.Column("urgency", sa.Integer(), nullable=False),
            sa.Column("unblock_score", sa.Integer(), nullable=False),
            sa.Column("risk_reduction", sa.Integer(), nullable=False),
            sa.Column("age_bonus", sa.Float(), nullable=False, server_default="0"),
            sa.Column("switching_cost", sa.Float(), nullable=False, server_default="0"),
            sa.Column("priority_score", sa.Float(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.CheckConstraint("work_request_id IS NOT NULL OR task_id IS NOT NULL", name="ck_priority_subject"),
        )

    if "agent_work_states" not in tables:
        op.create_table(
            "agent_work_states",
            sa.Column("agent_id", sa.Text(), sa.ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("workspace_id", sa.Text(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
            sa.Column("presence", sa.Text(), nullable=False, server_default="online"),
            sa.Column("activity", sa.Text(), nullable=False, server_default="available"),
            sa.Column("current_task_id", sa.Text(), sa.ForeignKey("tasks.id", ondelete="SET NULL")),
            sa.Column("current_run_id", sa.Text(), sa.ForeignKey("runs.id", ondelete="SET NULL")),
            sa.Column("pending_request_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("queue_depth", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("interruption_count_window", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("interruption_window_started_at", sa.Text()),
            sa.Column("state_since", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.Text(), nullable=False),
            sa.CheckConstraint("presence IN ('offline','online','degraded')", name="ck_agent_presence"),
            sa.CheckConstraint(
                "activity IN ('available','triaging','focused','waiting','blocked','degraded')",
                name="ck_agent_activity",
            ),
        )

    if "resource_leases" not in tables:
        op.create_table(
            "resource_leases",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
            sa.Column("resource_type", sa.Text(), nullable=False),
            sa.Column("resource_key", sa.Text(), nullable=False),
            sa.Column("mode", sa.Text(), nullable=False, server_default="exclusive"),
            sa.Column("owner_agent_id", sa.Text(), sa.ForeignKey("agents.id", ondelete="SET NULL")),
            sa.Column("run_id", sa.Text(), sa.ForeignKey("runs.id", ondelete="CASCADE")),
            sa.Column("lease_owner", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False, server_default="active"),
            sa.Column("expires_at", sa.Text(), nullable=False),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("released_at", sa.Text()),
        )

    if "task_resource_requirements" not in tables:
        op.create_table(
            "task_resource_requirements",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
            sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("resource_type", sa.Text(), nullable=False),
            sa.Column("resource_key", sa.Text(), nullable=False),
            sa.Column("mode", sa.Text(), nullable=False, server_default="exclusive"),
            sa.Column("units", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.UniqueConstraint("task_id", "resource_type", "resource_key", name="ux_task_resource_requirement"),
        )

    if "task_reviews" not in tables:
        op.create_table(
            "task_reviews",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
            sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("reviewer_agent_id", sa.Text(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
            sa.Column("review_task_id", sa.Text(), sa.ForeignKey("tasks.id", ondelete="SET NULL")),
            sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
            sa.Column("instructions", sa.Text(), nullable=False, server_default=""),
            sa.Column("decision_reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("decided_at", sa.Text()),
        )

    if "coordination_cases" not in tables:
        op.create_table(
            "coordination_cases",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
            sa.Column("work_request_id", sa.Text(), sa.ForeignKey("work_requests.id", ondelete="CASCADE"), nullable=False),
            sa.Column("raised_by_type", sa.Text(), nullable=False),
            sa.Column("raised_by_id", sa.Text(), nullable=False, server_default=""),
            sa.Column("coordinator_agent_id", sa.Text(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
            sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("evidence_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("run_id", sa.Text(), sa.ForeignKey("runs.id", ondelete="SET NULL")),
            sa.Column("decision", sa.Text(), nullable=False, server_default=""),
            sa.Column("decision_reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.Text(), nullable=False),
            sa.Column("resolved_at", sa.Text()),
            sa.CheckConstraint("raised_by_type IN ('user','agent','system')", name="ck_coordination_raiser"),
            sa.CheckConstraint(
                "status IN ('queued','evaluating','resolved','needs_goal','cancelled')",
                name="ck_coordination_status",
            ),
        )

    _add_columns(
        "tasks",
        [
            sa.Column("workflow_status", sa.Text(), nullable=False, server_default=""),
            sa.Column("waiting_reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("story_points", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("business_value", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("urgency", sa.Integer(), nullable=False, server_default="2"),
            sa.Column("unblock_score", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("risk_reduction", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("switching_cost", sa.Float(), nullable=False, server_default="0"),
            sa.Column("priority_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("preemptible", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("preemption_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("preempted_task_id", sa.Text()),
            sa.Column("checkpoint_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("review_required", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("risk_level", sa.Text(), nullable=False, server_default="low"),
        ],
    )
    _add_columns(
        "task_dependencies",
        [
            sa.Column("dependency_type", sa.Text(), nullable=False, server_default="hard_output"),
            sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
            sa.Column("satisfied_by_type", sa.Text(), nullable=False, server_default=""),
            sa.Column("satisfied_by_id", sa.Text()),
            sa.Column("satisfied_at", sa.Text()),
        ],
    )
    _add_columns(
        "task_plans",
        [sa.Column("workflow_status", sa.Text(), nullable=False, server_default="")],
    )
    _add_columns(
        "runs",
        [
            sa.Column("run_kind", sa.Text(), nullable=False, server_default=""),
            sa.Column("hermes_session_id", sa.Text()),
            sa.Column("pause_requested_at", sa.Text()),
            sa.Column("pause_reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("checkpoint_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("resource_requirements_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("preempted_by_request_id", sa.Text()),
            sa.Column("resume_of_run_id", sa.Text()),
        ],
    )

    op.execute(
        """UPDATE tasks SET workflow_status = CASE status
        WHEN '待认领' THEN 'queued' WHEN '待执行' THEN 'ready'
        WHEN '进行中' THEN 'in_progress' WHEN '待确认' THEN 'waiting_review'
        WHEN '阻塞' THEN 'waiting_information' WHEN '已完成' THEN 'completed'
        ELSE 'queued' END WHERE workflow_status = ''"""
    )
    op.execute(
        """UPDATE task_plans SET workflow_status = CASE status
        WHEN 'launching' THEN 'launching' WHEN 'active' THEN 'active'
        WHEN 'blocked' THEN 'degraded' WHEN 'completed' THEN 'completed'
        WHEN 'cancelled' THEN 'cancelled' ELSE 'active' END
        WHERE workflow_status = ''"""
    )
    op.execute(
        """UPDATE runs SET run_kind = CASE WHEN task_id IS NULL THEN 'chat' ELSE 'task' END
        WHERE run_kind = ''"""
    )
    op.execute("UPDATE runs SET runtime_status = status WHERE runtime_status <> status")
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(
            """INSERT OR IGNORE INTO agent_work_states (
              agent_id, workspace_id, presence, activity, state_since, updated_at
            ) SELECT id, workspace_id, 'online', 'available', created_at, created_at FROM agents"""
        )
    else:
        op.execute(
            """INSERT INTO agent_work_states (
              agent_id, workspace_id, presence, activity, state_since, updated_at
            ) SELECT id, workspace_id, 'online', 'available', created_at, created_at FROM agents
            ON CONFLICT (agent_id) DO NOTHING"""
        )

    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("work_requests")}
    if "idx_work_requests_agent_status" not in indexes:
        op.create_index(
            "idx_work_requests_agent_status",
            "work_requests",
            ["target_agent_id", "status", "created_at"],
        )
    resource_indexes = {index["name"] for index in sa.inspect(bind).get_indexes("resource_leases")}
    if "idx_resource_leases_lookup" not in resource_indexes:
        op.create_index(
            "idx_resource_leases_lookup",
            "resource_leases",
            ["workspace_id", "resource_type", "resource_key", "status", "expires_at"],
        )
    coordination_indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("coordination_cases")
    }
    if "idx_coordination_cases_status" not in coordination_indexes:
        op.create_index(
            "idx_coordination_cases_status",
            "coordination_cases",
            ["workspace_id", "status", "created_at"],
        )


def downgrade() -> None:
    # Forward-only by design: dropping scheduler tables would delete queue,
    # decisions and checkpoints, violating AgentPulse's audit/data-retention contract.
    pass
