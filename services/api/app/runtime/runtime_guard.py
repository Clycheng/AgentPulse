"""Fail-closed runtime guards shared by tool and local execution paths.

This is the small, framework-independent part borrowed from DeerFlow's
runtime/sandbox ideas. It deliberately does not become a second agent
protocol: it normalizes untrusted model output, scopes local paths and keeps
large or sensitive values out of durable run traces.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


CONTROL_MARKERS = ("<｜｜DSML｜｜", "<|DSML|>")
MAX_TOOL_OUTPUT = 20_000
MAX_ARGUMENT_TEXT = 12_000


def normalize_tool_arguments(value: Any) -> tuple[dict[str, Any], str | None]:
    if isinstance(value, dict):
        return value, None
    if not isinstance(value, str):
        return {}, "工具参数必须是 JSON 对象"
    if len(value) > MAX_ARGUMENT_TEXT:
        return {}, "工具参数超过大小限制"
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        return {}, f"工具参数不是合法 JSON: {exc.msg}"
    if not isinstance(parsed, dict):
        return {}, "工具参数必须是 JSON 对象"
    return parsed, None


def safe_project_path(project_root: str, requested: str) -> Path:
    """Resolve a project-relative path and reject traversal/symlink escape."""
    root = Path(project_root).expanduser().resolve(strict=True)
    candidate = Path(requested)
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=False)
    else:
        resolved = (root / candidate).resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise ValueError("路径超出已授权项目目录")
    current = resolved
    while current != root:
        if current.is_symlink():
            raise ValueError("不允许通过符号链接逃逸项目目录")
        current = current.parent
    return resolved


def redact_runtime_text(value: str, *, project_root: str | None = None) -> str:
    text = value
    if project_root:
        text = text.replace(project_root, "[已授权本机项目路径]")
    text = re.sub(r"(?:/[A-Za-z0-9._-]+){2,}", "[本机路径]", text)
    for marker in CONTROL_MARKERS:
        text = text.replace(marker, "")
    return text[:MAX_TOOL_OUTPUT]


def bounded_tool_output(value: Any, *, project_root: str | None = None) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    return redact_runtime_text(text, project_root=project_root)


def final_reply_is_grounded(text: str, *, execution_receipts: int) -> bool:
    """Prevent deterministic success claims without a real execution receipt."""
    if execution_receipts > 0:
        return True
    return not bool(
        re.search(r"(已创建|已读取|已执行|已完成|已经完成|successfully|completed)", text, re.I)
    )
