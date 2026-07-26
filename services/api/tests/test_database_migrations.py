"""Regression coverage for additive SQLite schema upgrades."""

from __future__ import annotations

import sqlite3

from app.core.config import settings
from app.core.database import _translate_placeholders, connect, init_db


def test_postgres_placeholder_translation_escapes_literal_percent():
    translated = _translate_placeholders(
        "SELECT id FROM tasks WHERE status LIKE 'waiting_%' AND owner_id = ?",
        "postgres",
    )

    assert translated == (
        "SELECT id FROM tasks WHERE status LIKE 'waiting_%%' AND owner_id = %s"
    )


def test_postgres_placeholder_translation_preserves_quoted_question_mark():
    translated = _translate_placeholders(
        "SELECT 'what?' AS label, id FROM tasks WHERE owner_id = ?",
        "postgres",
    )

    assert translated == "SELECT 'what?' AS label, id FROM tasks WHERE owner_id = %s"


def test_legacy_runs_input_message_column_becomes_nullable(tmp_path, monkeypatch):
    """Control Hermes Runs must not require a user-message foreign key."""
    database_path = tmp_path / "legacy-runs.sqlite3"
    raw = sqlite3.connect(database_path)
    raw.executescript(
        """
        CREATE TABLE runs (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL,
          conversation_id TEXT NOT NULL,
          agent_id TEXT NOT NULL,
          status TEXT NOT NULL,
          input_message_id TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE run_steps (
          id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
          type TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT '',
          title TEXT NOT NULL DEFAULT '',
          detail TEXT NOT NULL DEFAULT '',
          payload_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL
        );
        INSERT INTO runs VALUES ('run_legacy', 'ws', 'conv', 'agent', 'completed', 'msg', '2026-01-01T00:00:00Z');
        """
    )
    raw.close()
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{database_path}")

    init_db()

    conn = connect()
    try:
        columns = conn.execute("PRAGMA table_info(runs)").fetchall()
        input_message = next(column for column in columns if column["name"] == "input_message_id")
        assert input_message["notnull"] == 0
        assert conn.execute("SELECT id FROM runs WHERE id = 'run_legacy'").fetchone() is not None
        foreign_key = conn.execute("PRAGMA foreign_key_list(run_steps)").fetchone()
        assert foreign_key["table"] == "runs"
        conn.execute(
            """INSERT INTO run_steps (
              id, run_id, type, status, title, detail, payload_json, created_at
            ) VALUES ('step_legacy', 'run_legacy', 'status', '', '', '', '{}', '2026-01-01T00:00:00Z')"""
        )
        conn.commit()
    finally:
        conn.close()
