"""ChannelRouter (TD-09-T1): inbound external message → conversation/message.

route_inbound normalizes a raw provider payload, threads it into the right
conversation (fixed group, or one-per-external-user), drops duplicate webhook
redeliveries, and persists the message as a normal ``sender_type='user'`` row so
the existing agent flow can pick it up. The agent-trigger + reply-back path is
TD-09-T2/T3; the seam is marked below.
"""

from __future__ import annotations

import json

from app.channels.adapters import ChannelMessage, get_adapter
from app.core.database import Database, Row
from app.services.local_runtime import redact_local_paths
from app.services.workspace import new_id, now_iso


def route_inbound(conn: Database, channel_config: Row, raw_payload: dict) -> dict:
    """Route one inbound payload. Returns a result dict.

    { conversation_id, message_id | None, deduped: bool }
    ``deduped`` is True (and message_id None) when the external message was
    already processed.
    """
    config = json.loads(channel_config["config_json"] or "{}")
    adapter = get_adapter(channel_config["channel_type"])
    msg = adapter.normalize(config, raw_payload)

    conversation_id = find_or_create_conversation(
        conn, channel_config, msg.external_user_id
    )

    if msg.external_message_id and message_already_processed(
        conn, conversation_id, msg.external_message_id
    ):
        return {
            "conversation_id": conversation_id,
            "message_id": None,
            "deduped": True,
        }

    message_id = _insert_external_message(conn, conversation_id, msg)
    # TD-09-T2 seam: trigger_agent_response(conn, conversation_id, message_id)
    # reuses the existing send-message flow so the agent replies; ChannelReply
    # (TD-09-T3) sends that reply back out via conversation.source_channel.
    return {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "deduped": False,
    }


def find_or_create_conversation(
    conn: Database, channel_config: Row, external_user_id: str
) -> str:
    """Resolve the conversation an external message belongs to.

    - If the channel pins a ``target_conversation_id`` → always that (e.g. a
      shared support room).
    - Otherwise thread by ``(source_channel, external_conversation_id)`` so all
      messages from one external user land in one conversation; create it on
      first contact.
    """
    if channel_config["target_conversation_id"]:
        return channel_config["target_conversation_id"]

    channel_type = channel_config["channel_type"]
    existing = conn.execute(
        """
        SELECT id FROM conversations
        WHERE workspace_id = ? AND source_channel = ? AND external_conversation_id = ?
        ORDER BY created_at
        LIMIT 1
        """,
        (channel_config["workspace_id"], channel_type, external_user_id),
    ).fetchone()
    if existing is not None:
        return existing["id"]

    conversation_id = new_id("conv")
    created_at = now_iso()
    name = f"{channel_config['name']} · {external_user_id[:24]}"
    conn.execute(
        """
        INSERT INTO conversations (
          id, workspace_id, kind, name, unread, created_at, updated_at,
          source_channel, external_conversation_id
        )
        VALUES (?, ?, 'group', ?, 0, ?, ?, ?, ?)
        """,
        (
            conversation_id,
            channel_config["workspace_id"],
            name,
            created_at,
            created_at,
            channel_type,
            external_user_id,
        ),
    )
    if channel_config["target_agent_id"]:
        conn.execute(
            """
            INSERT INTO conversation_members (conversation_id, agent_id)
            VALUES (?, ?)
            """,
            (conversation_id, channel_config["target_agent_id"]),
        )
    return conversation_id


def message_already_processed(
    conn: Database, conversation_id: str, external_message_id: str
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM messages
        WHERE conversation_id = ? AND external_message_id = ?
        LIMIT 1
        """,
        (conversation_id, external_message_id),
    ).fetchone()
    return row is not None


def _insert_external_message(
    conn: Database, conversation_id: str, msg: ChannelMessage
) -> str:
    message_id = new_id("msg")
    created_at = now_iso()
    conn.execute(
        """
        INSERT INTO messages (
          id, conversation_id, sender_type, sender_id, content,
          provider, model, created_at, external_message_id
        )
        VALUES (?, ?, 'user', ?, ?, NULL, NULL, ?, ?)
        """,
        (
            message_id,
            conversation_id,
            msg.external_user_id,
            redact_local_paths(msg.content),
            created_at,
            msg.external_message_id,
        ),
    )
    conn.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?",
        (created_at, conversation_id),
    )
    return message_id
