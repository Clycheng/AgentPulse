"""TD-14R regression tests for fail-closed execution primitives."""

from __future__ import annotations

import sqlite3

import pytest

from app.core.database import Database
from app.runtime.dsml import parse_dsml
from app.runtime.runtime_guard import final_reply_is_grounded, safe_project_path
from app.services.execution_receipts import begin_receipt, finish_receipt, get_receipt


def test_valid_dsml_is_normalized_without_control_markers():
    result = parse_dsml(
        """我来处理。<｜｜DSML｜｜tool_calls>
        <｜｜DSML｜｜invoke name="list_agents">
        <｜｜DSML｜｜parameter name="status" string="true">进行中<｜｜DSML｜｜parameter>
        <｜｜DSML｜｜invoke>
        <｜｜DSML｜｜tool_calls>"""
    )
    assert result.errors == []
    assert result.calls[0]["name"] == "list_agents"
    assert "DSML" not in result.clean_text


def test_malformed_dsml_is_rejected():
    result = parse_dsml(
        '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="list_agents">'
    )
    assert result.errors
    assert result.calls == []


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


def test_success_language_without_receipt_is_not_grounded():
    assert final_reply_is_grounded("尚未执行，等待本机 Worker。", execution_receipts=0)
    assert not final_reply_is_grounded("已读取并完成分析。", execution_receipts=0)
    assert final_reply_is_grounded("已读取 README。", execution_receipts=1)
