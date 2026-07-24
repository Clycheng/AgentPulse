from __future__ import annotations

from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import re
import sqlite3

import psycopg
import psycopg_pool
from psycopg.rows import dict_row

from app.core.config import settings
from app.services.templates import seed_official_talent_market


Params = Sequence[Any]


class Row(dict):
    """Tiny dict row that keeps sqlite-style key access for service code."""


class Result:
    def __init__(self, rows: list[Row] | None = None):
        self._rows = rows or []

    def fetchone(self) -> Row | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Row]:
        return self._rows

# ---------------------------------------------------------------------------
# Placeholder translation — replaces `?` with the dialect-native placeholder
# but *not* inside single-quoted strings (avoids breaking string literals
# that happen to contain a literal ? character).
# ---------------------------------------------------------------------------

def _translate_placeholders(sql: str, dialect: str) -> str:
    """Replace ``?`` placeholders with dialect-native markers.

    Skips ``?`` inside single-quoted SQL string literals (e.g. ``'what?'``).
    """
    if dialect == "sqlite":
        return sql
    # PostgreSQL uses %s
    result: list[str] = []
    in_string = False
    for ch in sql:
        if ch == "'":
            in_string = not in_string
            result.append(ch)
        elif ch == "?" and not in_string:
            result.append("%s")
        else:
            result.append(ch)
    return "".join(result)




class Database:
    def __init__(
        self,
        conn: Any,
        dialect: str,
        pool: "psycopg_pool.ConnectionPool | None" = None,
    ):
        self.conn = conn
        self.dialect = dialect
        self._pool = pool
        self._closed = False

    def execute(self, sql: str, params: Params = ()) -> Result:
        if self.dialect == "postgres":
            return self._execute_postgres(sql, params)
        return self._execute_sqlite(sql, params)

    def executescript(self, script: str) -> None:
        if self.dialect == "postgres":
            for statement in split_sql_script(script):
                self.execute(statement)
            return
        self.conn.executescript(script)

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._pool is not None:
            self._pool.putconn(self.conn)
        else:
            self.conn.close()

    def _execute_postgres(self, sql: str, params: Params) -> Result:
        translated_sql = _translate_placeholders(sql, "postgres")
        with self.conn.cursor() as cursor:
            cursor.execute(translated_sql, tuple(params))
            if cursor.description is None:
                return Result()
            return Result([Row(row) for row in cursor.fetchall()])

    def _execute_sqlite(self, sql: str, params: Params) -> Result:
        cursor = self.conn.execute(sql, tuple(params))
        if cursor.description is None:
            return Result()
        return Result([Row(dict(row)) for row in cursor.fetchall()])

# ---------------------------------------------------------------------------
# Connection pooling
# ---------------------------------------------------------------------------

_pg_pool: "psycopg_pool.ConnectionPool | None" = None


def _pg_pool_instance() -> "psycopg_pool.ConnectionPool":
    global _pg_pool
    if _pg_pool is None:
        _pg_pool = psycopg_pool.ConnectionPool(
            settings.database_url,
            kwargs={"row_factory": dict_row},
            min_size=1,
            max_size=10,
            open=True,
        )
    return _pg_pool




def connect() -> Database:
    database_url = settings.database_url
    if is_sqlite_url(database_url):
        return connect_sqlite(database_url)
    pool = _pg_pool_instance()
    conn = pool.getconn()
    return Database(conn, "postgres", pool=pool)


def connect_sqlite(database_url: str) -> Database:
    path = sqlite_path(database_url)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return Database(conn, "sqlite")


def sqlite_path(database_url: str) -> Path:
    raw_path = database_url.removeprefix("sqlite:///")
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def is_sqlite_url(database_url: str) -> bool:
    return database_url.startswith("sqlite:///")


def get_db() -> Generator[Database, None, None]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def shutdown_db() -> None:
    """Close the PostgreSQL pool gracefully (call on app shutdown)."""
    global _pg_pool
    if _pg_pool is not None:
        _pg_pool.close()
        _pg_pool = None




def init_db() -> None:
    conn = connect()
    try:
        if conn.dialect == "postgres":
            init_postgres(conn)
        else:
            init_sqlite(conn)
        from app.services.credentials import reconcile_all_capability_credentials

        reconcile_all_capability_credentials(conn)
        seed_official_talent_market(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_postgres(conn: Database) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY,
          email TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          display_name TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS request_rate_limits (
          bucket TEXT NOT NULL,
          occurred_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rate_limit_bucket_time
          ON request_rate_limits(bucket, occurred_at);

        CREATE TABLE IF NOT EXISTS site_event_daily (
          event_name TEXT NOT NULL,
          event_day TEXT NOT NULL,
          count INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(event_name, event_day)
        );

        CREATE TABLE IF NOT EXISTS official_talent_categories (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          sort_order INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'published',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS official_agent_templates (
          id TEXT PRIMARY KEY,
          category_id TEXT NOT NULL REFERENCES official_talent_categories(id) ON DELETE RESTRICT,
          name TEXT NOT NULL,
          department TEXT NOT NULL DEFAULT '',
          description TEXT NOT NULL DEFAULT '',
          prompt TEXT NOT NULL,
          skills_json TEXT NOT NULL DEFAULT '[]',
          mcps_json TEXT NOT NULL DEFAULT '[]',
          publisher TEXT NOT NULL DEFAULT 'AgentPulse 官方',
          version TEXT NOT NULL DEFAULT 'v0.1.0',
          status TEXT NOT NULL DEFAULT 'published',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS workspaces (
          id TEXT PRIMARY KEY,
          owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          onboarding_completed BOOLEAN NOT NULL DEFAULT FALSE,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS departments (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          parent_id TEXT REFERENCES departments(id) ON DELETE CASCADE,
          sort_order INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          UNIQUE(workspace_id, parent_id, name)
        );

        CREATE TABLE IF NOT EXISTS agents (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          department_id TEXT NOT NULL REFERENCES departments(id) ON DELETE RESTRICT,
          name TEXT NOT NULL,
          role TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          prompt TEXT NOT NULL,
          hue INTEGER NOT NULL DEFAULT 220,
          glyph TEXT NOT NULL DEFAULT '◆',
          status_kind TEXT NOT NULL DEFAULT 'idle',
          status_label TEXT NOT NULL DEFAULT '在线待命',
          joined TEXT NOT NULL DEFAULT '今天入职',
          source TEXT NOT NULL DEFAULT 'custom',
          skills_json TEXT NOT NULL DEFAULT '[]',
          mcps_json TEXT NOT NULL DEFAULT '[]',
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversations (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          kind TEXT NOT NULL CHECK(kind IN ('dm', 'group')),
          name TEXT NOT NULL DEFAULT '',
          agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
          unread INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversation_members (
          id BIGSERIAL PRIMARY KEY,
          conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
          UNIQUE(conversation_id, agent_id)
        );

        CREATE TABLE IF NOT EXISTS messages (
          id TEXT PRIMARY KEY,
          conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          sender_type TEXT NOT NULL CHECK(sender_type IN ('user', 'agent', 'system')),
          sender_id TEXT NOT NULL DEFAULT '',
          content TEXT NOT NULL,
          provider TEXT,
          model TEXT,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          title TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          priority TEXT NOT NULL DEFAULT 'P2',
          owner_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
          status TEXT NOT NULL DEFAULT '进行中',
          progress INTEGER NOT NULL DEFAULT 0,
          conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
          due_date TEXT,
          parent_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
          task_plan_id TEXT,
          plan_item_key TEXT,
          expected_output TEXT NOT NULL DEFAULT '',
          output_type TEXT NOT NULL DEFAULT 'markdown',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS knowledge_sources (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          title TEXT NOT NULL,
          category TEXT NOT NULL DEFAULT '通用资料',
          content TEXT NOT NULL,
          created_by TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS task_events (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
          conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
          agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
          kind TEXT NOT NULL,
          title TEXT NOT NULL,
          content TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS task_outputs (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
          conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
          agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
          title TEXT NOT NULL,
          output_type TEXT NOT NULL DEFAULT 'markdown',
          content TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agent_experiences (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
          task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
          outcome TEXT NOT NULL,
          summary TEXT NOT NULL,
          lessons TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS consensus_briefs (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          discussion_conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'confirmed', 'rejected', 'superseded')),
          goal TEXT NOT NULL,
          scope TEXT NOT NULL DEFAULT '',
          constraints TEXT NOT NULL DEFAULT '',
          success_criteria TEXT NOT NULL DEFAULT '',
          owner_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
          participant_agent_ids_json TEXT NOT NULL DEFAULT '[]',
          work_items_json TEXT NOT NULL DEFAULT '[]',
          created_by_agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
          supersedes_brief_id TEXT REFERENCES consensus_briefs(id) ON DELETE SET NULL,
          derived_from_brief_id TEXT REFERENCES consensus_briefs(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          confirmed_at TEXT,
          confirmed_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
          task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
          status TEXT NOT NULL,
          input_message_id TEXT,
          output_message_id TEXT,
          hermes_profile_id TEXT,
          hermes_run_id TEXT,
          workdir TEXT,
          provider TEXT NOT NULL DEFAULT 'deepseek',
          model TEXT NOT NULL DEFAULT '',
          usage_json TEXT NOT NULL DEFAULT '{}',
          error TEXT NOT NULL DEFAULT '',
          attempt_no INTEGER NOT NULL DEFAULT 1,
          lease_owner TEXT,
          lease_expires_at TEXT,
          started_at TEXT,
          created_at TEXT NOT NULL,
          completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS approvals (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
          task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
          conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
          agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
          title TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'pending',
          risk_level TEXT NOT NULL DEFAULT 'medium',
          type TEXT NOT NULL DEFAULT 'high_risk'
            CHECK(type IN ('high_risk','clarification','capability_upgrade','business_tool')),
          payload_json TEXT NOT NULL DEFAULT '{}',
          resolved_by TEXT NOT NULL DEFAULT '',
          resolved_at TEXT,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS task_plans (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          brief_id TEXT NOT NULL UNIQUE REFERENCES consensus_briefs(id) ON DELETE CASCADE,
          root_task_id TEXT,
          status TEXT NOT NULL DEFAULT 'active'
            CHECK(status IN ('launching','active','blocked','completed','cancelled')),
          revision_count INTEGER NOT NULL DEFAULT 0,
          blocked_reason TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS task_dependencies (
          id TEXT PRIMARY KEY,
          task_plan_id TEXT NOT NULL REFERENCES task_plans(id) ON DELETE CASCADE,
          task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
          depends_on_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
          created_at TEXT NOT NULL,
          UNIQUE(task_id, depends_on_task_id),
          CHECK(task_id <> depends_on_task_id)
        );

        CREATE TABLE IF NOT EXISTS run_steps (
          id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
          type TEXT NOT NULL CHECK(type IN (
            'message','thinking','tool_call','tool_result',
            'approval_required','status','final'
          )),
          status TEXT NOT NULL DEFAULT '',
          title TEXT NOT NULL DEFAULT '',
          detail TEXT NOT NULL DEFAULT '',
          payload_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ideas (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          source_agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
          title TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          category TEXT NOT NULL CHECK(category IN (
            'improvement','opportunity','risk','learning'
          )),
          status TEXT NOT NULL DEFAULT 'new' CHECK(status IN (
            'new','reviewed','accepted','dismissed','converted'
          )),
          converted_brief_id TEXT REFERENCES consensus_briefs(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          reviewed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS channel_configs (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          channel_type TEXT NOT NULL CHECK(channel_type IN (
            'wechat','email','web_widget','generic_webhook'
          )),
          name TEXT NOT NULL,
          token TEXT NOT NULL UNIQUE,
          config_json TEXT NOT NULL DEFAULT '{}',
          target_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
          target_conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agent_specs (
          id TEXT PRIMARY KEY,
          agent_id TEXT NOT NULL UNIQUE REFERENCES agents(id) ON DELETE CASCADE,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          role_name TEXT NOT NULL,
          source_request TEXT NOT NULL DEFAULT '',
          responsibilities_json TEXT NOT NULL DEFAULT '[]',
          hermes_profile TEXT,
          status TEXT NOT NULL DEFAULT 'draft'
            CHECK(status IN ('draft','provisioning','blocked_on_credentials','ready','failed')),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agent_capabilities (
          id TEXT PRIMARY KEY,
          agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          capability_key TEXT NOT NULL,
          skill_refs_json TEXT NOT NULL DEFAULT '[]',
          toolset_refs_json TEXT NOT NULL DEFAULT '[]',
          mcp_refs_json TEXT NOT NULL DEFAULT '[]',
          required_credentials_json TEXT NOT NULL DEFAULT '[]',
          risk_gate TEXT NOT NULL DEFAULT 'auto'
            CHECK(risk_gate IN ('auto','approval','prohibited_auto')),
          status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','credential_missing','enabled','disabled')),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(agent_id, capability_key)
        );

        CREATE TABLE IF NOT EXISTS agent_credentials (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
          credential_name TEXT NOT NULL,
          encrypted_value TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(agent_id, credential_name)
        );

        CREATE TABLE IF NOT EXISTS workspace_model_credentials (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          provider TEXT NOT NULL CHECK(provider IN ('deepseek')),
          encrypted_api_key TEXT NOT NULL,
          model TEXT NOT NULL,
          validation_status TEXT NOT NULL DEFAULT 'valid'
            CHECK(validation_status IN ('valid','invalid')),
          last_validated_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(workspace_id, provider)
        );

        CREATE TABLE IF NOT EXISTS business_tool_policies (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
          tool_name TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1,
          created_by TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          revoked_at TEXT,
          UNIQUE(workspace_id, agent_id, tool_name)
        );

        CREATE TABLE IF NOT EXISTS business_actions (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
          task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
          conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
          agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
          capability_key TEXT NOT NULL,
          tool_name TEXT NOT NULL,
          arguments_json TEXT NOT NULL DEFAULT '{}',
          arguments_hash TEXT NOT NULL,
          dedupe_key TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN (
            'pending_approval','approved','executing','succeeded',
            'rejected','expired','failed'
          )),
          approval_id TEXT UNIQUE REFERENCES approvals(id) ON DELETE SET NULL,
          provider TEXT NOT NULL DEFAULT '',
          external_id TEXT NOT NULL DEFAULT '',
          result_json TEXT NOT NULL DEFAULT '{}',
          error TEXT NOT NULL DEFAULT '',
          attempt_no INTEGER NOT NULL DEFAULT 0,
          lease_owner TEXT,
          lease_expires_at TEXT,
          expires_at TEXT,
          approved_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          completed_at TEXT
        );
        """
    )
    _upgrade_approval_type_check(conn)
    ensure_column(conn, "messages", "provider", "TEXT")
    ensure_column(conn, "messages", "model", "TEXT")
    ensure_column(conn, "tasks", "description", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "tasks", "due_date", "TEXT")
    ensure_column(conn, "tasks", "parent_task_id", "TEXT REFERENCES tasks(id) ON DELETE SET NULL")
    ensure_column(conn, "tasks", "consensus_brief_id", "TEXT REFERENCES consensus_briefs(id) ON DELETE SET NULL")
    ensure_column(conn, "tasks", "task_plan_id", "TEXT REFERENCES task_plans(id) ON DELETE CASCADE")
    ensure_column(conn, "tasks", "plan_item_key", "TEXT")
    ensure_column(conn, "tasks", "expected_output", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "tasks", "output_type", "TEXT NOT NULL DEFAULT 'markdown'")
    ensure_column(conn, "consensus_briefs", "work_items_json", "TEXT NOT NULL DEFAULT '[]'")
    ensure_column(conn, "knowledge_sources", "origin", "TEXT NOT NULL DEFAULT 'manual'")
    ensure_column(conn, "knowledge_sources", "source_ref", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "knowledge_sources", "source_hash", "TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_knowledge_sources_origin_ref "
        "ON knowledge_sources(workspace_id, origin, source_ref) "
        "WHERE source_ref <> ''"
    )
    ensure_column(conn, "conversations", "discussion_status", "TEXT NOT NULL DEFAULT 'discussing' CHECK(discussion_status IN ('discussing', 'aligned'))")
    # TD-03-T1: Run/RunStep data model. New columns are nullable here; RunService
    # (TD-03-T3) enforces "every Run belongs to a Task" + absolute workdir at the
    # application layer once execution actually flows through Hermes.
    ensure_column(conn, "runs", "task_id", "TEXT REFERENCES tasks(id) ON DELETE CASCADE")
    ensure_column(conn, "runs", "hermes_profile_id", "TEXT")
    ensure_column(conn, "runs", "hermes_run_id", "TEXT")
    ensure_column(conn, "runs", "workdir", "TEXT")
    ensure_column(conn, "runs", "attempt_no", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(conn, "runs", "lease_owner", "TEXT")
    ensure_column(conn, "runs", "lease_expires_at", "TEXT")
    ensure_column(conn, "runs", "started_at", "TEXT")
    ensure_column(conn, "runs", "context_manifest_id", "TEXT REFERENCES context_manifests(id) ON DELETE SET NULL")
    ensure_column(conn, "approvals", "run_id", "TEXT REFERENCES runs(id) ON DELETE SET NULL")
    ensure_column(
        conn,
        "approvals",
        "type",
        "TEXT NOT NULL DEFAULT 'high_risk' "
        "CHECK(type IN ('high_risk','clarification','capability_upgrade','business_tool'))",
    )
    ensure_column(
        conn, "approvals", "payload_json", "TEXT NOT NULL DEFAULT '{}'"
    )
    ensure_column(conn, "agents", "hermes_gateway_port", "INTEGER")
    # TD-08-T1: idea center (idle reflection). idle_thinking_enabled stored as
    # 0/1 INTEGER in both dialects (serialized to bool at the API layer).
    ensure_column(conn, "agent_specs", "last_idle_think_at", "TEXT")
    ensure_column(
        conn, "agent_specs", "idle_think_interval_hours", "INTEGER NOT NULL DEFAULT 6"
    )
    ensure_column(
        conn, "agent_specs", "idle_thinking_enabled", "INTEGER NOT NULL DEFAULT 1"
    )
    # TD-06-T1: skill self-sedimentation (reflection cron).
    ensure_column(
        conn, "agent_specs", "runs_since_last_reflection", "INTEGER NOT NULL DEFAULT 0"
    )
    ensure_column(conn, "agent_specs", "last_skill_reflection_at", "TEXT")
    ensure_column(
        conn, "agent_specs", "reflection_interval", "INTEGER NOT NULL DEFAULT 5"
    )
    ensure_column(
        conn, "conversations", "idea_id", "TEXT REFERENCES ideas(id) ON DELETE SET NULL"
    )
    # TD-09-T1: external channel adapters. Conversations remember which channel
    # (and external thread) they came from; messages carry the external id for
    # webhook-redelivery dedup.
    ensure_column(conn, "conversations", "source_channel", "TEXT")
    ensure_column(conn, "conversations", "external_conversation_id", "TEXT")
    ensure_column(conn, "messages", "external_message_id", "TEXT")
    # Multi-level org chart: departments can nest under a parent department
    # (technical dept → backend center → data group, à la DingTalk/Feishu),
    # instead of the flat list that forced "A/B/C" path strings into `name`.
    ensure_column(
        conn, "departments", "parent_id", "TEXT REFERENCES departments(id) ON DELETE CASCADE"
    )
    if conn.dialect == "postgres":
        conn.execute("ALTER TABLE runs ALTER COLUMN input_message_id DROP NOT NULL")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_runs_task_attempt "
        "ON runs(task_id, attempt_no) WHERE task_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_plan_item "
        "ON tasks(task_plan_id, plan_item_key) WHERE task_plan_id IS NOT NULL "
        "AND plan_item_key IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_business_actions_active_dedupe "
        "ON business_actions(dedupe_key) WHERE status IN "
        "('pending_approval','approved','executing','succeeded')"
    )
    _init_td13_tables(conn)
    _init_td14_tables(conn)


def init_sqlite(conn: Database) -> None:
    _upgrade_approval_type_check(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY,
          email TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          display_name TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS request_rate_limits (
          bucket TEXT NOT NULL,
          occurred_at BIGINT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rate_limit_bucket_time
          ON request_rate_limits(bucket, occurred_at);

        CREATE TABLE IF NOT EXISTS site_event_daily (
          event_name TEXT NOT NULL,
          event_day TEXT NOT NULL,
          count BIGINT NOT NULL DEFAULT 0,
          PRIMARY KEY(event_name, event_day)
        );

        CREATE TABLE IF NOT EXISTS official_talent_categories (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          sort_order INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'published',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS official_agent_templates (
          id TEXT PRIMARY KEY,
          category_id TEXT NOT NULL REFERENCES official_talent_categories(id) ON DELETE RESTRICT,
          name TEXT NOT NULL,
          department TEXT NOT NULL DEFAULT '',
          description TEXT NOT NULL DEFAULT '',
          prompt TEXT NOT NULL,
          skills_json TEXT NOT NULL DEFAULT '[]',
          mcps_json TEXT NOT NULL DEFAULT '[]',
          publisher TEXT NOT NULL DEFAULT 'AgentPulse 官方',
          version TEXT NOT NULL DEFAULT 'v0.1.0',
          status TEXT NOT NULL DEFAULT 'published',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS workspaces (
          id TEXT PRIMARY KEY,
          owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          onboarding_completed INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS departments (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          parent_id TEXT REFERENCES departments(id) ON DELETE CASCADE,
          sort_order INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          UNIQUE(workspace_id, parent_id, name)
        );

        CREATE TABLE IF NOT EXISTS agents (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          department_id TEXT NOT NULL REFERENCES departments(id) ON DELETE RESTRICT,
          name TEXT NOT NULL,
          role TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          prompt TEXT NOT NULL,
          hue INTEGER NOT NULL DEFAULT 220,
          glyph TEXT NOT NULL DEFAULT '◆',
          status_kind TEXT NOT NULL DEFAULT 'idle',
          status_label TEXT NOT NULL DEFAULT '在线待命',
          joined TEXT NOT NULL DEFAULT '今天入职',
          source TEXT NOT NULL DEFAULT 'custom',
          skills_json TEXT NOT NULL DEFAULT '[]',
          mcps_json TEXT NOT NULL DEFAULT '[]',
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversations (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          kind TEXT NOT NULL CHECK(kind IN ('dm', 'group')),
          name TEXT NOT NULL DEFAULT '',
          agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
          unread INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversation_members (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
          UNIQUE(conversation_id, agent_id)
        );

        CREATE TABLE IF NOT EXISTS messages (
          id TEXT PRIMARY KEY,
          conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          sender_type TEXT NOT NULL CHECK(sender_type IN ('user', 'agent', 'system')),
          sender_id TEXT NOT NULL DEFAULT '',
          content TEXT NOT NULL,
          provider TEXT,
          model TEXT,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          title TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          priority TEXT NOT NULL DEFAULT 'P2',
          owner_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
          status TEXT NOT NULL DEFAULT '进行中',
          progress INTEGER NOT NULL DEFAULT 0,
          conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
          due_date TEXT,
          parent_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
          task_plan_id TEXT,
          plan_item_key TEXT,
          expected_output TEXT NOT NULL DEFAULT '',
          output_type TEXT NOT NULL DEFAULT 'markdown',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS knowledge_sources (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          title TEXT NOT NULL,
          category TEXT NOT NULL DEFAULT '通用资料',
          content TEXT NOT NULL,
          created_by TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS task_events (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
          conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
          agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
          kind TEXT NOT NULL,
          title TEXT NOT NULL,
          content TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS task_outputs (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
          conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
          agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
          title TEXT NOT NULL,
          output_type TEXT NOT NULL DEFAULT 'markdown',
          content TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS approvals (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
          task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
          conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
          agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
          title TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'pending',
          risk_level TEXT NOT NULL DEFAULT 'medium',
          type TEXT NOT NULL DEFAULT 'high_risk'
            CHECK(type IN ('high_risk','clarification','capability_upgrade','business_tool')),
          payload_json TEXT NOT NULL DEFAULT '{}',
          resolved_by TEXT NOT NULL DEFAULT '',
          resolved_at TEXT,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agent_experiences (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
          task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
          outcome TEXT NOT NULL,
          summary TEXT NOT NULL,
          lessons TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS consensus_briefs (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          discussion_conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'confirmed', 'rejected', 'superseded')),
          goal TEXT NOT NULL,
          scope TEXT NOT NULL DEFAULT '',
          constraints TEXT NOT NULL DEFAULT '',
          success_criteria TEXT NOT NULL DEFAULT '',
          owner_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
          participant_agent_ids_json TEXT NOT NULL DEFAULT '[]',
          work_items_json TEXT NOT NULL DEFAULT '[]',
          created_by_agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
          supersedes_brief_id TEXT REFERENCES consensus_briefs(id) ON DELETE SET NULL,
          derived_from_brief_id TEXT REFERENCES consensus_briefs(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          confirmed_at TEXT,
          confirmed_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
          task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
          status TEXT NOT NULL,
          input_message_id TEXT,
          output_message_id TEXT,
          hermes_profile_id TEXT,
          hermes_run_id TEXT,
          workdir TEXT,
          provider TEXT NOT NULL DEFAULT 'deepseek',
          model TEXT NOT NULL DEFAULT '',
          usage_json TEXT NOT NULL DEFAULT '{}',
          error TEXT NOT NULL DEFAULT '',
          attempt_no INTEGER NOT NULL DEFAULT 1,
          lease_owner TEXT,
          lease_expires_at TEXT,
          started_at TEXT,
          created_at TEXT NOT NULL,
          completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS task_plans (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          brief_id TEXT NOT NULL UNIQUE REFERENCES consensus_briefs(id) ON DELETE CASCADE,
          root_task_id TEXT,
          status TEXT NOT NULL DEFAULT 'active'
            CHECK(status IN ('launching','active','blocked','completed','cancelled')),
          revision_count INTEGER NOT NULL DEFAULT 0,
          blocked_reason TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS task_dependencies (
          id TEXT PRIMARY KEY,
          task_plan_id TEXT NOT NULL REFERENCES task_plans(id) ON DELETE CASCADE,
          task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
          depends_on_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
          created_at TEXT NOT NULL,
          UNIQUE(task_id, depends_on_task_id),
          CHECK(task_id <> depends_on_task_id)
        );

        CREATE TABLE IF NOT EXISTS run_steps (
          id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
          type TEXT NOT NULL CHECK(type IN (
            'message','thinking','tool_call','tool_result',
            'approval_required','status','final'
          )),
          status TEXT NOT NULL DEFAULT '',
          title TEXT NOT NULL DEFAULT '',
          detail TEXT NOT NULL DEFAULT '',
          payload_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ideas (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          source_agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
          title TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          category TEXT NOT NULL CHECK(category IN (
            'improvement','opportunity','risk','learning'
          )),
          status TEXT NOT NULL DEFAULT 'new' CHECK(status IN (
            'new','reviewed','accepted','dismissed','converted'
          )),
          converted_brief_id TEXT REFERENCES consensus_briefs(id) ON DELETE SET NULL,
          created_at TEXT NOT NULL,
          reviewed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS channel_configs (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          channel_type TEXT NOT NULL CHECK(channel_type IN (
            'wechat','email','web_widget','generic_webhook'
          )),
          name TEXT NOT NULL,
          token TEXT NOT NULL UNIQUE,
          config_json TEXT NOT NULL DEFAULT '{}',
          target_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
          target_conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agent_specs (
          id TEXT PRIMARY KEY,
          agent_id TEXT NOT NULL UNIQUE REFERENCES agents(id) ON DELETE CASCADE,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          role_name TEXT NOT NULL,
          source_request TEXT NOT NULL DEFAULT '',
          responsibilities_json TEXT NOT NULL DEFAULT '[]',
          hermes_profile TEXT,
          status TEXT NOT NULL DEFAULT 'draft'
            CHECK(status IN ('draft','provisioning','blocked_on_credentials','ready','failed')),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agent_capabilities (
          id TEXT PRIMARY KEY,
          agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          capability_key TEXT NOT NULL,
          skill_refs_json TEXT NOT NULL DEFAULT '[]',
          toolset_refs_json TEXT NOT NULL DEFAULT '[]',
          mcp_refs_json TEXT NOT NULL DEFAULT '[]',
          required_credentials_json TEXT NOT NULL DEFAULT '[]',
          risk_gate TEXT NOT NULL DEFAULT 'auto'
            CHECK(risk_gate IN ('auto','approval','prohibited_auto')),
          status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','credential_missing','enabled','disabled')),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(agent_id, capability_key)
        );

        CREATE TABLE IF NOT EXISTS agent_credentials (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
          credential_name TEXT NOT NULL,
          encrypted_value TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(agent_id, credential_name)
        );

        CREATE TABLE IF NOT EXISTS workspace_model_credentials (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          provider TEXT NOT NULL CHECK(provider IN ('deepseek')),
          encrypted_api_key TEXT NOT NULL,
          model TEXT NOT NULL,
          validation_status TEXT NOT NULL DEFAULT 'valid'
            CHECK(validation_status IN ('valid','invalid')),
          last_validated_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(workspace_id, provider)
        );

        CREATE TABLE IF NOT EXISTS business_tool_policies (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
          tool_name TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1,
          created_by TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          revoked_at TEXT,
          UNIQUE(workspace_id, agent_id, tool_name)
        );

        CREATE TABLE IF NOT EXISTS business_actions (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
          task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
          conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
          agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
          capability_key TEXT NOT NULL,
          tool_name TEXT NOT NULL,
          arguments_json TEXT NOT NULL DEFAULT '{}',
          arguments_hash TEXT NOT NULL,
          dedupe_key TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN (
            'pending_approval','approved','executing','succeeded',
            'rejected','expired','failed'
          )),
          approval_id TEXT UNIQUE REFERENCES approvals(id) ON DELETE SET NULL,
          provider TEXT NOT NULL DEFAULT '',
          external_id TEXT NOT NULL DEFAULT '',
          result_json TEXT NOT NULL DEFAULT '{}',
          error TEXT NOT NULL DEFAULT '',
          attempt_no INTEGER NOT NULL DEFAULT 0,
          lease_owner TEXT,
          lease_expires_at TEXT,
          expires_at TEXT,
          approved_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          completed_at TEXT
        );
        """
    )
    ensure_column(conn, "messages", "provider", "TEXT")
    ensure_column(conn, "messages", "model", "TEXT")
    ensure_column(conn, "tasks", "description", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "tasks", "due_date", "TEXT")
    ensure_column(conn, "tasks", "parent_task_id", "TEXT REFERENCES tasks(id) ON DELETE SET NULL")
    ensure_column(conn, "tasks", "consensus_brief_id", "TEXT REFERENCES consensus_briefs(id) ON DELETE SET NULL")
    ensure_column(conn, "tasks", "task_plan_id", "TEXT REFERENCES task_plans(id) ON DELETE CASCADE")
    ensure_column(conn, "tasks", "plan_item_key", "TEXT")
    ensure_column(conn, "tasks", "expected_output", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "tasks", "output_type", "TEXT NOT NULL DEFAULT 'markdown'")
    ensure_column(conn, "consensus_briefs", "work_items_json", "TEXT NOT NULL DEFAULT '[]'")
    ensure_column(conn, "knowledge_sources", "origin", "TEXT NOT NULL DEFAULT 'manual'")
    ensure_column(conn, "knowledge_sources", "source_ref", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "knowledge_sources", "source_hash", "TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_knowledge_sources_origin_ref "
        "ON knowledge_sources(workspace_id, origin, source_ref) "
        "WHERE source_ref <> ''"
    )
    ensure_column(conn, "conversations", "discussion_status", "TEXT NOT NULL DEFAULT 'discussing' CHECK(discussion_status IN ('discussing', 'aligned'))")
    # TD-03-T1: Run/RunStep data model. New columns are nullable here; RunService
    # (TD-03-T3) enforces "every Run belongs to a Task" + absolute workdir at the
    # application layer once execution actually flows through Hermes.
    ensure_column(conn, "runs", "task_id", "TEXT REFERENCES tasks(id) ON DELETE CASCADE")
    ensure_column(conn, "runs", "hermes_profile_id", "TEXT")
    ensure_column(conn, "runs", "hermes_run_id", "TEXT")
    ensure_column(conn, "runs", "workdir", "TEXT")
    ensure_column(conn, "runs", "attempt_no", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(conn, "runs", "lease_owner", "TEXT")
    ensure_column(conn, "runs", "lease_expires_at", "TEXT")
    ensure_column(conn, "runs", "started_at", "TEXT")
    ensure_column(conn, "runs", "context_manifest_id", "TEXT REFERENCES context_manifests(id) ON DELETE SET NULL")
    ensure_column(conn, "approvals", "run_id", "TEXT REFERENCES runs(id) ON DELETE SET NULL")
    ensure_column(
        conn,
        "approvals",
        "type",
        "TEXT NOT NULL DEFAULT 'high_risk' "
        "CHECK(type IN ('high_risk','clarification','capability_upgrade','business_tool'))",
    )
    ensure_column(
        conn, "approvals", "payload_json", "TEXT NOT NULL DEFAULT '{}'"
    )
    ensure_column(conn, "agents", "hermes_gateway_port", "INTEGER")
    # TD-08-T1: idea center (idle reflection). idle_thinking_enabled stored as
    # 0/1 INTEGER in both dialects (serialized to bool at the API layer).
    ensure_column(conn, "agent_specs", "last_idle_think_at", "TEXT")
    ensure_column(
        conn, "agent_specs", "idle_think_interval_hours", "INTEGER NOT NULL DEFAULT 6"
    )
    ensure_column(
        conn, "agent_specs", "idle_thinking_enabled", "INTEGER NOT NULL DEFAULT 1"
    )
    # TD-06-T1: skill self-sedimentation (reflection cron).
    ensure_column(
        conn, "agent_specs", "runs_since_last_reflection", "INTEGER NOT NULL DEFAULT 0"
    )
    ensure_column(conn, "agent_specs", "last_skill_reflection_at", "TEXT")
    ensure_column(
        conn, "agent_specs", "reflection_interval", "INTEGER NOT NULL DEFAULT 5"
    )
    ensure_column(
        conn, "conversations", "idea_id", "TEXT REFERENCES ideas(id) ON DELETE SET NULL"
    )
    # TD-09-T1: external channel adapters. Conversations remember which channel
    # (and external thread) they came from; messages carry the external id for
    # webhook-redelivery dedup.
    ensure_column(conn, "conversations", "source_channel", "TEXT")
    ensure_column(conn, "conversations", "external_conversation_id", "TEXT")
    ensure_column(conn, "messages", "external_message_id", "TEXT")
    # Multi-level org chart: departments can nest under a parent department
    # (technical dept → backend center → data group, à la DingTalk/Feishu),
    # instead of the flat list that forced "A/B/C" path strings into `name`.
    ensure_column(
        conn, "departments", "parent_id", "TEXT REFERENCES departments(id) ON DELETE CASCADE"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_runs_task_attempt "
        "ON runs(task_id, attempt_no) WHERE task_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_plan_item "
        "ON tasks(task_plan_id, plan_item_key) WHERE task_plan_id IS NOT NULL "
        "AND plan_item_key IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_business_actions_active_dedupe "
        "ON business_actions(dedupe_key) WHERE status IN "
        "('pending_approval','approved','executing','succeeded')"
    )
    _init_td13_tables(conn)
    _init_td14_tables(conn)


def _init_td14_tables(conn: Database) -> None:
    """TD-14 durable execution, device and local-project primitives.

    Keep these additive and idempotent so existing SQLite databases and
    PostgreSQL deployments upgrade in place without relying on the unrelated
    untracked Alembic directory.
    """
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
          status TEXT NOT NULL CHECK(status IN ('started','succeeded','failed','rejected')),
          result_json TEXT NOT NULL DEFAULT '{}',
          error TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          completed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS local_devices (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          device_name TEXT NOT NULL DEFAULT '',
          platform TEXT NOT NULL,
          architecture TEXT NOT NULL DEFAULT '',
          worker_version TEXT NOT NULL DEFAULT '',
          hermes_version TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'offline'
            CHECK(status IN ('online','offline','revoked')),
          device_token_hash TEXT NOT NULL DEFAULT '',
          token_expires_at TEXT,
          last_heartbeat_at TEXT,
          capabilities_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS local_projects (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL,
          device_id TEXT NOT NULL,
          display_name TEXT NOT NULL,
          path_hash TEXT NOT NULL,
          allowed_scopes_json TEXT NOT NULL DEFAULT '["read"]',
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(device_id, path_hash)
        )
        """
    )
    ensure_column(conn, "runs", "execution_target", "TEXT NOT NULL DEFAULT 'server'")
    ensure_column(conn, "runs", "device_id", "TEXT")
    ensure_column(conn, "runs", "local_project_id", "TEXT")
    ensure_column(conn, "runs", "runtime_status", "TEXT NOT NULL DEFAULT 'server'")
    ensure_column(conn, "runs", "execution_receipt_id", "TEXT")
    ensure_column(conn, "runs", "last_event_id", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "runs", "prompt_text", "TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_receipts_run "
        "ON execution_receipts(run_id, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_local_devices_workspace "
        "ON local_devices(workspace_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_local_projects_workspace "
        "ON local_projects(workspace_id, active)"
    )


def _init_td13_tables(conn: Database) -> None:
    """Create the durable company world model used by TD-13.

    The schema deliberately uses portable TEXT/INTEGER/REAL primitives so the
    same startup migration works for SQLite development and PostgreSQL.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS company_events (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          event_type TEXT NOT NULL,
          source_id TEXT NOT NULL,
          conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
          task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
          actor_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
          actor_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
          title TEXT NOT NULL DEFAULT '',
          content TEXT NOT NULL DEFAULT '',
          occurred_at TEXT NOT NULL,
          importance REAL NOT NULL DEFAULT 1.0,
          confidence REAL NOT NULL DEFAULT 1.0,
          metadata_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          UNIQUE(workspace_id, event_type, source_id)
        );

        CREATE TABLE IF NOT EXISTS agent_memories (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
          memory_type TEXT NOT NULL CHECK(memory_type IN (
            'observation','episode','reflection','relationship','lesson'
          )),
          title TEXT NOT NULL DEFAULT '',
          content TEXT NOT NULL,
          importance REAL NOT NULL DEFAULT 1.0,
          confidence REAL NOT NULL DEFAULT 1.0,
          is_private INTEGER NOT NULL DEFAULT 1,
          promoted INTEGER NOT NULL DEFAULT 0,
          access_count INTEGER NOT NULL DEFAULT 0,
          last_accessed_at TEXT,
          expires_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS memory_links (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          memory_id TEXT NOT NULL REFERENCES agent_memories(id) ON DELETE CASCADE,
          event_id TEXT NOT NULL REFERENCES company_events(id) ON DELETE CASCADE,
          relation TEXT NOT NULL DEFAULT 'evidence',
          created_at TEXT NOT NULL,
          UNIQUE(memory_id, event_id, relation)
        );

        CREATE TABLE IF NOT EXISTS agent_relationships (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
          colleague_agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
          summary TEXT NOT NULL DEFAULT '',
          trust_score REAL NOT NULL DEFAULT 0.5,
          interaction_count INTEGER NOT NULL DEFAULT 0,
          evidence_event_ids_json TEXT NOT NULL DEFAULT '[]',
          last_interacted_at TEXT,
          updated_at TEXT NOT NULL,
          UNIQUE(workspace_id, agent_id, colleague_agent_id),
          CHECK(agent_id <> colleague_agent_id)
        );

        CREATE TABLE IF NOT EXISTS context_manifests (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
          conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
          task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
          agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
          query TEXT NOT NULL DEFAULT '',
          focus_json TEXT NOT NULL DEFAULT '[]',
          selected_event_ids_json TEXT NOT NULL DEFAULT '[]',
          selected_memory_ids_json TEXT NOT NULL DEFAULT '[]',
          prompt_hash TEXT NOT NULL DEFAULT '',
          token_budget INTEGER NOT NULL DEFAULT 12000,
          created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_company_events_workspace_time
          ON company_events(workspace_id, occurred_at DESC);
        CREATE INDEX IF NOT EXISTS idx_company_events_source
          ON company_events(workspace_id, event_type, source_id);
        CREATE INDEX IF NOT EXISTS idx_agent_memories_owner_time
          ON agent_memories(workspace_id, agent_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_agent_memories_promoted
          ON agent_memories(workspace_id, promoted, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memory_links_memory
          ON memory_links(workspace_id, memory_id);
        CREATE INDEX IF NOT EXISTS idx_context_manifests_run
          ON context_manifests(workspace_id, run_id, created_at DESC);
        """
    )


def ensure_column(
    conn: Database, table_name: str, column_name: str, definition: str
) -> None:
    if conn.dialect == "postgres":
        exists = conn.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = ? AND column_name = ?
            """,
            (table_name, column_name),
        ).fetchone()
        if exists is not None:
            return
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
        return

    columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    if any(column["name"] == column_name for column in columns):
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def _upgrade_approval_type_check(conn: Database) -> None:
    """Allow business-tool approvals on databases created before TD-10."""
    if conn.dialect == "postgres":
        exists = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'approvals'"
        ).fetchone()
        if exists is None:
            return
        conn.execute("ALTER TABLE approvals DROP CONSTRAINT IF EXISTS approvals_type_check")
        conn.execute(
            "ALTER TABLE approvals ADD CONSTRAINT approvals_type_check "
            "CHECK(type IN ('high_risk','clarification','capability_upgrade','business_tool'))"
        )
        return

    schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'approvals'"
    ).fetchone()
    if schema is None or "business_tool" in (schema["sql"] or ""):
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(approvals)").fetchall()}
    required = {
        "id", "workspace_id", "run_id", "task_id", "conversation_id", "agent_id",
        "title", "description", "status", "risk_level", "type", "payload_json",
        "resolved_by", "resolved_at", "created_at",
    }
    if not required.issubset(columns):
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        """
        CREATE TABLE approvals_td10 (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
          task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
          conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
          agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
          title TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'pending',
          risk_level TEXT NOT NULL DEFAULT 'medium',
          type TEXT NOT NULL DEFAULT 'high_risk'
            CHECK(type IN ('high_risk','clarification','capability_upgrade','business_tool')),
          payload_json TEXT NOT NULL DEFAULT '{}',
          resolved_by TEXT NOT NULL DEFAULT '',
          resolved_at TEXT,
          created_at TEXT NOT NULL
        );
        INSERT INTO approvals_td10 (
          id, workspace_id, run_id, task_id, conversation_id, agent_id,
          title, description, status, risk_level, type, payload_json,
          resolved_by, resolved_at, created_at
        ) SELECT
          id, workspace_id, run_id, task_id, conversation_id, agent_id,
          title, description, status, risk_level, type, payload_json,
          resolved_by, resolved_at, created_at
        FROM approvals;
        DROP TABLE approvals;
        ALTER TABLE approvals_td10 RENAME TO approvals;
        """
    )
    conn.execute("PRAGMA foreign_keys = ON")


def reset_database_for_tests() -> None:
    conn = connect()
    try:
        if conn.dialect == "postgres":
            conn.executescript(
                """
                DROP TABLE IF EXISTS business_actions CASCADE;
                DROP TABLE IF EXISTS business_tool_policies CASCADE;
                DROP TABLE IF EXISTS context_manifests CASCADE;
                DROP TABLE IF EXISTS memory_links CASCADE;
                DROP TABLE IF EXISTS agent_relationships CASCADE;
                DROP TABLE IF EXISTS agent_memories CASCADE;
                DROP TABLE IF EXISTS company_events CASCADE;
                DROP TABLE IF EXISTS workspace_model_credentials CASCADE;
                DROP TABLE IF EXISTS agent_credentials CASCADE;
                DROP TABLE IF EXISTS task_dependencies CASCADE;
                DROP TABLE IF EXISTS task_plans CASCADE;
                DROP TABLE IF EXISTS run_steps CASCADE;
                DROP TABLE IF EXISTS runs CASCADE;
                DROP TABLE IF EXISTS agent_experiences CASCADE;
                DROP TABLE IF EXISTS approvals CASCADE;
                DROP TABLE IF EXISTS task_outputs CASCADE;
                DROP TABLE IF EXISTS task_events CASCADE;
                DROP TABLE IF EXISTS knowledge_sources CASCADE;
                DROP TABLE IF EXISTS tasks CASCADE;
                DROP TABLE IF EXISTS messages CASCADE;
                DROP TABLE IF EXISTS conversation_members CASCADE;
                DROP TABLE IF EXISTS conversations CASCADE;
                DROP TABLE IF EXISTS agents CASCADE;
                DROP TABLE IF EXISTS departments CASCADE;
                DROP TABLE IF EXISTS workspaces CASCADE;
                DROP TABLE IF EXISTS users CASCADE;
                DROP TABLE IF EXISTS official_agent_templates CASCADE;
                DROP TABLE IF EXISTS official_talent_categories CASCADE;
                """
            )
        else:
            conn.executescript(
                """
                DROP TABLE IF EXISTS business_actions;
                DROP TABLE IF EXISTS business_tool_policies;
                DROP TABLE IF EXISTS context_manifests;
                DROP TABLE IF EXISTS memory_links;
                DROP TABLE IF EXISTS agent_relationships;
                DROP TABLE IF EXISTS agent_memories;
                DROP TABLE IF EXISTS company_events;
                DROP TABLE IF EXISTS workspace_model_credentials;
                DROP TABLE IF EXISTS agent_credentials;
                DROP TABLE IF EXISTS task_dependencies;
                DROP TABLE IF EXISTS task_plans;
                DROP TABLE IF EXISTS run_steps;
                DROP TABLE IF EXISTS runs;
                DROP TABLE IF EXISTS agent_experiences;
                DROP TABLE IF EXISTS approvals;
                DROP TABLE IF EXISTS task_outputs;
                DROP TABLE IF EXISTS task_events;
                DROP TABLE IF EXISTS knowledge_sources;
                DROP TABLE IF EXISTS tasks;
                DROP TABLE IF EXISTS messages;
                DROP TABLE IF EXISTS conversation_members;
                DROP TABLE IF EXISTS conversations;
                DROP TABLE IF EXISTS agents;
                DROP TABLE IF EXISTS departments;
                DROP TABLE IF EXISTS workspaces;
                DROP TABLE IF EXISTS users;
                DROP TABLE IF EXISTS official_agent_templates;
                DROP TABLE IF EXISTS official_talent_categories;
                """
            )
        conn.commit()
    finally:
        conn.close()


def split_sql_script(script: str) -> list[str]:
    return [statement.strip() for statement in script.split(";") if statement.strip()]


def database_kind() -> str:
    return "sqlite" if is_sqlite_url(settings.database_url) else "postgres"


def safe_database_label() -> str:
    parsed = urlparse(settings.database_url)
    if parsed.scheme.startswith("sqlite"):
        return str(sqlite_path(settings.database_url))
    host = parsed.hostname or "localhost"
    port = f":{parsed.port}" if parsed.port else ""
    database = parsed.path.lstrip("/") or "postgres"
    return f"{parsed.scheme}://{host}{port}/{database}"


def validate_identifier(identifier: str) -> None:
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", identifier):
        raise ValueError(f"unsafe SQL identifier: {identifier}")
