from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TaskPlanTaskOut(BaseModel):
    id: str
    title: str
    description: str
    owner_agent_id: str | None
    status: str
    progress: int
    parent_task_id: str | None
    plan_item_key: str | None
    expected_output: str
    output_type: str
    blocked_reason: str = ""
    outputs: list[dict[str, Any]] = Field(default_factory=list)
    approvals: list[dict[str, Any]] = Field(default_factory=list)
    runs: list[dict[str, Any]] = Field(default_factory=list)
    business_actions: list[dict[str, Any]] = Field(default_factory=list)


class TaskDependencyOut(BaseModel):
    task_id: str
    depends_on_task_id: str


class TaskPlanOut(BaseModel):
    id: str
    workspace_id: str
    brief_id: str
    root_task_id: str | None
    status: str
    revision_count: int
    blocked_reason: str
    created_at: str
    updated_at: str
    completed_at: str | None
    tasks: list[TaskPlanTaskOut] = Field(default_factory=list)
    dependencies: list[TaskDependencyOut] = Field(default_factory=list)


class TaskRunOut(BaseModel):
    id: str
    task_id: str
    status: str
    attempt_no: int
    error: str
    lease_owner: str | None
    lease_expires_at: str | None
    started_at: str | None
    created_at: str
    completed_at: str | None
    steps: list[dict[str, Any]] = Field(default_factory=list)
    business_actions: list[dict[str, Any]] = Field(default_factory=list)
    context_manifest_id: str | None = None
    context_event_ids: list[str] = Field(default_factory=list)
    context_memory_ids: list[str] = Field(default_factory=list)


class ResumeTaskRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
