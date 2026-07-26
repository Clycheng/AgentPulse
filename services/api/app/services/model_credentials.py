from __future__ import annotations

import httpx

from app.core.config import settings
from app.core.database import Database
from app.services.credentials import CredentialError, decrypt_value, encrypt_value
from app.services.workspace import new_id, now_iso


class ModelCredentialError(ValueError):
    pass


class ModelCredentialRequired(ModelCredentialError):
    pass


class ModelCredentialValidationError(ModelCredentialError):
    pass


def validate_model_name(model: str | None) -> str:
    selected = (model or settings.deepseek_model).strip()
    if selected not in settings.deepseek_allowed_models:
        raise ModelCredentialValidationError("该模型不在 AgentPulse 支持列表中")
    return selected


def _row(conn: Database, workspace_id: str):
    return conn.execute(
        "SELECT * FROM workspace_model_credentials "
        "WHERE workspace_id = ? AND provider = 'deepseek'",
        (workspace_id,),
    ).fetchone()


def has_workspace_model_credential(conn: Database, workspace_id: str) -> bool:
    return _row(conn, workspace_id) is not None


def get_workspace_model_api_key(conn: Database, workspace_id: str) -> str:
    try:
        row = _row(conn, workspace_id)
    except Exception:
        # A few low-level runtime unit tests intentionally build only the
        # tables needed by RunService. Production always initializes the full
        # schema, and BYOK mode must still fail loudly there.
        if settings.model_byok_required:
            raise
        row = None
    if row is not None:
        try:
            return decrypt_value(row["encrypted_api_key"])
        except CredentialError as exc:
            raise ModelCredentialError("模型凭证无法解密，请重新配置") from exc
    if not settings.model_byok_required and settings.deepseek_api_key:
        return settings.deepseek_api_key
    raise ModelCredentialRequired("请先在 AgentPulse 中配置 DeepSeek API Key")


def runtime_model_environment(conn: Database, workspace_id: str) -> dict[str, str]:
    try:
        key = get_workspace_model_api_key(conn, workspace_id)
    except ModelCredentialRequired:
        if settings.model_byok_required:
            raise
        return {}
    return {"DEEPSEEK_API_KEY": key}


async def validate_deepseek_key(api_key: str) -> None:
    """Validate without spending tokens; never include provider bodies in errors."""
    try:
        async with httpx.AsyncClient(
            timeout=settings.deepseek_timeout_seconds,
            trust_env=False,
        ) as client:
            response = await client.get(
                f"{settings.deepseek_base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.TimeoutException as exc:
        raise ModelCredentialValidationError("DeepSeek 凭证验证超时") from exc
    except httpx.HTTPError as exc:
        raise ModelCredentialValidationError("无法连接 DeepSeek 验证凭证") from exc
    if response.status_code >= 400:
        raise ModelCredentialValidationError("DeepSeek API Key 无效或无权访问")


def put_workspace_model_credential(
    conn: Database,
    *,
    workspace_id: str,
    api_key: str,
    model: str | None = None,
) -> None:
    timestamp = now_iso()
    selected_model = validate_model_name(model)
    conn.execute(
        """INSERT INTO workspace_model_credentials (
          id, workspace_id, provider, encrypted_api_key, model,
          validation_status, last_validated_at, created_at, updated_at
        ) VALUES (?, ?, 'deepseek', ?, ?, 'valid', ?, ?, ?)
        ON CONFLICT (workspace_id, provider) DO UPDATE SET
          encrypted_api_key = excluded.encrypted_api_key,
          model = excluded.model,
          validation_status = 'valid',
          last_validated_at = excluded.last_validated_at,
          updated_at = excluded.updated_at""",
        (
            new_id("modelcred"),
            workspace_id,
            encrypt_value(api_key.strip()),
            selected_model,
            timestamp,
            timestamp,
            timestamp,
        ),
    )


def delete_workspace_model_credential(conn: Database, workspace_id: str) -> bool:
    existing = _row(conn, workspace_id)
    if existing is None:
        return False
    conn.execute(
        "DELETE FROM workspace_model_credentials WHERE id = ?", (existing["id"],)
    )
    conn.execute(
        """UPDATE agent_specs SET status = 'blocked_on_credentials', updated_at = ?
        WHERE workspace_id = ? AND status IN ('ready','provisioning')""",
        (now_iso(), workspace_id),
    )
    return True


def provision_workspace_agents(conn: Database, workspace_id: str) -> None:
    if not settings.hermes_provisioning:
        return
    from app.orchestration.capability_catalog import split_by_credentials
    from app.orchestration.supply import build_provisioner_from_settings, provision
    from app.runtime.upgrade import UpgradeError, execute_upgrade

    provisioner = build_provisioner_from_settings()
    rows = conn.execute(
        """SELECT agent_id FROM agent_specs
        WHERE workspace_id = ? AND status IN ('draft','failed','blocked_on_credentials')
        ORDER BY created_at""",
        (workspace_id,),
    ).fetchall()
    for row in rows:
        try:
            capability_rows = conn.execute(
                "SELECT capability_key FROM agent_capabilities WHERE agent_id = ?",
                (row["agent_id"],),
            ).fetchall()
            _, pending_keys = split_by_credentials(
                [item["capability_key"] for item in capability_rows]
            )
            if pending_keys:
                placeholders = ", ".join("?" for _ in pending_keys)
                conn.execute(
                    f"DELETE FROM agent_capabilities WHERE agent_id = ? "
                    f"AND capability_key IN ({placeholders})",
                    (row["agent_id"], *pending_keys),
                )
            provision(conn, row["agent_id"], provisioner)
            for key in pending_keys:
                try:
                    execute_upgrade(
                        conn,
                        approval={
                            "agent_id": row["agent_id"],
                            "workspace_id": workspace_id,
                        },
                        approved_capability_key=key,
                        provisioner=provisioner,
                    )
                except UpgradeError:
                    continue
        except Exception:
            # The status machine records failed. One employee must not prevent
            # the valid credential from being saved for the rest of the team.
            continue


def serialize_model_provider(conn: Database, workspace_id: str) -> dict[str, Any]:
    row = _row(conn, workspace_id)
    masked = ""
    validation_status = "unconfigured"
    if row is not None:
        validation_status = row["validation_status"]
        try:
            value = decrypt_value(row["encrypted_api_key"])
            masked = f"{value[:3]}...{value[-4:]}" if len(value) > 7 else "已配置"
        except CredentialError:
            validation_status = "invalid"

    counts = conn.execute(
        """SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN status = 'ready' THEN 1 ELSE 0 END) AS ready,
          SUM(CASE WHEN status IN ('draft','provisioning','blocked_on_credentials') THEN 1 ELSE 0 END) AS waiting,
          SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
        FROM agent_specs WHERE workspace_id = ?""",
        (workspace_id,),
    ).fetchone()
    return {
        "provider": "deepseek",
        "model": row["model"] if row is not None else settings.deepseek_model,
        "configured": row is not None,
        "masked_api_key": masked,
        "validation_status": validation_status,
        "last_validated_at": row["last_validated_at"] if row is not None else None,
        "agents_total": int(counts["total"] or 0),
        "agents_ready": int(counts["ready"] or 0),
        "agents_waiting": int(counts["waiting"] or 0),
        "agents_failed": int(counts["failed"] or 0),
    }
