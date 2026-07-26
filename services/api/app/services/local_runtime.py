"""Small server-side checks for requests that require the user's computer."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from app.core.database import Database


_LOCAL_REQUEST_WORDS = re.compile(
    r"(读取|打开|查看|看看|分析|项目|文件|目录|代码|运行测试|终端|命令|电脑|桌面|computer_use|read_file|terminal)",
    re.IGNORECASE,
)
_UNIX_ABSOLUTE_PATH = re.compile(r"(?:^|\s)(?:/[A-Za-z0-9._-]+){2,}")
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r'(^|\s)[A-Za-z]:\\(?:[^\\/:*?"<>|\s]+\\)+[^\\/:*?"<>|\s]+'
)
_REDACTED_PROJECT_PATH = "[已授权本机项目路径]"


def requires_local_execution(text: str) -> bool:
    """Return true for an explicit local-computer request."""
    normalized = text.strip()
    return bool(_LOCAL_REQUEST_WORDS.search(normalized)) and bool(
        _UNIX_ABSOLUTE_PATH.search(normalized)
        or _WINDOWS_ABSOLUTE_PATH.search(normalized)
        or _REDACTED_PROJECT_PATH in normalized
        or re.search(r"(本机|本地|桌面|文件系统|项目目录)", normalized, re.I)
    )


def local_runtime_online(conn: Database, workspace_id: str) -> bool:
    return online_device(conn, workspace_id) is not None


def retire_replaced_device(
    conn: Database,
    *,
    workspace_id: str,
    user_id: str,
    replaced_device_id: str | None,
    updated_at: str,
) -> None:
    """Invalidate the previous process identity for the same desktop install."""
    if not replaced_device_id:
        return
    conn.execute(
        """UPDATE local_devices SET status = 'offline', device_token_hash = '',
        updated_at = ? WHERE id = ? AND workspace_id = ? AND user_id = ?
        AND status <> 'revoked'""",
        (updated_at, replaced_device_id, workspace_id, user_id),
    )


def online_device(conn: Database, workspace_id: str):
    try:
        row = conn.execute(
            """SELECT * FROM local_devices
            WHERE workspace_id = ? AND status = 'online'
            ORDER BY last_heartbeat_at DESC LIMIT 1""",
            (workspace_id,),
        ).fetchone()
    except Exception:
        return None
    if row is None or not row["last_heartbeat_at"]:
        return None
    try:
        heartbeat = datetime.fromisoformat(str(row["last_heartbeat_at"]))
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=UTC)
        return row if datetime.now(UTC) - heartbeat < timedelta(seconds=45) else None
    except ValueError:
        return None


def redact_local_paths(text: str) -> str:
    """Keep user absolute paths out of cloud Run prompts and audit rows."""
    redacted = re.sub(
        r"(?:/[A-Za-z0-9._-]+){2,}", _REDACTED_PROJECT_PATH, text
    )
    return _WINDOWS_ABSOLUTE_PATH.sub(
        lambda match: match.group(1) + _REDACTED_PROJECT_PATH, redacted
    )
