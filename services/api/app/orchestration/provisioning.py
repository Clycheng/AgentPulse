"""Provisioning orchestration: role_spec drafting (TD-04-T3).

Core function:
- draft_role_spec: LLM drafts responsibilities + capability keys, with hard validation

SOUL.md is deterministic and runtime-neutral in ``runtime.employee_soul``.

Security rules (hardcoded, not up to LLM):
- Unknown capability keys are silently stripped
- risk_gate is always taken from catalog, never from LLM output
- domain_register is always prohibited_auto
"""

from __future__ import annotations

import json
from typing import Any

from app.orchestration.capability_catalog import (
    CATALOG,
)

# Prompts for LLM drafting
_ROLE_SPEC_SYSTEM_PROMPT = """你是一个 AI 员工配置助手。根据用户描述，输出该员工的职责和能力需求。

输出格式（严格 JSON，不要多余文字）：
{{
  "responsibilities": ["职责1", "职责2", ...],
  "suggested_capability_keys": ["key1", "key2", ...]
}}

可用能力 key（只从以下选取）：
{available_keys}

规则：
1. responsibilities 不超过 12 条，每条不超过 200 字
2. suggested_capability_keys 只从上面的可用 key 中选取
3. 根据用户描述合理推断需要的能力，宁多勿少
"""

class RoleSpecDraft:
    """Result of drafting a role spec."""

    def __init__(
        self,
        role_name: str,
        source_request: str,
        responsibilities: list[str],
        capability_keys: list[str],
        invalid_keys_stripped: list[str],
    ) -> None:
        self.role_name = role_name
        self.source_request = source_request
        self.responsibilities = responsibilities
        self.capability_keys = capability_keys
        self.invalid_keys_stripped = invalid_keys_stripped


def draft_role_spec(
    role_name: str,
    source_request: str,
    user_capability_keys: list[str] | None = None,
    llm_output: dict[str, Any] | None = None,
) -> RoleSpecDraft:
    """Draft a role specification for an agent.

    This function combines LLM-suggested capabilities with user-selected ones,
    applying hard validation rules:
    - Unknown capability keys are stripped (not an error)
    - risk_gate is always from catalog, never from LLM
    - User keys and LLM keys are unioned

    Args:
        role_name: Role name (e.g. "前端工程师")
        source_request: User's natural language description
        user_capability_keys: Keys explicitly selected by user
        llm_output: Pre-parsed LLM output dict with
            'responsibilities' and 'suggested_capability_keys'.
            If None, only user keys are used (no LLM call here).

    Returns:
        RoleSpecDraft with validated keys and stripped invalid keys listed
    """
    user_keys = set(user_capability_keys or [])
    llm_keys: set[str] = set()

    if llm_output:
        llm_keys = set(llm_output.get("suggested_capability_keys", []))

    # Union of user and LLM keys
    all_keys = user_keys | llm_keys

    # Validate: separate known from unknown
    valid_keys: list[str] = []
    invalid_keys: list[str] = []
    for key in sorted(all_keys):
        if key in CATALOG:
            valid_keys.append(key)
        else:
            invalid_keys.append(key)

    # Get responsibilities from LLM output, or empty
    responsibilities: list[str] = []
    if llm_output:
        raw_resp = llm_output.get("responsibilities", [])
        if isinstance(raw_resp, list):
            # Limit to 12 items, each <= 200 chars
            responsibilities = [
                str(r)[:200] for r in raw_resp[:12]
            ]

    return RoleSpecDraft(
        role_name=role_name,
        source_request=source_request,
        responsibilities=responsibilities,
        capability_keys=valid_keys,
        invalid_keys_stripped=invalid_keys,
    )


def build_role_spec_prompt(source_request: str) -> str:
    """Build the system prompt for LLM role_spec drafting.

    This is called by the API layer before making the LLM call.
    The LLM output is then passed to draft_role_spec().
    """
    available_keys = "\n".join(
        f"- {key}: {cap.description}" for key, cap in sorted(CATALOG.items())
    )
    return _ROLE_SPEC_SYSTEM_PROMPT.format(available_keys=available_keys)
