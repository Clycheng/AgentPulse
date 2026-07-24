"""Tests for the idea center API (TD-08-T1).

Covers idea CRUD, status flow (review / convert), the DB CHECK constraints,
convert → conversation linkage (conversations.idea_id), and idle-thinking config.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import connect, init_db
from app.main import app


def make_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(
        settings, "database_url", f"sqlite:///{tmp_path / 'test_ideas.sqlite3'}"
    )
    monkeypatch.setattr(settings, "password_iterations", 1_000)
    init_db()
    return TestClient(app)


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def register(client: TestClient) -> tuple[str, str]:
    """Register and return (token, secretary_agent_id)."""
    resp = client.post(
        "/api/auth/register",
        json={
            "email": "founder@example.com",
            "password": "agentpulse123",
            "display_name": "老板",
            "workspace_name": "测试公司",
        },
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    return token, bootstrap["agents"][0]["id"]


def _make_idea(client, token, agent_id, *, title="优化拆解流程", category="improvement"):
    return client.post(
        "/api/ideas",
        headers=auth_header(token),
        json={
            "source_agent_id": agent_id,
            "title": title,
            "description": "最近拆任务时重复问相同的背景，可以做个模板。",
            "category": category,
        },
    )


def test_create_and_get_idea(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    token, agent_id = register(client)

    created = _make_idea(client, token, agent_id)
    assert created.status_code == 200
    idea = created.json()
    assert idea["status"] == "new"
    assert idea["category"] == "improvement"
    assert idea["source_agent_name"] == "小秘"

    fetched = client.get(f"/api/ideas/{idea['id']}", headers=auth_header(token))
    assert fetched.status_code == 200
    assert fetched.json()["id"] == idea["id"]


def test_list_ideas_with_filters_and_new_first(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    token, agent_id = register(client)

    _make_idea(client, token, agent_id, title="A", category="improvement")
    opp = _make_idea(client, token, agent_id, title="B", category="opportunity").json()
    # dismiss one so it is not 'new'
    client.post(
        f"/api/ideas/{opp['id']}/review",
        headers=auth_header(token),
        json={"action": "dismiss"},
    )

    all_ideas = client.get("/api/ideas", headers=auth_header(token)).json()
    assert len(all_ideas) == 2
    # 'new' ordered before reviewed
    assert all_ideas[0]["status"] == "new"

    only_opp = client.get(
        "/api/ideas?category=opportunity", headers=auth_header(token)
    ).json()
    assert len(only_opp) == 1
    assert only_opp[0]["title"] == "B"

    only_new = client.get("/api/ideas?status=new", headers=auth_header(token)).json()
    assert [i["status"] for i in only_new] == ["new"]


def test_review_accept_and_dismiss(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    token, agent_id = register(client)
    idea = _make_idea(client, token, agent_id).json()

    accepted = client.post(
        f"/api/ideas/{idea['id']}/review",
        headers=auth_header(token),
        json={"action": "accept"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"
    assert accepted.json()["reviewed_at"] is not None


def test_convert_idea_creates_linked_conversation(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    token, agent_id = register(client)
    idea = _make_idea(client, token, agent_id).json()

    resp = client.post(f"/api/ideas/{idea['id']}/convert", headers=auth_header(token))
    assert resp.status_code == 200
    body = resp.json()
    conversation_id = body["conversation_id"]
    assert body["idea"]["status"] == "converted"

    # The idea seeds the discussion as the first system message.
    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    messages = bootstrap["messages_by_conversation"][conversation_id]
    assert any(idea["title"] in m["content"] for m in messages)

    # conversations.idea_id is linked (verified directly against the DB).
    db = connect()
    try:
        row = db.execute(
            "SELECT idea_id, kind FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
    finally:
        db.close()
    assert row["idea_id"] == idea["id"]
    assert row["kind"] == "group"

    # Re-converting a converted idea is rejected.
    again = client.post(f"/api/ideas/{idea['id']}/convert", headers=auth_header(token))
    assert again.status_code == 400


def test_invalid_category_rejected_by_schema(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    token, agent_id = register(client)
    resp = _make_idea(client, token, agent_id, category="nonsense")
    assert resp.status_code == 422  # Pydantic Literal rejects before the DB


def test_db_check_constraint_blocks_bad_status(tmp_path, monkeypatch):
    """DB-level backstop: the status CHECK constraint rejects illegal values."""
    import sqlite3

    client = make_client(tmp_path, monkeypatch)
    token, agent_id = register(client)
    idea = _make_idea(client, token, agent_id).json()

    db = connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE ideas SET status = 'bogus' WHERE id = ?", (idea["id"],)
            )
    finally:
        db.close()


def test_idle_thinking_update(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    token, _ = register(client)

    # Create an agent with a role_spec so an agent_specs row exists.
    agent = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "增长分析师",
            "description": "分析增长",
            "department_name": "增长部",
            "prompt": "你分析增长数据",
            "role_spec": {
                "role_name": "增长分析师",
                "source_request": "需要一个会写代码的分析师",
                "responsibilities": ["分析数据"],
                "capability_keys": ["write_code"],
            },
        },
    ).json()

    resp = client.patch(
        f"/api/agents/{agent['id']}/idle-thinking",
        headers=auth_header(token),
        json={"enabled": False, "interval_hours": 12},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["idle_thinking_enabled"] is False
    assert body["idle_think_interval_hours"] == 12
    assert body["agent_id"] == agent["id"]


def test_idle_thinking_without_spec_returns_404(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    token, secretary_id = register(client)
    # The default secretary has a full capability spec even in fallback mode.
    resp = client.patch(
        f"/api/agents/{secretary_id}/idle-thinking",
        headers=auth_header(token),
        json={"enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json()["idle_thinking_enabled"] is False
