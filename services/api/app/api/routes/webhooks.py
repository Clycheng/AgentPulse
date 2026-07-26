"""Public inbound webhook endpoint for external channels (TD-09-T2).

No JWT: the unguessable token in the URL (plus an optional per-channel HMAC
signature) authenticates the caller. Verified payloads are handed to the
channel router, which threads them into an ordinary conversation; if the channel
pins a target agent, we best-effort trigger a reply through the existing
execution layer (the same complete_agent_reply the app uses, so it swaps to
Hermes for free in TD-03-T3).
"""

import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.channels.adapters import UnsupportedChannelError
from app.channels.router import route_inbound
from app.core.database import Database, get_db
from app.core.config import settings
from app.runtime.hermes_client import HermesBackendError
from app.services.model_credentials import ModelCredentialError
from app.services.channels import get_channel_by_token, verify_signature

router = APIRouter(tags=["webhooks"])


@router.post("/webhooks/{channel_type}/{token}")
async def inbound_webhook(
    channel_type: str,
    token: str,
    request: Request,
    x_signature: str | None = Header(default=None),
    conn: Database = Depends(get_db),
) -> dict:
    if not settings.inbound_webhooks_enabled:
        raise HTTPException(status_code=404, detail="channel not found")
    channel = get_channel_by_token(conn, channel_type, token)
    if channel is None:
        raise HTTPException(status_code=404, detail="channel not found")

    raw_body = await request.body()
    if len(raw_body) > settings.max_request_body_bytes:
        raise HTTPException(status_code=413, detail="request body too large")
    if not verify_signature(channel, raw_body, x_signature):
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = json.loads(raw_body or b"{}")
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")

    try:
        result = route_inbound(conn, channel, payload)
    except UnsupportedChannelError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    replied = False
    if not result["deduped"] and channel["target_agent_id"]:
        replied = await _maybe_reply(
            conn,
            workspace_id=channel["workspace_id"],
            conversation_id=result["conversation_id"],
            agent_id=channel["target_agent_id"],
            user_message_id=result["message_id"],
        )

    return {"ok": True, **result, "replied": replied}


async def _maybe_reply(
    conn: Database,
    *,
    workspace_id: str,
    conversation_id: str,
    agent_id: str,
    user_message_id: str,
) -> bool:
    """Best-effort: let the pinned agent answer the inbound message.

    Reuses the app's execution entry (complete_agent_reply). Any execution-layer
    failure (e.g. no model key configured) is swallowed so the provider still
    gets a 200 — the message is already persisted and can be answered later.
    """
    # Imported lazily to avoid a route-module import cycle at startup.
    from app.api.routes.workspace import complete_agent_reply, load_agent_for_reply

    workspace = conn.execute(
        "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
    ).fetchone()
    conversation = conn.execute(
        "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    agent = load_agent_for_reply(conn, workspace_id, agent_id)
    user_message = conn.execute(
        "SELECT * FROM messages WHERE id = ?", (user_message_id,)
    ).fetchone()
    if not (workspace and conversation and agent and user_message):
        return False

    try:
        await complete_agent_reply(
            conn,
            workspace=workspace,
            conversation=conversation,
            conversation_id=conversation_id,
            agent=agent,
            user_message=user_message,
        )
    except (HermesBackendError, ModelCredentialError):
        return False
    return True
