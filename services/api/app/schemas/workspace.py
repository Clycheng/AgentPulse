from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.agent_spec import AgentSpecOut, RoleSpecIn


class DepartmentOut(BaseModel):
    id: str
    name: str
    parent_id: str | None = None
    sort_order: int


class AgentOut(BaseModel):
    id: str
    name: str
    role: str
    description: str
    department_id: str
    prompt: str
    hue: int
    glyph: str
    status_kind: str
    status_label: str
    joined: str
    source: str
    skills: list[str]
    mcps: list[str]


class ConversationOut(BaseModel):
    id: str
    kind: Literal["dm", "group"]
    name: str
    agent_id: str | None
    member_ids: list[str]
    unread: int
    updated_at: str
    discussion_status: str = "discussing"


class MessageOut(BaseModel):
    id: str
    conversation_id: str
    sender_type: Literal["user", "agent", "system"]
    sender_id: str
    content: str
    created_at: str
    provider: str | None = None
    model: str | None = None


class TaskOut(BaseModel):
    id: str
    title: str
    description: str
    priority: str
    owner_agent_id: str | None
    suggested_agent_id: str | None = None
    suggested_agent_reason: str = ""
    status: str
    progress: int
    conversation_id: str | None
    due_date: str | None = None
    parent_task_id: str | None = None
    consensus_brief_id: str | None = None
    task_plan_id: str | None = None
    plan_item_key: str | None = None
    expected_output: str = ""
    output_type: str = "markdown"
    workflow_status: str = "queued"
    waiting_reason: str = ""
    story_points: int = 3
    priority_score: float = 0
    review_required: bool = False
    risk_level: str = "low"
    created_at: str
    updated_at: str


class TaskEventOut(BaseModel):
    id: str
    task_id: str
    conversation_id: str | None
    agent_id: str | None
    kind: str
    title: str
    content: str
    created_at: str


class TaskOutputOut(BaseModel):
    id: str
    task_id: str
    conversation_id: str | None
    agent_id: str | None
    title: str
    output_type: str
    content: str
    created_at: str


class ApprovalOut(BaseModel):
    id: str
    task_id: str | None
    conversation_id: str | None
    agent_id: str | None
    title: str
    description: str
    status: str
    risk_level: str
    type: str = "high_risk"
    payload: dict = Field(default_factory=dict)
    resolved_by: str
    resolved_at: str | None = None
    created_at: str


class AgentExperienceOut(BaseModel):
    id: str
    agent_id: str
    task_id: str | None
    outcome: str
    summary: str
    lessons: str
    created_at: str


class KnowledgeSourceOut(BaseModel):
    id: str
    title: str
    category: str
    content: str
    created_by: str
    created_at: str
    updated_at: str
    origin: str = "manual"
    source_ref: str = ""


class AgentTemplateOut(BaseModel):
    id: str
    name: str
    category_id: str
    category: str
    department: str
    description: str
    prompt: str
    skills: list[str]
    mcps: list[str]
    publisher: str = "AgentPulse 官方"
    version: str = "v0.1.0"
    status: str = "published"


class AgentTemplateCategoryOut(BaseModel):
    id: str
    name: str
    description: str
    sort_order: int


class BootstrapResponse(BaseModel):
    workspace: dict
    departments: list[DepartmentOut]
    agents: list[AgentOut]
    conversations: list[ConversationOut]
    messages_by_conversation: dict[str, list[MessageOut]]
    tasks: list[TaskOut]
    knowledge_sources: list[KnowledgeSourceOut]
    task_events_by_task: dict[str, list[TaskEventOut]]
    task_outputs_by_task: dict[str, list[TaskOutputOut]]
    approvals_by_task: dict[str, list[ApprovalOut]]
    agent_experiences_by_agent: dict[str, list[AgentExperienceOut]]
    agent_template_categories: list[AgentTemplateCategoryOut]
    agent_templates: list[AgentTemplateOut]
    anomaly_count_24h: int = Field(
        default=0,
        description="Failed runs + expired approvals in the last 24h — "
        "borrowed from service-claw-cloud's playbook_runs.anomaly_count_24h "
        "cached-cover-number pattern. A boss-facing 'did anything actually "
        "go wrong' signal, distinct from a rejected approval (that's the "
        "owner's own deliberate call, not an anomaly).",
    )


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=12000)
    target_agent_id: str | None = None
    local_project_id: str | None = None
    execution_target: Literal["server", "local_desktop"] = "server"


class SendMessageResponse(BaseModel):
    user_message: MessageOut
    agent_message: MessageOut
    agent_messages: list[MessageOut] = Field(default_factory=list)
    created_task: TaskOut | None = None
    created_agent: AgentOut | None = None


class CreateAgentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=255)
    department_name: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=12000)
    role_spec: RoleSpecIn | None = None


class RecruitAgentRequest(BaseModel):
    template_id: str
    department_name: str | None = Field(default=None, max_length=120)


class CreateKnowledgeSourceRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    category: str = Field(default="通用资料", max_length=80)
    content: str = Field(min_length=1, max_length=20000)


class ObsidianDocumentIn(BaseModel):
    relative_path: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=100000)
    modified_at: str | None = Field(default=None, max_length=80)


class ObsidianSyncRequest(BaseModel):
    documents: list[ObsidianDocumentIn] = Field(min_length=1, max_length=200)


class CreateGroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    member_ids: list[str] = Field(min_length=1, max_length=12)
    related_task_ids: list[str] = Field(default_factory=list, max_length=12)


class AddConversationMembersRequest(BaseModel):
    member_ids: list[str] = Field(min_length=1, max_length=12)


class CreateTaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    priority: str = Field(default="P2", max_length=20)
    owner_agent_id: str | None = None
    status: str = Field(default="进行中", max_length=40)
    progress: int = Field(default=0, ge=0, le=100)
    conversation_id: str | None = None
    due_date: str | None = Field(default=None, max_length=40)
    parent_task_id: str | None = None
    consensus_brief_id: str | None = None  # Gate condition: must be confirmed brief
    task_plan_id: str | None = None
    plan_item_key: str | None = Field(default=None, max_length=80)
    expected_output: str = Field(default="", max_length=2000)
    output_type: str = Field(default="markdown", max_length=80)


class UpdateTaskRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    priority: str | None = Field(default=None, max_length=20)
    owner_agent_id: str | None = None
    status: str | None = Field(default=None, max_length=40)
    progress: int | None = Field(default=None, ge=0, le=100)
    conversation_id: str | None = None
    due_date: str | None = Field(default=None, max_length=40)
    parent_task_id: str | None = None


class ClaimTaskRequest(BaseModel):
    agent_id: str


class ResolveApprovalRequest(BaseModel):
    status: Literal["approved", "rejected"]
    # TD-06-T2: for capability_upgrade approvals, the owner confirms/adjusts which
    # catalog capability to grant (defaults to the agent's suggested key).
    approved_capability_key: str | None = None
    # ADR 0008: "once" = allow this time; "always" = remember (Hermes persists it
    # to its allowlist so the same action won't ask again).
    scope: Literal["once", "always"] = "once"
