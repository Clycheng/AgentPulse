"""TD-14R regression tests for fail-closed execution primitives."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app.core.database import Database
from app.core.config import settings
from app.core.database import connect, init_db
from app.api.routes.workspace import complete_agent_reply
from app.runtime.employee_soul import build_employee_soul
from app.runtime.hermes_client import HermesBackendError, _safe_path
from app.runtime.runtime_guard import final_reply_is_grounded, safe_project_path
from app.services.execution_receipts import begin_receipt, finish_receipt, get_receipt
from app.services.local_runtime import redact_local_paths, requires_local_execution
from app.services.workspace import add_message, create_workspace_for_user, now_iso


def test_execution_receipt_redacts_secret_and_records_failure():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db = Database(conn, "sqlite")
    receipt_id = begin_receipt(
        db,
        workspace_id="ws_1",
        agent_id="agent_1",
        tool_name="send_email",
        arguments={"to": ["owner@example.com"], "api_key": "secret-value"},
    )
    finish_receipt(db, receipt_id, status="failed", error="provider unavailable")
    row = get_receipt(db, receipt_id)
    assert row is not None
    assert "secret-value" not in row["arguments_json"]
    assert row["status"] == "failed"


def test_project_path_guard_rejects_escape_and_allows_relative_file(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "README.md").write_text("hello", encoding="utf-8")
    assert safe_project_path(str(root), "README.md") == root / "README.md"
    with pytest.raises(ValueError):
        safe_project_path(str(root), "../outside.txt")


def test_project_path_guard_rejects_symlink_escape(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (root / "outside-link").symlink_to(outside)

    with pytest.raises(ValueError, match="符号链接"):
        safe_project_path(str(root), "outside-link")
    with pytest.raises(HermesBackendError, match="unsafe workdir path"):
        _safe_path(str(root), "outside-link")


def test_employee_soul_is_shared_and_preserves_execution_boundaries():
    soul = build_employee_soul(
        name="小秘",
        role="老板秘书",
        prompt="协助推进公司事务。",
        responsibilities=["整理事项"],
    )
    assert "自行申请能力升级" in soul
    assert "工具未返回真实结果" in soul
    assert "## 自我进步" in soul


def test_non_streaming_local_request_blocks_before_server_hermes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings, "database_url", f"sqlite:///{tmp_path / 'local-block.sqlite3'}"
    )
    init_db()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, display_name, created_at) "
            "VALUES ('owner', 'owner@example.com', 'x', '老板', ?)",
            (now_iso(),),
        )
        workspace = create_workspace_for_user(conn, "owner", "测试公司")
        agent = conn.execute(
            "SELECT * FROM agents WHERE workspace_id = ? ORDER BY created_at LIMIT 1",
            (workspace["id"],),
        ).fetchone()
        conversation = conn.execute(
            "SELECT * FROM conversations WHERE workspace_id = ? AND agent_id = ?",
            (workspace["id"], agent["id"]),
        ).fetchone()
        message = add_message(
            conn,
            conversation_id=conversation["id"],
            sender_type="user",
            sender_id="owner",
            content="读取本机项目 /Users/example/code/agentpulse 并汇报",
        )
        conn.commit()

        reply = asyncio.run(
            complete_agent_reply(
                conn,
                workspace=workspace,
                conversation=conversation,
                conversation_id=conversation["id"],
                agent=agent,
                user_message=message,
            )
        )
        assert "尚未执行" in reply["content"]
        assert "本机" in reply["content"]
        assert conn.execute("SELECT COUNT(*) AS count FROM runs").fetchone()["count"] == 0
    finally:
        conn.close()


def test_success_language_without_receipt_is_not_grounded():
    assert final_reply_is_grounded("尚未执行，等待本机 Worker。", execution_receipts=0)
    assert not final_reply_is_grounded("已读取并完成分析。", execution_receipts=0)
    assert final_reply_is_grounded("已读取 README。", execution_receipts=1)


def test_windows_local_path_is_redacted_and_still_detected():
    source = r"读取 C:\Users\owner\project\README.md"
    redacted = redact_local_paths(source)
    assert r"C:\Users" not in redacted
    assert "[已授权本机项目路径]" in redacted
    assert requires_local_execution(redacted)
