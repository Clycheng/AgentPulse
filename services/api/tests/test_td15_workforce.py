from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.core.database import connect, init_db
from app.orchestration.brief import confirm_brief, create_brief
from app.orchestration.workforce import (
    PriorityInputs,
    age_bonus,
    calculate_priority_score,
    refresh_task_dependency_state,
    should_preempt,
)
from app.runtime.hermes_client import AgentEvent, RunContext
from app.runtime.runner import start_run
from app.runtime.runs import RunStatus, create_run
from app.runtime.task_scheduler import TaskScheduler
from app.schemas.workforce import LocalRunClaimIn, ResourceClaim
from app.services.workforce import (
    WorkforceError,
    acquire_task_resources,
    add_task_dependency,
    create_work_request,
    decide_task_review,
    decide_work_request,
    get_workforce_overview,
    open_coordination_case,
    release_run_resources,
    request_task_review,
    resume_preempted_task_after,
    satisfy_information_dependency,
)
from app.services import company_tools
from app.services.local_run_claims import claim_local_runs
from app.services.workspace import (
    add_task_output,
    create_agent,
    create_task,
    create_workspace_for_user,
    new_id,
    now_iso,
)


def _company(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'td15.sqlite3'}")
    monkeypatch.setattr(settings, "task_worker_enabled", False)
    init_db()
    conn = connect()
    conn.execute(
        """INSERT INTO users (id, email, password_hash, display_name, created_at)
        VALUES ('owner', 'owner@example.com', 'x', '老板', ?)""",
        (now_iso(),),
    )
    workspace = create_workspace_for_user(conn, "owner", "百人测试公司")
    agents = conn.execute(
        "SELECT * FROM agents WHERE workspace_id = ? ORDER BY created_at, id",
        (workspace["id"],),
    ).fetchall()
    conversation = conn.execute(
        """SELECT * FROM conversations WHERE workspace_id = ? AND kind = 'group'
        ORDER BY created_at LIMIT 1""",
        (workspace["id"],),
    ).fetchone()
    brief = create_brief(
        conn,
        workspace_id=workspace["id"],
        discussion_conversation_id=conversation["id"],
        goal="完成已对齐的产品发布",
        scope="研发、文档与评审",
        constraints="所有外部动作需审批",
        success_criteria="交付物通过评审",
        owner_agent_id=agents[0]["id"],
        participant_agent_ids=[agent["id"] for agent in agents],
        work_items=[
            {
                "key": f"item_{index}",
                "title": f"计划任务 {index + 1}",
                "description": "执行已对齐范围内的工作",
                "owner_agent_id": agents[index]["id"],
                "expected_output": "可审计交付物",
                "output_type": "content_package_v1" if index == 2 else "markdown",
                "depends_on_keys": [],
                "final_delivery": index == 2,
            }
            for index in range(3)
        ],
        created_by_agent_id=agents[0]["id"],
    )
    confirm_brief(
        conn,
        workspace_id=workspace["id"],
        brief_id=brief["id"],
        confirmed_by_user_id="owner",
    )
    plan_id = new_id("plan")
    conn.execute(
        """INSERT INTO task_plans (
          id, workspace_id, brief_id, root_task_id, status, workflow_status,
          revision_count, blocked_reason, created_at, updated_at
        ) VALUES (?, ?, ?, NULL, 'active', 'active', 0, '', ?, ?)""",
        (plan_id, workspace["id"], brief["id"], now_iso(), now_iso()),
    )
    tasks = []
    for index in range(3):
        tasks.append(
            create_task(
                conn,
                workspace_id=workspace["id"],
                title=f"计划任务 {index + 1}",
                owner_agent_id=agents[index]["id"],
                status="待执行",
                conversation_id=conversation["id"],
                consensus_brief_id=brief["id"],
                task_plan_id=plan_id,
                plan_item_key=f"item_{index}",
                expected_output="可审计交付物",
            )
        )
    conn.commit()
    return conn, workspace, agents, conversation, brief, tasks


def test_priority_aging_and_preemption_rules():
    score = calculate_priority_score(
        PriorityInputs(
            story_points=3,
            business_value=5,
            urgency=4,
            unblock_score=3,
            risk_reduction=2,
            switching_cost=1,
        )
    )
    assert score == 9.3333
    assert age_bonus((datetime.now(UTC) - timedelta(days=20)).isoformat()) == 3
    assert should_preempt(
        incoming_score=12,
        current_score=7,
        current_preemptible=True,
        current_in_atomic_tool=False,
        recent_interruptions=1,
    )
    assert not should_preempt(
        incoming_score=20,
        current_score=7,
        current_preemptible=True,
        current_in_atomic_tool=True,
        recent_interruptions=0,
    )
    assert not should_preempt(
        incoming_score=20,
        current_score=7,
        current_preemptible=True,
        current_in_atomic_tool=False,
        recent_interruptions=2,
    )


def test_work_request_delivery_and_acceptance_create_a_scored_task(tmp_path, monkeypatch):
    conn, workspace, agents, conversation, brief, tasks = _company(tmp_path, monkeypatch)
    try:
        conn.execute(
            """UPDATE tasks SET workflow_status = 'in_progress', priority_score = 2,
            status = '进行中' WHERE id = ?""",
            (tasks[0]["id"],),
        )
        running = create_run(
            conn,
            workspace_id=workspace["id"],
            conversation_id=conversation["id"],
            agent_id=agents[0]["id"],
            task_id=tasks[0]["id"],
            input_message_id=None,
            workdir=str(tmp_path),
            status=RunStatus.RUNNING,
            hermes_session_id="session_original",
        )
        request = create_work_request(
            conn,
            workspace_id=workspace["id"],
            requester_type="agent",
            requester_id=agents[1]["id"],
            target_agent_id=agents[0]["id"],
            content="请优先确认发布阻断问题并给出修复。",
            conversation_id=conversation["id"],
            source_task_id=tasks[1]["id"],
            consensus_brief_id=brief["id"],
        )
        assert request["status"] == "delivered"
        assert conn.execute("SELECT pause_requested_at FROM runs WHERE id = ?", (running,)).fetchone()[
            "pause_requested_at"
        ]
        conn.execute("UPDATE runs SET status = 'paused' WHERE id = ?", (running,))
        conn.execute("UPDATE tasks SET workflow_status = 'paused' WHERE id = ?", (tasks[0]["id"],))
        decision = decide_work_request(
            conn,
            workspace_id=workspace["id"],
            request_id=request["id"],
            target_agent_id=agents[0]["id"],
            payload={
                "decision": "accepted",
                "response_content": "已接受，先解除发布阻断。",
                "decision_reason": "解锁两位同事",
                "title": "解除发布阻断",
                "expected_output": "修复及验证记录",
                "output_type": "markdown",
                "story_points": 1,
                "business_value": 5,
                "urgency": 5,
                "unblock_score": 5,
                "risk_reduction": 4,
                "switching_cost": 1,
                "risk_level": "high",
                "review_required": True,
            },
        )
        assert decision["status"] == "accepted"
        assert decision["task"]["story_points"] == 1
        assert decision["task"]["priority_score"] > 20
        assert decision["task"]["workflow_status"] == "ready"
        assert decision["preempts_task_id"] == tasks[0]["id"]
        work_state = conn.execute(
            "SELECT * FROM agent_work_states WHERE agent_id = ?", (agents[0]["id"],)
        ).fetchone()
        assert work_state["interruption_count_window"] == 1
        assert work_state["interruption_window_started_at"]
        overview = get_workforce_overview(conn, workspace_id=workspace["id"])
        assert sum(overview["agents"].values()) == len(agents)
        assert {row["agent_id"] for row in overview["agent_states"]} == {
            agent["id"] for agent in agents
        }
        resumed_run_id = resume_preempted_task_after(
            conn, task_id=decision["task"]["id"]
        )
        assert resumed_run_id == running
        resumed = conn.execute("SELECT * FROM runs WHERE id = ?", (running,)).fetchone()
        assert resumed["status"] == "queued"
        assert resumed["runtime_status"] == resumed["status"]
        assert resumed["hermes_session_id"] == "session_original"
        receipt = conn.execute(
            "SELECT content FROM messages WHERE content LIKE 'WORK_REQUEST_RECEIPT:%'"
        ).fetchone()
        assert "已送达" in receipt["content"]
    finally:
        conn.close()


def test_typed_dependencies_cycle_review_and_resource_lock(tmp_path, monkeypatch):
    conn, workspace, agents, conversation, _, tasks = _company(tmp_path, monkeypatch)
    try:
        info = add_task_dependency(
            conn,
            workspace_id=workspace["id"],
            task_id=tasks[1]["id"],
            dependency_type="information",
            depends_on_task_id=tasks[0]["id"],
        )
        assert info["workflow_status"] == "waiting_information"
        satisfied = satisfy_information_dependency(
            conn,
            dependency_id=info["id"],
            evidence_type="message",
            evidence_id="msg_evidence",
        )
        assert satisfied["status"] == "satisfied"
        with pytest.raises(WorkforceError, match="cycle"):
            add_task_dependency(
                conn,
                workspace_id=workspace["id"],
                task_id=tasks[0]["id"],
                dependency_type="hard_output",
                depends_on_task_id=tasks[1]["id"],
            )

        hard = add_task_dependency(
            conn,
            workspace_id=workspace["id"],
            task_id=tasks[2]["id"],
            dependency_type="hard_output",
            depends_on_task_id=tasks[0]["id"],
        )
        conn.execute(
            "UPDATE tasks SET status = '已完成', workflow_status = 'completed' WHERE id = ?",
            (tasks[0]["id"],),
        )
        assert refresh_task_dependency_state(conn, tasks[2]["id"]) == "waiting_dependency"
        add_task_output(
            conn,
            workspace_id=workspace["id"],
            task_id=tasks[0]["id"],
            conversation_id=conversation["id"],
            agent_id=agents[0]["id"],
            title="合法交付物",
            output_type="markdown",
            content="验收证据",
        )
        assert refresh_task_dependency_state(conn, tasks[2]["id"]) == "ready"
        assert hard["dependency_type"] == "hard_output"

        review = request_task_review(
            conn,
            workspace_id=workspace["id"],
            task_id=tasks[2]["id"],
            reviewer_agent_id=agents[3]["id"],
            instructions="核对成功标准和证据。",
        )
        decided = decide_task_review(
            conn,
            workspace_id=workspace["id"],
            review_id=review["id"],
            reviewer_agent_id=agents[3]["id"],
            decision="changes_requested",
            reason="缺少重启验证证据",
        )
        assert decided["status"] == "changes_requested"
        assert conn.execute("SELECT workflow_status FROM tasks WHERE id = ?", (tasks[2]["id"],)).fetchone()[
            "workflow_status"
        ] == "ready"

        for task in tasks[:2]:
            add_task_dependency(
                conn,
                workspace_id=workspace["id"],
                task_id=task["id"],
                dependency_type="resource",
                resource_type="computer_use",
                resource_key="device:mac",
                mode="exclusive",
            )
        run_a = create_run(
            conn,
            workspace_id=workspace["id"],
            conversation_id=conversation["id"],
            agent_id=agents[0]["id"],
            task_id=tasks[0]["id"],
            input_message_id=None,
            workdir=str(tmp_path),
        )
        run_b = create_run(
            conn,
            workspace_id=workspace["id"],
            conversation_id=conversation["id"],
            agent_id=agents[1]["id"],
            task_id=tasks[1]["id"],
            input_message_id=None,
            workdir=str(tmp_path),
        )
        first = acquire_task_resources(
            conn,
            workspace_id=workspace["id"],
            task_id=tasks[0]["id"],
            run_id=run_a,
            agent_id=agents[0]["id"],
            lease_owner="worker-a",
            ttl_seconds=30,
        )
        assert first and len(first) == 1
        assert acquire_task_resources(
            conn,
            workspace_id=workspace["id"],
            task_id=tasks[1]["id"],
            run_id=run_b,
            agent_id=agents[1]["id"],
            lease_owner="worker-b",
            ttl_seconds=30,
        ) is None
        release_run_resources(conn, run_id=run_a)
        assert acquire_task_resources(
            conn,
            workspace_id=workspace["id"],
            task_id=tasks[1]["id"],
            run_id=run_b,
            agent_id=agents[1]["id"],
            lease_owner="worker-b",
            ttl_seconds=30,
        )
    finally:
        conn.close()


def test_runner_pauses_at_safe_event_and_saves_session(tmp_path, monkeypatch):
    conn, workspace, agents, conversation, _, _ = _company(tmp_path, monkeypatch)

    class PausableBackend:
        async def run(self, ctx, *, permission_resolver=None):
            yield AgentEvent("session", {"session_id": "session_td15", "resumed": False})
            yield AgentEvent("message", {"content": {"text": "阶段产出"}})
            yield AgentEvent("final", {})

    try:
        run_id = create_run(
            conn,
            workspace_id=workspace["id"],
            conversation_id=conversation["id"],
            agent_id=agents[0]["id"],
            input_message_id=None,
            workdir=str(tmp_path),
        )
        conn.execute(
            "UPDATE runs SET pause_requested_at = ?, pause_reason = 'triage' WHERE id = ?",
            (now_iso(), run_id),
        )
        result = asyncio.run(
            start_run(
                conn,
                ctx=RunContext(
                    run_id=run_id,
                    prompt="继续任务",
                    workdir=str(tmp_path),
                    profile="test-profile",
                    agent_id=agents[0]["id"],
                    workspace_id=workspace["id"],
                    conversation_id=conversation["id"],
                ),
                backend=PausableBackend(),
                input_message_id=None,
                persist_message=False,
                existing_run_id=run_id,
            )
        )
        stored = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert result["status"] == "paused"
        assert stored["runtime_status"] == stored["status"]
        assert stored["hermes_session_id"] == "session_td15"
        assert "paused_after_event" in stored["checkpoint_json"]
    finally:
        conn.close()


def test_scheduler_wakes_hermes_triage_and_records_real_decision(tmp_path, monkeypatch):
    conn, workspace, agents, conversation, _, _ = _company(tmp_path, monkeypatch)
    try:
        conn.execute(
            """INSERT INTO agent_specs (
              id, agent_id, workspace_id, role_name, source_request,
              responsibilities_json, hermes_profile, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'test', '[]', 'triage-profile', 'ready', ?, ?)
            ON CONFLICT (agent_id) DO UPDATE SET hermes_profile = 'triage-profile',
              status = 'ready', updated_at = excluded.updated_at""",
            (
                new_id("spec"),
                agents[0]["id"],
                workspace["id"],
                agents[0]["role"],
                now_iso(),
                now_iso(),
            ),
        )
        request = create_work_request(
            conn,
            workspace_id=workspace["id"],
            requester_type="user",
            requester_id="owner",
            target_agent_id=agents[0]["id"],
            content="当前发布版本号是多少？",
            conversation_id=conversation["id"],
        )
        conn.commit()
    finally:
        conn.close()

    class DecidingBackend:
        async def run(self, ctx, *, permission_resolver=None):
            decision_conn = connect()
            try:
                company_tools.decide_my_work_request(
                    decision_conn,
                    {
                        "workspace_id": ctx.workspace_id,
                        "run_id": ctx.run_id,
                        "agent_id": ctx.agent_id,
                        "conversation_id": ctx.conversation_id,
                        "run_kind": "triage",
                    },
                    request_id=request["id"],
                    decision="answered",
                    response_content="当前发布版本是 v0.1.0。",
                )
                decision_conn.commit()
            finally:
                decision_conn.close()
            yield AgentEvent("message", {"content": {"text": "已完成请求评估。"}})
            yield AgentEvent("final", {})

    async def run_scheduler():
        scheduler = TaskScheduler(backend_factory=DecidingBackend)
        await scheduler.tick()
        await asyncio.gather(*scheduler._active.values())
        await scheduler.close()

    asyncio.run(run_scheduler())
    conn = connect()
    try:
        stored = conn.execute(
            "SELECT * FROM work_requests WHERE id = ?", (request["id"],)
        ).fetchone()
        assert stored["status"] == "answered"
        triage = conn.execute("SELECT * FROM runs WHERE id = ?", (stored["triage_run_id"],)).fetchone()
        assert triage["run_kind"] == "triage"
        assert triage["status"] == "completed"
        assert conn.execute(
            "SELECT content FROM messages WHERE content = '当前发布版本是 v0.1.0。'"
        ).fetchone()
    finally:
        conn.close()


def test_scheduler_runs_independent_hermes_coordination(tmp_path, monkeypatch):
    conn, workspace, agents, conversation, _, _ = _company(tmp_path, monkeypatch)
    try:
        coordinator = agents[2]
        conn.execute(
            """INSERT INTO agent_specs (
              id, agent_id, workspace_id, role_name, source_request,
              responsibilities_json, hermes_profile, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'test', '[]', 'coord-profile', 'ready', ?, ?)
            ON CONFLICT (agent_id) DO UPDATE SET hermes_profile = 'coord-profile',
              status = 'ready', updated_at = excluded.updated_at""",
            (
                new_id("spec"),
                coordinator["id"],
                workspace["id"],
                coordinator["role"],
                now_iso(),
                now_iso(),
            ),
        )
        request = create_work_request(
            conn,
            workspace_id=workspace["id"],
            requester_type="agent",
            requester_id=agents[1]["id"],
            target_agent_id=agents[0]["id"],
            content="请处理已经阻塞两人的发布问题。",
            conversation_id=conversation["id"],
        )
        conn.execute(
            "UPDATE work_requests SET status = 'rejected' WHERE id = ?",
            (request["id"],),
        )
        case = open_coordination_case(
            conn,
            workspace_id=workspace["id"],
            work_request_id=request["id"],
            raised_by_type="agent",
            raised_by_id=agents[1]["id"],
            reason="拒绝结论没有考虑解锁两位同事。",
            evidence_ids=["event_release_blocked"],
        )
        assert case["coordinator_agent_id"] not in {
            agents[0]["id"], agents[1]["id"]
        }
        conn.commit()
    finally:
        conn.close()

    class CoordinatingBackend:
        async def run(self, ctx, *, permission_resolver=None):
            decision_conn = connect()
            try:
                company_tools.decide_my_coordination_case(
                    decision_conn,
                    {
                        "workspace_id": ctx.workspace_id,
                        "run_id": ctx.run_id,
                        "agent_id": ctx.agent_id,
                        "conversation_id": ctx.conversation_id,
                        "run_kind": "coordination",
                    },
                    case_id=case["id"],
                    decision="uphold",
                    reason="已确认目标要求先解除发布阻断，且证据显示会解锁两位同事。",
                )
                decision_conn.commit()
            finally:
                decision_conn.close()
            yield AgentEvent("message", {"content": {"text": "协调已完成。"}})
            yield AgentEvent("final", {})

    async def run_scheduler():
        scheduler = TaskScheduler(backend_factory=CoordinatingBackend)
        await scheduler.tick()
        await asyncio.gather(*scheduler._active.values())
        await scheduler.close()

    asyncio.run(run_scheduler())
    conn = connect()
    try:
        stored_case = conn.execute(
            "SELECT * FROM coordination_cases WHERE id = ?", (case["id"],)
        ).fetchone()
        assert stored_case["status"] == "resolved"
        assert stored_case["decision"] == "uphold"
        assert conn.execute(
            "SELECT status FROM work_requests WHERE id = ?", (request["id"],)
        ).fetchone()["status"] == "delivered"
        run = conn.execute(
            "SELECT * FROM runs WHERE id = ?", (stored_case["run_id"],)
        ).fetchone()
        assert run["run_kind"] == "coordination"
        assert run["status"] == "completed"
    finally:
        conn.close()


def test_local_batch_claim_is_atomic_per_agent_and_computer(tmp_path, monkeypatch):
    conn, workspace, agents, conversation, _, _ = _company(tmp_path, monkeypatch)
    try:
        device_id = new_id("device")
        conn.execute(
            """INSERT INTO local_devices (
              id, workspace_id, user_id, device_name, platform, architecture,
              worker_version, hermes_version, status, device_token_hash,
              capabilities_json, last_heartbeat_at, created_at, updated_at
            ) VALUES (?, ?, 'owner', 'Mac', 'darwin', 'arm64', 'test', '0.18.2',
              'online', 'hash', '{}', ?, ?, ?)""",
            (device_id, workspace["id"], now_iso(), now_iso(), now_iso()),
        )
        device = conn.execute("SELECT * FROM local_devices WHERE id = ?", (device_id,)).fetchone()
        for agent in (agents[0], agents[0], agents[1], agents[2]):
            create_run(
                conn,
                workspace_id=workspace["id"],
                conversation_id=conversation["id"],
                agent_id=agent["id"],
                input_message_id=None,
                status=RunStatus.QUEUED,
                execution_target="local_desktop",
                device_id=device_id,
                resource_requirements=[
                    {
                        "resource_type": "computer_use",
                        "resource_key": device_id,
                        "mode": "exclusive",
                    }
                ],
            )
        claimed = claim_local_runs(
            conn,
            device=device,
            payload=LocalRunClaimIn(
                max_runs=4,
                available_resources=[
                    ResourceClaim(
                        resource_type="computer_use",
                        resource_key=device_id,
                        mode="exclusive",
                    )
                ],
            ),
        )
        assert len(claimed) == 1
        assert claimed[0]["status"] == "leased"
    finally:
        conn.close()


def test_hundred_agents_with_ten_runs_each_claim_fairly(tmp_path, monkeypatch):
    conn, workspace, _, conversation, _, _ = _company(tmp_path, monkeypatch)
    try:
        department = conn.execute(
            "SELECT id FROM departments WHERE workspace_id = ? ORDER BY sort_order LIMIT 1",
            (workspace["id"],),
        ).fetchone()
        agent_ids = []
        for index in range(100):
            agent_ids.append(
                create_agent(
                    conn,
                    workspace_id=workspace["id"],
                    department_id=department["id"],
                    name=f"员工 {index:03d}",
                    role="并发测试员工",
                    description="",
                    prompt="执行分配工作",
                    skills=[],
                    mcps=[],
                )
            )
        for agent_id in agent_ids:
            for _ in range(10):
                create_run(
                    conn,
                    workspace_id=workspace["id"],
                    conversation_id=conversation["id"],
                    agent_id=agent_id,
                    input_message_id=None,
                    workdir=str(tmp_path),
                    status=RunStatus.QUEUED,
                )
        monkeypatch.setattr(settings, "task_server_slots", 30)
        monkeypatch.setattr(settings, "task_workspace_concurrency", 100)
        scheduler = TaskScheduler()
        first = scheduler._claim_runs(conn)
        assert len(first) == 30
        first_agents = {
            conn.execute("SELECT agent_id FROM runs WHERE id = ?", (run_id,)).fetchone()["agent_id"]
            for run_id in first
        }
        assert len(first_agents) == 30
        for run_id in first:
            conn.execute(
                """UPDATE runs SET status = 'completed', lease_owner = NULL,
                lease_expires_at = NULL WHERE id = ?""",
                (run_id,),
            )
        second = scheduler._claim_runs(conn)
        second_agents = {
            conn.execute("SELECT agent_id FROM runs WHERE id = ?", (run_id,)).fetchone()["agent_id"]
            for run_id in second
        }
        assert len(second) == 30
        assert first_agents.isdisjoint(second_agents)
        assert conn.execute(
            """SELECT COUNT(*) AS count FROM (
              SELECT agent_id FROM runs WHERE status = 'leased'
              GROUP BY agent_id HAVING COUNT(*) > 1
            ) duplicate"""
        ).fetchone()["count"] == 0
    finally:
        conn.close()
