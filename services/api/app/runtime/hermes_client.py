"""HermesBackend — drive a Hermes employee via ACP (TD-03-T2, per ADR 0007).

Hermes v0.18.x exposes no REST Runs API; the programmatic surface is the Agent
Client Protocol (`hermes --profile <p> acp`, newline-delimited JSON-RPC over
stdio). This module spawns that subprocess, speaks ACP with the official
``agent-client-protocol`` client library, and turns the session's streaming
updates into a uniform ``AgentEvent`` stream that RunService (TD-03-T3) writes
to ``run_steps`` / messages.

Safety (ADR 0005): ``workdir`` must be absolute; the subprocess cwd and the
ACP session cwd are both bound to it, and the client's file-I/O handlers refuse
paths that escape it.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Reasoning is forwarded through step updates; map ACP update class names to our
# coarse AgentEvent types.
_UPDATE_TYPE_MAP = {
    "AgentMessageChunk": "message",
    "UserMessageChunk": "message",
    "AgentThoughtChunk": "thinking",
    "ToolCallStart": "tool_call",
    "ToolCallProgress": "tool_result",
    "AgentPlanUpdate": "status",
    "UsageUpdate": "usage",
}

# Permission decision: given the tool-call info, return one of
# "allow_once" | "allow_always" | "deny". Default policy denies (safe).
PermissionResolver = Callable[[dict], Awaitable[str]]
_ENV_PASSTHROUGH = (
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "TZ",
    "TMPDIR",
)
_RUN_SECRET_ENV = frozenset({"DEEPSEEK_API_KEY"})


@dataclass
class RunContext:
    run_id: str
    prompt: str
    workdir: str  # absolute isolation dir (ADR 0005)
    profile: str  # hermes profile name (= employee)
    agent_id: str = ""
    workspace_id: str = ""
    conversation_id: str = ""
    task_id: str = ""
    context_manifest_id: str | None = None
    mcp_servers: list[dict] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    timeout: int = 600


@dataclass
class AgentEvent:
    """Uniform event from a Hermes run (maps to run_steps rows)."""

    type: str  # message | thinking | tool_call | tool_result | approval_required | final | usage | error
    payload: dict = field(default_factory=dict)


class HermesBackendError(RuntimeError):
    pass


def _subprocess_environment(overrides: dict[str, str]) -> dict[str, str]:
    unknown = set(overrides) - _RUN_SECRET_ENV
    if unknown:
        raise HermesBackendError(
            f"unsupported Hermes environment variables: {', '.join(sorted(unknown))}"
        )
    environment = {
        name: value for name in _ENV_PASSTHROUGH if (value := os.environ.get(name))
    }
    environment.update(overrides)
    return environment


def _build_mcp_servers(acp_module: Any, servers: list[dict]) -> list[Any]:
    """Convert per-run company endpoints to ACP's typed MCP representation."""
    return [
        acp_module.schema.HttpMcpServer(
            type="http",
            name=server["name"],
            url=server["url"],
            headers=[
                acp_module.schema.HttpHeader(name=name, value=value)
                for name, value in server.get("headers", {}).items()
            ],
        )
        for server in servers
    ]


def _safe_path(workdir: str, path: str) -> Path:
    """Resolve ``path`` and refuse anything outside ``workdir`` (ADR 0005)."""
    root = Path(workdir).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if root != resolved and root not in resolved.parents:
        raise HermesBackendError(f"path escapes workdir: {path!r}")
    return resolved


def _make_client(
    queue: asyncio.Queue,
    workdir: str,
    permission_resolver: PermissionResolver | None,
):
    """Build an ACP Client that pushes AgentEvents onto ``queue``."""
    import acp

    class _StreamClient:
        async def session_update(self, session_id: str, update: Any, **_: Any) -> None:
            name = type(update).__name__
            etype = _UPDATE_TYPE_MAP.get(name, "status")
            try:
                data = update.model_dump(by_alias=True, exclude_none=True)
            except Exception:
                data = {"repr": repr(update)}
            await queue.put(AgentEvent(etype, {"update": name, **data}))

        async def request_permission(
            self, options: list, session_id: str, tool_call: Any, **_: Any
        ):
            try:
                info = tool_call.model_dump(by_alias=True, exclude_none=True)
            except Exception:
                info = {"repr": repr(tool_call)}
            # Correlate the emitted event with the resolver's wait via one id, so
            # RunService can persist an approval row keyed to this exact request
            # and the /resolve endpoint can wake the same suspended run.
            approval_id = "appr_" + secrets.token_hex(8)
            category = str(info.get("category") or "high_risk")
            await queue.put(
                AgentEvent(
                    "approval_required",
                    {"approval_id": approval_id, "category": category, "tool_call": info},
                )
            )
            decision = "deny"
            if permission_resolver is not None:
                decision = await permission_resolver(
                    {"approval_id": approval_id, "category": category, **info}
                )
            # Map the owner's decision to a concrete offered option, then return
            # the outcome Hermes actually understands. CRITICAL (verified against
            # Hermes v0.18.2 acp_adapter/permissions.py): the agent only treats a
            # response as "allowed" when it is an ``AllowedOutcome``; a
            # ``DeniedOutcome`` (or anything else) is read as deny. Hermes maps
            # the allow back by OPTION ID (allow_once/allow_session/allow_always),
            # so we must match by option_id first (kinds are ambiguous — Hermes
            # gives "allow_session" the kind "allow_always").
            allow = decision in ("allow", "allow_once", "allow_always")
            if decision == "allow_always":
                prefer_ids = ("allow_always", "allow_session", "allow_once")
                prefer_kinds = ("allow_always", "allow_once")
            elif allow:  # allow_once / generic allow
                prefer_ids = ("allow_once", "allow_session", "allow_always")
                prefer_kinds = ("allow_once", "allow_always")
            else:  # deny
                prefer_ids = ("deny", "deny_always")
                prefer_kinds = ("reject_once", "reject_always")

            def _pick():
                for oid in prefer_ids:
                    for opt in options:
                        if str(getattr(opt, "option_id", "")) == oid:
                            return opt
                for kind in prefer_kinds:
                    for opt in options:
                        if str(getattr(opt, "kind", "")) == kind:
                            return opt
                return None

            chosen = _pick()
            if allow and chosen is not None:
                return acp.RequestPermissionResponse(
                    outcome=acp.schema.AllowedOutcome(
                        outcome="selected", option_id=chosen.option_id
                    )
                )
            # Deny (or allow requested but no allow option offered → fail closed).
            return acp.RequestPermissionResponse(
                outcome=acp.schema.DeniedOutcome(outcome="cancelled")
            )

        async def read_text_file(
            self, path: str, session_id: str, limit=None, line=None, **_: Any
        ):
            content = _safe_path(workdir, path).read_text(encoding="utf-8")
            return acp.ReadTextFileResponse(content=content)

        async def write_text_file(
            self, content: str, path: str, session_id: str, **_: Any
        ):
            target = _safe_path(workdir, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return None

        # Terminal toolset is disabled for employees by default; deny if asked.
        async def create_terminal(self, *a: Any, **k: Any):
            raise HermesBackendError("terminal tool not permitted for this employee")

        async def terminal_output(self, *a: Any, **k: Any):
            raise HermesBackendError("terminal tool not permitted")

        async def release_terminal(self, *a: Any, **k: Any):
            return None

        async def wait_for_terminal_exit(self, *a: Any, **k: Any):
            raise HermesBackendError("terminal tool not permitted")

        async def kill_terminal(self, *a: Any, **k: Any):
            return None

        async def ext_method(self, method: str, params: dict) -> dict:
            return {}

        async def ext_notification(self, method: str, params: dict) -> None:
            return None

    return _StreamClient()


class HermesBackend:
    """Runs a prompt on a Hermes employee profile over ACP, streaming events."""

    def __init__(self, *, hermes_bin: str = "hermes") -> None:
        self.hermes_bin = hermes_bin

    async def run(
        self,
        ctx: RunContext,
        *,
        permission_resolver: PermissionResolver | None = None,
    ) -> AsyncIterator[AgentEvent]:
        if not os.path.isabs(ctx.workdir):
            raise HermesBackendError(
                f"workdir must be absolute (ADR 0005): {ctx.workdir!r}"
            )
        os.makedirs(ctx.workdir, exist_ok=True)

        try:
            import acp
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise HermesBackendError(
                "agent-client-protocol not installed (pip install agent-client-protocol)"
            ) from exc

        proc = await asyncio.create_subprocess_exec(
            self.hermes_bin,
            "--profile",
            ctx.profile,
            "acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=ctx.workdir,
            env=_subprocess_environment(ctx.environment),
        )

        queue: asyncio.Queue = asyncio.Queue()
        client = _make_client(queue, ctx.workdir, permission_resolver)
        conn = acp.connect_to_agent(
            client,
            input_stream=proc.stdin,
            output_stream=proc.stdout,
            use_unstable_protocol=True,
        )

        async def drive() -> AgentEvent:
            await conn.initialize(protocol_version=acp.PROTOCOL_VERSION)
            mcp_servers = _build_mcp_servers(acp, ctx.mcp_servers)
            session = await conn.new_session(cwd=ctx.workdir, mcp_servers=mcp_servers)
            result = await conn.prompt(
                prompt=[acp.schema.TextContentBlock(type="text", text=ctx.prompt)],
                session_id=session.session_id,
            )
            stop = getattr(result, "stop_reason", None)
            return AgentEvent("final", {"stop_reason": str(stop)})

        drive_task = asyncio.create_task(drive())
        suspended = False  # awaiting owner approval — don't apply the run timeout
        try:
            while True:
                queue_get = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {queue_get, drive_task},
                    timeout=None if suspended else ctx.timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if queue_get in done:
                    ev = queue_get.result()
                    # An approval request suspends the run until the owner resolves
                    # it (could take minutes); resumption arrives as the next event.
                    suspended = ev.type == "approval_required"
                    yield ev
                    continue
                queue_get.cancel()
                if drive_task in done:
                    # Flush any buffered events, then emit the final result.
                    while not queue.empty():
                        yield queue.get_nowait()
                    yield drive_task.result()
                    return
                # timeout
                yield AgentEvent("error", {"detail": "hermes run timed out"})
                return
        finally:
            drive_task.cancel()
            try:
                await conn.close()
            except Exception:
                pass
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except Exception:
                    proc.kill()
