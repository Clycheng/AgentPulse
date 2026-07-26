"""Durable, approval-gated external business actions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.core.database import Database, connect
from app.core.logging import get_logger
from app.orchestration.capability_catalog import CATALOG, CapabilityDef
from app.services.credentials import CredentialError, get_credential
from app.services.email_providers import send_resend_email
from app.services.workspace import new_id, now_iso

logger = get_logger(__name__)

TERMINAL_STATUSES = {"succeeded", "rejected", "expired", "failed"}
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class BusinessToolError(ValueError):
    pass


def _record_external_action_event(
    conn: Database,
    action: dict,
    *,
    status: str,
    content: str,
    source_suffix: str,
    metadata: dict | None = None,
) -> None:
    """Project a provider result into the immutable company event ledger."""
    from app.services.company_memory import record_company_event

    record_company_event(
        conn,
        workspace_id=action["workspace_id"],
        event_type="external_action_result",
        source_id=f"{action['id']}:{source_suffix}",
        title=f"外部动作{status}",
        content=content[:12000],
        conversation_id=action.get("conversation_id"),
        task_id=action.get("task_id"),
        actor_agent_id=action.get("agent_id"),
        importance=4.0 if status == "succeeded" else 3.0,
        metadata={
            "action_id": action["id"],
            "tool_name": action["tool_name"],
            "provider": action.get("provider"),
            "status": status,
            **(metadata or {}),
        },
    )


def _iso_after(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def tool_definitions() -> dict[str, CapabilityDef]:
    return {cap.business_tool: cap for cap in CATALOG.values() if cap.business_tool}


def enabled_business_tools(conn: Database, agent_id: str) -> list[str]:
    if conn.dialect == "sqlite" and conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agent_capabilities'"
    ).fetchone() is None:
        return []
    enabled = {
        row["capability_key"]
        for row in conn.execute(
            "SELECT capability_key FROM agent_capabilities "
            "WHERE agent_id = ? AND status = 'enabled'",
            (agent_id,),
        ).fetchall()
    }
    return sorted(
        cap.business_tool
        for key, cap in CATALOG.items()
        if key in enabled and cap.business_tool
    )


def authorize_tool(conn: Database, claims: dict, tool_name: str) -> tuple[dict, CapabilityDef]:
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (claims["run_id"],)).fetchone()
    if run is None:
        raise BusinessToolError("business tool run does not exist")
    expected = {
        "workspace_id": run["workspace_id"],
        "conversation_id": run["conversation_id"],
        "run_id": run["id"],
        "agent_id": run["agent_id"],
        "task_id": run["task_id"],
    }
    if any(claims.get(key) != value for key, value in expected.items()):
        raise BusinessToolError("business tool token does not match run ownership")
    if run["status"] not in ("running", "waiting_user", "waiting_clarify"):
        raise BusinessToolError("business tool run is not active")

    capability = tool_definitions().get(tool_name)
    if capability is None:
        raise BusinessToolError("unknown business tool")
    enabled = conn.execute(
        "SELECT 1 FROM agent_capabilities WHERE agent_id = ? "
        "AND capability_key = ? AND status = 'enabled'",
        (claims["agent_id"], capability.key),
    ).fetchone()
    if enabled is None:
        raise BusinessToolError("employee is not allowed to use this business tool")
    return dict(run), capability


def _resolve_email_channel(
    conn: Database, *, workspace_id: str, agent_id: str, channel_id: str | None
) -> tuple[dict, dict]:
    params: list[object] = [workspace_id, agent_id]
    where = "workspace_id = ? AND channel_type = 'email' AND active = 1 AND target_agent_id = ?"
    if channel_id:
        where += " AND id = ?"
        params.append(channel_id)
    rows = conn.execute(
        f"SELECT * FROM channel_configs WHERE {where} ORDER BY created_at",
        tuple(params),
    ).fetchall()
    if not rows:
        raise BusinessToolError("未配置绑定当前员工的可用邮件渠道")
    if not channel_id and len(rows) != 1:
        raise BusinessToolError("当前员工有多个邮件渠道，请明确指定 channel_id")
    channel = dict(rows[0])
    config = json.loads(channel["config_json"] or "{}")
    if config.get("provider") != "resend":
        raise BusinessToolError("邮件渠道尚未配置 Resend provider")
    if not config.get("from_address") or not _EMAIL_RE.match(config["from_address"]):
        raise BusinessToolError("邮件渠道缺少合法的 from_address")
    return channel, config


def _prepare_email(
    conn: Database, *, run: dict, arguments: dict
) -> tuple[dict, dict]:
    raw_to = arguments.get("to")
    recipients = [raw_to] if isinstance(raw_to, str) else list(raw_to or [])
    recipients = [str(value).strip() for value in recipients if str(value).strip()]
    if not recipients or len(recipients) > 50 or any(not _EMAIL_RE.match(item) for item in recipients):
        raise BusinessToolError("to 必须包含 1-50 个合法邮箱地址")
    subject = str(arguments.get("subject") or "").strip()
    body = str(arguments.get("body") or "").strip()
    reply_to = str(arguments.get("reply_to") or "").strip() or None
    if not subject or len(subject) > 500:
        raise BusinessToolError("邮件主题不能为空且不能超过 500 字符")
    if not body or len(body) > 100_000:
        raise BusinessToolError("邮件正文不能为空且不能超过 100000 字符")
    if reply_to and not _EMAIL_RE.match(reply_to):
        raise BusinessToolError("reply_to 不是合法邮箱地址")
    get_credential(conn, agent_id=run["agent_id"], credential_name="EMAIL_API_KEY")
    channel, config = _resolve_email_channel(
        conn,
        workspace_id=run["workspace_id"],
        agent_id=run["agent_id"],
        channel_id=str(arguments.get("channel_id") or "").strip() or None,
    )
    normalized = {
        "to": recipients,
        "subject": subject,
        "body": body,
        "channel_id": channel["id"],
        "reply_to": reply_to,
    }
    preview = {
        "channel_name": channel["name"],
        "from": (
            f"{config.get('from_name')} <{config['from_address']}>"
            if config.get("from_name")
            else config["from_address"]
        ),
        "to": recipients,
        "subject": subject,
        "body_preview": body[:500],
    }
    return normalized, preview


def prepare_arguments(
    conn: Database, *, run: dict, tool_name: str, arguments: dict
) -> tuple[dict, dict, str]:
    if tool_name != "send_email":
        raise BusinessToolError(f"{tool_name} 的真实 provider 尚未配置")
    normalized, preview = _prepare_email(conn, run=run, arguments=arguments)
    return normalized, preview, "resend"


def _canonical_hash(arguments: dict) -> tuple[str, str]:
    serialized = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return serialized, hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def serialize_action(row: dict) -> dict:
    arguments = json.loads(row["arguments_json"] or "{}")
    preview = {}
    if row["tool_name"] == "send_email":
        preview = {
            "to": arguments.get("to", []),
            "subject": arguments.get("subject", ""),
            "body_preview": str(arguments.get("body", ""))[:500],
            "channel_id": arguments.get("channel_id"),
        }
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "conversation_id": row["conversation_id"],
        "agent_id": row["agent_id"],
        "capability_key": row["capability_key"],
        "tool_name": row["tool_name"],
        "preview": preview,
        "status": row["status"],
        "approval_id": row["approval_id"],
        "provider": row["provider"],
        "external_id": row["external_id"],
        "result": json.loads(row["result_json"] or "{}"),
        "error": row["error"],
        "attempt_no": int(row["attempt_no"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
    }


def create_or_reuse_action(
    conn: Database, claims: dict, *, tool_name: str, arguments: dict
) -> dict:
    run, capability = authorize_tool(conn, claims, tool_name)
    try:
        normalized, preview, provider = prepare_arguments(
            conn, run=run, tool_name=tool_name, arguments=arguments
        )
    except CredentialError as exc:
        raise BusinessToolError(str(exc)) from exc
    arguments_json, arguments_hash = _canonical_hash(normalized)
    scope = f"task:{run['task_id']}" if run["task_id"] else f"run:{run['id']}"
    dedupe_key = hashlib.sha256(
        f"{scope}:{tool_name}:{arguments_hash}".encode("utf-8")
    ).hexdigest()
    existing = conn.execute(
        "SELECT * FROM business_actions WHERE dedupe_key = ? AND status IN "
        "('pending_approval','approved','executing','succeeded') "
        "ORDER BY created_at DESC LIMIT 1",
        (dedupe_key,),
    ).fetchone()
    if existing:
        return serialize_action(existing)

    policy = conn.execute(
        "SELECT 1 FROM business_tool_policies WHERE workspace_id = ? AND agent_id = ? "
        "AND tool_name = ? AND active = 1",
        (run["workspace_id"], run["agent_id"], tool_name),
    ).fetchone()
    bypass = capability.risk_gate == "auto" or (
        capability.risk_gate == "approval" and policy is not None
    )
    action_id = new_id("bact")
    approval_id = None if bypass else new_id("approval")
    timestamp = now_iso()
    expires_at = None if bypass else _iso_after(settings.approval_bridge_timeout_seconds)
    if approval_id:
        payload = {
            "business_action_id": action_id,
            "tool": tool_name,
            "capability_key": capability.key,
            "risk_gate": capability.risk_gate,
            "preview": preview,
        }
        conn.execute(
            """INSERT INTO approvals (
              id, workspace_id, run_id, task_id, conversation_id, agent_id,
              title, description, status, risk_level, type, payload_json,
              resolved_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'high',
              'business_tool', ?, '', ?)""",
            (
                approval_id,
                run["workspace_id"],
                run["id"],
                run["task_id"],
                run["conversation_id"],
                run["agent_id"],
                "发送邮件待确认" if tool_name == "send_email" else "业务动作待确认",
                f"{preview.get('from', '')} -> {', '.join(preview.get('to', []))}: {preview.get('subject', tool_name)}",
                json.dumps(payload, ensure_ascii=False),
                timestamp,
            ),
        )
    status = "approved" if bypass else "pending_approval"
    conn.execute(
        """INSERT INTO business_actions (
          id, workspace_id, run_id, task_id, conversation_id, agent_id,
          capability_key, tool_name, arguments_json, arguments_hash, dedupe_key,
          status, approval_id, provider, external_id, result_json, error,
          attempt_no, lease_owner, lease_expires_at, expires_at, approved_at,
          created_at, updated_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '{}', '',
          0, NULL, NULL, ?, ?, ?, ?, NULL)""",
        (
            action_id,
            run["workspace_id"],
            run["id"],
            run["task_id"],
            run["conversation_id"],
            run["agent_id"],
            capability.key,
            tool_name,
            arguments_json,
            arguments_hash,
            dedupe_key,
            status,
            approval_id,
            provider,
            expires_at,
            timestamp if bypass else None,
            timestamp,
            timestamp,
        ),
    )
    if approval_id:
        conn.execute(
            """UPDATE runs SET status = 'waiting_user', runtime_status = 'waiting_user'
            WHERE id = ? AND status = 'running'""",
            (run["id"],),
        )
    row = conn.execute("SELECT * FROM business_actions WHERE id = ?", (action_id,)).fetchone()
    return serialize_action(row)


def expire_pending_actions(conn: Database) -> None:
    rows = conn.execute(
        "SELECT id, approval_id, run_id FROM business_actions "
        "WHERE status = 'pending_approval' AND expires_at < ?",
        (now_iso(),),
    ).fetchall()
    timestamp = now_iso()
    for row in rows:
        conn.execute(
            "UPDATE business_actions SET status = 'expired', error = ?, updated_at = ?, "
            "completed_at = ? WHERE id = ? AND status = 'pending_approval'",
            ("老板未在时限内处理", timestamp, timestamp, row["id"]),
        )
        if row["approval_id"]:
            conn.execute(
                "UPDATE approvals SET status = 'expired', resolved_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (timestamp, row["approval_id"]),
            )
        conn.execute(
            """UPDATE runs SET status = 'running', runtime_status = 'running'
            WHERE id = ? AND status = 'waiting_user'""",
            (row["run_id"],),
        )


async def wait_for_action(action_id: str, *, timeout: float = 90.0) -> dict:
    elapsed = 0.0
    while elapsed < timeout:
        conn = connect()
        try:
            expire_pending_actions(conn)
            row = conn.execute(
                "SELECT * FROM business_actions WHERE id = ?", (action_id,)
            ).fetchone()
            conn.commit()
        finally:
            conn.close()
        if row is None:
            return {"ok": False, "status": "failed", "message": "业务动作不存在"}
        if row["status"] in TERMINAL_STATUSES:
            result = serialize_action(row)
            if row["status"] == "succeeded":
                return {"ok": True, **result}
            message = {
                "rejected": "老板拒绝了该业务动作，未执行外部调用",
                "expired": "老板未在时限内处理，业务动作已取消",
                "failed": row["error"] or "业务动作执行失败",
            }[row["status"]]
            return {"ok": False, **result, "message": message}
        await asyncio.sleep(0.25)
        elapsed += 0.25
    return {"ok": False, "status": "failed", "message": "等待业务动作结果超时"}


async def invoke_business_tool(claims: dict, *, tool_name: str, arguments: dict) -> dict:
    conn = connect()
    try:
        action = create_or_reuse_action(
            conn, claims, tool_name=tool_name, arguments=arguments
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if action["status"] == "succeeded":
        return {"ok": True, **action}
    return await wait_for_action(action["id"])


def list_actions(
    conn: Database,
    *,
    workspace_id: str,
    agent_id: str | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
    status: str | None = None,
) -> list[dict]:
    where = ["workspace_id = ?"]
    params: list[object] = [workspace_id]
    for column, value in (("agent_id", agent_id), ("task_id", task_id), ("run_id", run_id), ("status", status)):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    rows = conn.execute(
        f"SELECT * FROM business_actions WHERE {' AND '.join(where)} "
        "ORDER BY created_at DESC LIMIT 200",
        tuple(params),
    ).fetchall()
    return [serialize_action(row) for row in rows]


def list_policies(conn: Database, *, workspace_id: str, agent_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM business_tool_policies WHERE workspace_id = ? AND agent_id = ? "
        "AND active = 1 ORDER BY created_at DESC",
        (workspace_id, agent_id),
    ).fetchall()
    return [dict(row) for row in rows]


def revoke_policy(
    conn: Database, *, workspace_id: str, agent_id: str, tool_name: str
) -> bool:
    row = conn.execute(
        "SELECT id FROM business_tool_policies WHERE workspace_id = ? AND agent_id = ? "
        "AND tool_name = ? AND active = 1",
        (workspace_id, agent_id, tool_name),
    ).fetchone()
    if row is None:
        return False
    timestamp = now_iso()
    conn.execute(
        "UPDATE business_tool_policies SET active = 0, revoked_at = ?, updated_at = ? "
        "WHERE id = ?",
        (timestamp, timestamp, row["id"]),
    )
    return True


def resolve_business_approval(
    conn: Database,
    *,
    approval: dict,
    decision: str,
    scope: str,
    resolved_by: str,
) -> None:
    action = conn.execute(
        "SELECT * FROM business_actions WHERE approval_id = ?", (approval["id"],)
    ).fetchone()
    if action is None or action["status"] != "pending_approval":
        raise BusinessToolError("业务动作已处理或不存在")
    capability = CATALOG.get(action["capability_key"])
    if scope == "always" and capability and capability.risk_gate == "prohibited_auto":
        raise BusinessToolError("该业务动作禁止长期放行，只能批准一次")
    timestamp = now_iso()
    if decision == "approved":
        conn.execute(
            "UPDATE business_actions SET status = 'approved', approved_at = ?, "
            "updated_at = ? WHERE id = ?",
            (timestamp, timestamp, action["id"]),
        )
        if scope == "always":
            conn.execute(
                """INSERT INTO business_tool_policies (
                  id, workspace_id, agent_id, tool_name, active, created_by,
                  created_at, updated_at, revoked_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, NULL)
                ON CONFLICT (workspace_id, agent_id, tool_name) DO UPDATE SET
                  active = 1, created_by = excluded.created_by,
                  updated_at = excluded.updated_at, revoked_at = NULL""",
                (
                    new_id("btp"),
                    action["workspace_id"],
                    action["agent_id"],
                    action["tool_name"],
                    resolved_by,
                    timestamp,
                    timestamp,
                ),
            )
    else:
        conn.execute(
            "UPDATE business_actions SET status = 'rejected', error = ?, "
            "updated_at = ?, completed_at = ? WHERE id = ?",
            ("老板拒绝", timestamp, timestamp, action["id"]),
        )
        conn.execute(
            """UPDATE runs SET status = 'running', runtime_status = 'running'
            WHERE id = ? AND status = 'waiting_user'""",
            (action["run_id"],),
        )


class BusinessActionWorker:
    def __init__(
        self,
        *,
        email_sender: Callable[..., Awaitable[dict]] = send_resend_email,
    ) -> None:
        self.worker_id = f"business_{secrets.token_hex(6)}"
        self.email_sender = email_sender
        self._active: dict[str, asyncio.Task] = {}

    async def close(self) -> None:
        tasks = list(self._active.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()

    async def tick(self) -> None:
        for action_id, task in list(self._active.items()):
            if task.done():
                try:
                    task.result()
                except (Exception, asyncio.CancelledError):
                    pass
                self._active.pop(action_id, None)
        conn = connect()
        try:
            expire_pending_actions(conn)
            claimed = self._claim(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        for action_id in claimed:
            self._active[action_id] = asyncio.create_task(self._execute(action_id))

    def _claim(self, conn: Database) -> list[str]:
        rows = conn.execute(
            """SELECT * FROM business_actions
            WHERE status = 'approved' OR (
              status = 'executing' AND lease_expires_at < ?
            ) ORDER BY created_at LIMIT 8""",
            (now_iso(),),
        ).fetchall()
        claimed: list[str] = []
        for row in rows:
            if int(row["attempt_no"]) >= settings.business_action_max_attempts:
                timestamp = now_iso()
                conn.execute(
                    "UPDATE business_actions SET status = 'failed', error = ?, "
                    "lease_owner = NULL, lease_expires_at = NULL, updated_at = ?, "
                    "completed_at = ? WHERE id = ?",
                    ("业务动作重试次数已用尽", timestamp, timestamp, row["id"]),
                )
                conn.execute(
                    "UPDATE runs SET status = 'running', runtime_status = 'running' "
                    "WHERE id = ? AND status = 'waiting_user'",
                    (row["run_id"],),
                )
                _record_external_action_event(
                    conn,
                    dict(row),
                    status="failed",
                    content="业务动作重试次数已用尽",
                    source_suffix=f"attempt-{row['attempt_no']}",
                    metadata={"attempt_no": row["attempt_no"], "terminal": True},
                )
                continue
            conn.execute(
                """UPDATE business_actions SET status = 'executing',
                attempt_no = attempt_no + 1, lease_owner = ?, lease_expires_at = ?,
                updated_at = ? WHERE id = ? AND (
                  status = 'approved' OR (status = 'executing' AND lease_expires_at < ?)
                )""",
                (
                    self.worker_id,
                    _iso_after(settings.business_action_lease_seconds),
                    now_iso(),
                    row["id"],
                    now_iso(),
                ),
            )
            owner = conn.execute(
                "SELECT lease_owner FROM business_actions WHERE id = ?", (row["id"],)
            ).fetchone()
            if owner and owner["lease_owner"] == self.worker_id:
                claimed.append(row["id"])
        return claimed

    async def _execute(self, action_id: str) -> None:
        conn = connect()
        try:
            action = conn.execute(
                "SELECT * FROM business_actions WHERE id = ? AND lease_owner = ?",
                (action_id, self.worker_id),
            ).fetchone()
            if action is None:
                return
            arguments = json.loads(action["arguments_json"] or "{}")
            if action["tool_name"] != "send_email":
                raise BusinessToolError(f"{action['tool_name']} 的真实 provider 尚未配置")
            _, config = _resolve_email_channel(
                conn,
                workspace_id=action["workspace_id"],
                agent_id=action["agent_id"],
                channel_id=arguments["channel_id"],
            )
            api_key = get_credential(
                conn,
                agent_id=action["agent_id"],
                credential_name="EMAIL_API_KEY",
            )
            result = await self.email_sender(
                api_key=api_key,
                idempotency_key=f"business-action/{action_id}",
                from_address=config["from_address"],
                from_name=str(config.get("from_name") or ""),
                to=arguments["to"],
                subject=arguments["subject"],
                body=arguments["body"],
                reply_to=arguments.get("reply_to"),
            )
            timestamp = now_iso()
            conn.execute(
                """UPDATE business_actions SET status = 'succeeded', external_id = ?,
                result_json = ?, error = '', lease_owner = NULL, lease_expires_at = NULL,
                updated_at = ?, completed_at = ? WHERE id = ?""",
                (
                    result["id"],
                    json.dumps(result, ensure_ascii=False),
                    timestamp,
                    timestamp,
                    action_id,
                ),
            )
            conn.execute(
                """UPDATE runs SET status = 'running', runtime_status = 'running'
                WHERE id = ? AND status = 'waiting_user'""",
                (action["run_id"],),
            )
            _record_external_action_event(
                conn,
                dict(action),
                status="succeeded",
                content=json.dumps(result, ensure_ascii=False),
                source_suffix="succeeded",
                metadata={"external_id": result.get("id")},
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            self._record_failure(action_id, str(exc))
        finally:
            conn.close()

    def _record_failure(self, action_id: str, error: str) -> None:
        conn = connect()
        try:
            action = conn.execute(
                "SELECT * FROM business_actions WHERE id = ?",
                (action_id,),
            ).fetchone()
            if action is None:
                return
            terminal = int(action["attempt_no"]) >= settings.business_action_max_attempts
            status = "failed" if terminal else "approved"
            timestamp = now_iso()
            conn.execute(
                """UPDATE business_actions SET status = ?, error = ?, lease_owner = NULL,
                lease_expires_at = NULL, updated_at = ?, completed_at = ? WHERE id = ?""",
                (status, error[:2000], timestamp, timestamp if terminal else None, action_id),
            )
            if terminal:
                conn.execute(
                    """UPDATE runs SET status = 'running', runtime_status = 'running'
                    WHERE id = ? AND status = 'waiting_user'""",
                    (action["run_id"],),
                )
            _record_external_action_event(
                conn,
                dict(action),
                status="failed" if terminal else "retrying",
                content=error,
                source_suffix=f"attempt-{action['attempt_no']}",
                metadata={"attempt_no": action["attempt_no"], "terminal": terminal},
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            logger.error("business_action_failure_record_failed", action_id=action_id, error=str(exc))
        finally:
            conn.close()
