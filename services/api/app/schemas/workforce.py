from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


StoryPoints = Literal[1, 2, 3, 5, 8, 13]
WorkDecision = Literal["answered", "accepted", "deferred", "rejected", "needs_info"]
DependencyType = Literal["hard_output", "information", "review", "resource"]


class WorkRequestCreate(BaseModel):
    target_agent_id: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=80)
    source_message_id: str | None = Field(default=None, max_length=80)
    source_task_id: str | None = Field(default=None, max_length=80)
    consensus_brief_id: str | None = Field(default=None, max_length=80)


class WorkRequestDecisionIn(BaseModel):
    decision: WorkDecision
    response_content: str = Field(min_length=1, max_length=4000)
    decision_reason: str = Field(default="", max_length=2000)
    title: str | None = Field(default=None, min_length=1, max_length=160)
    expected_output: str = Field(default="", max_length=2000)
    output_type: str = Field(default="markdown", max_length=80)
    story_points: StoryPoints | None = None
    business_value: int = Field(default=3, ge=0, le=5)
    urgency: int = Field(default=2, ge=0, le=5)
    unblock_score: int = Field(default=0, ge=0, le=5)
    risk_reduction: int = Field(default=0, ge=0, le=5)
    switching_cost: float = Field(default=0, ge=0, le=20)
    risk_level: Literal["low", "medium", "high", "critical"] = "low"
    review_required: bool = False

    @model_validator(mode="after")
    def validate_task_decision(self):
        if self.decision in ("accepted", "deferred") and self.story_points is None:
            raise ValueError("accepted/deferred work requires story_points")
        return self


class CoordinationCaseCreate(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)


class CoordinationDecisionIn(BaseModel):
    decision: Literal["uphold", "defer", "reject", "needs_goal"]
    reason: str = Field(min_length=1, max_length=3000)


class TaskDependencyCreate(BaseModel):
    dependency_type: DependencyType
    depends_on_task_id: str | None = Field(default=None, max_length=80)
    resource_type: Literal[
        "server_hermes",
        "model",
        "local_file",
        "local_terminal",
        "browser_context",
        "computer_use",
        "project_write",
        "git_worktree",
    ] | None = None
    resource_key: str | None = Field(default=None, max_length=500)
    mode: Literal["shared", "exclusive"] = "exclusive"
    units: int = Field(default=1, ge=1, le=100)

    @model_validator(mode="after")
    def validate_target(self):
        if self.dependency_type == "resource":
            if not self.resource_type or not self.resource_key:
                raise ValueError("resource dependency requires resource_type and resource_key")
        elif not self.depends_on_task_id:
            raise ValueError("task dependency requires depends_on_task_id")
        return self


class TaskReviewCreate(BaseModel):
    reviewer_agent_id: str = Field(min_length=1, max_length=80)
    instructions: str = Field(default="", max_length=2000)


class ReviewDecisionIn(BaseModel):
    decision: Literal["approved", "changes_requested"]
    reason: str = Field(min_length=1, max_length=2000)


class RunPauseIn(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class RunResumeIn(BaseModel):
    reason: str = Field(default="继续此前工作", max_length=1000)


class ResourceClaim(BaseModel):
    resource_type: str = Field(min_length=1, max_length=80)
    resource_key: str = Field(min_length=1, max_length=500)
    mode: Literal["shared", "exclusive"] = "exclusive"


class LocalRunClaimIn(BaseModel):
    max_runs: int = Field(default=1, ge=1, le=32)
    available_resources: list[ResourceClaim] = Field(default_factory=list, max_length=100)


class WorkbenchOut(BaseModel):
    agent: dict[str, Any]
    state: dict[str, Any]
    current_task: dict[str, Any] | None = None
    requests: list[dict[str, Any]] = Field(default_factory=list)
    queue: list[dict[str, Any]] = Field(default_factory=list)
    paused: list[dict[str, Any]] = Field(default_factory=list)
    resources: list[dict[str, Any]] = Field(default_factory=list)
