"""Ephemeral ACP executor launched by the Electron Local Worker.

The Electron main process owns device credentials and approval polling. This
sidecar owns only one leased run: it receives its configuration over stdin,
starts the bundled Hermes ACP subprocess, and emits JSONL events over stdout.
No credential is written to a profile or ordinary file.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_FILE_BYTES = 1_000_000
MAX_OUTPUT_BYTES = 65_536
MAX_COMMAND_SECONDS = 120
UPDATE_TYPES = {
    "AgentMessageChunk": "message",
    "UserMessageChunk": "message",
    "AgentThoughtChunk": "thinking",
    "ToolCallStart": "tool_call",
    "ToolCallProgress": "tool_result",
    "AgentPlanUpdate": "status",
    "UsageUpdate": "usage",
}


class WorkerError(RuntimeError):
    pass


def emit(kind: str, **payload: Any) -> None:
    print(json.dumps({"type": kind, **payload}, ensure_ascii=False), flush=True)


def read_initial_config() -> dict[str, Any]:
    raw = sys.stdin.readline()
    if not raw:
        raise WorkerError("Local Worker did not receive a run configuration")
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkerError("Local Worker configuration is invalid") from exc
    required = ("hermes_bin", "profile", "project_root", "prompt", "hermes_home")
    if not all(isinstance(config.get(key), str) and config[key] for key in required):
        raise WorkerError("Local Worker configuration is incomplete")
    return config


def _project_path(root: Path, requested: str, *, allow_missing: bool = False) -> Path:
    if not requested or "\x00" in requested:
        raise WorkerError("invalid project path")
    root = root.resolve(strict=True)
    candidate = Path(requested)
    lexical = candidate if candidate.is_absolute() else root / candidate
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise WorkerError("path escapes the authorized project") from exc
    if ".." in relative.parts:
        raise WorkerError("path traversal is not permitted")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            target = current.resolve(strict=False)
            if target != root and root not in target.parents:
                raise WorkerError("symlink escapes the authorized project")
    resolved = lexical.resolve(strict=not allow_missing)
    if resolved != root and root not in resolved.parents:
        raise WorkerError("path escapes the authorized project")
    return resolved


def _trim(value: str, limit: int = MAX_OUTPUT_BYTES) -> tuple[str, bool]:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="ignore") + "\n[output truncated]", True


@dataclass
class Terminal:
    process: subprocess.Popen[bytes]
    output_limit: int
    output: str = ""
    truncated: bool = False
    complete: bool = False


class LocalAcpClient:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.terminals: dict[str, Terminal] = {}
        self.agent_approval_granted = False
        self._agent_messages: list[str] = []
        self._emitted_updates: set[str] = set()
        self.control_commands: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.pause_requested = False

    def on_connect(self, connection: Any) -> None:
        """Observe inbound ACP frames as a lossless fallback for text chunks.

        The typed ``session_update`` callback remains the primary path.  Some
        Hermes/ACP combinations complete a prompt immediately after emitting
        the final notification, however, and a client shutdown can otherwise
        race that callback.  The connection observer sees the same in-flight
        JSON-RPC frame, not Hermes' local history or a synthetic response.
        """
        raw_connection = getattr(connection, "_conn", None)
        if raw_connection is not None:
            raw_connection.add_observer(self._observe_frame)

    @staticmethod
    def _text_from_update(update: Any) -> str:
        if not isinstance(update, dict):
            return ""
        if str(update.get("sessionUpdate") or update.get("session_update") or "") != "agent_message_chunk":
            return ""
        content = update.get("content")
        if isinstance(content, dict):
            return str(content.get("text") or "")
        return str(content or "")

    def _remember_agent_message(self, payload: dict[str, Any]) -> None:
        text = self._text_from_update(payload)
        # The typed callback and the raw-frame observer see the same ACP
        # notification. Keep one copy so the fallback is byte-for-byte the
        # Hermes response rather than a duplicated stream.
        if text and (not self._agent_messages or self._agent_messages[-1] != text):
            self._agent_messages.append(text)

    def _emit_session_update(self, payload: dict[str, Any]) -> None:
        """Forward every ACP update exactly once over the Worker JSONL bridge.

        Hermes can close an ACP prompt immediately after delivering its final
        notification.  The raw observer is therefore the lossless source for
        both text and native tool events; the typed callback is retained for
        compatibility and feeds this same de-duplication point.
        """
        try:
            fingerprint = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            fingerprint = repr(payload)
        if fingerprint in self._emitted_updates:
            return
        self._emitted_updates.add(fingerprint)
        self._remember_agent_message(payload)
        name = str(payload.get("sessionUpdate") or payload.get("session_update") or "")
        event_type = {
            "agent_message_chunk": "message",
            "user_message_chunk": "message",
            "agent_thought_chunk": "thinking",
            "tool_call": "tool_call",
            "tool_call_update": "tool_result",
            "agent_plan_update": "status",
            "usage_update": "usage",
        }.get(name, "status")
        emit("session_update", event_type=event_type, update=name or "ACP 更新", payload=payload)

    async def _observe_frame(self, frame: Any) -> None:
        direction = str(getattr(frame, "direction", ""))
        message = getattr(frame, "message", None)
        if not direction.endswith("INCOMING") or not isinstance(message, dict):
            return
        if str(message.get("method") or "") != "session/update":
            return
        params = message.get("params")
        if not isinstance(params, dict):
            return
        update = params.get("update")
        if isinstance(update, dict):
            self._emit_session_update(update)

    @property
    def final_text(self) -> str:
        return "".join(self._agent_messages).strip()

    async def _wait_for_decision(self, category: str, tool_call: dict[str, Any]) -> bool:
        approval_id = "local_" + secrets.token_hex(10)
        emit(
            "approval_required",
            approval_id=approval_id,
            category=category,
            tool_call=tool_call,
        )
        while True:
            message = await self.control_commands.get()
            if message.get("type") != "approval_decision":
                continue
            if message.get("approval_id") != approval_id:
                continue
            return message.get("decision") in {"allow", "allow_once", "allow_always"}

    async def session_update(self, session_id: str, update: Any, **_: Any) -> None:
        name = type(update).__name__
        try:
            # ``mode=json`` converts nested ACP/Pydantic content blocks before
            # they cross the JSONL boundary to Electron.
            payload = update.model_dump(mode="json", by_alias=True, exclude_none=True)
        except Exception:
            payload = {"update": name}
        self._emit_session_update(payload)

    async def request_permission(self, options: list, session_id: str, tool_call: Any, **_: Any):
        import acp

        try:
            info = tool_call.model_dump(by_alias=True, exclude_none=True)
        except Exception:
            info = {"title": "高风险本机操作"}
        allowed = await self._wait_for_decision(str(info.get("category") or "high_risk"), info)
        if not allowed:
            return acp.RequestPermissionResponse(
                outcome=acp.schema.DeniedOutcome(outcome="cancelled")
            )
        self.agent_approval_granted = True
        for option in options:
            if str(getattr(option, "option_id", "")) in {"allow_once", "allow_session", "allow_always"}:
                return acp.RequestPermissionResponse(
                    outcome=acp.schema.AllowedOutcome(
                        outcome="selected", option_id=option.option_id
                    )
                )
        return acp.RequestPermissionResponse(
            outcome=acp.schema.DeniedOutcome(outcome="cancelled")
        )

    async def read_text_file(self, path: str, session_id: str, limit=None, line=None, **_: Any):
        import acp

        operation_id = "op_" + secrets.token_hex(8)
        emit("operation_started", operation_id=operation_id, tool="read_file", arguments={"path": path})
        try:
            target = _project_path(self.root, path)
            if not target.is_file():
                raise WorkerError("requested path is not a regular file")
            if target.stat().st_size > MAX_FILE_BYTES:
                raise WorkerError("file exceeds the Local Worker read limit")
            content = target.read_text(encoding="utf-8")
            if line is not None:
                lines = content.splitlines()
                start = max(0, int(line) - 1)
                content = "\n".join(lines[start:])
            if limit is not None:
                content = content[: max(0, min(int(limit), MAX_FILE_BYTES))]
            emit("operation_finished", operation_id=operation_id, status="succeeded", result={"path": target.name, "bytes": len(content.encode("utf-8"))})
            return acp.ReadTextFileResponse(content=content)
        except Exception as exc:
            emit("operation_finished", operation_id=operation_id, status="failed", error=str(exc))
            raise

    async def write_text_file(self, content: str, path: str, session_id: str, **_: Any):
        operation_id = "op_" + secrets.token_hex(8)
        emit("operation_started", operation_id=operation_id, tool="write_file", arguments={"path": path, "bytes": len(content.encode("utf-8"))})
        try:
            target = _project_path(self.root, path, allow_missing=True)
            if len(content.encode("utf-8")) > MAX_FILE_BYTES:
                raise WorkerError("write payload exceeds the Local Worker limit")
            allowed = self.agent_approval_granted
            self.agent_approval_granted = False
            if not allowed:
                allowed = await self._wait_for_decision(
                    "local_write",
                    {"title": "写入本机文件", "path": str(target.relative_to(self.root)), "bytes": len(content.encode("utf-8"))},
                )
            if not allowed:
                raise WorkerError("owner rejected the file write")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            emit("operation_finished", operation_id=operation_id, status="succeeded", result={"path": str(target.relative_to(self.root)), "bytes": len(content.encode("utf-8"))})
            return None
        except Exception as exc:
            emit("operation_finished", operation_id=operation_id, status="rejected" if "rejected" in str(exc) else "failed", error=str(exc))
            raise

    async def create_terminal(self, command: str, session_id: str, args=None, cwd=None, env=None, output_byte_limit=None, **_: Any):
        import acp

        arguments = list(args or [])
        operation_id = "op_" + secrets.token_hex(8)
        emit("operation_started", operation_id=operation_id, tool="terminal", arguments={"command": command, "args": arguments})
        try:
            if not command or "\x00" in command or any("\x00" in str(item) for item in arguments):
                raise WorkerError("invalid terminal command")
            command_cwd = _project_path(self.root, cwd or str(self.root))
            allowed = self.agent_approval_granted
            self.agent_approval_granted = False
            if not allowed:
                allowed = await self._wait_for_decision(
                    "local_terminal",
                    {"title": "运行本机命令", "command": command, "args": arguments, "cwd": str(command_cwd.relative_to(self.root)) if command_cwd != self.root else "."},
                )
            if not allowed:
                raise WorkerError("owner rejected the terminal command")
            allowed_env = {"LANG", "LC_ALL", "TERM", "TZ"}
            command_env = {key: value for key, value in os.environ.items() if key in allowed_env}
            for item in env or []:
                name = str(getattr(item, "name", ""))
                value = str(getattr(item, "value", ""))
                if name in allowed_env:
                    command_env[name] = value
            proc = subprocess.Popen(
                [command, *[str(item) for item in arguments]],
                cwd=command_cwd,
                env=command_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=(os.name != "nt"),
            )
            terminal_id = "terminal_" + secrets.token_hex(8)
            self.terminals[terminal_id] = Terminal(
                process=proc,
                output_limit=max(1_024, min(int(output_byte_limit or MAX_OUTPUT_BYTES), MAX_OUTPUT_BYTES)),
            )
            emit("operation_finished", operation_id=operation_id, status="succeeded", result={"terminal_id": terminal_id})
            return acp.CreateTerminalResponse(terminal_id=terminal_id)
        except Exception as exc:
            emit("operation_finished", operation_id=operation_id, status="rejected" if "rejected" in str(exc) else "failed", error=str(exc))
            raise

    async def _collect_terminal(self, terminal_id: str) -> Terminal:
        terminal = self.terminals.get(terminal_id)
        if terminal is None:
            raise WorkerError("unknown terminal")
        if not terminal.complete:
            try:
                output, _ = await asyncio.wait_for(
                    asyncio.to_thread(terminal.process.communicate),
                    timeout=MAX_COMMAND_SECONDS,
                )
            except TimeoutError:
                terminal.process.kill()
                output, _ = await asyncio.to_thread(terminal.process.communicate)
            terminal.output, terminal.truncated = _trim(
                output.decode("utf-8", errors="replace"), terminal.output_limit
            )
            terminal.complete = True
        return terminal

    async def terminal_output(self, session_id: str, terminal_id: str, **_: Any):
        import acp

        terminal = await self._collect_terminal(terminal_id)
        return acp.TerminalOutputResponse(
            output=terminal.output,
            truncated=terminal.truncated,
            exit_status=None,
        )

    async def wait_for_terminal_exit(self, session_id: str, terminal_id: str, **_: Any):
        import acp

        terminal = await self._collect_terminal(terminal_id)
        return acp.WaitForTerminalExitResponse(exit_code=terminal.process.returncode, signal=None)

    async def release_terminal(self, session_id: str, terminal_id: str, **_: Any):
        terminal = self.terminals.pop(terminal_id, None)
        if terminal and terminal.process.poll() is None:
            terminal.process.terminate()
        return None

    async def kill_terminal(self, session_id: str, terminal_id: str, **_: Any):
        terminal = self.terminals.get(terminal_id)
        if terminal and terminal.process.poll() is None:
            terminal.process.kill()
        return None

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None


async def read_control_commands(
    client: LocalAcpClient, connection: Any, session_id: str
) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    try:
        while True:
            raw = await reader.readline()
            if not raw:
                return
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if message.get("type") == "pause":
                client.pause_requested = True
                await connection.cancel(session_id=session_id)
                emit("paused", session_id=session_id)
                return
            await client.control_commands.put(message)
    finally:
        transport.close()


async def run(config: dict[str, Any]) -> None:
    import acp

    root = Path(config["project_root"]).resolve(strict=True)
    if not root.is_dir():
        raise WorkerError("authorized project root is unavailable")
    environment = {
        "HERMES_HOME": config["hermes_home"],
        "DEEPSEEK_API_KEY": str(config.get("model_env", {}).get("DEEPSEEK_API_KEY", "")),
        "NO_COLOR": "1",
        "PYTHONNOUSERSITE": "1",
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(root)),
    }
    if not environment["DEEPSEEK_API_KEY"]:
        raise WorkerError("runtime model credential is missing")
    proc = await asyncio.create_subprocess_exec(
        config["hermes_bin"],
        "--profile",
        config["profile"],
        "acp",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=root,
        env=environment,
    )
    client = LocalAcpClient(root)
    conn = acp.connect_to_agent(
        client,
        input_stream=proc.stdin,
        output_stream=proc.stdout,
        use_unstable_protocol=True,
    )
    control_task: asyncio.Task[None] | None = None
    try:
        await conn.initialize(
            protocol_version=acp.PROTOCOL_VERSION,
            client_capabilities=acp.schema.ClientCapabilities(
                fs=acp.schema.FileSystemCapabilities(
                    read_text_file=True,
                    write_text_file=True,
                ),
                terminal=True,
            ),
        )
        servers = []
        for server in config.get("mcp_servers", []):
            servers.append(
                acp.schema.HttpMcpServer(
                    type="http",
                    name=server["name"],
                    url=server["url"],
                    headers=[
                        acp.schema.HttpHeader(name=name, value=value)
                        for name, value in server.get("headers", {}).items()
                    ],
                )
            )
        resume_session_id = str(config.get("resume_session_id") or "").strip()
        if resume_session_id:
            try:
                await conn.load_session(
                    cwd=str(root),
                    session_id=resume_session_id,
                    mcp_servers=servers,
                )
            except Exception:
                await conn.resume_session(
                    cwd=str(root),
                    session_id=resume_session_id,
                    mcp_servers=servers,
                )
            session_id = resume_session_id
            resumed = True
        else:
            session = await conn.new_session(cwd=str(root), mcp_servers=servers)
            session_id = session.session_id
            resumed = False
        emit("session_started", session_id=session_id, resumed=resumed)
        control_task = asyncio.create_task(
            read_control_commands(client, conn, session_id)
        )
        result = await conn.prompt(
            prompt=[acp.schema.TextContentBlock(type="text", text=config["prompt"])],
            session_id=session_id,
        )
        if not client.pause_requested:
            emit(
                "final",
                stop_reason=str(getattr(result, "stop_reason", "")),
                content=client.final_text,
            )
    finally:
        if control_task is not None:
            control_task.cancel()
            await asyncio.gather(control_task, return_exceptions=True)
        try:
            await conn.close()
        except Exception:
            pass
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                proc.kill()


def main() -> int:
    try:
        config = read_initial_config()
        asyncio.run(run(config))
        return 0
    except Exception as exc:
        emit("error", detail=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
