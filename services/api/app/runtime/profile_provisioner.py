"""Profile provisioner abstraction (TD-04-T2).

Abstracts the physical Hermes profile operations so that:
- RecordOnlyProvisioner: v1/tests — records actions without side effects
- LocalHermesProvisioner: T6 — real Hermes CLI calls (requires agentpulse session)

See TD-04 for design details.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


# Built-in Hermes toolsets that reach outside the chat (network, filesystem,
# code execution, real browser, real desktop, external media/search APIs).
# `hermes profile create` ships every one of these ON by default — verified
# 2026-07-15 against Hermes v0.18.2: a fresh profile granted only
# ("web", "browser") via `hermes tools enable` still had computer_use,
# terminal, file and code_execution enabled, because `tools enable` is
# additive and never touches the rest of the default set. Any capability not
# explicitly listed in the requesting agent_capabilities row must be disabled
# here, or the capability catalog's risk_gate is cosmetic — every employee
# gets full desktop/shell/network access the moment they're real-provisioned,
# regardless of what was actually granted.
GATED_TOOLSETS = frozenset({
    "web", "browser", "terminal", "file", "code_execution", "computer_use",
    "image_gen", "video_gen", "video", "x_search", "tts",
    "homeassistant", "spotify", "yuanbao",
})
HOSTED_DENIED_TOOLSETS = frozenset(
    {"web", "browser", "terminal", "file", "code_execution", "computer_use"}
)


@dataclass
class ProvisioningAction:
    """Record of a single provisioning action (for audit/testing)."""

    action: str
    profile_name: str
    details: dict = field(default_factory=dict)


class ProfileProvisioner(Protocol):
    """Abstract interface for provisioning Hermes profiles.

    Implementations:
    - RecordOnlyProvisioner: records actions, no side effects (v1/tests)
    - LocalHermesProvisioner: real Hermes CLI (TD-04-T6, needs agentpulse session)
    """

    def create_profile(self, profile_name: str) -> None:
        """Create a new Hermes profile (hermes profile create)."""
        ...

    def write_soul(self, profile_name: str, soul_md: str) -> None:
        """Write SOUL.md to the profile directory."""
        ...

    def configure(
        self,
        profile_name: str,
        *,
        model: str,
        toolsets: list[str],
        mcp: list[str],
    ) -> None:
        """Configure profile model, toolsets, and MCP servers."""
        ...

    def install_skills(self, profile_name: str, skills: list[str]) -> None:
        """Install skills into the profile."""
        ...

    def write_credentials(
        self, profile_name: str, creds: dict[str, str]
    ) -> None:
        """Write credentials to profile's .env file.

        SECURITY: credential values must NEVER appear in logs or responses.
        """
        ...

    def update_skill(self, profile_name: str, skill_name: str, content: str) -> None:
        """Persist an auto-learned skill (TD-06-T1) into the profile."""
        ...

    def list_skills(self, profile_name: str) -> list[dict]:
        """List auto-learned skills for a profile ({name, content})."""
        ...

    def add_capability(
        self,
        profile_name: str,
        capability_key: str,
        bundle: dict,
    ) -> None:
        """TD-06-T2: Add a new capability to an existing profile.

        Enables new toolsets, installs new skills, writes credentials,
        and reloads the gateway so the capability takes effect without
        recreating the profile.

        Args:
            profile_name: Target Hermes profile name.
            capability_key: The capability key from the catalog.
            bundle: Resolved bundle dict from ``resolve_bundle``, with keys
                ``toolsets``, ``skills``, ``mcp``, ``required_credentials``,
                and ``risk_gate``.
        """
        ...

    def reload_gateway(self, profile_name: str) -> None:
        """TD-06-T2: Hot-reload (or restart) the profile's gateway.

        Best-effort: if no gateway is running, this is a no-op.
        """
        ...


class RecordOnlyProvisioner:
    """Provisioner that records all actions without side effects.

    Used in v1 and tests. Allows asserting on the call sequence without
    actually touching the filesystem or running Hermes CLI.
    """

    def __init__(self) -> None:
        self.actions: list[ProvisioningAction] = []
        self.skills: dict[str, dict[str, str]] = {}

    def create_profile(self, profile_name: str) -> None:
        self.actions.append(
            ProvisioningAction(
                action="create_profile",
                profile_name=profile_name,
            )
        )

    def write_soul(self, profile_name: str, soul_md: str) -> None:
        self.actions.append(
            ProvisioningAction(
                action="write_soul",
                profile_name=profile_name,
                details={"soul_length": len(soul_md)},
            )
        )

    def configure(
        self,
        profile_name: str,
        *,
        model: str,
        toolsets: list[str],
        mcp: list[str],
    ) -> None:
        self.actions.append(
            ProvisioningAction(
                action="configure",
                profile_name=profile_name,
                details={
                    "model": model,
                    "toolsets": list(toolsets),
                    "mcp": list(mcp),
                },
            )
        )

    def install_skills(self, profile_name: str, skills: list[str]) -> None:
        self.actions.append(
            ProvisioningAction(
                action="install_skills",
                profile_name=profile_name,
                details={"skills": list(skills)},
            )
        )

    def write_credentials(
        self, profile_name: str, creds: dict[str, str]
    ) -> None:
        # SECURITY: do NOT record credential values, only key names
        self.actions.append(
            ProvisioningAction(
                action="write_credentials",
                profile_name=profile_name,
                details={"credential_keys": sorted(creds.keys())},
            )
        )

    def update_skill(self, profile_name: str, skill_name: str, content: str) -> None:
        self.skills.setdefault(profile_name, {})[skill_name] = content
        self.actions.append(
            ProvisioningAction(
                action="update_skill",
                profile_name=profile_name,
                details={"skill_name": skill_name, "content_length": len(content)},
            )
        )

    def list_skills(self, profile_name: str) -> list[dict]:
        return [
            {"name": name, "content": content}
            for name, content in self.skills.get(profile_name, {}).items()
        ]

    def add_capability(
        self,
        profile_name: str,
        capability_key: str,
        bundle: dict,
    ) -> None:
        self.actions.append(
            ProvisioningAction(
                action="add_capability",
                profile_name=profile_name,
                details={
                    "capability_key": capability_key,
                    "toolsets": list(bundle.get("toolsets", [])),
                    "skills": list(bundle.get("skills", [])),
                    "mcp": list(bundle.get("mcp", [])),
                    "required_credentials": list(
                        bundle.get("required_credentials", [])
                    ),
                },
            )
        )

    def reload_gateway(self, profile_name: str) -> None:
        self.actions.append(
            ProvisioningAction(
                action="reload_gateway",
                profile_name=profile_name,
            )
        )

    def get_actions(self) -> list[ProvisioningAction]:
        """Return recorded actions (for test assertions)."""
        return list(self.actions)

    def clear(self) -> None:
        """Clear recorded actions."""
        self.actions.clear()
        self.skills.clear()


class HermesProvisionError(RuntimeError):
    """Raised when a `hermes` CLI provisioning command fails."""


class LocalHermesProvisioner:
    """Real provisioner — drives the `hermes` CLI to create + configure profiles.

    Verified against Hermes Agent v0.18.2 (2026-07) with these exact commands:
      - hermes profile create <name> --no-alias --no-skills
      - hermes --profile <name> config set model <provider/model>
      - hermes --profile <name> config set terminal.working_dir <ABS>
      - hermes --profile <name> tools enable <toolset...>
      - hermes --profile <name> skills install <id> --yes
      - credentials appended to <profile>/.env (path via `config env-path`)

    ADR 0005: every profile's terminal.working_dir is forced to an absolute path
    under ``work_root`` so an agent's file/tool activity can never escape into an
    unrelated repo. ``--no-alias`` avoids writing wrapper scripts into
    ~/.local/bin (the import-wrapper pitfall noted in the Hermes facts).

    Must run in an agentpulse-anchored session (never inside UnitPulse).
    """

    def __init__(
        self,
        *,
        work_root: str,
        hermes_bin: str = "hermes",
        default_model: str = "deepseek/deepseek-v4-flash",
        timeout: int = 120,
        hosted_safe_mode: bool = False,
    ) -> None:
        if not os.path.isabs(work_root):
            raise HermesProvisionError(
                f"work_root must be an absolute path (ADR 0005): {work_root!r}"
            )
        self.work_root = work_root
        self.hermes_bin = hermes_bin
        self.default_model = default_model
        self.timeout = timeout
        self.hosted_safe_mode = hosted_safe_mode

    def _run(self, args: list[str], *, profile: str | None = None) -> str:
        cmd = [self.hermes_bin]
        if profile:
            cmd += ["--profile", profile]
        cmd += args
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout
            )
        except FileNotFoundError as exc:
            raise HermesProvisionError(
                f"`{self.hermes_bin}` not found on PATH"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise HermesProvisionError(f"`{' '.join(cmd)}` timed out") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise HermesProvisionError(f"`{' '.join(cmd)}` failed: {detail}")
        return proc.stdout

    def _profile_dir(self, profile_name: str) -> Path:
        env_path = self._run(["config", "env-path"], profile=profile_name).strip()
        return Path(env_path).parent

    def _profile_exists(self, profile_name: str) -> bool:
        listing = self._run(["profile", "list"])
        # Names appear as bare tokens (the default profile is prefixed with ◆).
        tokens = listing.replace("◆", " ").split()
        return profile_name in tokens

    # --- ProfileProvisioner protocol ---

    def create_profile(self, profile_name: str) -> None:
        if self._profile_exists(profile_name):
            return  # idempotent
        self._run(
            ["profile", "create", profile_name, "--no-alias", "--no-skills"]
        )

    def write_soul(self, profile_name: str, soul_md: str) -> None:
        (self._profile_dir(profile_name) / "SOUL.md").write_text(
            soul_md, encoding="utf-8"
        )

    def configure(
        self,
        profile_name: str,
        *,
        model: str,
        toolsets: list[str],
        mcp: list[str],
    ) -> None:
        self._run(
            ["config", "set", "model", model or self.default_model],
            profile=profile_name,
        )
        # ADR 0005: bind an absolute, per-profile work dir; create it up front.
        workdir = os.path.join(self.work_root, profile_name, "work")
        os.makedirs(workdir, exist_ok=True)
        self._run(
            ["config", "set", "terminal.working_dir", workdir],
            profile=profile_name,
        )
        # ADR 0008: force human-in-the-loop approvals. Without this Hermes falls
        # to `smart` mode and an auxiliary model auto-approves dangerous actions
        # (verified: agent ran `rm -rf` with 0 approvals). `manual` routes them
        # through ACP request_permission → our approval bridge → the owner.
        self._run(
            ["config", "set", "approvals.mode", "manual"],
            profile=profile_name,
        )
        # Deliberately NOT writing `approvals.timeout` here (item 4 of ADR
        # 0008's "待实现" list once suggested it). Verified against the
        # installed Hermes source: the ACP adapter's approval callback
        # (acp_adapter/permissions.py::make_approval_callback, invoked from
        # acp_adapter/server.py:1421) hardcodes a 60s fail-close and never
        # reads `approvals.timeout` — that config key only feeds the
        # CLI-interactive prompt (tools/approval.py::prompt_dangerous_approval).
        # Writing it here would be a dead config write. The real fix lives on
        # our side: `settings.approval_bridge_timeout_seconds` in the durable
        # database polling resolver, kept comfortably under 60s so we
        # always resolve before Hermes's hardcoded timeout would.
        effective_toolsets = set(toolsets)
        if self.hosted_safe_mode:
            effective_toolsets -= HOSTED_DENIED_TOOLSETS
        if effective_toolsets:
            self._run(
                ["tools", "enable", *sorted(effective_toolsets)],
                profile=profile_name,
            )
        # Deny-by-default: disable every gated toolset this agent's granted
        # capabilities didn't ask for (see GATED_TOOLSETS docstring — a fresh
        # profile ships with computer_use/terminal/file/code_execution/web/
        # browser already on, and `tools enable` above is additive only).
        to_disable = sorted(GATED_TOOLSETS - effective_toolsets)
        if to_disable:
            self._run(["tools", "disable", *to_disable], profile=profile_name)
        # NOTE: real MCP servers need endpoints/auth (`hermes mcp add ...`); the
        # catalog only carries logical names, so MCP wiring is deferred until a
        # channel/tool endpoint exists. Left intentionally unhandled here.

    def install_skills(self, profile_name: str, skills: list[str]) -> None:
        for skill in skills:
            self._run(["skills", "install", skill, "--yes"], profile=profile_name)

    def write_credentials(
        self, profile_name: str, creds: dict[str, str]
    ) -> None:
        # SECURITY: values are written only to the profile's gitignored .env,
        # never logged or returned.
        if self.hosted_safe_mode and creds:
            raise HermesProvisionError(
                "hosted profiles cannot persist provider credentials"
            )
        env_path = self._profile_dir(profile_name) / ".env"
        with env_path.open("a", encoding="utf-8") as handle:
            for key, value in creds.items():
                handle.write(f"{key}={value}\n")

    def _auto_skills_dir(self, profile_name: str) -> Path:
        return self._profile_dir(profile_name) / "skills" / "auto"

    @staticmethod
    def _skill_filename(skill_name: str) -> str:
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", skill_name.strip()).strip("-").lower()
        if not safe:
            # All-CJK (or otherwise non-ASCII) names sanitize to empty; hash the
            # original so distinct names get distinct files instead of colliding.
            digest = hashlib.sha256(skill_name.strip().encode("utf-8")).hexdigest()[:10]
            safe = f"skill-{digest}"
        return safe[:64] + ".md"

    def update_skill(self, profile_name: str, skill_name: str, content: str) -> None:
        # TD-06-T1: auto-learned skills live under skills/auto/ as SKILL.md fragments.
        skills_dir = self._auto_skills_dir(profile_name)
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / self._skill_filename(skill_name)).write_text(
            content, encoding="utf-8"
        )

    def list_skills(self, profile_name: str) -> list[dict]:
        skills_dir = self._auto_skills_dir(profile_name)
        if not skills_dir.is_dir():
            return []
        out: list[dict] = []
        for path in sorted(skills_dir.glob("*.md")):
            out.append(
                {"name": path.stem, "content": path.read_text(encoding="utf-8")}
            )
        return out

    def delete_profile(self, profile_name: str) -> None:
        """Tear down a profile (employee removed). Best-effort."""
        if self._profile_exists(profile_name):
            self._run(["profile", "delete", profile_name, "--yes"])

    def add_capability(
        self,
        profile_name: str,
        capability_key: str,
        bundle: dict,
    ) -> None:
        """TD-06-T2: Add a new capability to an existing profile.

        Enables new toolsets, installs new skills, writes credentials,
        and reloads the gateway.
        """
        new_toolsets: list[str] = bundle.get("toolsets", [])
        if self.hosted_safe_mode:
            new_toolsets = [
                tool for tool in new_toolsets if tool not in HOSTED_DENIED_TOOLSETS
            ]
        new_skills: list[str] = bundle.get("skills", [])

        # Enable new toolsets (append to existing)
        if new_toolsets:
            self._run(["tools", "enable", *new_toolsets], profile=profile_name)

        # Install new skills
        if new_skills:
            self.install_skills(profile_name, new_skills)

        # Credentials — already stored in .env from initial provisioning,
        # and required_credentials for added capabilities may need new ones.
        # The caller (UpgradeService) handles credential_missing separately;
        # here we only write credentials that are actually provided.
        # MCP wiring remains deferred (same as configure()).

        # No reload needed: our execution model spawns a fresh `hermes acp`
        # subprocess per run, which re-reads config.yaml — so a newly enabled
        # toolset is available on the NEXT run automatically.
        self.reload_gateway(profile_name)

    def reload_gateway(self, profile_name: str) -> None:
        """No-op in the ACP execution model.

        We do NOT run a persistent per-employee gateway; each run is a fresh
        ``hermes acp`` subprocess that reads config.yaml at startup. (`hermes
        gateway reload` is not a real subcommand — verified against v0.18.2 —
        and there is no long-lived process to reload here.) Kept for interface
        compatibility; a config change takes effect on the next run.
        """
        return None


def build_provisioner_from_settings() -> ProfileProvisioner:
    """Pick the provisioner from config: LocalHermes when hermes_provisioning is
    on (real `hermes` CLI), else RecordOnly (tests / non-Hermes envs)."""
    from app.core.config import settings

    if settings.hermes_provisioning:
        work_root = os.path.abspath(settings.hermes_work_root or ".hermes-data")
        return LocalHermesProvisioner(
            work_root=work_root,
            hermes_bin=settings.hermes_bin,
            hosted_safe_mode=settings.hermes_hosted_safe_mode,
        )
    return RecordOnlyProvisioner()
