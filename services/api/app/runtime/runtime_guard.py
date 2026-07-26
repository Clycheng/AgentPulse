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
def safe_project_path(project_root: str, requested: str) -> Path:
    """Resolve a project-relative path and reject traversal/symlink escape."""
    root = Path(project_root).expanduser().resolve(strict=True)
    if not requested or "\x00" in requested:
        raise ValueError("路径无效")
    candidate = Path(requested)
    lexical = candidate if candidate.is_absolute() else root / candidate
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise ValueError("路径超出已授权项目目录") from exc
    if ".." in relative.parts:
        raise ValueError("不允许路径穿越")

    # Inspect existing path components before resolving. After resolve() the
    # symlink itself has disappeared, so a late check cannot enforce policy.
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            target = current.resolve(strict=False)
            if target != root and root not in target.parents:
                raise ValueError("不允许通过符号链接逃逸项目目录")

    resolved = lexical.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise ValueError("路径超出已授权项目目录")
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
