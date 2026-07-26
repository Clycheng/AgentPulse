"""
TD-01-T2: End-to-end brief lifecycle verification.

Simulates the 5-step manual test from TD-01 via the FastAPI TestClient.
Focuses on brief lifecycle + task gate — does NOT depend on a working LLM.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import connect, init_db
from app.main import app
from app.services.workspace import new_id, now_iso


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings, "database_url", f"sqlite:///{tmp_path / 'e2e.sqlite3'}"
    )
    monkeypatch.setattr(settings, "password_iterations", 1_000)
    init_db()
    client = TestClient(app)

    reg = client.post(
        "/api/auth/register",
        json={
            "email": "boss@test.com",
            "password": "test123456",
            "display_name": "老板",
            "workspace_name": "测试公司",
        },
    ).json()
    token = reg["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    bootstrap = client.get("/api/me/bootstrap", headers=headers).json()
    workspace_id = bootstrap["workspace"]["id"]
    # Use first agent as "secretary"
    secretary = bootstrap["agents"][0]
    group = client.post(
        "/api/conversations/group",
        headers=headers,
        json={"name": "测试组", "member_ids": [secretary["id"]]},
    ).json()
    conv_id = group["id"]

    db = connect()
    try:
        db.execute(
            """INSERT INTO agent_specs (
              id, agent_id, workspace_id, role_name, source_request,
              responsibilities_json, hermes_profile, status, created_at, updated_at
            ) VALUES (?, ?, ?, '老板秘书', 'test', '[]', 'test-secretary', 'ready', ?, ?)
            ON CONFLICT (agent_id) DO UPDATE SET
              role_name = excluded.role_name,
              source_request = excluded.source_request,
              responsibilities_json = excluded.responsibilities_json,
              hermes_profile = excluded.hermes_profile,
              status = excluded.status,
              updated_at = excluded.updated_at""",
            (new_id("spec"), secretary["id"], workspace_id, now_iso(), now_iso()),
        )
        db.commit()
    finally:
        db.close()

    return client, headers, workspace_id, secretary, conv_id


def work_items(agent_id: str) -> list[dict]:
    return [
        {"key": "research", "title": "调研", "description": "整理依据", "owner_agent_id": agent_id, "expected_output": "研究摘要", "output_type": "markdown", "depends_on_keys": [], "final_delivery": False},
        {"key": "draft", "title": "写作", "description": "完成草稿", "owner_agent_id": agent_id, "expected_output": "内容草稿", "output_type": "markdown", "depends_on_keys": ["research"], "final_delivery": False},
        {"key": "package", "title": "组包", "description": "整理内容包", "owner_agent_id": agent_id, "expected_output": "待发布内容包", "output_type": "content_package_v1", "depends_on_keys": ["draft"], "final_delivery": True},
    ]


def test_step1_no_auto_task(setup):
    """Step 1: send message -> no auto task."""
    client, headers, ws_id, secretary, conv_id = setup

    # This fixture marks the fake profile ready for launch tests. Disable it
    # before exercising chat so an ordinary test can never start real Hermes.
    db = connect()
    try:
        db.execute(
            "UPDATE agent_specs SET status = 'failed' WHERE agent_id = ?",
            (secretary["id"],),
        )
        db.commit()
    finally:
        db.close()

    resp = client.post(
        f"/api/conversations/{conv_id}/messages",
        headers=headers,
        json={"content": "帮我搞下周内容"},
    )
    assert resp.status_code == 200
    assert "尚未执行" in resp.json()["agent_message"]["content"]

    db = connect()
    try:
        assert db.execute(
            "SELECT COUNT(*) AS count FROM tasks WHERE workspace_id = ?",
            (ws_id,),
        ).fetchone()["count"] == 0
    finally:
        db.close()

    # Conversation should still be 'discussing'
    bootstrap = client.get("/api/me/bootstrap", headers=headers).json()
    conv = next(c for c in bootstrap["conversations"] if c["id"] == conv_id)
    assert conv.get("discussion_status", "discussing") == "discussing"


def test_step2_create_brief(setup):
    """Step 2: create brief -> draft + BRIEF_CARD."""
    client, headers, ws_id, secretary, conv_id = setup

    resp = client.post(
        "/api/briefs",
        headers=headers,
        json={
            "discussion_conversation_id": conv_id,
            "goal": "策划下周3篇减脂餐小红书内容",
            "scope": "面向减肥上班族，3篇图文",
            "constraints": "周三前至少发1篇",
            "success_criteria": "3篇选题+结构定稿",
            "owner_agent_id": secretary["id"],
            "participant_agent_ids": [secretary["id"]],
            "work_items": work_items(secretary["id"]),
            "created_by_agent_id": secretary["id"],
        },
    )
    assert resp.status_code == 200, f"create brief failed: {resp.text}"
    brief = resp.json()
    assert brief["status"] == "draft"
    assert brief["goal"] == "策划下周3篇减脂餐小红书内容"

    # Verify BRIEF_CARD system message
    db = connect()
    try:
        msgs = db.execute(
            "SELECT content FROM messages WHERE conversation_id=? AND sender_type='system'",
            (conv_id,),
        ).fetchall()
        card_found = any(m["content"].startswith("BRIEF_CARD:") for m in msgs)
        assert card_found, "No BRIEF_CARD system message"
    finally:
        db.close()


def test_step3_reject_brief(setup):
    """Step 3: reject -> rejected, discussion continues."""
    client, headers, ws_id, secretary, conv_id = setup

    # Create
    brief = client.post(
        "/api/briefs",
        headers=headers,
        json={
            "discussion_conversation_id": conv_id,
            "goal": "测试reject",
            "owner_agent_id": secretary["id"],
            "participant_agent_ids": [secretary["id"]],
            "work_items": work_items(secretary["id"]),
            "created_by_agent_id": secretary["id"],
        },
    ).json()

    # Reject
    resp = client.post(f"/api/briefs/{brief['id']}/reject", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"

    # Discussion status stays discussing
    bootstrap = client.get("/api/me/bootstrap", headers=headers).json()
    conv = next(c for c in bootstrap["conversations"] if c["id"] == conv_id)
    assert conv.get("discussion_status", "discussing") == "discussing"


def test_step4_confirm_launches_task_plan(setup):
    """Step 4: compatibility confirm -> aligned + launches the durable plan."""
    client, headers, ws_id, secretary, conv_id = setup

    brief = client.post(
        "/api/briefs",
        headers=headers,
        json={
            "discussion_conversation_id": conv_id,
            "goal": "本周输出3篇内容",
            "owner_agent_id": secretary["id"],
            "participant_agent_ids": [secretary["id"]],
            "work_items": work_items(secretary["id"]),
            "created_by_agent_id": secretary["id"],
        },
    ).json()
    brief_id = brief["id"]

    # Confirm
    resp = client.post(f"/api/briefs/{brief_id}/confirm", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "confirmed"

    # Discussion status should be 'aligned'
    bootstrap = client.get("/api/me/bootstrap", headers=headers).json()
    conv = next(c for c in bootstrap["conversations"] if c["id"] == conv_id)
    assert conv.get("discussion_status") == "aligned", (
        f"Expected aligned, got {conv.get('discussion_status')}"
    )

    # The confirm compatibility endpoint delegates launch: root + 3 work items.
    bootstrap = client.get("/api/me/bootstrap", headers=headers).json()
    plan_tasks = [t for t in bootstrap["tasks"] if t["consensus_brief_id"] == brief_id]
    assert len(plan_tasks) == 4
    assert len({task["task_plan_id"] for task in plan_tasks}) == 1


def test_step5_gate_rejects(setup):
    """Step 5: task without brief -> 400."""
    client, headers, ws_id, secretary, conv_id = setup

    resp = client.post(
        "/api/tasks",
        headers=headers,
        json={
            "title": "没有brief的任务",
            "owner_agent_id": secretary["id"],
            "conversation_id": conv_id,
        },
    )
    assert resp.status_code == 400, (
        f"Gate should reject, got {resp.status_code}: {resp.text}"
    )
