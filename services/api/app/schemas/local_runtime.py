from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class LocalDeviceRegisterIn(BaseModel):
    device_name: str = Field(default="", max_length=120)
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
