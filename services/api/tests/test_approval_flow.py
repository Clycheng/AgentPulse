"""Tests for durable approval suspend/resume + clarification."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import connect, init_db
from app.main import app
from app.runtime.hermes_client import AgentEvent, RunContext
from app.runtime.runner import make_bridge_resolver, stream_agent_run
from app.runtime.runs import RunStatus, create_run, get_run
from app.services.workspace import (
    add_message,
    create_agent,
    create_workspace_for_user,
    new_id,
    now_iso,
)


# ------------------------------------------------------------------ db helpers


def _setup_db(tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings, "database_url", f"sqlite:///{tmp_path / 'appr.sqlite3'}"
    )
    monkeypatch.setattr(settings, "password_iterations", 1_000)
    init_db()
    conn = connect()
    user_id = new_id("user")
    conn.execute(
        "INSERT INTO users (id, email, password_hash, display_name, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, "boss@example.com", "x", "老板", now_iso()),
    )
    ws = create_workspace_for_user(conn, user_id, "测试公司")
    dept = conn.execute(
        "SELECT id FROM departments WHERE workspace_id = ? LIMIT 1", (ws["id"],)
    ).fetchone()["id"]
    agent_id = create_agent(
        conn, workspace_id=ws["id"], department_id=dept, name="阿伦", role="内容主笔",
        description="", prompt="", skills=[], mcps=[],
    )
    conv_id = new_id("conv")
    conn.execute(
        "INSERT INTO conversations (id, workspace_id, kind, name, unread, created_at, updated_at) "
        "VALUES (?, ?, 'group', 'g', 0, ?, ?)",
        (conv_id, ws["id"], now_iso(), now_iso()),
    )
    msg = add_message(
        conn, conversation_id=conv_id, sender_type="user", sender_id=user_id,
        content="上线新版本",
    )
    conn.commit()
    return conn, ws["id"], agent_id, conv_id, msg["id"]


class _ApprovalBackend:
    """Fake backend: emits an approval request, then resolves via the injected
    durable resolver and emits the outcome, mimicking ACP's
    request_permission awaiting in place."""

    def __init__(self, approval_id, category="high_risk"):
        self.aid = approval_id
        self.category = category

    async def run(self, ctx, *, permission_resolver=None):
        yield AgentEvent("message", {"content": {"text": "开始执行…"}})
        yield AgentEvent(
            "approval_required",
            {"approval_id": self.aid, "category": self.category,
             "tool_call": {"title": "deploy_prod"}},
        )
        decision = "deny"
        if permission_resolver is not None:
            decision = await permission_resolver(
                {"approval_id": self.aid, "category": self.category}
            )
        text = "已上线完成" if str(decision).startswith("allow") else "已取消"
        yield AgentEvent("message", {"content": {"text": text}})


def _ctx(ws_id, agent_id, conv_id, tmp_path):
    return RunContext(
        run_id="", prompt="上线", workdir=str(tmp_path / "wd"),
        profile="p_arun", agent_id=agent_id, workspace_id=ws_id, conversation_id=conv_id,
    )


# --------------------------------------------------- suspend / resume mechanism


def _run_suspend_scenario(tmp_path, monkeypatch, decision: str):
    conn, ws_id, agent_id, conv_id, msg_id = _setup_db(tmp_path, monkeypatch)
    aid = "appr_deploy"

    async def scenario():
        events = []

        async def drain():
            async for ev in stream_agent_run(
                conn,
                ctx=_ctx(ws_id, agent_id, conv_id, tmp_path),
                backend=_ApprovalBackend(aid),
                input_message_id=msg_id,
                permission_resolver=make_bridge_resolver(conn),
            ):
                events.append(ev)

        task = asyncio.create_task(drain())
        for _ in range(200):
            pending = conn.execute(
                "SELECT * FROM approvals WHERE id = ?", (aid,)
            ).fetchone()
            if pending is not None:
                break
            await asyncio.sleep(0.005)
        assert pending is not None, "run did not persist its approval"

        # approval row persisted + run suspended
        appr = conn.execute("SELECT * FROM approvals WHERE id = ?", (aid,)).fetchone()
        assert appr is not None and appr["status"] == "pending"
        assert appr["type"] == "high_risk" and appr["run_id"]
        run = get_run(conn, appr["run_id"])
        assert run["status"] == RunStatus.WAITING_USER

        status = "approved" if decision.startswith("allow") else "rejected"
        scope = "always" if decision == "allow_always" else "once"
        other = connect()
        try:
            other.execute(
                "UPDATE approvals SET status = ?, payload_json = ? WHERE id = ?",
                (status, f'{{"scope":"{scope}"}}', aid),
            )
            other.commit()
        finally:
            other.close()
        await task
        return conn, appr["run_id"], events

    return asyncio.run(scenario())


def test_run_suspends_and_resumes_on_approve(tmp_path, monkeypatch):
    conn, run_id, events = _run_suspend_scenario(tmp_path, monkeypatch, "allow_once")
    assert get_run(conn, run_id)["status"] == RunStatus.COMPLETED
    msgs = conn.execute(
        "SELECT content FROM messages WHERE sender_type = 'agent'"
    ).fetchall()
    assert any("已上线完成" in m["content"] for m in msgs)
    assert any(e["type"] == "approval_required" for e in events)


def test_run_expires_and_marks_approval_row(tmp_path, monkeypatch):
    """ADR 0008 item 4: an unanswered approval resolves on its own — the
    approvals row ends up 'expired' (not stuck at 'pending' forever) and the
    run still finishes, exactly like a manual deny."""
    monkeypatch.setattr(settings, "approval_bridge_timeout_seconds", 0.05)
    conn, ws_id, agent_id, conv_id, msg_id = _setup_db(tmp_path, monkeypatch)
    aid = "appr_expire"

    async def scenario():
        async for _ in stream_agent_run(
            conn,
            ctx=_ctx(ws_id, agent_id, conv_id, tmp_path),
            backend=_ApprovalBackend(aid),
            input_message_id=msg_id,
            permission_resolver=make_bridge_resolver(conn),
        ):
            pass

    asyncio.run(scenario())

    appr = conn.execute("SELECT * FROM approvals WHERE id = ?", (aid,)).fetchone()
    assert appr["status"] == "expired"
    run = get_run(conn, appr["run_id"])
    assert run["status"] == RunStatus.COMPLETED
    msgs = conn.execute(
        "SELECT content FROM messages WHERE sender_type = 'agent'"
    ).fetchall()
    assert any("已取消" in m["content"] for m in msgs)


def test_run_resumes_on_deny_with_alternative(tmp_path, monkeypatch):
    conn, run_id, _ = _run_suspend_scenario(tmp_path, monkeypatch, "deny")
    assert get_run(conn, run_id)["status"] == RunStatus.COMPLETED
    msgs = conn.execute(
        "SELECT content FROM messages WHERE sender_type = 'agent'"
    ).fetchall()
    assert any("已取消" in m["content"] for m in msgs)


# --------------------------------------------------------------- HTTP endpoints


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings, "database_url", f"sqlite:///{tmp_path / 'appr_api.sqlite3'}"
    )
    monkeypatch.setattr(settings, "password_iterations", 1_000)
    init_db()
    client = TestClient(app)
    resp = client.post(
        "/api/auth/register",
        json={"email": "boss@ex.com", "password": "agentpulse123",
              "display_name": "老板", "workspace_name": "公司"},
    )
    token = resp.json()["access_token"]
    boot = client.get("/api/me/bootstrap", headers={"Authorization": f"Bearer {token}"}).json()
    ws_id = boot["workspace"]["id"]
    agent_id = boot["agents"][0]["id"]
    return client, token, ws_id, agent_id


def _seed_run_approval(ws_id, agent_id, *, category, run_status, appr_status="pending"):
    conn = connect()
    conv_id = new_id("conv")
    conn.execute(
        "INSERT INTO conversations (id, workspace_id, kind, name, unread, created_at, updated_at) "
        "VALUES (?, ?, 'group', 'g', 0, ?, ?)",
        (conv_id, ws_id, now_iso(), now_iso()),
    )
    msg = add_message(conn, conversation_id=conv_id, sender_type="user", sender_id="u", content="go")
    run_id = create_run(
        conn, workspace_id=ws_id, conversation_id=conv_id, agent_id=agent_id,
        input_message_id=msg["id"], status=RunStatus.RUNNING,
    )
    from app.runtime.runs import transition_run
    transition_run(conn, run_id, run_status)
    aid = new_id("appr")
    atype = "clarification" if category == "clarification" else "high_risk"
    conn.execute(
        "INSERT INTO approvals (id, workspace_id, conversation_id, agent_id, title, "
        "description, status, risk_level, type, run_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, '', ?, 'high', ?, ?, ?)",
        (aid, ws_id, conv_id, agent_id, "需确认", appr_status, atype, run_id, now_iso()),
    )
    conn.commit()
    return aid, run_id, conv_id


def test_resolve_endpoint_records_decision_without_transitioning_run(tmp_path, monkeypatch):
    client, token, ws_id, agent_id = _client(tmp_path, monkeypatch)
    aid, run_id, _ = _seed_run_approval(
        ws_id, agent_id, category="high_risk", run_status=RunStatus.WAITING_USER
    )
    resp = client.post(
        f"/api/approvals/{aid}/resolve", json={"status": "approved"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    # RunService owns the transition after its durable resolver observes this row.
    assert get_run(connect(), run_id)["status"] == RunStatus.WAITING_USER


def test_answer_endpoint_records_without_transitioning_run(tmp_path, monkeypatch):
    client, token, ws_id, agent_id = _client(tmp_path, monkeypatch)
    aid, run_id, conv_id = _seed_run_approval(
        ws_id, agent_id, category="clarification", run_status=RunStatus.WAITING_CLARIFY
    )
    resp = client.post(
        f"/api/approvals/{aid}/answer", json={"answer": "主打办公场景"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "answered"
    conn = connect()
    assert get_run(conn, run_id)["status"] == RunStatus.WAITING_CLARIFY
    msgs = conn.execute(
        "SELECT content FROM messages WHERE conversation_id = ? AND sender_type = 'user'",
        (conv_id,),
    ).fetchall()
    assert any("主打办公场景" in m["content"] for m in msgs)


def test_answer_endpoint_rejects_non_clarification(tmp_path, monkeypatch):
    client, token, ws_id, agent_id = _client(tmp_path, monkeypatch)
    aid, _, _ = _seed_run_approval(
        ws_id, agent_id, category="high_risk", run_status=RunStatus.WAITING_USER
    )
    resp = client.post(
        f"/api/approvals/{aid}/answer", json={"answer": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404  # not a clarification approval
