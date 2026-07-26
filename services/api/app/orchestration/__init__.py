"""Orchestration layer for group discussion protocol.

This module implements the core orchestration logic for AgentPulse:
- Discussion state machine (discussing -> aligned)
- Multi-agent speaker selection + discussion rounds
- Consensus brief management
- Task creation gate (must have confirmed brief)
- Capability catalog (system-level static asset)
- Provisioning orchestration (role_spec drafting + SOUL generation)

See ADR 0006 for design details.
"""

from app.orchestration.brief import (
    create_brief,
    confirm_brief,
    reject_brief,
    get_brief_by_id,
    serialize_brief,
)
from app.orchestration.capability_catalog import (
    CATALOG,
    ROLE_BUNDLES,
    CapabilityDef,
    get_capability,
    get_role_bundle,
    list_role_bundles,
    resolve_bundle,
    validate_capability_keys,
)
from app.orchestration.discussion import (
    DiscussionStatus,
    get_discussion_status,
    set_discussion_status,
    select_next_speaker,
    resolve_next_speaker,
    build_speaker_selection_prompt,
    run_discussion_round,
    build_discussion_context,
    check_convergence,
    build_convergence_prompt,
    build_brief_draft_prompt,
    build_discussion_agent_prompt,
)
from app.orchestration.gate import (
    validate_task_creation_gate,
    TaskCreationGateError,
)
from app.orchestration.provisioning import (
    RoleSpecDraft,
    build_role_spec_prompt,
    draft_role_spec,
)
from app.orchestration.supply import (
    ProvisioningError,
    create_agent_spec,
    provision,
)

__all__ = [
    "create_brief",
    "confirm_brief",
    "reject_brief",
    "get_brief_by_id",
    "serialize_brief",
    "DiscussionStatus",
    "get_discussion_status",
    "set_discussion_status",
    "select_next_speaker",
    "resolve_next_speaker",
    "build_speaker_selection_prompt",
    "run_discussion_round",
    "build_discussion_context",
    "check_convergence",
    "build_convergence_prompt",
    "build_brief_draft_prompt",
    "build_discussion_agent_prompt",
    "validate_task_creation_gate",
    "TaskCreationGateError",
    "CATALOG",
    "ROLE_BUNDLES",
    "CapabilityDef",
    "get_capability",
    "get_role_bundle",
    "list_role_bundles",
    "resolve_bundle",
    "validate_capability_keys",
    "RoleSpecDraft",
    "build_role_spec_prompt",
    "draft_role_spec",
    "ProvisioningError",
    "create_agent_spec",
    "provision",
]
