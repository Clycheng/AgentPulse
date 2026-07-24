"""Small, fail-closed parser for DeepSeek's DSML tool-call output.

DSML is a provider wire format, not an execution protocol. This module turns
only the supported invoke/parameter grammar into normal ToolCall objects; any
ambiguous or incomplete block is returned as an error and must not execute.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any


_MARKER = "<｜｜DSML｜｜"
_KNOWN_TAGS = ("tool_calls", "invoke", "parameter")


@dataclass
class DsmlResult:
    marker_found: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)
    clean_text: str = ""
    errors: list[str] = field(default_factory=list)


class _DsmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.calls: list[dict[str, Any]] = []
        self.current_call: dict[str, Any] | None = None
        self.current_parameter: dict[str, str] | None = None
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        if tag == "invoke":
            if self.current_call is not None:
                self.errors.append("嵌套 invoke 不受支持")
                return
            name = attrs_dict.get("name", "").strip()
            if not name:
                self.errors.append("invoke 缺少工具名称")
                return
            self.current_call = {"name": name, "arguments": {}}
        elif tag == "parameter":
            if self.current_call is None:
                self.errors.append("parameter 不在 invoke 内")
                return
            if self.current_parameter is not None:
                self.errors.append("嵌套 parameter 不受支持")
                return
            name = attrs_dict.get("name", "").strip()
            if not name:
                self.errors.append("parameter 缺少名称")
                return
            self.current_parameter = {
                "name": name,
                "string": attrs_dict.get("string", "true").lower(),
                "value": "",
            }

    def handle_data(self, data: str) -> None:
        if self.current_parameter is not None:
            self.current_parameter["value"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "parameter" and self.current_parameter is not None:
            parameter = self.current_parameter
            self.current_parameter = None
            value: Any = parameter["value"].strip()
            if parameter["string"] != "true":
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    self.errors.append(f"参数 {parameter['name']} 不是合法 JSON")
                    return
            assert self.current_call is not None
            self.current_call["arguments"][parameter["name"]] = value
        elif tag == "invoke" and self.current_call is not None:
            self.calls.append(self.current_call)
            self.current_call = None

    def close(self) -> None:
        super().close()
        if self.current_parameter is not None:
            self.errors.append("parameter 未闭合")
        if self.current_call is not None:
            self.errors.append("invoke 未闭合")


def _normalize_markers(text: str) -> str:
    open_tool_calls = False

    def replace(match: re.Match[str]) -> str:
        nonlocal open_tool_calls
        closing = bool(match.group(1) or match.group(2))
        tag = match.group(3)
        attrs = match.group(4) or ""
        if closing:
            return f"</{tag}>"
        if tag == "tool_calls":
            if open_tool_calls:
                return "</tool_calls>"
            open_tool_calls = True
            return "<tool_calls>"
        # DeepSeek emits the closing invoke/parameter marker with the same
        # name and no attributes. An attributed tag is always an opening tag.
        if not attrs.strip():
            return f"</{tag}>"
        return f"<{tag}{attrs}>"

    pattern = r"<(/)?｜｜DSML｜｜(/)?(tool_calls|invoke|parameter)([^>]*)>"
    return re.sub(pattern, replace, text)


def _strip_block(text: str) -> str:
    normalized = _normalize_markers(text)
    normalized = re.sub(r"<tool_calls\b[^>]*>.*?</tool_calls>", "", normalized, flags=re.S)
    normalized = re.sub(r"<tool_calls\b[^>]*>.*$", "", normalized, flags=re.S)
    normalized = re.sub(r"</?(?:invoke|parameter)\b[^>]*>", "", normalized)
    normalized = normalized.replace(_MARKER, "")
    return normalized.strip()


def parse_dsml(text: str) -> DsmlResult:
    if _MARKER not in text and "<｜｜DSML｜｜" not in text:
        return DsmlResult(clean_text=text.strip())

    normalized = _normalize_markers(text)
    parser = _DsmlParser()
    try:
        parser.feed(normalized)
        parser.close()
    except Exception as exc:  # fail closed; provider text is untrusted input
        parser.errors.append(f"DSML 解析异常: {exc}")

    errors = list(parser.errors)
    if not parser.calls and not errors:
        errors.append("DSML 中没有可执行的 invoke")
    calls: list[dict[str, Any]] = []
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    for index, call in enumerate(parser.calls):
        calls.append({
            "id": f"dsml_{digest}_{index}",
            "name": call["name"],
            "arguments": call["arguments"],
        })
    return DsmlResult(
        marker_found=True,
        calls=calls if not errors else [],
        clean_text=_strip_block(text),
        errors=errors,
    )
