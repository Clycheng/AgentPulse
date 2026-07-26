from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import init_db
from app.main import app


def chat_payload() -> dict:
    return {
        "company_name": "星野工作室",
        "conversation_title": "私聊 · 小秘",
        "agent": {
            "id": "sec",
            "name": "小秘",
            "role": "老板秘书",
            "department": "老板办公室",
            "prompt": "你负责帮老板拆解任务、整理下一步。",
            "skills": ["任务拆解"],
        },
        "messages": [{"role": "user", "name": "老板", "content": "帮我规划今天任务"}],
    }


def _authenticated_client(tmp_path, monkeypatch) -> tuple[TestClient, dict[str, str]]:
    monkeypatch.setattr(
        settings, "database_url", f"sqlite:///{tmp_path / 'runs.sqlite3'}"
    )
    monkeypatch.setattr(settings, "password_iterations", 1_000)
    init_db()
    client = TestClient(app)
    response = client.post(
        "/api/auth/register",
        json={
            "email": "runs@example.com",
            "password": "agentpulse123",
            "display_name": "老板",
            "workspace_name": "测试公司",
        },
    )
    token = response.json()["access_token"]
    return client, {"Authorization": f"Bearer {token}"}


def test_llm_chat_is_retired_in_favor_of_hermes_runs(tmp_path, monkeypatch) -> None:
    client, headers = _authenticated_client(tmp_path, monkeypatch)

    response = client.post("/api/runs/llm-chat", json=chat_payload(), headers=headers)

    assert response.status_code == 410
    assert "Hermes Run" in response.json()["detail"]


def test_retired_llm_chat_does_not_parse_legacy_payload(tmp_path, monkeypatch) -> None:
    client, headers = _authenticated_client(tmp_path, monkeypatch)

    response = client.post("/api/runs/llm-chat", json={}, headers=headers)

    assert response.status_code == 410
