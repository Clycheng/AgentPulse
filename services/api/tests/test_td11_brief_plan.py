from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import connect, init_db
from app.main import app
from app.orchestration.brief import confirm_brief, validate_work_items
from app.services.task_plans import launch_brief
from app.services.workspace import new_id, now_iso, update_task


def _items(agent_ids: list[str]) -> list[dict]:
    return [
        {
            "key": "research",
            "title": "平台与受众研究",
            "description": "整理公司资料和必要的公开来源",
            "owner_agent_id": agent_ids[0],
            "expected_output": "带引用的研究摘要",
            "output_type": "markdown",
            "depends_on_keys": [],
            "final_delivery": False,
        },
        {
            "key": "writing",
            "title": "内容写作",
            "description": "完成标题、钩子和正文",
            "owner_agent_id": agent_ids[1],
            "expected_output": "内容草稿",
            "output_type": "markdown",
            "depends_on_keys": ["research"],
            "final_delivery": False,
        },
        {
            "key": "package",
            "title": "组装内容包",
            "description": "整理排期、素材、引用和未知项",
            "owner_agent_id": agent_ids[2],
            "expected_output": "结构化待发布内容包",
            "output_type": "content_package_v1",
            "depends_on_keys": ["writing"],
            "final_delivery": True,
        },
    ]


@pytest.mark.parametrize(
    "mutate,error",
    [
        (lambda items: items[0].update(owner_agent_id="outsider"), "not a group member"),
        (lambda items: items[1].update(key="research"), "duplicate work item key"),
        (lambda items: items[2].update(depends_on_keys=["missing"]), "unknown dependencies"),
        (lambda items: items[0].update(depends_on_keys=["writing"]), "acyclic"),
        (lambda items: items[2].update(final_delivery=False), "exactly one final"),
    ],
)
def test_work_item_validation_rejects_invalid_contracts(mutate, error):
    items = _items(["a", "b", "c"])
    mutate(items)
    with pytest.raises(ValueError, match=error):
        validate_work_items(items, allowed_agent_ids={"a", "b", "c"})


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'td11.sqlite3'}")
    monkeypatch.setattr(settings, "password_iterations", 1_000)
    monkeypatch.setattr(settings, "task_worker_enabled", False)
    init_db()
    client = TestClient(app)
    registered = client.post(
        "/api/auth/register",
        json={
            "email": "td11@example.com",
            "password": "test123456",
            "display_name": "老板",
            "workspace_name": "内容公司",
        },
    ).json()
    headers = {"Authorization": f"Bearer {registered['access_token']}"}
    bootstrap = client.get("/api/me/bootstrap", headers=headers).json()
    agents = bootstrap["agents"]
    group = next(item for item in bootstrap["conversations"] if item["name"] == "内容经营群")
    conn = connect()
    try:
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
                    new_id("spec"),
                    agent["id"],
                    registered["workspace"]["id"],
                    agent["role"],
                    f"td11-{index}",
                    now_iso(),
                    now_iso(),
                ),
            )
        conn.commit()
    finally:
        conn.close()

    return client, registered, headers, agents, group


def _create_brief(client, headers, agents, group, *, goal="小红书周计划"):
    items = _items([agents[1]["id"], agents[2]["id"], agents[3]["id"]])
    response = client.post(
        "/api/briefs",
        headers=headers,
        json={
            "discussion_conversation_id": group["id"],
            "goal": goal,
            "scope": "小红书一周 3 篇图文",
            "constraints": "不真实发布",
            "success_criteria": "交付可追踪的待发布内容包",
            "owner_agent_id": agents[0]["id"],
            "participant_agent_ids": [agent["id"] for agent in agents],
            "work_items": items,
            "created_by_agent_id": agents[0]["id"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_launch_is_atomic_idempotent_and_exposes_plan_snapshot(tmp_path, monkeypatch):
    client, registered, headers, agents, group = _client(tmp_path, monkeypatch)
    brief = _create_brief(client, headers, agents, group)

    launched = client.post(f"/api/briefs/{brief['id']}/launch", headers=headers)
    assert launched.status_code == 200, launched.text
    plan = launched.json()
    assert plan["status"] == "active"
    assert len(plan["tasks"]) == 4
    assert len(plan["dependencies"]) == 2
    assert sum(len(task["runs"]) for task in plan["tasks"]) == 1

    duplicate = client.post(f"/api/briefs/{brief['id']}/launch", headers=headers)
    assert duplicate.status_code == 200
    assert duplicate.json()["id"] == plan["id"]

    active_task = next(task for task in plan["tasks"] if task["runs"])
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO approvals (
              id, workspace_id, task_id, conversation_id, agent_id, title,
              description, status, risk_level, type, run_id, payload_json, created_at
            ) VALUES ('approval-live', ?, ?, ?, ?, '高风险动作', '请老板决定',
              'pending', 'high', 'high_risk', ?, '{}', ?)""",
            (
                registered["workspace"]["id"],
                active_task["id"],
                group["id"],
                active_task["owner_agent_id"],
                active_task["runs"][0]["id"],
                now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    fetched = client.get(f"/api/task-plans/{plan['id']}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["tasks"][1]["expected_output"]
    fetched_task = next(task for task in fetched.json()["tasks"] if task["id"] == active_task["id"])
    assert fetched_task["approvals"][0]["id"] == "approval-live"


def test_planned_task_cannot_complete_without_scheduler_verified_output(tmp_path, monkeypatch):
    client, registered, headers, agents, group = _client(tmp_path, monkeypatch)
    brief = _create_brief(client, headers, agents, group)
    plan = client.post(f"/api/briefs/{brief['id']}/launch", headers=headers).json()
    task = next(item for item in plan["tasks"] if item["plan_item_key"] == "package")

    conn = connect()
    try:
        with pytest.raises(ValueError, match="verified scheduler output"):
            update_task(
                conn,
                workspace_id=registered["workspace"]["id"],
                task_id=task["id"],
                changes={"status": "已完成", "progress": 100},
            )

        approval_id = "approval-legacy-plan"
        conn.execute(
            """INSERT INTO approvals (
              id, workspace_id, task_id, conversation_id, agent_id, title,
              description, status, risk_level, type, created_at
            ) VALUES (?, ?, ?, ?, ?, '旧式确认', '没有可验证交付物',
              'pending', 'low', 'high_risk', ?)""",
            (
                approval_id,
                registered["workspace"]["id"],
                task["id"],
                group["id"],
                task["owner_agent_id"],
                now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    rejected = client.post(
        f"/api/approvals/{approval_id}/resolve",
        headers=headers,
        json={"status": "approved", "scope": "once"},
    )
    assert rejected.status_code == 409
    conn = connect()
    try:
        assert conn.execute(
            "SELECT status FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()["status"] == "pending"
    finally:
        conn.close()

def test_concurrent_launch_returns_one_plan(tmp_path, monkeypatch):
    client, registered, headers, agents, group = _client(tmp_path, monkeypatch)
    brief = _create_brief(client, headers, agents, group)

    def launch_once() -> str:
        conn = connect()
        try:
            plan = launch_brief(
                conn,
                workspace_id=registered["workspace"]["id"],
                brief_id=brief["id"],
                confirmed_by_user_id=registered["user"]["id"],
            )
            conn.commit()
            return plan["id"]
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        plan_ids = list(executor.map(lambda _: launch_once(), range(2)))
    assert len(set(plan_ids)) == 1
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM task_plans WHERE brief_id = ?",
            (brief["id"],),
        ).fetchone()["count"] == 1
    finally:
        conn.close()


def test_rejected_brief_cannot_launch_and_readiness_failure_rolls_back(tmp_path, monkeypatch):
    client, registered, headers, agents, group = _client(tmp_path, monkeypatch)
    rejected = _create_brief(client, headers, agents, group, goal="reject me")
    assert client.post(f"/api/briefs/{rejected['id']}/reject", headers=headers).status_code == 200
    assert client.post(f"/api/briefs/{rejected['id']}/launch", headers=headers).status_code == 409

    pending = _create_brief(client, headers, agents, group, goal="not ready")
    conn = connect()
    try:
        conn.execute("UPDATE agent_specs SET status = 'failed' WHERE agent_id = ?", (agents[2]["id"],))
        conn.commit()
    finally:
        conn.close()
    failed = client.post(f"/api/briefs/{pending['id']}/launch", headers=headers)
    assert failed.status_code == 409
    conn = connect()
    try:
        assert conn.execute("SELECT id FROM task_plans WHERE brief_id = ?", (pending["id"],)).fetchone() is None
        row = conn.execute("SELECT status FROM consensus_briefs WHERE id = ?", (pending["id"],)).fetchone()
        assert row["status"] == "draft"
    finally:
        conn.close()


def test_legacy_confirmed_brief_can_be_backfilled(tmp_path, monkeypatch):
    client, registered, headers, agents, group = _client(tmp_path, monkeypatch)
    brief = _create_brief(client, headers, agents, group, goal="legacy")
    conn = connect()
    try:
        confirm_brief(
            conn,
            workspace_id=registered["workspace"]["id"],
            brief_id=brief["id"],
            confirmed_by_user_id=registered["user"]["id"],
        )
        conn.execute("UPDATE consensus_briefs SET work_items_json = '[]' WHERE id = ?", (brief["id"],))
        conn.commit()
    finally:
        conn.close()

    launched = client.post(f"/api/briefs/{brief['id']}/launch", headers=headers)
    assert launched.status_code == 200, launched.text
    child_types = [
        task["output_type"]
        for task in launched.json()["tasks"]
        if task["plan_item_key"] != "__root__"
    ]
    assert child_types == ["markdown", "markdown", "content_package_v1"]


def test_blocked_task_resume_creates_next_attempt(tmp_path, monkeypatch):
    client, _, headers, agents, group = _client(tmp_path, monkeypatch)
    brief = _create_brief(client, headers, agents, group)
    plan = client.post(f"/api/briefs/{brief['id']}/launch", headers=headers).json()
    task = next(item for item in plan["tasks"] if item["plan_item_key"] == "research")
    first_run = task["runs"][0]

    conn = connect()
    try:
        conn.execute(
            "UPDATE runs SET status = 'failed', error = 'needs input', completed_at = ? WHERE id = ?",
            (now_iso(), first_run["id"]),
        )
        conn.execute(
            "UPDATE tasks SET status = '阻塞' WHERE id = ?", (task["id"],)
        )
        conn.execute(
            "UPDATE task_plans SET status = 'blocked', blocked_reason = '缺少定位' WHERE id = ?",
            (plan["id"],),
        )
        conn.commit()
    finally:
        conn.close()

    resumed = client.post(
        f"/api/tasks/{task['id']}/resume",
        headers=headers,
        json={"message": "定位是服务忙碌上班族"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "active"
    runs = client.get(f"/api/tasks/{task['id']}/runs", headers=headers)
    assert [run["attempt_no"] for run in runs.json()] == [1, 2]
    assert runs.json()[1]["status"] == "queued"
    assert client.post(
        f"/api/tasks/{task['id']}/resume",
        headers=headers,
        json={"message": "重复恢复"},
    ).status_code == 409
