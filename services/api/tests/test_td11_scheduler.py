from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.core.database import connect, init_db
from app.orchestration.brief import create_brief
from app.runtime.hermes_client import AgentEvent
from app.runtime.task_scheduler import TaskScheduler
from app.services import company_tools
from app.services.task_plans import launch_brief
from app.services.workspace import create_workspace_for_user, new_id, now_iso


def _seed_plan(tmp_path, monkeypatch, *, independent=False):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'scheduler.sqlite3'}")
    monkeypatch.setattr(settings, "task_workspace_concurrency", 2)
    monkeypatch.setattr(settings, "task_worker_enabled", False)
    init_db()
    conn = connect()
    conn.execute(
        "INSERT INTO users (id, email, password_hash, display_name, created_at) VALUES ('user', 'u@x', 'x', '老板', ?)",
        (now_iso(),),
    )
    workspace = create_workspace_for_user(conn, "user", "内容公司")
    agents = conn.execute(
        "SELECT * FROM agents WHERE workspace_id = ? ORDER BY created_at, id",
        (workspace["id"],),
    ).fetchall()
    group = conn.execute(
        "SELECT * FROM conversations WHERE workspace_id = ? AND name = '内容经营群'",
        (workspace["id"],),
    ).fetchone()
    for index, agent in enumerate(agents):
        conn.execute(
            """INSERT INTO agent_specs (
              id, agent_id, workspace_id, role_name, source_request,
              responsibilities_json, hermes_profile, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'test', '[]', ?, 'ready', ?, ?)
            ON CONFLICT (agent_id) DO UPDATE SET
              role_name = excluded.role_name,
              source_request = excluded.source_request,
              responsibilities_json = excluded.responsibilities_json,
              hermes_profile = excluded.hermes_profile,
              status = excluded.status,
              updated_at = excluded.updated_at""",
            (
                new_id("spec"), agent["id"], workspace["id"], agent["role"],
                f"scheduler-{index}", now_iso(), now_iso(),
            ),
        )
    dependencies = [[], [], []] if independent else [[], ["research"], ["writing"]]
    items = [
        {
            "key": "research", "title": "研究", "description": "整理资料",
            "owner_agent_id": agents[1]["id"], "expected_output": "研究摘要",
            "output_type": "markdown", "depends_on_keys": dependencies[0],
            "final_delivery": False,
        },
        {
            "key": "writing", "title": "写作", "description": "形成草稿",
            "owner_agent_id": agents[2]["id"], "expected_output": "内容草稿",
            "output_type": "markdown", "depends_on_keys": dependencies[1],
            "final_delivery": False,
        },
        {
            "key": "package", "title": "组包", "description": "整理内容包",
            "owner_agent_id": agents[3]["id"], "expected_output": "待发布内容包",
            "output_type": "content_package_v1", "depends_on_keys": dependencies[2],
            "final_delivery": True,
        },
    ]
    brief = create_brief(
        conn,
        workspace_id=workspace["id"],
        discussion_conversation_id=group["id"],
        goal="完成小红书周计划",
        scope="一周三篇图文",
        constraints="不真实发布",
        success_criteria="合法内容包",
        owner_agent_id=agents[0]["id"],
        participant_agent_ids=[agent["id"] for agent in agents],
        work_items=items,
        created_by_agent_id=agents[0]["id"],
    )
    plan = launch_brief(
        conn,
        workspace_id=workspace["id"],
        brief_id=brief["id"],
        confirmed_by_user_id="user",
    )
    conn.commit()
    conn.close()
    return workspace, plan


class SuccessfulBackend:
    async def run(self, ctx, *, permission_resolver=None):
        if "交付类型：content_package_v1" in ctx.prompt:
            conn = connect()
            try:
                task = conn.execute(
                    "SELECT task_plan_id FROM tasks WHERE id = ?", (ctx.task_id,)
                ).fetchone()
                company_tools.submit_output(
                    conn,
                    {
                        "workspace_id": ctx.workspace_id,
                        "plan_id": task["task_plan_id"],
                        "task_id": ctx.task_id,
                        "run_id": ctx.run_id,
                        "agent_id": ctx.agent_id,
                    },
                    title="小红书周计划",
                    output_type="content_package_v1",
                    content={
                        "platform": "小红书",
                        "audience": "需要高效午餐的上班族",
                        "objective": "提高收藏率",
                        "schedule": [
                            {
                                "publish_at": "2026-07-27 12:00",
                                "order": 1,
                                "content_type": "图文",
                                "title": "一周午餐模板",
                                "hook": "每天中午都不知道吃什么？",
                                "body": "这里是一份可执行的午餐模板。",
                                "cta": "收藏后按表准备",
                                "asset_suggestion": "俯拍成品图",
                                "source_refs": ["https://example.com/source"],
                            }
                        ],
                        "sources": [
                            {
                                "title": "公开来源",
                                "url": "https://example.com/source",
                            }
                        ],
                        "assumptions": ["发布时间需老板最终确认"],
                    },
                )
                conn.commit()
            finally:
                conn.close()
        yield AgentEvent("message", {"content": {"text": "任务产出已提交。"}})
        yield AgentEvent("final", {"stop_reason": "end_turn"})


class FailingBackend:
    async def run(self, ctx, *, permission_resolver=None):
        yield AgentEvent("error", {"detail": "deterministic failure"})


def _drain_scheduler(scheduler: TaskScheduler, waves: int) -> None:
    async def run():
        for _ in range(waves):
            await scheduler.tick()
            active = list(scheduler._active.values())
            if active:
                await asyncio.gather(*active)
        await scheduler.close()

    asyncio.run(run())


def test_scheduler_runs_dependencies_and_completes_content_package(tmp_path, monkeypatch):
    _, plan = _seed_plan(tmp_path, monkeypatch)
    scheduler = TaskScheduler(backend_factory=SuccessfulBackend)
    _drain_scheduler(scheduler, 4)

    conn = connect()
    try:
        stored = conn.execute("SELECT * FROM task_plans WHERE id = ?", (plan["id"],)).fetchone()
        assert stored["status"] == "completed"
        children = conn.execute(
            "SELECT * FROM tasks WHERE task_plan_id = ? AND plan_item_key <> '__root__'",
            (plan["id"],),
        ).fetchall()
        assert {task["status"] for task in children} == {"已完成"}
        runs = conn.execute(
            "SELECT plan_item_key, attempt_no FROM runs JOIN tasks ON tasks.id = runs.task_id WHERE tasks.task_plan_id = ?",
            (plan["id"],),
        ).fetchall()
        assert [(row["plan_item_key"], row["attempt_no"]) for row in runs] == [
            ("research", 1), ("writing", 1), ("package", 1)
        ]
        output = conn.execute(
            "SELECT output_type FROM task_outputs WHERE output_type = 'content_package_v1'"
        ).fetchone()
        assert output is not None
    finally:
        conn.close()


def test_scheduler_retries_once_then_blocks(tmp_path, monkeypatch):
    _, plan = _seed_plan(tmp_path, monkeypatch)
    scheduler = TaskScheduler(backend_factory=FailingBackend)
    _drain_scheduler(scheduler, 3)

    conn = connect()
    try:
        task = conn.execute(
            "SELECT * FROM tasks WHERE task_plan_id = ? AND plan_item_key = 'research'",
            (plan["id"],),
        ).fetchone()
        assert task["status"] == "阻塞"
        runs = conn.execute(
            "SELECT status, attempt_no FROM runs WHERE task_id = ? ORDER BY attempt_no",
            (task["id"],),
        ).fetchall()
        assert [row["attempt_no"] for row in runs] == [1, 2]
        assert [row["status"] for row in runs] == ["failed", "failed"]
        stored = conn.execute("SELECT * FROM task_plans WHERE id = ?", (plan["id"],)).fetchone()
        assert stored["status"] == "blocked"
    finally:
        conn.close()


def test_recover_expired_lease_creates_second_attempt(tmp_path, monkeypatch):
    _, plan = _seed_plan(tmp_path, monkeypatch)
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    conn = connect()
    try:
        run = conn.execute(
            """SELECT runs.* FROM runs JOIN tasks ON tasks.id = runs.task_id
            WHERE tasks.task_plan_id = ? LIMIT 1""",
            (plan["id"],),
        ).fetchone()
        conn.execute(
            "UPDATE runs SET status = 'running', lease_owner = 'dead', lease_expires_at = ? WHERE id = ?",
            (expired, run["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    asyncio.run(TaskScheduler(backend_factory=SuccessfulBackend).recover_expired_runs())
    conn = connect()
    try:
        attempts = conn.execute(
            "SELECT status, attempt_no FROM runs WHERE task_id = ? ORDER BY attempt_no",
            (run["task_id"],),
        ).fetchall()
        assert [(row["status"], row["attempt_no"]) for row in attempts] == [
            ("failed", 1), ("queued", 2)
        ]
    finally:
        conn.close()


def test_workspace_concurrency_claims_at_most_two_runs(tmp_path, monkeypatch):
    _, plan = _seed_plan(tmp_path, monkeypatch, independent=True)

    class WaitingBackend:
        async def run(self, ctx, *, permission_resolver=None):
            await asyncio.sleep(60)
            yield AgentEvent("final", {})

    async def run():
        scheduler = TaskScheduler(backend_factory=WaitingBackend)
        await scheduler.tick()
        assert len(scheduler._active) == 2
        conn = connect()
        try:
            leased = conn.execute(
                """SELECT COUNT(*) AS count FROM runs JOIN tasks ON tasks.id = runs.task_id
                WHERE tasks.task_plan_id = ? AND runs.lease_owner IS NOT NULL""",
                (plan["id"],),
            ).fetchone()["count"]
            assert leased == 2
        finally:
            conn.close()
        await scheduler.close()

    asyncio.run(run())


def test_scheduler_renews_lease_while_tool_run_is_active(tmp_path, monkeypatch):
    _seed_plan(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "task_run_heartbeat_seconds", 0.02)
    monkeypatch.setattr(settings, "task_run_lease_seconds", 3)

    class SlowToolBackend:
        async def run(self, ctx, *, permission_resolver=None):
            yield AgentEvent("tool_call", {"title": "long lookup"})
            await asyncio.sleep(1)
            yield AgentEvent("final", {})

    async def run():
        scheduler = TaskScheduler(backend_factory=SlowToolBackend)
        await scheduler.tick()
        conn = connect()
        try:
            initial = conn.execute(
                "SELECT lease_expires_at FROM runs WHERE lease_owner = ?",
                (scheduler.worker_id,),
            ).fetchone()["lease_expires_at"]
        finally:
            conn.close()

        await asyncio.sleep(0.12)
        conn = connect()
        try:
            renewed = conn.execute(
                "SELECT lease_expires_at FROM runs WHERE lease_owner = ?",
                (scheduler.worker_id,),
            ).fetchone()["lease_expires_at"]
            assert renewed > initial
        finally:
            conn.close()
        await scheduler.close()

    asyncio.run(run())
