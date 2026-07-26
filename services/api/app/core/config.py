from __future__ import annotations

import os
import sys

from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_AUTH_SECRET = "agentpulse-local-dev-secret"


class Settings(BaseSettings):
    app_name: str = "AgentPulse API"
    app_version: str = "0.1.0"
    environment: str = "development"
    max_request_body_bytes: int = 1_048_576
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://localhost:5175",
        "http://127.0.0.1:5175",
        "https://agentpulse.cc",
        "https://www.agentpulse.cc",
        "app://agentpulse",
    ]
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-pro"
    deepseek_allowed_models: list[str] = ["deepseek-v4-pro"]
    deepseek_timeout_seconds: float = 60.0
    deepseek_temperature: float = 0.7
    deepseek_thinking_enabled: bool = False
    # Production desktop deployments require each workspace owner to provide
    # their own model key. Local development can keep using the legacy global
    # key by leaving this disabled.
    model_byok_required: bool = False
    database_url: str = "postgresql://agentpulse:agentpulse@127.0.0.1:55432/agentpulse"
    # Absolute root for Hermes per-employee work dirs (ADR 0005). Empty → resolved
    # to an absolute ".hermes-data" under the server's cwd at run time.
    hermes_work_root: str = ""
    hermes_bin: str = "hermes"
    # When true, provisioning drives the real `hermes` CLI (creates profiles).
    hermes_provisioning: bool = False
    # Hosted workers only expose AgentPulse's scoped MCP tools. Local shell,
    # filesystem, browser and arbitrary network tools remain a self-hosted mode.
    hermes_hosted_safe_mode: bool = False
    # ADR 0008 item 4: how long RunService polls the durable approval row
    # before auto-denying a suspended run. MUST stay comfortably under 60 —
    # Hermes's ACP adapter hardcodes a 60s fail-closed timeout for
    # request_permission (acp_adapter/permissions.py::make_approval_callback,
    # called with no `timeout=` override at acp_adapter/server.py:1421) and
    # does NOT read the `approvals.timeout` config key on that path (only the
    # CLI-interactive prompt_dangerous_approval() does). If our timeout ever
    # creeps above ~55s, Hermes's own timeout can fire first, racing our
    # database decision against a late owner click. Re-verify this
    # constant against the installed Hermes version after any `hermes update`.
    approval_bridge_timeout_seconds: int = 50
    # TD-11 durable task worker and per-run MCP company tools.
    task_worker_enabled: bool = True
    task_worker_poll_seconds: float = 2.0
    task_run_lease_seconds: int = 30
    task_run_heartbeat_seconds: int = 10
    # TD-15: server capacity is resource-based. The legacy workspace value is
    # parsed for one release so old environments still start, but is not used.
    task_server_slots: int = 8
    task_workspace_concurrency: int = 100
    company_tools_url: str = "http://127.0.0.1:8000/mcp/company-tools/"
    company_tool_token_ttl_seconds: int = 900
    local_device_token_ttl_seconds: int = 900
    local_runtime_session_ttl_seconds: int = 90
    business_tools_url: str = "http://127.0.0.1:8000/mcp/business-tools/"
    business_tool_token_ttl_seconds: int = 900
    business_worker_enabled: bool = True
    business_worker_poll_seconds: float = 1.0
    business_action_lease_seconds: int = 30
    business_action_max_attempts: int = 2
    resend_base_url: str = "https://api.resend.com"
    # TD-08-T2: idle-reflection cron.
    idle_thinking_cron: bool = False
    idle_cron_interval_seconds: int = 3600
    auth_secret_key: str = _DEFAULT_AUTH_SECRET
    credential_encryption_key: str = ""
    access_token_ttl_hours: int = 24
    password_iterations: int = 260_000
    auth_rate_limit_enabled: bool = True
    trust_proxy_headers: bool = False
    registration_max_users: int = 100
    registration_rate_limit: int = 5
    registration_rate_window_seconds: int = 3600
    login_rate_limit: int = 10
    login_rate_window_seconds: int = 600
    telemetry_rate_limit: int = 120
    telemetry_rate_window_seconds: int = 3600
    inbound_webhooks_enabled: bool = True
    mcp_allowed_hosts: list[str] = [
        "127.0.0.1:*",
        "localhost:*",
        "api:*",
        "testserver",
    ]
    mcp_allowed_origins: list[str] = []

    model_config = SettingsConfigDict(env_file=".env", env_prefix="AGENTPULSE_")


settings = Settings()


def _validate_secret_key() -> None:
    """Refuse to start with the default dev secret in non-dev environments."""
    if settings.auth_secret_key == _DEFAULT_AUTH_SECRET:
        # Allow the default only when explicitly opted-in via env var
        if os.environ.get("AGENTPULSE_ALLOW_DEFAULT_SECRET", "").lower() not in (
            "1",
            "true",
            "yes",
        ):
            print(
                "ERROR: AGENTPULSE_AUTH_SECRET_KEY is still the default dev value.\n"
                "       Set AGENTPULSE_AUTH_SECRET_KEY to a strong random secret,\n"
                "       or set AGENTPULSE_ALLOW_DEFAULT_SECRET=1 for local dev only.",
                file=sys.stderr,
            )
            sys.exit(1)
    if settings.environment == "production" and not settings.credential_encryption_key:
        print(
            "ERROR: AGENTPULSE_CREDENTIAL_ENCRYPTION_KEY is required in production.",
            file=sys.stderr,
        )
        sys.exit(1)
    if settings.environment != "production":
        return

    production_errors: list[str] = []
    if len(settings.auth_secret_key) < 32:
        production_errors.append("AGENTPULSE_AUTH_SECRET_KEY must be at least 32 characters")
    if len(settings.credential_encryption_key) < 32:
        production_errors.append(
            "AGENTPULSE_CREDENTIAL_ENCRYPTION_KEY must be at least 32 characters"
        )
    if settings.auth_secret_key == settings.credential_encryption_key:
        production_errors.append("authentication and credential keys must be different")
    invalid_origins = [
        origin
        for origin in settings.cors_origins
        if origin != "app://agentpulse" and not origin.startswith("https://")
    ]
    if invalid_origins or "*" in settings.cors_origins:
        production_errors.append("production CORS origins must use HTTPS or app://agentpulse")
    if settings.hermes_provisioning and not settings.hermes_hosted_safe_mode:
        production_errors.append("hosted Hermes provisioning requires safe mode")
    if not settings.model_byok_required:
        production_errors.append("production requires workspace model BYOK")
    if settings.inbound_webhooks_enabled:
        production_errors.append("production inbound webhooks remain disabled until TD-09")

    if production_errors:
        print(
            "ERROR: unsafe AgentPulse production configuration:\n       "
            + "\n       ".join(production_errors),
            file=sys.stderr,
        )
        sys.exit(1)


_validate_secret_key()
