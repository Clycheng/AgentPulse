"""Tests for HermesBackend (TD-03-T2, ACP transport per ADR 0007).

The e2e tests drive real Hermes over ACP and call DeepSeek, so they are SKIPPED
by default. To run them you need Hermes installed, the `agentpulse` profile
provisioned with a DeepSeek key, and an agentpulse-anchored shell (ADR 0005):

    HERMES_E2E=1 pytest tests/test_hermes_backend.py

`test_run_expires_pending_approval_instead_of_hanging` additionally needs the
`agentpulse` profile's `approvals.mode` set to `manual` (real employee profiles
get this from `LocalHermesProvisioner.configure`, but a hand-provisioned e2e
test profile won't have it unless set explicitly):

    hermes -p agentpulse config set approvals.mode manual

The always-on tests cover the ADR 0005 safety invariants without touching Hermes.
"""

import asyncio
import os
import shutil
import tempfile
import time
import pytest

from app.runtime.hermes_client import (
    AgentEvent,
    HermesBackend,
    HermesBackendError,
    RunContext,
    _build_mcp_servers,
    _safe_path,
)

_E2E = os.environ.get("HERMES_E2E") == "1" and shutil.which("hermes") is not None
requires_hermes = pytest.mark.skipif(
    not _E2E, reason="set HERMES_E2E=1 with hermes + agentpulse profile to run"
)


# --- Always-on: safety invariants ---

def test_relative_workdir_rejected():
    be = HermesBackend()
    ctx = RunContext(
        run_id="r", prompt="hi", workdir="relative/dir", profile="agentpulse"
    )
    gen = be.run(ctx)
    with pytest.raises(HermesBackendError):
        asyncio.run(gen.__anext__())


def test_safe_path_blocks_escape():
    from pathlib import Path

    # resolve() up front — macOS tempdirs live under a /var -> /private/var symlink
    root = str(Path(tempfile.mkdtemp()).resolve())
    # inside is fine
    assert str(_safe_path(root, "notes.txt")).startswith(root)
    # escaping is refused
    with pytest.raises(HermesBackendError):
        _safe_path(root, "../../etc/passwd")


def test_dynamic_http_mcp_servers_include_run_authorization_header():
    import acp

    servers = _build_mcp_servers(
        acp,
        [
            {
                "name": "agentpulse-company",
                "url": "http://127.0.0.1:8000/mcp/company-tools/",
                "headers": {"Authorization": "Bearer signed-run-token"},
            }
        ],
    )
    assert servers[0].name == "agentpulse-company"
    assert servers[0].headers[0].name == "Authorization"
    assert servers[0].headers[0].value == "Bearer signed-run-token"
    request = acp.schema.NewSessionRequest(cwd="/tmp", mcpServers=servers)
    assert request.mcp_servers[0].type == "http"


# --- E2E: real Hermes over ACP ---

@requires_hermes
def test_run_reaches_final_with_message():
    work = tempfile.mkdtemp(prefix="ap-hermes-run-")
    be = HermesBackend()
    ctx = RunContext(
        run_id="run_e2e",
        prompt="Reply with exactly: OK",
        workdir=work,
        profile="agentpulse",
        timeout=120,
    )

    async def collect() -> list[AgentEvent]:
        return [ev async for ev in be.run(ctx)]

    events = asyncio.run(collect())
    types = [e.type for e in events]
    assert "final" in types
    assert types[-1] == "final"
    assert any(e.type == "message" for e in events)
    # message chunks carry the reply text
    text = "".join(
        e.payload.get("content", {}).get("text", "")
        for e in events
        if e.type == "message"
    )
    assert "OK" in text


@requires_hermes
def test_run_expires_pending_approval_instead_of_hanging():
    """A denied permission must let the ACP session reach a final event."""

    work = tempfile.mkdtemp(prefix="ap-hermes-timeout-")
    be = HermesBackend()
    ctx = RunContext(
        run_id="run_e2e_timeout",
        prompt=(
            "Run the shell command `mkdir -p scratch && rm -rf scratch` in "
            "your working directory."
        ),
        workdir=work,
        profile="agentpulse",
        timeout=120,
    )

    async def resolver(info: dict) -> str:
        del info
        return "expired"

    async def collect() -> list[AgentEvent]:
        return [ev async for ev in be.run(ctx, permission_resolver=resolver)]

    started = time.monotonic()
    events = asyncio.run(collect())
    elapsed = time.monotonic() - started
    types = [e.type for e in events]
    assert "approval_required" in types
    assert "final" in types
    assert elapsed < 30, f"run did not resolve promptly after expiry: {elapsed}s"
