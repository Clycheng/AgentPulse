from typing import Any, Literal

from pydantic import BaseModel, Field


class HermesRunAgent(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=80)
    role: str = Field(default="", max_length=120)
    department: str = Field(default="", max_length=120)
    prompt: str = Field(min_length=1, max_length=12000)
    skills: list[str] = Field(default_factory=list, max_length=20)


class HermesRunMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=12000)
    name: str | None = Field(default=None, max_length=80)


class HermesTaskContext(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    status: str = Field(default="", max_length=40)
    priority: str = Field(default="", max_length=20)
    progress: int = Field(default=0, ge=0, le=100)
    owner_name: str | None = Field(default=None, max_length=80)
    description: str = Field(default="", max_length=2000)


class HermesAgentExperience(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    task_id: str | None = Field(default=None, max_length=80)
    outcome: str = Field(default="", max_length=40)
    summary: str = Field(default="", max_length=400)
    lessons: str = Field(default="", max_length=1000)


class HermesKnowledgeSource(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    category: str = Field(default="", max_length=80)
    content: str = Field(min_length=1, max_length=2000)


class HermesRunContext(BaseModel):
    company_name: str = Field(default="一人公司", max_length=120)
    conversation_title: str = Field(default="", max_length=160)
    agent: HermesRunAgent
    messages: list[HermesRunMessage] = Field(min_length=1, max_length=24)
    related_tasks: list[HermesTaskContext] = Field(default_factory=list, max_length=12)
    knowledge_sources: list[HermesKnowledgeSource] = Field(
        default_factory=list, max_length=5
    )
    agent_experiences: list[HermesAgentExperience] = Field(
        default_factory=list, max_length=8
    )
    discussion_context: str = Field(
        default="",
        max_length=4000,
        description="Group discussion context: other members, what they've said, role-based constraints",
    )
    cognitive_context: str = Field(
        default="",
        max_length=48000,
        description="Evidence-backed company memory selected for this turn",
    )
    context_manifest_id: str | None = Field(default=None, max_length=80)


class RunStepOut(BaseModel):
    """One entry in a run's activity trace (audit/timeline view)."""

    id: str
    type: str  # message | thinking | tool_call | tool_result | approval_required | status | final
    status: str
    title: str
    detail: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class RunOut(BaseModel):
    """One agent run, with its full step-by-step trace — the audit/timeline
    view requested independently by multiple early users (see README/CHANGELOG
    for the Product Hunt feedback this responds to)."""

    id: str
    agent_id: str
    agent_name: str
    task_id: str | None
    status: str
    provider: str
    model: str
    error: str
    created_at: str
    completed_at: str | None
    waiting_on: str | None = Field(
        default=None,
        description="Human-readable 'what/who this run is currently blocked "
        "on' (e.g. '等老板批准：发送邮件'), set only while status is "
        "waiting_user/waiting_clarify. Borrowed from service-claw-cloud's "
        "playbook_matter_state.waiting_on — surfaces the same signal the "
        "run trace already carries without requiring a click-through.",
    )
    steps: list[RunStepOut] = Field(default_factory=list)
    context_manifest_id: str | None = None
    context_event_ids: list[str] = Field(default_factory=list)
    context_memory_ids: list[str] = Field(default_factory=list)
