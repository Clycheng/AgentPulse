from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.company_tools_mcp import company_tools_app
from app.core.config import settings
from app.core.database import connect, init_db
from app.orchestration.brief import create_brief
from app.runtime.company_tools_auth import (
    create_company_tool_token,
    decode_company_tool_token,
)
from app.runtime.runner import make_bridge_resolver
from app.runtime.runs import transition_run
from app.schemas.content_package import ContentPackageV1
from app.services import company_tools
from app.services.content_packages import content_package_markdown
from app.services.task_plans import launch_brief
from app.services.workspace import create_workspace_for_user, new_id, now_iso


def _context(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'tools.sqlite3'}")
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
            (new_id("spec"), agent["id"], workspace["id"], agent["role"], f"tool-{index}", now_iso(), now_iso()),
        )
    items = [
        {"key": "research", "title": "研究", "description": "整理资料", "owner_agent_id": agents[1]["id"], "expected_output": "研究摘要", "output_type": "markdown", "depends_on_keys": [], "final_delivery": False},
        {"key": "writing", "title": "写作", "description": "形成草稿", "owner_agent_id": agents[2]["id"], "expected_output": "内容草稿", "output_type": "markdown", "depends_on_keys": ["research"], "final_delivery": False},
        {"key": "package", "title": "组包", "description": "整理内容包", "owner_agent_id": agents[3]["id"], "expected_output": "内容包", "output_type": "content_package_v1", "depends_on_keys": ["writing"], "final_delivery": True},
    ]
    brief = create_brief(
        conn,
        workspace_id=workspace["id"], discussion_conversation_id=group["id"],
        goal="小红书周计划", scope="一周三篇", constraints="不发布",
        success_criteria="合法内容包", owner_agent_id=agents[0]["id"],
        participant_agent_ids=[agent["id"] for agent in agents], work_items=items,
        created_by_agent_id=agents[0]["id"],
    )
    plan = launch_brief(
        conn, workspace_id=workspace["id"], brief_id=brief["id"],
        confirmed_by_user_id="user",
    )
    task = conn.execute(
        "SELECT * FROM tasks WHERE task_plan_id = ? AND plan_item_key = 'research'",
        (plan["id"],),
    ).fetchone()
    run = conn.execute("SELECT * FROM runs WHERE task_id = ?", (task["id"],)).fetchone()
    transition_run(conn, run["id"], "running")
    conn.execute("UPDATE runs SET started_at = ? WHERE id = ?", (now_iso(), run["id"]))
    conn.execute(
        """INSERT INTO knowledge_sources (
          id, workspace_id, title, category, content, created_by, created_at, updated_at
        ) VALUES ('knowledge_brand', ?, '品牌定位', '品牌资料', '面向忙碌上班族的高效饮食方案', '老板', ?, ?)""",
        (workspace["id"], now_iso(), now_iso()),
    )
    conn.commit()
    claims = {
        "workspace_id": workspace["id"], "plan_id": plan["id"],
        "task_id": task["id"], "run_id": run["id"], "agent_id": task["owner_agent_id"],
        "conversation_id": task["conversation_id"],
    }
    return conn, workspace, agents, plan, task, run, claims


def test_company_tool_token_expiry_and_run_ownership(tmp_path, monkeypatch):
    conn, _, _, _, _, _, claims = _context(tmp_path, monkeypatch)
    try:
        token = create_company_tool_token(**claims)
        assert decode_company_tool_token(token)["task_id"] == claims["task_id"]
        company_tools.authorize_run(conn, claims)
        with pytest.raises(company_tools.CompanyToolError, match="ownership"):
            company_tools.authorize_run(conn, {**claims, "agent_id": "other"})

        monkeypatch.setattr(settings, "company_tool_token_ttl_seconds", -1)
        expired = create_company_tool_token(**claims)
        with pytest.raises(ValueError, match="expired"):
            decode_company_tool_token(expired)
    finally:
        conn.close()


def test_company_tools_progress_output_adjustment_limit_and_block(tmp_path, monkeypatch):
    conn, _, agents, plan, task, _, claims = _context(tmp_path, monkeypatch)
    try:
        found = company_tools.search_company_knowledge(conn, claims, query="上班族")
        assert found[0]["id"] == "knowledge_brand"
        assert company_tools.report_progress(conn, claims, progress=42, summary="研究完成一半")["progress"] == 42
        output = company_tools.submit_output(
            conn, claims, title="研究摘要", output_type="markdown", content="# 摘要"
        )
        assert output["output_id"]

        subtask = company_tools.create_subtask(
            conn, claims, title="补充竞品", description="补充两个竞品",
            owner_agent_id=agents[2]["id"], expected_output="竞品摘要",
        )
        assert subtask["task_id"]
        support = company_tools.request_support(
            conn, claims, agent_id=agents[3]["id"], request="补充素材建议",
            expected_output="素材清单",
        )
        assert support["task_id"]
        with pytest.raises(company_tools.CompanyToolError, match="limit"):
            company_tools.create_subtask(
                conn, claims, title="第三次调整", description="越界",
                owner_agent_id=agents[2]["id"], expected_output="不应创建",
            )
        blocked = company_tools.block_task(conn, claims, reason="缺少品牌禁用词")
        assert blocked["status"] == "blocked"
        stored = conn.execute("SELECT * FROM task_plans WHERE id = ?", (plan["id"],)).fetchone()
        assert stored["revision_count"] == 2
        assert stored["status"] == "blocked"
        conn.commit()
    finally:
        conn.close()


def test_content_package_schema_sources_and_markdown_export():
    package = ContentPackageV1.model_validate(
        {
            "platform": "小红书",
            "audience": "上班族",
            "objective": "收藏",
            "schedule": [
                {
                    "publish_at": "2026-07-27 12:00", "order": 1,
                    "content_type": "图文", "title": "午餐计划", "hook": "午餐吃什么？",
                    "body": "正文", "cta": "收藏", "asset_suggestion": "成品图",
                    "source_refs": ["knowledge_brand"],
                }
            ],
            "sources": [{"id": "knowledge_brand", "title": "品牌定位"}],
            "assumptions": ["发布时间待确认"],
        }
    )
    markdown = content_package_markdown(package)
    assert "# 小红书 内容发布计划" in markdown
    assert "knowledge_brand" in markdown
    invalid = package.model_dump()
    invalid["schedule"][0]["source_refs"] = ["missing"]
    with pytest.raises(ValidationError, match="unknown sources"):
        ContentPackageV1.model_validate(invalid)


def test_approval_resolver_observes_cross_connection_and_times_out(tmp_path, monkeypatch):
    conn, workspace, _, _, task, run, claims = _context(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "approval_bridge_timeout_seconds", 1)

    async def approved():
        approval_id = new_id("appr")
        conn.execute(
            """INSERT INTO approvals (
              id, workspace_id, run_id, task_id, conversation_id, agent_id,
              title, description, status, risk_level, type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, '确认', '', 'pending', 'high', 'high_risk', '{}', ?)""",
            (approval_id, workspace["id"], run["id"], task["id"], task["conversation_id"], claims["agent_id"], now_iso()),
        )
        conn.commit()
        wait = asyncio.create_task(make_bridge_resolver(conn)({"approval_id": approval_id}))
        await asyncio.sleep(0.05)
        other = connect()
        try:
            other.execute(
                "UPDATE approvals SET status = 'approved', payload_json = '{\"scope\":\"always\"}' WHERE id = ?",
                (approval_id,),
            )
            other.commit()
        finally:
            other.close()
        assert await wait == "allow_always"

    asyncio.run(approved())

    monkeypatch.setattr(settings, "approval_bridge_timeout_seconds", 0.1)
    timeout_id = new_id("appr")
    conn.execute(
        """INSERT INTO approvals (
          id, workspace_id, run_id, task_id, conversation_id, agent_id,
          title, description, status, risk_level, type, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, '超时', '', 'pending', 'high', 'high_risk', '{}', ?)""",
        (timeout_id, workspace["id"], run["id"], task["id"], task["conversation_id"], claims["agent_id"], now_iso()),
    )
    conn.commit()
    assert asyncio.run(make_bridge_resolver(conn)({"approval_id": timeout_id})) == "deny"
    assert conn.execute("SELECT status FROM approvals WHERE id = ?", (timeout_id,)).fetchone()["status"] == "expired"
    conn.close()


def test_mcp_streamable_http_requires_token_and_lists_company_tools(tmp_path, monkeypatch):
    conn, _, _, _, _, _, claims = _context(tmp_path, monkeypatch)
    conn.close()
    token = create_company_tool_token(**claims)
    initialize = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(company_tools_app) as client:
        assert client.post("/", json=initialize, headers={"Accept": headers["Accept"]}).status_code == 401
        response = client.post("/", json=initialize, headers=headers)
        assert response.status_code == 200, response.text
        listed = client.post(
            "/",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers={**headers, "MCP-Protocol-Version": "2025-06-18"},
        )
        assert listed.status_code == 200, listed.text
        names = {tool["name"] for tool in listed.json()["result"]["tools"]}
        assert names >= {
            "search_company_knowledge", "report_progress", "submit_output",
            "create_subtask", "request_support", "block_task",
        }
