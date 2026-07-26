from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.database import connect, init_db
from app.runtime.hermes_prompts import build_hermes_context_prompt
from app.schemas.run import HermesRunAgent, HermesRunContext, HermesRunMessage
from app.services.company_memory import (
    build_context_manifest,
    record_company_event,
    record_memory,
    search_company_memory,
    send_internal_ping,
)
from app.services.workspace import add_message, sync_obsidian_documents
from app.main import app
from fastapi.testclient import TestClient


def _company(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'td13.sqlite3'}")
    monkeypatch.setattr(settings, "password_iterations", 1_000)
    init_db()
    client = TestClient(app)
    response = client.post(
        "/api/auth/register",
        json={
            "email": "td13@example.com",
            "password": "agentpulse123",
            "display_name": "创始人",
            "workspace_name": "记忆公司",
        },
    )
    payload = response.json()
    headers = {"Authorization": f"Bearer {payload['access_token']}"}
    bootstrap = client.get("/api/me/bootstrap", headers=headers).json()
    conn = connect()
    return client, headers, payload["workspace"]["id"], bootstrap, conn


def test_messages_are_shared_events_and_context_manifests_are_auditable(tmp_path, monkeypatch):
    _, _, workspace_id, bootstrap, conn = _company(tmp_path, monkeypatch)
    try:
        group = next(item for item in bootstrap["conversations"] if item["name"] == "内容经营群")
        agents = bootstrap["agents"]
        message = add_message(
            conn,
            conversation_id=group["id"],
            sender_type="agent",
            sender_id=agents[1]["id"],
            content="平台受众更关心可验证的案例。",
        )
        conn.commit()
        results = search_company_memory(
            conn,
            workspace_id=workspace_id,
            query="可验证案例",
            agent_id=agents[2]["id"],
        )
        assert any(item["kind"] == "event" and item["source_id"] == message["id"] for item in results)

        workspace = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        conversation = conn.execute("SELECT * FROM conversations WHERE id = ?", (group["id"],)).fetchone()
        agent = conn.execute("SELECT * FROM agents WHERE id = ?", (agents[2]["id"],)).fetchone()
        manifest = build_context_manifest(
            conn,
            workspace=workspace,
            conversation=conversation,
            agent=agent,
            current_text="请根据案例安排选题",
            token_budget=3000,
        )
        stored = conn.execute(
            "SELECT * FROM context_manifests WHERE id = ?", (manifest["id"],)
        ).fetchone()
        assert stored is not None
        assert any(item["source_id"] == message["id"] for item in manifest["events"])
        assert "案例" in manifest["text"]
    finally:
        conn.close()


def test_private_memory_isolated_but_promoted_memory_is_shared(tmp_path, monkeypatch):
    _, _, workspace_id, bootstrap, conn = _company(tmp_path, monkeypatch)
    try:
        first, second = bootstrap["agents"][:2]
        event = record_company_event(
            conn,
            workspace_id=workspace_id,
            event_type="test_fact",
            source_id="fact-1",
            title="测试事实",
            content="只有证据支持的事实",
        )
        record_memory(
            conn,
            workspace_id=workspace_id,
            agent_id=first["id"],
            memory_type="reflection",
            title="私有判断",
            content="只属于第一位员工的判断",
            evidence_event_ids=[event["id"]],
            is_private=True,
        )
        assert not any(
            item.get("content") == "只属于第一位员工的判断"
            for item in search_company_memory(
                conn, workspace_id=workspace_id, query="第一位员工判断", agent_id=second["id"]
            )
        )
        record_memory(
            conn,
            workspace_id=workspace_id,
            agent_id=first["id"],
            memory_type="lesson",
            title="共享经验",
            content="全公司可以复用的经验",
            evidence_event_ids=[event["id"]],
            is_private=False,
            promoted=True,
        )
        assert any(
            item.get("content") == "全公司可以复用的经验"
            for item in search_company_memory(
                conn, workspace_id=workspace_id, query="全公司复用经验", agent_id=second["id"]
            )
        )
    finally:
        conn.close()


def test_internal_ping_creates_dedicated_dm_and_blocks_duplicate(tmp_path, monkeypatch):
    _, _, workspace_id, bootstrap, conn = _company(tmp_path, monkeypatch)
    try:
        first, second = bootstrap["agents"][:2]
        result = send_internal_ping(
            conn,
            workspace_id=workspace_id,
            from_agent_id=first["id"],
            to_agent_id=second["id"],
            content="请确认受众案例的来源。",
            run_id="run-ping-1",
        )
        members = conn.execute(
            "SELECT agent_id FROM conversation_members WHERE conversation_id = ?",
            (result["conversation_id"],),
        ).fetchall()
        assert {row["agent_id"] for row in members} == {first["id"], second["id"]}
        with pytest.raises(ValueError, match="重复 ping"):
            send_internal_ping(
                conn,
                workspace_id=workspace_id,
                from_agent_id=first["id"],
                to_agent_id=second["id"],
                content="请确认受众案例的来源。",
            )
    finally:
        conn.close()


def test_transcript_overflow_creates_evidence_backed_episode_without_deleting_events(tmp_path, monkeypatch):
    _, _, workspace_id, bootstrap, conn = _company(tmp_path, monkeypatch)
    try:
        group = next(item for item in bootstrap["conversations"] if item["name"] == "内容经营群")
        agent = bootstrap["agents"][0]
        for index in range(26):
            add_message(
                conn,
                conversation_id=group["id"],
                sender_type="agent",
                sender_id=agent["id"],
                content=f"历史事实 {index}：平台案例和受众反馈。",
            )
        workspace = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        conversation = conn.execute("SELECT * FROM conversations WHERE id = ?", (group["id"],)).fetchone()
        agent_row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent["id"],)).fetchone()
        build_context_manifest(
            conn,
            workspace=workspace,
            conversation=conversation,
            agent=agent_row,
            current_text="回顾历史案例",
        )
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM company_events WHERE workspace_id = ? AND event_type = 'message'",
            (workspace_id,),
        ).fetchone()["count"] >= 26
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM agent_memories WHERE workspace_id = ? AND memory_type = 'episode'",
            (workspace_id,),
        ).fetchone()["count"] == 1
    finally:
        conn.close()


def test_employee_prompt_uses_colleague_semantics_without_runtime_identity_terms():
    request = HermesRunContext(
        company_name="记忆公司",
        agent=HermesRunAgent(
            id="internal-id",
            name="内容策划",
            role="内容策划",
            prompt="基于证据制定内容计划。",
        ),
        messages=[HermesRunMessage(role="user", content="整理本周选题")],
    )
    prompt = build_hermes_context_prompt(request)
    assert "公司中的一名同事" in prompt
    assert all(term not in prompt for term in ("AI 员工", "agent_id", "sender_type", "Hermes"))


def test_obsidian_sync_is_managed_area_idempotent_and_event_backed(tmp_path, monkeypatch):
    _, _, workspace_id, _, conn = _company(tmp_path, monkeypatch)
    try:
        first = sync_obsidian_documents(
            conn,
            workspace_id=workspace_id,
            created_by="user",
            documents=[
                {
                    "relative_path": "brand/positioning.md",
                    "title": "品牌定位",
                    "content": "服务忙碌上班族。",
                }
            ],
        )
        assert first == {"created": 1, "updated": 0, "unchanged": 0}
        second = sync_obsidian_documents(
            conn,
            workspace_id=workspace_id,
            created_by="user",
            documents=[
                {
                    "relative_path": "brand/positioning.md",
                    "title": "品牌定位",
                    "content": "服务忙碌上班族。",
                }
            ],
        )
        assert second == {"created": 0, "updated": 0, "unchanged": 1}
        with pytest.raises(ValueError):
            sync_obsidian_documents(
                conn,
                workspace_id=workspace_id,
                created_by="user",
                documents=[{"relative_path": "../secret.md", "title": "越界", "content": "x"}],
            )
        row = conn.execute(
            "SELECT origin, source_ref FROM knowledge_sources WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        assert row["origin"] == "obsidian"
        assert row["source_ref"] == "brand/positioning.md"
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM company_events WHERE workspace_id = ? AND event_type = 'knowledge'",
            (workspace_id,),
        ).fetchone()["count"] == 1
    finally:
        conn.close()
