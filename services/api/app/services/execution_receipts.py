"""Durable receipts for actions that changed or inspected company state.

The receipt is the server-side source of truth for tool execution. Model text
is never treated as evidence that a handler ran.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from app.core.database import Database
from app.services.workspace import new_id, now_iso


_SENSITIVE_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "authorization",
    "api_key",
    "apikey",
)


def _ensure_table(conn: Database) -> None:
    """Support focused tests that use a hand-written database schema."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_receipts (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL,
          run_id TEXT,
          agent_id TEXT,
          tool_name TEXT NOT NULL,
          arguments_json TEXT NOT NULL DEFAULT '{}',
          arguments_hash TEXT NOT NULL,
          status TEXT NOT NULL,
          result_json TEXT NOT NULL DEFAULT '{}',
          error TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          completed_at TEXT
        )
        """
    )


def _redact(value: Any, *, key: str = "") -> Any:
    lowered = key.lower()
    if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value[:100]]
    if isinstance(value, str) and len(value) > 2000:
        return f"{value[:2000]}...[truncated]"
    return value


def _canonical_json(arguments: Mapping[str, Any] | None) -> str:
    return json.dumps(
        dict(arguments or {}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def begin_receipt(
    conn: Database,
    *,
    workspace_id: str,
    agent_id: str | None,
    tool_name: str,
    arguments: Mapping[str, Any] | None,
    run_id: str | None = None,
) -> str:
    _ensure_table(conn)
    canonical = _canonical_json(arguments)
    receipt_id = new_id("receipt")
    conn.execute(
        """
        INSERT INTO execution_receipts (
          id, workspace_id, run_id, agent_id, tool_name,
          arguments_json, arguments_hash, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'started', ?)
        """,
        (
            receipt_id,
            workspace_id,
            run_id,
            agent_id,
            tool_name,
            json.dumps(_redact(dict(arguments or {})), ensure_ascii=False),
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            now_iso(),
        ),
    )
    return receipt_id


def finish_receipt(
    conn: Database,
    receipt_id: str,
    *,
    status: str,
    result: Any = None,
    error: str = "",
) -> None:
    if status not in {"succeeded", "failed", "rejected"}:
        raise ValueError(f"invalid execution receipt status: {status}")
    conn.execute(
        """
        UPDATE execution_receipts
        SET status = ?, result_json = ?, error = ?, completed_at = ?
        WHERE id = ?
        """,
        (
            status,
            json.dumps(_redact(result if result is not None else {}), ensure_ascii=False),
            error[:2000],
            now_iso(),
            receipt_id,
        ),
    )


def get_receipt(conn: Database, receipt_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM execution_receipts WHERE id = ?", (receipt_id,)
    ).fetchone()
    return dict(row) if row else None
