from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class LocalDeviceRegisterIn(BaseModel):
    device_name: str = Field(default="", max_length=120)
    replaces_device_id: str | None = Field(default=None, max_length=80)
    platform: Literal["darwin", "win32", "linux"]
    architecture: str = Field(default="", max_length=40)
    worker_version: str = Field(default="", max_length=40)
    hermes_version: str = Field(default="", max_length=40)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class LocalDeviceHeartbeatIn(BaseModel):
    worker_version: str | None = Field(default=None, max_length=40)
    hermes_version: str | None = Field(default=None, max_length=40)
    capabilities: dict[str, Any] | None = None


class LocalProjectCreateIn(BaseModel):
    device_id: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=160)
    path_hash: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    allowed_scopes: list[Literal["read", "write", "terminal", "computer_use"]] = Field(
        default_factory=lambda: ["read"], max_length=4
    )


class LocalProjectAuthorizeIn(BaseModel):
    allowed_scopes: list[Literal["read", "write", "terminal", "computer_use"]] = Field(
        default_factory=lambda: ["read"], max_length=4
    )


class LocalRuntimeSessionIn(BaseModel):
    run_id: str = Field(min_length=1, max_length=80)
    client_public_key: str = Field(min_length=40, max_length=160)


class LocalProfileSyncItem(BaseModel):
    agent_id: str = Field(min_length=1, max_length=80)
    profile_name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["ready", "failed"]
    error: str = Field(default="", max_length=500)


class LocalProfilesSyncIn(BaseModel):
    profiles: list[LocalProfileSyncItem] = Field(default_factory=list, max_length=100)


class LocalRunApprovalIn(BaseModel):
    tool_name: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=2_000)
    arguments: dict[str, Any] = Field(default_factory=dict)
    create_receipt: bool = False


class LocalReceiptStartIn(BaseModel):
    tool_name: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)


class LocalReceiptFinishIn(BaseModel):
    status: Literal["succeeded", "failed", "rejected"]
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = Field(default="", max_length=2_000)


class RunEventIn(BaseModel):
    event_seq: int = Field(ge=0, le=10_000_000)
    type: Literal[
        "message", "thinking", "tool_call", "tool_result",
        "approval_required", "status", "final"
    ]
    status: str = Field(default="", max_length=40)
    title: str = Field(default="", max_length=240)
    detail: str = Field(default="", max_length=10_000)
    payload: dict[str, Any] = Field(default_factory=dict)


class RunCompleteIn(BaseModel):
    message: str = Field(default="", max_length=50_000)
    output_message_id: str | None = Field(default=None, max_length=80)
    usage: dict[str, Any] = Field(default_factory=dict)


class RunFailIn(BaseModel):
    error: str = Field(min_length=1, max_length=4_000)


class LocalRunPauseCompleteIn(BaseModel):
    checkpoint: dict[str, Any] = Field(default_factory=dict)
