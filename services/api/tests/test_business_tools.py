from __future__ import annotations

import asyncio
import json
import os
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import Database, _upgrade_approval_type_check, connect, init_db
from app.runtime.hermes_client import AgentEvent, RunContext
from app.runtime.profile_provisioner import RecordOnlyProvisioner
from app.runtime.business_tools_auth import (
    create_business_tool_token,
    decode_business_tool_token,
)
from app.runtime.runner import start_run
from app.runtime.runs import create_run, transition_run
from app.services.business_actions import (
    BusinessActionWorker,
    BusinessToolError,
    authorize_tool,
    create_or_reuse_action,
    expire_pending_actions,
    resolve_business_approval,
)
from app.services.credentials import (
    CredentialError,
    decrypt_value,
    delete_credential,
    get_credential,
    put_credential,
    reconcile_all_capability_credentials,
)
from app.services.email_providers import send_resend_email
from app.services.workspace import create_task, create_workspace_for_user, new_id, now_iso
from app.main import app


def _context(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'business.sqlite3'}")
    monkeypatch.setattr(settings, "business_action_max_attempts", 2)
    init_db()
    conn = connect()
    conn.execute(
        "INSERT INTO users (id, email, password_hash, display_name, created_at) "
        "VALUES ('owner', 'owner@example.com', 'x', '老板', ?)",
        (now_iso(),),
    )
    workspace = create_workspace_for_user(conn, "owner", "内容公司")
    agent = conn.execute(
        "SELECT * FROM agents WHERE workspace_id = ? AND role = '运营执行'",
        (workspace["id"],),
    ).fetchone()
    conversation = conn.execute(
        "SELECT * FROM conversations WHERE workspace_id = ? AND agent_id = ?",
        (workspace["id"], agent["id"]),
    ).fetchone()
    timestamp = now_iso()
    conn.execute(
        """INSERT INTO agent_specs (
          id, agent_id, workspace_id, role_name, source_request,
          responsibilities_json, hermes_profile, status, created_at, updated_at
        ) VALUES (?, ?, ?, '运营执行', 'test', '[]', 'ops', 'ready', ?, ?)""",
        (new_id("spec"), agent["id"], workspace["id"], timestamp, timestamp),
    )
    conn.execute(
        """INSERT INTO agent_capabilities (
          id, agent_id, workspace_id, capability_key, skill_refs_json,
          toolset_refs_json, mcp_refs_json, required_credentials_json,
          risk_gate, status, created_at, updated_at
        ) VALUES (?, ?, ?, 'email_sending', '[]', '[]', '["email_service"]',
          '["EMAIL_API_KEY"]', 'approval', 'credential_missing', ?, ?)""",
        (new_id("cap"), agent["id"], workspace["id"], timestamp, timestamp),
    )
    put_credential(
        conn,
        workspace_id=workspace["id"],
        agent_id=agent["id"],
        credential_name="EMAIL_API_KEY",
        value="re_secret_value",
    )
    conn.execute(
        """INSERT INTO channel_configs (
          id, workspace_id, channel_type, name, token, config_json,
          target_agent_id, target_conversation_id, active, created_at
        ) VALUES ('email_channel', ?, 'email', '品牌邮件', 'email-token', ?, ?, NULL, 1, ?)""",
        (
            workspace["id"],
            json.dumps(
                {
                    "provider": "resend",
                    "from_address": "onboarding@resend.dev",
                    "from_name": "AgentPulse",
                }
            ),
            agent["id"],
            timestamp,
        ),
    )
    run_id = create_run(
        conn,
        workspace_id=workspace["id"],
        conversation_id=conversation["id"],
        agent_id=agent["id"],
        input_message_id=None,
        hermes_profile_id="ops",
        workdir=str(tmp_path),
    )
    transition_run(conn, run_id, "running")
    conn.commit()
    claims = {
        "workspace_id": workspace["id"],
        "conversation_id": conversation["id"],
        "run_id": run_id,
        "agent_id": agent["id"],
        "task_id": None,
    }
    args = {
        "to": ["delivered@resend.dev"],
        "subject": "周计划",
        "body": "本周内容计划已整理完成。",
        "channel_id": "email_channel",
        "reply_to": "",
    }
    return conn, workspace, agent, claims, args


def test_credentials_are_encrypted_and_revocable(tmp_path, monkeypatch):
    conn, workspace, agent, _, _ = _context(tmp_path, monkeypatch)
    try:
        row = conn.execute(
            "SELECT encrypted_value FROM agent_credentials WHERE agent_id = ?",
            (agent["id"],),
        ).fetchone()
        assert "re_secret_value" not in row["encrypted_value"]
        assert get_credential(
            conn, agent_id=agent["id"], credential_name="EMAIL_API_KEY"
        ) == "re_secret_value"
        with pytest.raises(CredentialError):
            decrypt_value(row["encrypted_value"], secret="wrong-secret")
        assert delete_credential(
            conn,
            workspace_id=workspace["id"],
            agent_id=agent["id"],
            credential_name="EMAIL_API_KEY",
        )
        status = conn.execute(
            "SELECT status FROM agent_capabilities WHERE agent_id = ? "
            "AND capability_key = 'email_sending'",
            (agent["id"],),
        ).fetchone()
        assert status["status"] == "credential_missing"

        conn.execute(
            "UPDATE agent_capabilities SET status = 'enabled' WHERE agent_id = ?",
            (agent["id"],),
        )
        reconcile_all_capability_credentials(conn)
        repaired = conn.execute(
            "SELECT status FROM agent_capabilities WHERE agent_id = ? "
            "AND capability_key = 'email_sending'",
            (agent["id"],),
        ).fetchone()
        assert repaired["status"] == "credential_missing"

        timestamp = now_iso()
        conn.execute(
            """INSERT INTO agent_capabilities (
              id, agent_id, workspace_id, capability_key, skill_refs_json,
              toolset_refs_json, mcp_refs_json, required_credentials_json,
              risk_gate, status, created_at, updated_at
            ) VALUES (?, ?, ?, 'email_archive', '[]', '[]', '[]',
              '[\"EMAIL_API_KEY\"]', 'approval', 'credential_missing', ?, ?)""",
            (new_id("cap"), agent["id"], workspace["id"], timestamp, timestamp),
        )
        put_credential(
            conn,
            workspace_id=workspace["id"],
            agent_id=agent["id"],
            credential_name="EMAIL_API_KEY",
            value="re_shared_value",
        )
        shared_statuses = conn.execute(
            "SELECT status FROM agent_capabilities WHERE agent_id = ? "
            "AND capability_key IN ('email_sending', 'email_archive')",
            (agent["id"],),
        ).fetchall()
        assert {row["status"] for row in shared_statuses} == {"enabled"}
    finally:
        conn.close()


def test_business_token_and_run_ownership(tmp_path, monkeypatch):
    conn, _, _, claims, _ = _context(tmp_path, monkeypatch)
    try:
        token = create_business_tool_token(**claims)
        assert decode_business_tool_token(token)["run_id"] == claims["run_id"]
        authorize_tool(conn, claims, "send_email")
        with pytest.raises(BusinessToolError, match="ownership"):
            authorize_tool(conn, {**claims, "agent_id": "other"}, "send_email")
        monkeypatch.setattr(settings, "business_tool_token_ttl_seconds", -1)
        with pytest.raises(ValueError, match="expired"):
            decode_business_tool_token(create_business_tool_token(**claims))
    finally:
        conn.close()


def test_reject_never_calls_provider(tmp_path, monkeypatch):
    conn, _, _, claims, args = _context(tmp_path, monkeypatch)
    calls = []

    async def sender(**kwargs):
        calls.append(kwargs)
        return {"id": "email_should_not_send"}

    try:
        action = create_or_reuse_action(conn, claims, tool_name="send_email", arguments=args)
        approval = conn.execute(
            "SELECT * FROM approvals WHERE id = ?", (action["approval_id"],)
        ).fetchone()
        resolve_business_approval(
            conn,
            approval=dict(approval),
            decision="rejected",
            scope="once",
            resolved_by="owner",
        )
        conn.execute(
            "UPDATE approvals SET status = 'rejected', resolved_by = 'owner', resolved_at = ? "
            "WHERE id = ?",
            (now_iso(), approval["id"]),
        )
        conn.commit()
        worker = BusinessActionWorker(email_sender=sender)
        asyncio.run(worker.tick())
        assert calls == []
        stored = conn.execute(
            "SELECT status FROM business_actions WHERE id = ?", (action["id"],)
        ).fetchone()
        assert stored["status"] == "rejected"
    finally:
        conn.close()


def test_approve_executes_once_and_long_term_policy_bypasses_next_prompt(tmp_path, monkeypatch):
    conn, _, _, claims, args = _context(tmp_path, monkeypatch)
    calls = []

    async def sender(**kwargs):
        calls.append(kwargs)
        return {"id": f"email_{len(calls)}"}

    async def run_worker(worker):
        await worker.tick()
        await asyncio.gather(*worker._active.values())

    try:
        first = create_or_reuse_action(conn, claims, tool_name="send_email", arguments=args)
        approval = conn.execute(
            "SELECT * FROM approvals WHERE id = ?", (first["approval_id"],)
        ).fetchone()
        resolve_business_approval(
            conn,
            approval=dict(approval),
            decision="approved",
            scope="always",
            resolved_by="owner",
        )
        conn.execute(
            "UPDATE approvals SET status = 'approved', resolved_by = 'owner', resolved_at = ? "
            "WHERE id = ?",
            (now_iso(), approval["id"]),
        )
        conn.commit()
        worker = BusinessActionWorker(email_sender=sender)
        asyncio.run(run_worker(worker))
        stored = conn.execute(
            "SELECT * FROM business_actions WHERE id = ?", (first["id"],)
        ).fetchone()
        assert stored["status"] == "succeeded"
        assert stored["external_id"] == "email_1"
        assert calls[0]["idempotency_key"] == f"business-action/{first['id']}"

        transition_run(conn, claims["run_id"], "completed")
        second_run = create_run(
            conn,
            workspace_id=claims["workspace_id"],
            conversation_id=claims["conversation_id"],
            agent_id=claims["agent_id"],
            input_message_id=None,
            hermes_profile_id="ops",
            workdir=str(tmp_path),
        )
        transition_run(conn, second_run, "running")
        second = create_or_reuse_action(
            conn,
            {**claims, "run_id": second_run},
            tool_name="send_email",
            arguments={**args, "subject": "第二封"},
        )
        assert second["status"] == "approved"
        assert second["approval_id"] is None
        conn.commit()
        asyncio.run(run_worker(worker))
        assert len(calls) == 2
    finally:
        conn.close()


def test_prohibited_auto_rejects_always_scope(tmp_path, monkeypatch):
    conn, workspace, agent, claims, _ = _context(tmp_path, monkeypatch)
    try:
        timestamp = now_iso()
        conn.execute(
            """INSERT INTO agent_capabilities (
              id, agent_id, workspace_id, capability_key, skill_refs_json,
              toolset_refs_json, mcp_refs_json, required_credentials_json,
              risk_gate, status, created_at, updated_at
            ) VALUES (?, ?, ?, 'payment_execution', '[]', '[]', '[]',
              '["PAYMENT_API_KEY"]', 'prohibited_auto', 'enabled', ?, ?)""",
            (new_id("cap"), agent["id"], workspace["id"], timestamp, timestamp),
        )
        approval_id = new_id("approval")
        action_id = new_id("bact")
        conn.execute(
            """INSERT INTO approvals (
              id, workspace_id, run_id, conversation_id, agent_id, title,
              description, status, risk_level, type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, '付款', '付款', 'pending', 'high',
              'business_tool', '{}', ?)""",
            (
                approval_id,
                workspace["id"],
                claims["run_id"],
                claims["conversation_id"],
                agent["id"],
                timestamp,
            ),
        )
        conn.execute(
            """INSERT INTO business_actions (
              id, workspace_id, run_id, conversation_id, agent_id,
              capability_key, tool_name, arguments_json, arguments_hash,
              dedupe_key, status, approval_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'payment_execution', 'execute_payment',
              '{}', 'hash', ?, 'pending_approval', ?, ?, ?)""",
            (
                action_id,
                workspace["id"],
                claims["run_id"],
                claims["conversation_id"],
                agent["id"],
                new_id("dedupe"),
                approval_id,
                timestamp,
                timestamp,
            ),
        )
        approval = conn.execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        with pytest.raises(BusinessToolError, match="禁止长期放行"):
            resolve_business_approval(
                conn,
                approval=dict(approval),
                decision="approved",
                scope="always",
                resolved_by="owner",
            )
    finally:
        conn.close()


def test_resend_contract_uses_idempotency_key(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "resend_email_1"})

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await send_resend_email(
                api_key="re_test",
                idempotency_key="business-action/123",
                from_address="onboarding@resend.dev",
                from_name="AgentPulse",
                to=["delivered@resend.dev"],
                subject="测试",
                body="正文",
                client=client,
            )

    result = asyncio.run(run())
    assert result == {"id": "resend_email_1"}
    assert captured["headers"]["Idempotency-Key"] == "business-action/123"
    assert captured["body"]["text"] == "正文"


def test_pending_action_expires_without_provider_call(tmp_path, monkeypatch):
    conn, _, _, claims, args = _context(tmp_path, monkeypatch)
    try:
        action = create_or_reuse_action(
            conn, claims, tool_name="send_email", arguments=args
        )
        conn.execute(
            "UPDATE business_actions SET expires_at = '2000-01-01T00:00:00+00:00' "
            "WHERE id = ?",
            (action["id"],),
        )
        conn.commit()

        expire_pending_actions(conn)
        stored = conn.execute(
            "SELECT status FROM business_actions WHERE id = ?", (action["id"],)
        ).fetchone()
        approval = conn.execute(
            "SELECT status FROM approvals WHERE id = ?", (action["approval_id"],)
        ).fetchone()
        run = conn.execute(
            "SELECT status FROM runs WHERE id = ?", (claims["run_id"],)
        ).fetchone()
        assert stored["status"] == "expired"
        assert approval["status"] == "expired"
        assert run["status"] == "running"
    finally:
        conn.close()


def test_worker_retries_once_with_stable_idempotency_key(tmp_path, monkeypatch):
    conn, _, _, claims, args = _context(tmp_path, monkeypatch)
    calls: list[str] = []

    async def sender(**kwargs):
        calls.append(kwargs["idempotency_key"])
        if len(calls) == 1:
            raise httpx.ReadTimeout("provider timed out")
        return {"id": "email_after_retry"}

    async def execute_two_passes(worker):
        await worker.tick()
        await asyncio.gather(*worker._active.values())
        await worker.tick()
        await asyncio.gather(*worker._active.values())

    try:
        action = create_or_reuse_action(
            conn, claims, tool_name="send_email", arguments=args
        )
        approval = conn.execute(
            "SELECT * FROM approvals WHERE id = ?", (action["approval_id"],)
        ).fetchone()
        resolve_business_approval(
            conn,
            approval=dict(approval),
            decision="approved",
            scope="once",
            resolved_by="owner",
        )
        conn.commit()

        asyncio.run(execute_two_passes(BusinessActionWorker(email_sender=sender)))
        stored = conn.execute(
            "SELECT status, attempt_no, external_id FROM business_actions WHERE id = ?",
            (action["id"],),
        ).fetchone()
        assert stored["status"] == "succeeded"
        assert stored["attempt_no"] == 2
        assert stored["external_id"] == "email_after_retry"
        assert calls == [
            f"business-action/{action['id']}",
            f"business-action/{action['id']}",
        ]
    finally:
        conn.close()


def test_worker_recovers_exhausted_expired_lease_and_unblocks_run(
    tmp_path, monkeypatch
):
    conn, _, _, claims, args = _context(tmp_path, monkeypatch)
    calls = []

    async def sender(**kwargs):
        calls.append(kwargs)
        return {"id": "must_not_send"}

    try:
        action = create_or_reuse_action(
            conn, claims, tool_name="send_email", arguments=args
        )
        conn.execute(
            "UPDATE business_actions SET status = 'executing', attempt_no = ?, "
            "lease_owner = 'dead-worker', lease_expires_at = ? WHERE id = ?",
            (
                settings.business_action_max_attempts,
                "2000-01-01T00:00:00+00:00",
                action["id"],
            ),
        )
        conn.commit()

        asyncio.run(BusinessActionWorker(email_sender=sender).tick())
        stored = conn.execute(
            "SELECT status, lease_owner FROM business_actions WHERE id = ?",
            (action["id"],),
        ).fetchone()
        run = conn.execute(
            "SELECT status FROM runs WHERE id = ?", (claims["run_id"],)
        ).fetchone()
        assert stored["status"] == "failed"
        assert stored["lease_owner"] is None
        assert run["status"] == "running"
        assert calls == []
    finally:
        conn.close()


def test_unimplemented_provider_creates_no_action(tmp_path, monkeypatch):
    conn, workspace, agent, claims, _ = _context(tmp_path, monkeypatch)
    try:
        timestamp = now_iso()
        conn.execute(
            """INSERT INTO agent_capabilities (
              id, agent_id, workspace_id, capability_key, skill_refs_json,
              toolset_refs_json, mcp_refs_json, required_credentials_json,
              risk_gate, status, created_at, updated_at
            ) VALUES (?, ?, ?, 'social_content', '[]', '[]', '[]',
              '[\"PLATFORM_TOKEN\"]', 'approval', 'enabled', ?, ?)""",
            (new_id("cap"), agent["id"], workspace["id"], timestamp, timestamp),
        )
        with pytest.raises(BusinessToolError, match="provider 尚未配置"):
            create_or_reuse_action(
                conn,
                claims,
                tool_name="publish_social_content",
                arguments={"platform": "xiaohongshu", "content": "test"},
            )
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM business_actions"
        ).fetchone()
        assert count["c"] == 0
    finally:
        conn.close()


def test_chat_and_task_runs_receive_isolated_business_mcp(tmp_path, monkeypatch):
    conn, workspace, agent, claims, _ = _context(tmp_path, monkeypatch)

    class CaptureBackend:
        async def run(self, ctx, *, permission_resolver=None):
            yield AgentEvent("final", {"stop_reason": "end_turn"})

    try:
        chat_ctx = RunContext(
            run_id="",
            prompt="send an email",
            workdir=str(tmp_path),
            profile="ops",
            agent_id=agent["id"],
            workspace_id=workspace["id"],
            conversation_id=claims["conversation_id"],
        )
        asyncio.run(
            start_run(
                conn,
                ctx=chat_ctx,
                backend=CaptureBackend(),
                input_message_id=None,
            )
        )
        assert [server["name"] for server in chat_ctx.mcp_servers] == [
            "agentpulse-company",
            "agentpulse-business",
        ]
        chat_token = next(
            server for server in chat_ctx.mcp_servers
            if server["name"] == "agentpulse-business"
        )["headers"]["Authorization"].split()[1]
        assert decode_business_tool_token(chat_token)["task_id"] is None

        task = create_task(
            conn,
            workspace_id=workspace["id"],
            title="发送周报",
            owner_agent_id=agent["id"],
            conversation_id=claims["conversation_id"],
            bypass_gate=True,
        )
        task_ctx = RunContext(
            run_id="",
            prompt="deliver the task",
            workdir=str(tmp_path),
            profile="ops",
            agent_id=agent["id"],
            workspace_id=workspace["id"],
            conversation_id=claims["conversation_id"],
            task_id=task["id"],
            mcp_servers=[
                {
                    "name": "agentpulse-company",
                    "url": "http://127.0.0.1/company",
                    "headers": {"Authorization": "Bearer company-token"},
                }
            ],
        )
        asyncio.run(
            start_run(
                conn,
                ctx=task_ctx,
                backend=CaptureBackend(),
                input_message_id=None,
            )
        )
        assert [server["name"] for server in task_ctx.mcp_servers] == [
            "agentpulse-company",
            "agentpulse-business",
        ]
        task_token = task_ctx.mcp_servers[1]["headers"]["Authorization"].split()[1]
        assert decode_business_tool_token(task_token)["task_id"] == task["id"]
    finally:
        conn.close()


def test_sqlite_approval_check_upgrade_preserves_rows(tmp_path):
    raw = sqlite3.connect(tmp_path / "legacy.sqlite3")
    raw.row_factory = sqlite3.Row
    conn = Database(raw, "sqlite")
    try:
        conn.executescript(
            """
            CREATE TABLE approvals (
              id TEXT PRIMARY KEY,
              workspace_id TEXT NOT NULL,
              run_id TEXT,
              task_id TEXT,
              conversation_id TEXT,
              agent_id TEXT,
              title TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'pending',
              risk_level TEXT NOT NULL DEFAULT 'medium',
              type TEXT NOT NULL DEFAULT 'high_risk'
                CHECK(type IN ('high_risk','clarification','capability_upgrade')),
              payload_json TEXT NOT NULL DEFAULT '{}',
              resolved_by TEXT NOT NULL DEFAULT '',
              resolved_at TEXT,
              created_at TEXT NOT NULL
            );
            INSERT INTO approvals (
              id, workspace_id, title, type, created_at
            ) VALUES ('approval_old', 'ws_old', '旧审批', 'high_risk', '2026-01-01');
            """
        )
        _upgrade_approval_type_check(conn)
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'approvals'"
        ).fetchone()["sql"]
        assert "business_tool" in schema
        assert conn.execute(
            "SELECT title FROM approvals WHERE id = 'approval_old'"
        ).fetchone()["title"] == "旧审批"
    finally:
        conn.close()


def test_public_business_api_credential_approval_and_policy_flow(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        settings, "database_url", f"sqlite:///{tmp_path / 'business-api.sqlite3'}"
    )
    monkeypatch.setattr(settings, "password_iterations", 1_000)
    monkeypatch.setattr(settings, "business_worker_enabled", False)
    init_db()
    client = TestClient(app)

    registered = client.post(
        "/api/auth/register",
        json={
            "email": "business-owner@example.com",
            "password": "agentpulse123",
            "display_name": "老板",
            "workspace_name": "业务测试公司",
        },
    )
    assert registered.status_code == 200
    token = registered.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    bootstrap = client.get("/api/me/bootstrap", headers=headers).json()
    agent = next(item for item in bootstrap["agents"] if item["role"] == "运营执行")
    conversation = next(
        item for item in bootstrap["conversations"] if item["kind"] == "group"
    )

    granted = client.post(
        f"/api/agents/{agent['id']}/capabilities",
        headers=headers,
        json={"capability_key": "email_sending"},
    )
    assert granted.status_code == 200
    assert granted.json()["capability_key"] == "email_sending"
    assert granted.json()["status"] == "credential_missing"

    provisioner = RecordOnlyProvisioner()
    monkeypatch.setattr(
        "app.orchestration.supply.build_provisioner_from_settings",
        lambda: provisioner,
    )
    conn = connect()
    try:
        conn.execute(
            "UPDATE agent_specs SET status = 'blocked_on_credentials' WHERE agent_id = ?",
            (agent["id"],),
        )
        conn.commit()
    finally:
        conn.close()

    credential = client.post(
        f"/api/agents/{agent['id']}/credentials",
        headers=headers,
        json={"credential_name": "EMAIL_API_KEY", "value": "re_never_return_me"},
    )
    assert credential.status_code == 200
    assert "re_never_return_me" not in credential.text
    email_cap = next(
        cap
        for cap in credential.json()["capabilities"]
        if cap["capability_key"] == "email_sending"
    )
    assert email_cap["status"] == "enabled"
    assert email_cap["credential_status"] == {"EMAIL_API_KEY": True}
    assert all(action.action != "write_credentials" for action in provisioner.get_actions())

    invalid_channel = client.post(
        "/api/channels",
        headers=headers,
        json={"channel_type": "email", "name": "无发送人", "config": {}},
    )
    assert invalid_channel.status_code == 400
    channel = client.post(
        "/api/channels",
        headers=headers,
        json={
            "channel_type": "email",
            "name": "品牌邮件",
            "target_agent_id": agent["id"],
            "config": {
                "provider": "resend",
                "from_address": "onboarding@resend.dev",
                "from_name": "AgentPulse",
            },
        },
    )
    assert channel.status_code == 200

    conn = connect()
    try:
        workspace_id = bootstrap["workspace"]["id"]
        run_id = create_run(
            conn,
            workspace_id=workspace_id,
            conversation_id=conversation["id"],
            agent_id=agent["id"],
            input_message_id=None,
            hermes_profile_id="ops",
            workdir=str(tmp_path),
        )
        transition_run(conn, run_id, "running")
        action = create_or_reuse_action(
            conn,
            {
                "workspace_id": workspace_id,
                "conversation_id": conversation["id"],
                "run_id": run_id,
                "agent_id": agent["id"],
                "task_id": None,
            },
            tool_name="send_email",
            arguments={
                "to": ["delivered@resend.dev"],
                "subject": "待老板确认",
                "body": "这封邮件尚未发送。",
                "channel_id": channel.json()["id"],
            },
        )
        conn.commit()
    finally:
        conn.close()

    pending = client.get(
        f"/api/conversations/{conversation['id']}/approvals?status=pending",
        headers=headers,
    )
    assert pending.status_code == 200
    assert pending.json()[0]["type"] == "business_tool"
    assert pending.json()[0]["payload"]["preview"]["subject"] == "待老板确认"

    resolved = client.post(
        f"/api/approvals/{action['approval_id']}/resolve",
        headers=headers,
        json={"status": "approved", "scope": "always"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "approved"
    policies = client.get(
        f"/api/agents/{agent['id']}/business-tool-policies", headers=headers
    )
    assert [item["tool_name"] for item in policies.json()] == ["send_email"]
    actions = client.get(
        f"/api/business-actions?agent_id={agent['id']}", headers=headers
    )
    assert actions.status_code == 200
    assert actions.json()[0]["status"] == "approved"
    assert "re_never_return_me" not in actions.text

    revoked_policy = client.delete(
        f"/api/agents/{agent['id']}/business-tool-policies/send_email",
        headers=headers,
    )
    assert revoked_policy.status_code == 200
    assert client.get(
        f"/api/agents/{agent['id']}/business-tool-policies", headers=headers
    ).json() == []

    revoked_credential = client.delete(
        f"/api/agents/{agent['id']}/credentials/EMAIL_API_KEY", headers=headers
    )
    assert revoked_credential.status_code == 200
    email_cap = next(
        cap
        for cap in revoked_credential.json()["capabilities"]
        if cap["capability_key"] == "email_sending"
    )
    assert email_cap["status"] == "credential_missing"
    assert email_cap["credential_status"] == {"EMAIL_API_KEY": False}


_RESEND_E2E = (
    os.environ.get("HERMES_E2E") == "1"
    and os.environ.get("RESEND_E2E") == "1"
    and bool(os.environ.get("RESEND_API_KEY"))
    and bool(os.environ.get("RESEND_FROM_ADDRESS"))
)


@pytest.mark.skipif(
    not _RESEND_E2E,
    reason=(
        "set HERMES_E2E=1 RESEND_E2E=1 RESEND_API_KEY and "
        "RESEND_FROM_ADDRESS for the guarded real-email test"
    ),
)
def test_real_resend_reject_then_send_once_across_worker_restart(tmp_path, monkeypatch):
    conn, _, agent, claims, args = _context(tmp_path, monkeypatch)

    async def execute(worker):
        await worker.tick()
        await asyncio.gather(*worker._active.values())

    try:
        put_credential(
            conn,
            workspace_id=claims["workspace_id"],
            agent_id=agent["id"],
            credential_name="EMAIL_API_KEY",
            value=os.environ["RESEND_API_KEY"],
        )
        config = {
            "provider": "resend",
            "from_address": os.environ["RESEND_FROM_ADDRESS"],
            "from_name": "AgentPulse E2E",
        }
        conn.execute(
            "UPDATE channel_configs SET config_json = ? WHERE id = 'email_channel'",
            (json.dumps(config),),
        )

        rejected = create_or_reuse_action(
            conn, claims, tool_name="send_email", arguments=args
        )
        rejection = conn.execute(
            "SELECT * FROM approvals WHERE id = ?", (rejected["approval_id"],)
        ).fetchone()
        resolve_business_approval(
            conn,
            approval=dict(rejection),
            decision="rejected",
            scope="once",
            resolved_by="owner",
        )
        conn.commit()
        asyncio.run(execute(BusinessActionWorker()))
        rejected_row = conn.execute(
            "SELECT status, external_id FROM business_actions WHERE id = ?",
            (rejected["id"],),
        ).fetchone()
        assert rejected_row["status"] == "rejected"
        assert rejected_row["external_id"] == ""

        approved = create_or_reuse_action(
            conn, claims, tool_name="send_email", arguments=args
        )
        approval = conn.execute(
            "SELECT * FROM approvals WHERE id = ?", (approved["approval_id"],)
        ).fetchone()
        resolve_business_approval(
            conn,
            approval=dict(approval),
            decision="approved",
            scope="once",
            resolved_by="owner",
        )
        conn.commit()
        asyncio.run(execute(BusinessActionWorker()))
        sent = conn.execute(
            "SELECT status, external_id FROM business_actions WHERE id = ?",
            (approved["id"],),
        ).fetchone()
        assert sent["status"] == "succeeded"
        assert sent["external_id"]

        asyncio.run(execute(BusinessActionWorker()))
        after_restart = conn.execute(
            "SELECT status, external_id, attempt_no FROM business_actions WHERE id = ?",
            (approved["id"],),
        ).fetchone()
        assert dict(after_restart) == {
            "status": "succeeded",
            "external_id": sent["external_id"],
            "attempt_no": 1,
        }
    finally:
        conn.close()
