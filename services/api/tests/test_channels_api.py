"""Tests for channel management API + public webhook endpoint (TD-09-T2)."""

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.api.routes import workspace as workspace_routes
from app.core.config import settings
from app.core.database import connect, init_db
from app.main import app
from app.services.workspace import add_message


def make_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(
        settings, "database_url", f"sqlite:///{tmp_path / 'test_chan_api.sqlite3'}"
    )
    monkeypatch.setattr(settings, "password_iterations", 1_000)
    init_db()
    return TestClient(app)


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def register(client: TestClient) -> tuple[str, str]:
    resp = client.post(
        "/api/auth/register",
        json={
            "email": "founder@example.com",
            "password": "agentpulse123",
            "display_name": "老板",
            "workspace_name": "测试公司",
        },
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    return token, bootstrap["agents"][0]["id"]


def test_channel_crud(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    token, agent_id = register(client)

    created = client.post(
        "/api/channels",
        headers=auth_header(token),
        json={
            "channel_type": "generic_webhook",
            "name": "官网客服",
            "target_agent_id": agent_id,
        },
    )
    assert created.status_code == 200
    chan = created.json()
    assert chan["active"] is True
    assert chan["webhook_url"].startswith("/webhooks/generic_webhook/")
    assert chan["token"] in chan["webhook_url"]

    listed = client.get("/api/channels", headers=auth_header(token)).json()
    assert len(listed) == 1

    detail = client.get(
        f"/api/channels/{chan['id']}", headers=auth_header(token)
    ).json()
    assert detail["stats"]["messages_today"] == 0
    assert detail["stats"]["active_external_users"] == 0

    patched = client.patch(
        f"/api/channels/{chan['id']}",
        headers=auth_header(token),
        json={"name": "重命名客服"},
    ).json()
    assert patched["name"] == "重命名客服"

    deleted = client.delete(
        f"/api/channels/{chan['id']}", headers=auth_header(token)
    ).json()
    assert deleted["active"] is False


def test_create_channel_rejects_unknown_target_agent(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    token, _ = register(client)
    resp = client.post(
        "/api/channels",
        headers=auth_header(token),
        json={
            "channel_type": "generic_webhook",
            "name": "x",
            "target_agent_id": "agent_nope",
        },
    )
    assert resp.status_code == 400


def _create_generic_channel(client, token, agent_id=None, config=None):
    return client.post(
        "/api/channels",
        headers=auth_header(token),
        json={
            "channel_type": "generic_webhook",
            "name": "官网客服",
            "config": config or {},
            "target_agent_id": agent_id,
        },
    ).json()


def test_webhook_inbound_persists_message(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    token, _ = register(client)
    chan = _create_generic_channel(client, token)

    resp = client.post(
        chan["webhook_url"],
        json={"user_id": "cust_1", "message": "你们几点营业？", "message_id": "m1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["deduped"] is False
    assert body["message_id"] is not None

    # Redelivery of the same external message id is deduped.
    again = client.post(
        chan["webhook_url"],
        json={"user_id": "cust_1", "message": "你们几点营业？", "message_id": "m1"},
    )
    assert again.json()["deduped"] is True

    # Stats reflect the one inbound message + one external user.
    detail = client.get(
        f"/api/channels/{chan['id']}", headers=auth_header(token)
    ).json()
    assert detail["stats"]["messages_today"] == 1
    assert detail["stats"]["active_external_users"] == 1


def test_webhook_redacts_local_path_and_blocks_server_execution(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    token, agent_id = register(client)
    chan = _create_generic_channel(client, token, agent_id=agent_id)

    response = client.post(
        chan["webhook_url"],
        json={
            "user_id": "cust_local",
            "message": "读取 /Users/example/code/private-project 并汇报",
            "message_id": "local-path-1",
        },
    )
    assert response.status_code == 200
    assert response.json()["replied"] is True

    conn = connect()
    try:
        messages = conn.execute(
            "SELECT content, sender_type FROM messages ORDER BY created_at"
        ).fetchall()
        assert all("/Users/example" not in row["content"] for row in messages)
        assert any(
            row["sender_type"] == "user" and "[已授权本机项目路径]" in row["content"]
            for row in messages
        )
        assert any(
            row["sender_type"] == "agent" and "尚未执行" in row["content"]
            for row in messages
        )
        assert conn.execute("SELECT COUNT(*) AS count FROM runs").fetchone()["count"] == 0
    finally:
        conn.close()


def test_webhook_unknown_token_404(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    register(client)
    resp = client.post(
        "/webhooks/generic_webhook/not-a-real-token",
        json={"user_id": "u", "message": "hi"},
    )
    assert resp.status_code == 404


def test_webhook_inactive_channel_404(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    token, _ = register(client)
    chan = _create_generic_channel(client, token)
    client.delete(f"/api/channels/{chan['id']}", headers=auth_header(token))
    resp = client.post(chan["webhook_url"], json={"user_id": "u", "message": "hi"})
    assert resp.status_code == 404


def test_webhook_signature_enforced_when_secret_set(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    token, _ = register(client)
    chan = _create_generic_channel(client, token, config={"secret": "s3cr3t"})

    raw = json.dumps({"user_id": "u", "message": "hi", "message_id": "s1"}).encode()

    # Missing signature → 401
    bad = client.post(
        chan["webhook_url"],
        content=raw,
        headers={"Content-Type": "application/json"},
    )
    assert bad.status_code == 401

    # Correct HMAC → 200
    sig = hmac.new(b"s3cr3t", raw, hashlib.sha256).hexdigest()
    ok = client.post(
        chan["webhook_url"],
        content=raw,
        headers={"Content-Type": "application/json", "X-Signature": sig},
    )
    assert ok.status_code == 200


def test_webhook_unsupported_channel_type(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    token, _ = register(client)
    chan = client.post(
        "/api/channels",
        headers=auth_header(token),
        json={"channel_type": "wechat", "name": "公众号"},
    ).json()
    resp = client.post(chan["webhook_url"], json={"user_id": "u", "message": "hi"})
    assert resp.status_code == 400  # no wechat adapter yet


def test_webhook_triggers_agent_reply(tmp_path, monkeypatch):
    async def fake_hermes_reply(
        conn, *, conversation_id, agent, **_kwargs
    ):
        message = add_message(
            conn,
            conversation_id=conversation_id,
            sender_type="agent",
            sender_id=agent["id"],
            content="您好，我们 9:00-18:00 营业。",
            provider="hermes",
            model="",
        )
        conn.commit()
        return message

    monkeypatch.setattr(workspace_routes, "complete_agent_reply", fake_hermes_reply)

    client = make_client(tmp_path, monkeypatch)
    token, agent_id = register(client)
    chan = _create_generic_channel(client, token, agent_id=agent_id)

    resp = client.post(
        chan["webhook_url"],
        json={"user_id": "cust_1", "message": "几点营业？", "message_id": "r1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["replied"] is True

    # An agent reply now exists in the routed conversation.
    msgs_resp = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    conv_id = body["conversation_id"]
    messages = msgs_resp["messages_by_conversation"][conv_id]
    assert any(m["sender_type"] == "agent" for m in messages)
