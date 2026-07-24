import json

from fastapi.testclient import TestClient

from app.api.routes import workspace as workspace_routes
from app.core.config import settings
from app.core.database import connect, init_db
from app.main import app
from app.orchestration.brief import confirm_brief
from app.schemas.run import LlmChatResponse
from app.services.workspace import add_message, new_id, now_iso


def make_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(
        settings,
        "database_url",
        f"sqlite:///{tmp_path / 'agentpulse.sqlite3'}",
    )
    monkeypatch.setattr(settings, "password_iterations", 1_000)
    init_db()
    return TestClient(app)


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def register_user(client: TestClient) -> dict:
    response = client.post(
        "/api/auth/register",
        json={
            "email": "founder@example.com",
            "password": "agentpulse123",
            "display_name": "老板",
            "workspace_name": "AgentPulse 工作室",
        },
    )
    assert response.status_code == 200
    return response.json()


def brief_work_items(agent_id: str) -> list[dict]:
    return [
        {
            "key": "research",
            "title": "调研",
            "description": "整理执行依据",
            "owner_agent_id": agent_id,
            "expected_output": "研究摘要",
            "output_type": "markdown",
            "depends_on_keys": [],
            "final_delivery": False,
        },
        {
            "key": "draft",
            "title": "起草",
            "description": "完成草稿",
            "owner_agent_id": agent_id,
            "expected_output": "内容草稿",
            "output_type": "markdown",
            "depends_on_keys": ["research"],
            "final_delivery": False,
        },
        {
            "key": "package",
            "title": "组包",
            "description": "整理待发布内容包",
            "owner_agent_id": agent_id,
            "expected_output": "待发布内容包",
            "output_type": "content_package_v1",
            "depends_on_keys": ["draft"],
            "final_delivery": True,
        },
    ]


def create_confirmed_brief(
    client: TestClient,
    token: str,
    conversation_id: str,
    agent_id: str,
    goal: str,
) -> dict:
    """Helper: create a draft brief and confirm it for task creation."""
    brief = client.post(
        "/api/briefs",
        headers=auth_header(token),
        json={
            "discussion_conversation_id": conversation_id,
            "goal": goal,
            "owner_agent_id": agent_id,
            "participant_agent_ids": [agent_id],
            "work_items": brief_work_items(agent_id),
            "created_by_agent_id": agent_id,
        },
    )
    assert brief.status_code == 200
    brief_id = brief.json()["id"]

    conn = connect()
    try:
        row = conn.execute("SELECT workspace_id FROM consensus_briefs WHERE id = ?", (brief_id,)).fetchone()
        user = conn.execute("SELECT id FROM users ORDER BY created_at LIMIT 1").fetchone()
        confirmed = confirm_brief(
            conn,
            workspace_id=row["workspace_id"],
            brief_id=brief_id,
            confirmed_by_user_id=user["id"],
        )
        conn.commit()
        return confirmed
    finally:
        conn.close()


def test_register_bootstrap_creates_real_workspace_foundation(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)

    auth = register_user(client)
    bootstrap = client.get(
        "/api/me/bootstrap",
        headers=auth_header(auth["access_token"]),
    )

    assert bootstrap.status_code == 200
    data = bootstrap.json()
    assert data["workspace"]["name"] == "AgentPulse 工作室"
    assert data["workspace"]["onboarding_completed"] is False
    assert [department["name"] for department in data["departments"]] == [
        "老板办公室", "内容经营部",
    ]
    assert [agent["name"] for agent in data["agents"]] == [
        "小秘", "内容策划", "内容主笔", "运营执行",
    ]
    assert data["agents"][0]["role"] == "老板秘书"
    assert data["tasks"] == []
    assert len(data["conversations"]) == 5
    assert data["conversations"][0]["kind"] == "dm"
    welcome_messages = data["messages_by_conversation"][data["conversations"][0]["id"]]
    assert len(welcome_messages) == 1
    assert welcome_messages[0]["sender_type"] == "agent"
    assert "我是小秘" in welcome_messages[0]["content"]
    assert any(
        conversation["kind"] == "group" and conversation["name"] == "内容经营群"
        for conversation in data["conversations"]
    )
    assert data["agent_template_categories"]
    assert data["agent_template_categories"][0]["id"] == "business-ops"
    assert data["agent_templates"]
    assert data["agent_templates"][0]["category_id"] == "business-ops"

    completed = client.post(
        "/api/me/onboarding/complete",
        headers=auth_header(auth["access_token"]),
        json={},
    )
    assert completed.status_code == 200
    reloaded = client.get(
        "/api/me/bootstrap",
        headers=auth_header(auth["access_token"]),
    ).json()
    assert reloaded["workspace"]["onboarding_completed"] is True


def test_admin_talent_market_catalog_exposes_official_templates(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)

    response = client.get("/api/admin/talent-market")

    assert response.status_code == 200
    payload = response.json()
    assert payload["categories"][0]["id"] == "business-ops"
    assert payload["templates"][0]["publisher"] == "AgentPulse 官方"
    assert payload["templates"][0]["status"] == "published"
    conn = connect()
    try:
        table_count = conn.execute(
            "SELECT COUNT(*) AS count FROM official_agent_templates"
        ).fetchone()
        assert table_count["count"] == len(payload["templates"])
    finally:
        conn.close()


def test_employee_with_ready_hermes_profile_routes_to_hermes_not_function_loop(
    tmp_path, monkeypatch
):
    """An employee with a real, ready Hermes profile must go straight to
    Hermes — that's the only path that can ever hit the approval gate
    (ADR 0008). Regression test for a bug where _stream_reply_events tried
    the Agent Action Bridge (function_loop) FIRST for every employee
    regardless of Hermes status; since function_loop always "succeeds" (it
    has its own no-tool-needed fallback), Hermes was silently never reached
    even for employees explicitly granted real capabilities. Found live
    2026-07-15: a "运维小哥" employee with a granted run_tests capability and
    a real ready Hermes profile still answered "I don't have file system
    access" from the function_loop fallback instead of running for real."""

    async def fail_if_called(*_a, **_kw):
        raise AssertionError(
            "run_function_loop must not be called for an employee with a "
            "ready Hermes profile — Hermes must be tried first"
        )
        yield  # pragma: no cover — makes this an async generator

    async def fake_stream_agent_run(conn, *, ctx, backend, input_message_id, permission_resolver=None):
        from app.services.workspace import add_message
        msg = add_message(
            conn, conversation_id=ctx.conversation_id,
            sender_type="agent", sender_id=ctx.agent_id,
            content="来自 Hermes 的真实回复", provider="hermes", model="deepseek-v4-pro",
        )
        conn.commit()
        yield {"type": "chunk", "content": "来自 Hermes 的真实回复"}
        yield {"type": "message", "message": msg}

    monkeypatch.setattr(workspace_routes, "run_function_loop", fail_if_called)
    monkeypatch.setattr(workspace_routes, "stream_agent_run", fake_stream_agent_run)

    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    agent = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "运维小哥",
            "description": "负责服务器运维",
            "department_name": "技术部",
            "prompt": "你负责服务器运维操作。",
        },
    ).json()

    # Simulate a completed real Hermes provisioning (bypassing the actual
    # `hermes` CLI, which this test suite must never invoke — see the .env
    # comment on AGENTPULSE_HERMES_PROVISIONING for why).
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO agent_specs (
              id, agent_id, workspace_id, role_name, source_request,
              responsibilities_json, hermes_profile, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?)
            """,
            (
                new_id("spec"), agent["id"], auth["workspace"]["id"], "运维工程师",
                "test", "[]", "ap_test_profile", now_iso(), now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    dm = next(
        c for c in bootstrap["conversations"]
        if c["kind"] == "dm" and c.get("agent_id") == agent["id"]
    )

    with client.stream(
        "POST", f"/api/conversations/{dm['id']}/messages/stream",
        headers=auth_header(token),
        json={"content": "帮我删除一个测试文件"},
    ) as resp:
        body = "".join(resp.iter_text())

    assert "来自 Hermes 的真实回复" in body


def test_login_secretary_chat_persists_deepseek_metadata(tmp_path, monkeypatch):
    async def fake_complete(self, payload):
        assert payload.agent.name == "小秘"
        assert payload.messages[-1].content == "帮我拆一下今天的推进计划"
        # NOTE: Auto-task creation has been removed (ADR 0006)
        # related_tasks is now empty unless explicitly created
        return LlmChatResponse(
            reply="先做三件事：确认目标、拆任务、安排负责人。",
            provider="deepseek",
            model=settings.deepseek_model,
            usage={"total_tokens": 88},
        )

    monkeypatch.setattr(
        workspace_routes.DeepSeekChatClient,
        "complete",
        fake_complete,
    )
    client = make_client(tmp_path, monkeypatch)
    register_user(client)

    login = client.post(
        "/api/auth/login",
        json={"email": "founder@example.com", "password": "agentpulse123"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    secretary_chat = bootstrap["conversations"][0]
    secretary_agent = bootstrap["agents"][0]  # 小秘
    send = client.post(
        f"/api/conversations/{secretary_chat['id']}/messages",
        headers=auth_header(token),
        json={"content": "帮我拆一下今天的推进计划"},
    )

    assert send.status_code == 200
    payload = send.json()
    assert payload["user_message"]["sender_type"] == "user"
    # Agent Action Bridge: the agent now has tools and may respond differently.
    # Just verify we got a non-empty response with correct metadata.
    assert len(payload["agent_message"]["content"]) > 10
    assert payload["agent_message"]["provider"] == "deepseek"
    assert payload["agent_message"]["model"] == settings.deepseek_model
    # NOTE: Auto-task creation removed (ADR 0006) - created_task is now None
    assert payload["created_task"] is None

    reloaded = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    messages = reloaded["messages_by_conversation"][secretary_chat["id"]]
    assert [message["sender_type"] for message in messages] == [
        "agent",
        "user",
        "agent",
    ]
    assert messages[-1]["provider"] == "deepseek"
    assert messages[-1]["model"] == settings.deepseek_model


def test_knowledge_source_is_injected_into_agent_context(tmp_path, monkeypatch):
    async def fake_complete(self, payload):
        assert payload.knowledge_sources
        assert payload.knowledge_sources[0].title == "品牌定位"
        assert "一人公司 AI 工作台" in payload.knowledge_sources[0].content
        return LlmChatResponse(
            reply="我会按品牌定位来写：突出一人公司 AI 工作台。",
            provider="deepseek",
            model="deepseek-v4-flash",
            usage={"total_tokens": 72},
        )

    monkeypatch.setattr(
        workspace_routes.DeepSeekChatClient,
        "complete",
        fake_complete,
    )
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    created_source = client.post(
        "/api/knowledge-sources",
        headers=auth_header(token),
        json={
            "title": "品牌定位",
            "category": "品牌资料",
            "content": "AgentPulse 是一人公司 AI 工作台，帮助老板招聘 AI 员工、拉群协作和验收成果。",
        },
    )
    assert created_source.status_code == 200
    assert created_source.json()["category"] == "品牌资料"

    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    assert bootstrap["knowledge_sources"][0]["title"] == "品牌定位"
    secretary_chat = bootstrap["conversations"][0]

    sent = client.post(
        f"/api/conversations/{secretary_chat['id']}/messages",
        headers=auth_header(token),
        json={"content": "参考品牌定位，帮我写一句官网首屏文案"},
    )
    assert sent.status_code == 200
    assert "一人公司 AI 工作台" in sent.json()["agent_message"]["content"]


def test_secretary_chat_can_create_agent_from_recruit_intent(tmp_path, monkeypatch):
    async def fake_complete(self, payload):
        assert payload.agent.name == "小秘"
        return LlmChatResponse(
            reply="我已经先帮你把市场分析师建好，后续可以继续补充工具和资料库权限。",
            provider="deepseek",
            model="deepseek-v4-flash",
            usage={"total_tokens": 66},
        )

    monkeypatch.setattr(
        workspace_routes.DeepSeekChatClient,
        "complete",
        fake_complete,
    )
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]
    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    secretary_chat = bootstrap["conversations"][0]

    send = client.post(
        f"/api/conversations/{secretary_chat['id']}/messages",
        headers=auth_header(token),
        json={"content": "帮我招一个市场分析师"},
    )

    assert send.status_code == 200
    payload = send.json()
    assert payload["created_task"] is None
    assert payload["created_agent"]["name"] == "市场分析师"
    assert payload["created_agent"]["source"] == "chat_factory"

    reloaded = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    agents = {agent["name"]: agent for agent in reloaded["agents"]}
    assert "市场分析师" in agents
    departments = {department["name"] for department in reloaded["departments"]}
    assert "市场部" in departments
    dm_chats = [
        chat
        for chat in reloaded["conversations"]
        if chat["kind"] == "dm" and chat["agent_id"] == agents["市场分析师"]["id"]
    ]
    assert dm_chats
    messages = reloaded["messages_by_conversation"][secretary_chat["id"]]
    assert any("已创建员工：市场分析师" in message["content"] for message in messages)


def test_agent_creation_recruitment_and_group_conversation(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    created = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "增长分析师",
            "description": "负责增长数据分析",
            "department_name": "增长与客户",
            "prompt": "你负责分析渠道数据，输出下一步增长实验建议。",
        },
    )
    assert created.status_code == 200
    created_agent = created.json()
    assert created_agent["name"] == "增长分析师"
    assert created_agent["source"] == "custom"

    recruited = client.post(
        "/api/agents/recruit",
        headers=auth_header(token),
        json={"template_id": "content-writer", "department_name": "内容部"},
    )
    assert recruited.status_code == 200
    recruited_agent = recruited.json()
    assert recruited_agent["name"] == "内容主笔"
    assert recruited_agent["source"] == "template:content-writer"

    task_id = new_id("task")
    conn = connect()
    try:
        created_at = now_iso()
        conn.execute(
            """
            INSERT INTO tasks (
              id, workspace_id, title, priority, owner_agent_id, status,
              progress, conversation_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                auth["workspace"]["id"],
                "官网首屏文案确认",
                "P1",
                created_agent["id"],
                "进行中",
                30,
                None,
                created_at,
                created_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    group = client.post(
        "/api/conversations/group",
        headers=auth_header(token),
        json={
            "name": "官网改版作战室",
            "member_ids": [created_agent["id"], recruited_agent["id"]],
            "related_task_ids": [task_id],
        },
    )
    assert group.status_code == 200
    group_id = group.json()["id"]

    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    agents = {agent["id"]: agent for agent in bootstrap["agents"]}
    departments = {department["name"] for department in bootstrap["departments"]}
    conversations = {chat["id"]: chat for chat in bootstrap["conversations"]}
    assert created_agent["id"] in agents
    assert recruited_agent["id"] in agents
    assert {"老板办公室", "增长与客户", "内容部"} <= departments
    assert conversations[group_id]["kind"] == "group"
    assert conversations[group_id]["member_ids"] == [
        created_agent["id"],
        recruited_agent["id"],
    ]
    tasks = {task["id"]: task for task in bootstrap["tasks"]}
    assert tasks[task_id]["conversation_id"] == group_id
    group_messages = bootstrap["messages_by_conversation"][group_id]
    system_message = group_messages[0]
    assert system_message["sender_type"] == "system"
    assert "官网改版作战室" not in system_message["content"]
    assert "增长分析师" in system_message["content"]
    assert "内容主笔" in system_message["content"]
    assert group_messages[1]["sender_type"] == "system"
    assert "官网首屏文案确认" in group_messages[1]["content"]

    extra = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "客服专员",
            "description": "负责客户问题整理",
            "department_name": "增长与客户",
            "prompt": "你负责整理客户问题，输出 FAQ 和跟进建议。",
        },
    )
    assert extra.status_code == 200
    extra_agent = extra.json()

    add_members = client.post(
        f"/api/conversations/{group_id}/members",
        headers=auth_header(token),
        json={"member_ids": [extra_agent["id"]]},
    )
    assert add_members.status_code == 200
    assert add_members.json()["added_member_ids"] == [extra_agent["id"]]

    reloaded = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    reloaded_conversations = {chat["id"]: chat for chat in reloaded["conversations"]}
    assert reloaded_conversations[group_id]["member_ids"] == [
        created_agent["id"],
        recruited_agent["id"],
        extra_agent["id"],
    ]
    group_messages = reloaded["messages_by_conversation"][group_id]
    assert group_messages[-1]["sender_type"] == "system"
    assert "客服专员" in group_messages[-1]["content"]


def test_group_chat_returns_multiple_agent_replies_when_not_mentioned(
    tmp_path, monkeypatch
):
    captured_agents = []

    async def fake_complete(self, payload):
        captured_agents.append(payload.agent.name)
        return LlmChatResponse(
            reply=f"{payload.agent.name}：我基于群聊上下文补充一条建议。",
            provider="deepseek",
            model="deepseek-v4-flash",
            usage={"total_tokens": 42},
        )

    monkeypatch.setattr(
        workspace_routes.DeepSeekChatClient,
        "complete",
        fake_complete,
    )
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    analyst = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "增长分析师",
            "description": "负责增长数据分析",
            "department_name": "增长与客户",
            "prompt": "你负责分析渠道数据，输出下一步增长实验建议。",
        },
    ).json()
    writer = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "内容策划",
            "description": "负责内容方案",
            "department_name": "内容部",
            "prompt": "你负责把需求转成内容选题和文案计划。",
        },
    ).json()

    # Create brief first (ADR 0006 gate)
    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    secretary_chat = bootstrap["conversations"][0]
    secretary_agent = bootstrap["agents"][0]
    brief = create_confirmed_brief(
        client, token, secretary_chat["id"], secretary_agent["id"], "本周增长打法"
    )

    task = client.post(
        "/api/tasks",
        headers=auth_header(token),
        json={
            "title": "本周增长打法",
            "description": "形成渠道、内容和执行节奏建议。",
            "priority": "P1",
            "owner_agent_id": analyst["id"],
            "status": "进行中",
            "progress": 20,
            "consensus_brief_id": brief["id"],
        },
    ).json()
    group = client.post(
        "/api/conversations/group",
        headers=auth_header(token),
        json={
            "name": "增长作战室",
            "member_ids": [analyst["id"], writer["id"]],
            "related_task_ids": [task["id"]],
        },
    )
    assert group.status_code == 200
    group_id = group.json()["id"]

    send = client.post(
        f"/api/conversations/{group_id}/messages",
        headers=auth_header(token),
        json={"content": "我们一起讨论下本周增长打法"},
    )

    assert send.status_code == 200
    payload = send.json()
    # In discussion mode (TD-02), agents reply via round-robin orchestration.
    # Two agents × 4 max turns = 4 messages alternating between them.
    sender_ids = [message["sender_id"] for message in payload["agent_messages"]]
    assert len(sender_ids) == 4  # MAX_AGENT_TURNS_PER_ROUND
    # First speaker is the one who spoke least recently (round-robin)
    assert sender_ids[0] in [analyst["id"], writer["id"]]
    # Speakers alternate
    assert len(set(sender_ids)) == 2
    # All agents were called (captured_agents may include "主持人" from speaker selection)
    assert {"增长分析师", "内容策划"}.issubset(set(captured_agents))

    reloaded = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    persisted = reloaded["messages_by_conversation"][group_id]
    # In discussion mode: 1 user message + 4 agent messages
    assert [message["sender_type"] for message in persisted[-5:]] == [
        "user",
        "agent",
        "agent",
        "agent",
        "agent",
    ]
    # Agent IDs alternate between the two members
    agent_sender_ids = [m["sender_id"] for m in persisted[-4:]]
    assert len(set(agent_sender_ids)) == 2
    outputs = reloaded["task_outputs_by_task"][task["id"]]
    assert {output["agent_id"] for output in outputs} == {analyst["id"], writer["id"]}

    mentioned = client.post(
        f"/api/conversations/{group_id}/messages",
        headers=auth_header(token),
        json={
            "content": "@内容策划 你单独给个文案方向",
            "target_agent_id": writer["id"],
        },
    )
    assert mentioned.status_code == 200
    mentioned_payload = mentioned.json()
    assert [message["sender_id"] for message in mentioned_payload["agent_messages"]] == [
        writer["id"],
    ]


def test_stream_group_discussion_routes_through_orchestration(tmp_path, monkeypatch):
    """TD-02-T5 architecture assertion: the production /messages/stream path
    for a group discussion must actually call orchestration.run_discussion_round
    (not a hand-rolled loop in the route). We mock the orchestration entry point
    and assert it is invoked via a real HTTP request.
    """
    calls: list[dict] = []

    async def fake_run_discussion_round(conn, **kwargs):
        calls.append(kwargs)
        yield {"type": "end", "converged": False, "turns_used": 0}

    monkeypatch.setattr(
        workspace_routes, "run_discussion_round", fake_run_discussion_round
    )

    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    analyst = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "增长分析师",
            "description": "负责增长数据分析",
            "department_name": "增长与客户",
            "prompt": "你负责分析渠道数据。",
        },
    ).json()
    writer = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "内容策划",
            "description": "负责内容方案",
            "department_name": "内容部",
            "prompt": "你负责把需求转成内容选题。",
        },
    ).json()

    group = client.post(
        "/api/conversations/group",
        headers=auth_header(token),
        json={"name": "增长作战室", "member_ids": [analyst["id"], writer["id"]]},
    )
    assert group.status_code == 200
    group_id = group.json()["id"]

    resp = client.post(
        f"/api/conversations/{group_id}/messages/stream",
        headers=auth_header(token),
        json={"content": "我们一起讨论下本周增长打法"},
    )
    assert resp.status_code == 200
    # Consume the SSE stream so the generator body runs to completion.
    body = resp.text
    assert "event: user_message" in body

    assert len(calls) == 1, (
        "run_discussion_round must be called from the streaming production path"
    )
    assert calls[0]["conversation_id"] == group_id
    assert {a["id"] for a in calls[0]["member_agents"]} == {
        analyst["id"],
        writer["id"],
    }
    assert callable(calls[0]["turn_executor"])
    assert callable(calls[0]["llm_complete"])


def _give_agent_ready_hermes_profile(agent_id: str, workspace_id: str) -> None:
    """Simulate a completed real Hermes provisioning (same as
    test_employee_with_ready_hermes_profile_routes_to_hermes_not_function_loop)."""
    conn = connect()
    try:
        conn.execute(
            """UPDATE agent_specs
               SET role_name = ?, source_request = ?, responsibilities_json = ?,
                   hermes_profile = ?, status = 'ready', updated_at = ?
             WHERE agent_id = ? AND workspace_id = ?""",
            (
                "测试角色", "test", "[]", "ap_test_profile", now_iso(),
                agent_id, workspace_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_group_discussion_auto_creates_brief_when_converged(tmp_path, monkeypatch):
    """讨论轮收敛 → 路由层消费 brief_draft 事件，自动落 draft 共识 brief 并发
    BRIEF_CARD 系统消息；同一会话已有 draft brief 时去重不重复发卡。"""
    speaker_calls = {"n": 0}

    async def noop_function_loop(**_kwargs):
        return
        yield  # pragma: no cover — makes this an async generator

    async def fake_complete(self, payload):
        if payload.agent.name == "主持人":
            assert payload.messages[-1].content == "请严格按照系统指令完成输出"
            prompt = payload.agent.prompt or ""
            if "判断讨论是否已经充分" in prompt:
                return LlmChatResponse(
                    reply='{"converged": true, "missing": []}',
                    provider="deepseek", model="deepseek-v4-flash", usage={},
                )
            if "提炼共识纪要" in prompt:
                return LlmChatResponse(
                    reply=json.dumps(
                        {
                            "goal": "自动产出的共识目标",
                            "scope": "范围A",
                            "constraints": "约束B",
                            "success_criteria": "标准C",
                            "owner_agent_id": analyst["id"],
                            "work_items": [
                                *brief_work_items(analyst["id"])[:1],
                                {
                                    **brief_work_items(writer["id"])[1],
                                    "depends_on_keys": ["research"],
                                },
                                {
                                    **brief_work_items(writer["id"])[2],
                                    "depends_on_keys": ["draft"],
                                },
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    provider="deepseek", model="deepseek-v4-flash", usage={},
                )
            # 选人：返回非 JSON → 编排层 round-robin 兜底，轮流发言跑满 4 轮
            speaker_calls["n"] += 1
            return LlmChatResponse(
                reply="我选不出来",
                provider="deepseek", model="deepseek-v4-flash", usage={},
            )
        return LlmChatResponse(
            reply=f"{payload.agent.name}：好的，我补充一点。",
            provider="deepseek", model="deepseek-v4-flash", usage={},
        )

    monkeypatch.setattr(workspace_routes, "run_function_loop", noop_function_loop)
    monkeypatch.setattr(workspace_routes.DeepSeekChatClient, "complete", fake_complete)

    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    analyst = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "增长分析师",
            "description": "负责增长数据分析",
            "department_name": "增长与客户",
            "prompt": "你负责分析渠道数据。",
        },
    ).json()
    writer = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "内容策划",
            "description": "负责内容方案",
            "department_name": "内容部",
            "prompt": "你负责把需求转成内容选题。",
        },
    ).json()
    group = client.post(
        "/api/conversations/group",
        headers=auth_header(token),
        json={"name": "增长作战室", "member_ids": [analyst["id"], writer["id"]]},
    ).json()
    group_id = group["id"]

    send = client.post(
        f"/api/conversations/{group_id}/messages",
        headers=auth_header(token),
        json={"content": "我们一起讨论下本周增长打法"},
    )
    assert send.status_code == 200
    payload = send.json()
    # BRIEF_CARD 系统消息并入响应（在 agent_messages 末尾）
    card_messages = [
        m for m in payload["agent_messages"] if m["sender_type"] == "system"
    ]
    assert len(card_messages) == 1
    assert card_messages[0]["content"].startswith("BRIEF_CARD:")
    assert "自动产出的共识目标" in card_messages[0]["content"]

    conn = connect()
    try:
        briefs = conn.execute(
            "SELECT * FROM consensus_briefs WHERE discussion_conversation_id = ?",
            (group_id,),
        ).fetchall()
        assert len(briefs) == 1
        assert briefs[0]["status"] == "draft"
        assert briefs[0]["goal"] == "自动产出的共识目标"
        assert briefs[0]["scope"] == "范围A"

        # 再发一条：讨论再次收敛，但已有 draft brief → 去重不重复建
        send2 = client.post(
            f"/api/conversations/{group_id}/messages",
            headers=auth_header(token),
            json={"content": "那就按这个方向细化一下"},
        )
        assert send2.status_code == 200
        briefs_after = conn.execute(
            "SELECT * FROM consensus_briefs WHERE discussion_conversation_id = ?",
            (group_id,),
        ).fetchall()
        assert len(briefs_after) == 1
        cards = conn.execute(
            """SELECT id FROM messages
            WHERE conversation_id = ? AND sender_type = 'system'
              AND content LIKE 'BRIEF_CARD:%'""",
            (group_id,),
        ).fetchall()
        assert len(cards) == 1
    finally:
        conn.close()


def test_stream_group_discussion_emits_brief_card_system_event(tmp_path, monkeypatch):
    """流式端点消费 brief_draft 事件：落库 draft brief 并把 BRIEF_CARD 系统消息
    作为 event: system 推给前端（前端实时渲染卡片，不用刷新）。"""

    async def fake_run_discussion_round(conn, **kwargs):
        yield {
            "type": "brief_draft",
            "draft": {
                "goal": "流式产出的共识目标",
                "scope": "",
                "constraints": "",
                "success_criteria": "",
                "owner_agent_id": analyst["id"],
                "work_items": [
                    *brief_work_items(analyst["id"])[:1],
                    {
                        **brief_work_items(writer["id"])[1],
                        "depends_on_keys": ["research"],
                    },
                    {
                        **brief_work_items(writer["id"])[2],
                        "depends_on_keys": ["draft"],
                    },
                ],
            },
        }
        yield {"type": "end", "converged": True, "turns_used": 2}

    monkeypatch.setattr(
        workspace_routes, "run_discussion_round", fake_run_discussion_round
    )

    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    analyst = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "增长分析师",
            "description": "负责增长数据分析",
            "department_name": "增长与客户",
            "prompt": "你负责分析渠道数据。",
        },
    ).json()
    writer = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "内容策划",
            "description": "负责内容方案",
            "department_name": "内容部",
            "prompt": "你负责把需求转成内容选题。",
        },
    ).json()
    group = client.post(
        "/api/conversations/group",
        headers=auth_header(token),
        json={"name": "增长作战室", "member_ids": [analyst["id"], writer["id"]]},
    ).json()
    group_id = group["id"]

    resp = client.post(
        f"/api/conversations/{group_id}/messages/stream",
        headers=auth_header(token),
        json={"content": "我们一起讨论下本周增长打法"},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "event: system" in body
    assert "BRIEF_CARD:" in body
    assert "流式产出的共识目标" in body

    conn = connect()
    try:
        brief = conn.execute(
            "SELECT * FROM consensus_briefs WHERE discussion_conversation_id = ?",
            (group_id,),
        ).fetchone()
        assert brief is not None
        assert brief["status"] == "draft"
        assert brief["goal"] == "流式产出的共识目标"
    finally:
        conn.close()


def test_group_reply_agents_not_limited_to_three(tmp_path, monkeypatch):
    """群成员 LIMIT 3 已去掉：4 人群里第 4 个成员也要能参与回复（建群上限
    12 即天然上限）。"""
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    member_ids = []
    for index in range(4):
        agent = client.post(
            "/api/agents",
            headers=auth_header(token),
            json={
                "name": f"员工{index + 1}号",
                "description": "负责讨论",
                "department_name": "综合部",
                "prompt": "你负责参与讨论。",
            },
        ).json()
        member_ids.append(agent["id"])

    group = client.post(
        "/api/conversations/group",
        headers=auth_header(token),
        json={"name": "四人群", "member_ids": member_ids},
    ).json()

    conn = connect()
    try:
        conversation = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (group["id"],)
        ).fetchone()
        agents = workspace_routes.resolve_reply_agents(
            conn,
            auth["workspace"]["id"],
            conversation,
            workspace_routes.SendMessageRequest(content="大家好"),
        )
        assert len(agents) == 4
        assert {a["id"] for a in agents} == set(member_ids)
    finally:
        conn.close()


def test_secretary_with_hermes_profile_uses_function_loop_first(tmp_path, monkeypatch):
    """小秘（system_secretary）即使有 ready Hermes profile 也必须先走 Agent
    Action Bridge——招人/建群/建任务等系统工具只挂在 function_loop 上，先走
    Hermes 她永远拿不到工具（只会嘴上答应）。Bridge 成功时 Hermes 不该被调用。"""
    calls = {"function_loop": 0}

    async def fake_function_loop(**_kwargs):
        calls["function_loop"] += 1
        yield {"type": "tool_call", "payload": {"name": "create_task"}}
        yield {"type": "chunk", "content": "已通过系统工具处理好"}

    async def fail_stream_agent_run(*_a, **_kw):
        raise AssertionError(
            "小秘在 function_loop 成功时不该走 Hermes（stream_agent_run 被调用）"
        )
        yield  # pragma: no cover — makes this an async generator

    monkeypatch.setattr(workspace_routes, "run_function_loop", fake_function_loop)
    monkeypatch.setattr(workspace_routes, "stream_agent_run", fail_stream_agent_run)

    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]
    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    secretary = bootstrap["agents"][0]
    assert secretary["source"] == "system_secretary"
    secretary_dm = bootstrap["conversations"][0]
    _give_agent_ready_hermes_profile(secretary["id"], auth["workspace"]["id"])

    # 非流式路径（complete_agent_reply）
    send = client.post(
        f"/api/conversations/{secretary_dm['id']}/messages",
        headers=auth_header(token),
        json={"content": "今天有什么安排？"},
    )
    assert send.status_code == 200
    assert send.json()["agent_message"]["content"] == "已通过系统工具处理好"
    assert calls["function_loop"] == 1

    # 流式路径（_stream_reply_events）
    with client.stream(
        "POST",
        f"/api/conversations/{secretary_dm['id']}/messages/stream",
        headers=auth_header(token),
        json={"content": "今天有什么安排？"},
    ) as resp:
        body = "".join(resp.iter_text())
    assert "已通过系统工具处理好" in body
    assert calls["function_loop"] == 2


def test_group_discussion_secretary_cannot_use_action_bridge(tmp_path, monkeypatch):
    """Discussion-mode production routing keeps the secretary away from
    create_task/create_group tools until the owner launches a valid brief."""
    calls = {"hermes": 0}

    async def fail_function_loop(**_kwargs):
        raise AssertionError("discussion turn must not call the Action Bridge")
        yield  # pragma: no cover

    async def fake_stream_agent_run(conn, *, ctx, **_kwargs):
        calls["hermes"] += 1
        message = add_message(
            conn,
            conversation_id=ctx.conversation_id,
            sender_type="agent",
            sender_id=ctx.agent_id,
            content="小秘只讨论，不创建任务。",
            provider="hermes",
        )
        conn.commit()
        yield {"type": "chunk", "content": message["content"]}
        yield {"type": "message", "message": message}

    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]
    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    secretary = next(agent for agent in bootstrap["agents"] if agent["source"] == "system_secretary")
    group = next(conv for conv in bootstrap["conversations"] if conv["name"] == "内容经营群")
    _give_agent_ready_hermes_profile(secretary["id"], auth["workspace"]["id"])

    async def fake_discussion_round(conn, *, turn_executor, **_kwargs):
        async for event in turn_executor(conn, secretary["id"]):
            yield event
        yield {"type": "end", "converged": False, "turns_used": 1}

    monkeypatch.setattr(workspace_routes, "run_function_loop", fail_function_loop)
    monkeypatch.setattr(workspace_routes, "stream_agent_run", fake_stream_agent_run)
    monkeypatch.setattr(workspace_routes, "run_discussion_round", fake_discussion_round)

    with client.stream(
        "POST",
        f"/api/conversations/{group['id']}/messages/stream",
        headers=auth_header(token),
        json={"content": "先讨论清楚再分工。"},
    ) as response:
        body = "".join(response.iter_text())
    assert response.status_code == 200
    assert "小秘只讨论，不创建任务" in body
    assert calls["hermes"] == 1

    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM tasks WHERE workspace_id = ?",
            (auth["workspace"]["id"],),
        ).fetchone()["count"] == 0
    finally:
        conn.close()


def test_recruit_intent_not_triggered_outside_secretary_dm(tmp_path, monkeypatch):
    """招聘意图正则只在小秘私聊生效：群聊/普通员工 DM 里说"招个分析师"是在
    讨论需求，不该被截胡建一个无能力的纸片员工（chat_factory）。"""

    async def noop_function_loop(**_kwargs):
        return
        yield  # pragma: no cover — makes this an async generator

    async def fake_complete(self, payload):
        return LlmChatResponse(
            reply=f"{payload.agent.name}：收到，我们先讨论。",
            provider="deepseek", model="deepseek-v4-flash", usage={},
        )

    monkeypatch.setattr(workspace_routes, "run_function_loop", noop_function_loop)
    monkeypatch.setattr(workspace_routes.DeepSeekChatClient, "complete", fake_complete)

    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    analyst = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "增长分析师",
            "description": "负责增长数据分析",
            "department_name": "增长与客户",
            "prompt": "你负责分析渠道数据。",
        },
    ).json()
    writer = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "内容策划",
            "description": "负责内容方案",
            "department_name": "内容部",
            "prompt": "你负责把需求转成内容选题。",
        },
    ).json()
    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    agent_count_before = len(bootstrap["agents"])  # 小秘 + 2 名员工

    # ① 群聊里说"帮我招一个市场分析师" → 不建员工
    group = client.post(
        "/api/conversations/group",
        headers=auth_header(token),
        json={"name": "讨论群", "member_ids": [analyst["id"], writer["id"]]},
    ).json()
    send = client.post(
        f"/api/conversations/{group['id']}/messages",
        headers=auth_header(token),
        json={"content": "帮我招一个市场分析师"},
    )
    assert send.status_code == 200
    assert send.json()["created_agent"] is None

    # ② 普通员工 DM 里说同样的话 → 也不建员工
    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    analyst_dm = next(
        c for c in bootstrap["conversations"]
        if c["kind"] == "dm" and c.get("agent_id") == analyst["id"]
    )
    send2 = client.post(
        f"/api/conversations/{analyst_dm['id']}/messages",
        headers=auth_header(token),
        json={"content": "帮我招一个市场分析师"},
    )
    assert send2.status_code == 200
    assert send2.json()["created_agent"] is None

    reloaded = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    assert len(reloaded["agents"]) == agent_count_before
    assert not any(a["source"] == "chat_factory" for a in reloaded["agents"])


def test_task_api_updates_and_injects_related_context(tmp_path, monkeypatch):
    captured_payloads = []

    async def fake_complete(self, payload):
        captured_payloads.append(payload)
        return LlmChatResponse(
            reply="我会基于关联任务推进：先确认文案目标，再产出首屏草案。",
            provider="deepseek",
            model="deepseek-v4-flash",
            usage={"total_tokens": 96},
        )

    monkeypatch.setattr(
        workspace_routes.DeepSeekChatClient,
        "complete",
        fake_complete,
    )
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    created = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "内容策划",
            "description": "负责官网内容策划",
            "department_name": "内容部",
            "prompt": "你负责把老板的想法转成官网文案和内容计划。",
        },
    )
    assert created.status_code == 200
    agent = created.json()

    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    dm_chat = next(
        chat
        for chat in bootstrap["conversations"]
        if chat["kind"] == "dm" and chat["agent_id"] == agent["id"]
    )
    secretary_chat = bootstrap["conversations"][0]
    secretary_agent = bootstrap["agents"][0]

    # Create brief first (ADR 0006 gate)
    brief = create_confirmed_brief(
        client, token, secretary_chat["id"], secretary_agent["id"], "官网首屏文案"
    )

    task = client.post(
        "/api/tasks",
        headers=auth_header(token),
        json={
            "title": "官网首屏文案",
            "description": "输出三版首屏标题、副标题和 CTA。",
            "priority": "P1",
            "owner_agent_id": agent["id"],
            "conversation_id": dm_chat["id"],
            "status": "进行中",
            "progress": 20,
            "consensus_brief_id": brief["id"],
        },
    )
    assert task.status_code == 200
    task_payload = task.json()
    assert task_payload["description"] == "输出三版首屏标题、副标题和 CTA。"
    assert task_payload["conversation_id"] == dm_chat["id"]

    after_create = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    created_events = after_create["task_events_by_task"][task_payload["id"]]
    assert created_events[0]["kind"] == "task_created"
    assert any(event["kind"] == "task_assigned" for event in created_events)

    patched = client.patch(
        f"/api/tasks/{task_payload['id']}",
        headers=auth_header(token),
        json={"status": "待确认", "progress": 80},
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "待确认"
    assert patched.json()["progress"] == 80

    send = client.post(
        f"/api/conversations/{dm_chat['id']}/messages",
        headers=auth_header(token),
        json={"content": "继续推进这个任务"},
    )
    assert send.status_code == 200
    assert captured_payloads
    llm_payload = captured_payloads[-1]
    assert llm_payload.related_tasks[0].title == "官网首屏文案"
    assert llm_payload.related_tasks[0].status == "待确认"
    assert llm_payload.related_tasks[0].owner_name == "内容策划"

    reloaded = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    messages = reloaded["messages_by_conversation"][dm_chat["id"]]
    assert any("已创建任务：官网首屏文案" in message["content"] for message in messages)
    assert any("任务更新：官网首屏文案 · 待确认" in message["content"] for message in messages)
    events = reloaded["task_events_by_task"][task_payload["id"]]
    assert any(event["kind"] == "approval_requested" for event in events)
    assert any(event["kind"] == "agent_output_generated" for event in events)
    outputs = reloaded["task_outputs_by_task"][task_payload["id"]]
    assert outputs[0]["content"] == "我会基于关联任务推进：先确认文案目标，再产出首屏草案。"
    approvals = reloaded["approvals_by_task"][task_payload["id"]]
    assert approvals[0]["status"] == "pending"

    resolved = client.post(
        f"/api/approvals/{approvals[0]['id']}/resolve",
        headers=auth_header(token),
        json={"status": "approved"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "approved"

    completed = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    completed_task = next(
        task for task in completed["tasks"] if task["id"] == task_payload["id"]
    )
    assert completed_task["status"] == "已完成"
    completed_events = completed["task_events_by_task"][task_payload["id"]]
    assert any(event["kind"] == "approval_resolved" for event in completed_events)
    experiences = completed["agent_experiences_by_agent"][agent["id"]]
    assert experiences[0]["task_id"] == task_payload["id"]
    assert experiences[0]["outcome"] == "success"
    assert "老板已确认通过" in experiences[0]["summary"]

    follow_up = client.post(
        f"/api/conversations/{dm_chat['id']}/messages",
        headers=auth_header(token),
        json={"content": "参考之前的经验，再给我一个下一步建议"},
    )
    assert follow_up.status_code == 200
    experience_payload = captured_payloads[-1]
    assert experience_payload.agent_experiences
    assert experience_payload.agent_experiences[0].outcome == "success"
    assert "官网首屏文案" in experience_payload.agent_experiences[0].summary


def test_rejected_approval_creates_agent_lesson_experience(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    created = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "销售顾问",
            "description": "负责线索跟进",
            "department_name": "增长与客户",
            "prompt": "你负责线索跟进、报价和成交卡点复盘。",
        },
    )
    assert created.status_code == 200
    agent = created.json()

    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    dm_chat = next(
        chat
        for chat in bootstrap["conversations"]
        if chat["kind"] == "dm" and chat["agent_id"] == agent["id"]
    )
    secretary_chat = bootstrap["conversations"][0]
    secretary_agent = bootstrap["agents"][0]

    # Create brief first (ADR 0006 gate)
    brief = create_confirmed_brief(
        client, token, secretary_chat["id"], secretary_agent["id"], "整理报价策略"
    )

    task = client.post(
        "/api/tasks",
        headers=auth_header(token),
        json={
            "title": "整理报价策略",
            "description": "输出报价策略和风险提示。",
            "priority": "P1",
            "owner_agent_id": agent["id"],
            "conversation_id": dm_chat["id"],
            "status": "进行中",
            "progress": 40,
            "consensus_brief_id": brief["id"],
        },
    )
    assert task.status_code == 200
    task_payload = task.json()
    patched = client.patch(
        f"/api/tasks/{task_payload['id']}",
        headers=auth_header(token),
        json={"status": "待确认", "progress": 80},
    )
    assert patched.status_code == 200
    approval = client.get("/api/me/bootstrap", headers=auth_header(token)).json()[
        "approvals_by_task"
    ][task_payload["id"]][0]

    rejected = client.post(
        f"/api/approvals/{approval['id']}/resolve",
        headers=auth_header(token),
        json={"status": "rejected"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    reloaded = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    rejected_task = next(
        task for task in reloaded["tasks"] if task["id"] == task_payload["id"]
    )
    assert rejected_task["status"] == "阻塞"
    experiences = reloaded["agent_experiences_by_agent"][agent["id"]]
    assert experiences[0]["outcome"] == "lesson"
    assert "被老板驳回" in experiences[0]["summary"]


def test_unassigned_task_enters_pool_and_can_be_claimed(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    agent = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "运营助理",
            "description": "负责运营执行",
            "department_name": "运营部",
            "prompt": "你负责把运营任务拆成可执行动作。",
        },
    ).json()

    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    secretary_chat = bootstrap["conversations"][0]
    secretary_agent = bootstrap["agents"][0]

    # Create brief first (ADR 0006 gate)
    brief = create_confirmed_brief(
        client, token, secretary_chat["id"], secretary_agent["id"], "整理本周运营事项"
    )

    task = client.post(
        "/api/tasks",
        headers=auth_header(token),
        json={
            "title": "整理本周运营事项",
            "description": "从任务池认领后推进。",
            "priority": "P2",
            "status": "进行中",
            "progress": 10,
            "consensus_brief_id": brief["id"],
        },
    )
    assert task.status_code == 200
    task_payload = task.json()
    assert task_payload["owner_agent_id"] is None
    assert task_payload["status"] == "待认领"
    assert task_payload["progress"] == 0

    claimed = client.post(
        f"/api/tasks/{task_payload['id']}/claim",
        headers=auth_header(token),
        json={"agent_id": agent["id"]},
    )
    assert claimed.status_code == 200
    claimed_payload = claimed.json()
    assert claimed_payload["owner_agent_id"] == agent["id"]
    assert claimed_payload["status"] == "进行中"
    assert claimed_payload["progress"] == 20

    reloaded = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    events = reloaded["task_events_by_task"][task_payload["id"]]
    assert any(event["kind"] == "task_claimed" for event in events)


def test_task_pool_suggests_matching_agent_for_unassigned_task(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    writer = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "内容策划",
            "description": "负责公众号文案和官网内容",
            "department_name": "内容部",
            "prompt": "你负责选题、文案和官网内容产出。",
        },
    ).json()
    client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "财务助理",
            "description": "负责记账和对账",
            "department_name": "财务行政",
            "prompt": "你负责财务报表和费用对账。",
        },
    )

    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    secretary_chat = bootstrap["conversations"][0]
    secretary_agent = bootstrap["agents"][0]

    # Create brief first (ADR 0006 gate)
    brief = create_confirmed_brief(
        client, token, secretary_chat["id"], secretary_agent["id"], "写官网首页文案"
    )

    task = client.post(
        "/api/tasks",
        headers=auth_header(token),
        json={
            "title": "写官网首页文案",
            "description": "输出一版官网首屏内容和价值主张。",
            "priority": "P1",
            "consensus_brief_id": brief["id"],
        },
    ).json()
    reloaded = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    matched_task = next(item for item in reloaded["tasks"] if item["id"] == task["id"])

    assert matched_task["status"] == "待认领"
    assert matched_task["suggested_agent_id"] == writer["id"]
    assert matched_task["suggested_agent_reason"]


def test_discussion_status_changes_on_brief_lifecycle(tmp_path, monkeypatch):
    """Test that discussion_status updates correctly on brief confirm/reject.

    TD-01-T1: brief confirm/reject 接线讨论态状态机 (G3)
    - create_brief → discussion_status = 'discussing'
    - confirm_brief → discussion_status = 'aligned'
    - reject_brief → discussion_status = 'discussing'
    """
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    secretary_chat = bootstrap["conversations"][0]
    secretary_agent = bootstrap["agents"][0]

    # Initial status should be 'discussing'
    assert secretary_chat["discussion_status"] == "discussing"

    # Create a brief draft
    brief = client.post(
        "/api/briefs",
        headers=auth_header(token),
        json={
            "discussion_conversation_id": secretary_chat["id"],
            "goal": "本周增长打法",
            "owner_agent_id": secretary_agent["id"],
            "participant_agent_ids": [secretary_agent["id"]],
            "work_items": brief_work_items(secretary_agent["id"]),
            "created_by_agent_id": secretary_agent["id"],
        },
    )
    assert brief.status_code == 200

    # After creating brief, status should still be 'discussing'
    reloaded = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    chat_after_create = next(
        c for c in reloaded["conversations"] if c["id"] == secretary_chat["id"]
    )
    assert chat_after_create["discussion_status"] == "discussing"

    # Reject the brief
    rejected = client.post(
        f"/api/briefs/{brief.json()['id']}/reject",
        headers=auth_header(token),
    )
    assert rejected.status_code == 200

    # After reject, status should still be 'discussing'
    reloaded = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    chat_after_reject = next(
        c for c in reloaded["conversations"] if c["id"] == secretary_chat["id"]
    )
    assert chat_after_reject["discussion_status"] == "discussing"

    # Create another brief and confirm it
    brief2 = client.post(
        "/api/briefs",
        headers=auth_header(token),
        json={
            "discussion_conversation_id": secretary_chat["id"],
            "goal": "官网改版计划",
            "owner_agent_id": secretary_agent["id"],
            "participant_agent_ids": [secretary_agent["id"]],
            "work_items": brief_work_items(secretary_agent["id"]),
            "created_by_agent_id": secretary_agent["id"],
        },
    )
    assert brief2.status_code == 200

    conn = connect()
    try:
        confirm_brief(
            conn,
            workspace_id=auth["workspace"]["id"],
            brief_id=brief2.json()["id"],
            confirmed_by_user_id=auth["user"]["id"],
        )
        conn.commit()
    finally:
        conn.close()

    # After confirm, status should be 'aligned'
    reloaded = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    chat_after_confirm = next(
        c for c in reloaded["conversations"] if c["id"] == secretary_chat["id"]
    )
    assert chat_after_confirm["discussion_status"] == "aligned"


def test_task_out_includes_consensus_brief_id(tmp_path, monkeypatch):
    """Test that TaskOut includes consensus_brief_id field.

    TD-01-T1b: TaskOut 补 consensus_brief_id (G2)
    - Task created with consensus_brief_id should return it
    - bootstrap should include consensus_brief_id in task data
    """
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    secretary_chat = bootstrap["conversations"][0]
    secretary_agent = bootstrap["agents"][0]

    # Create and confirm a brief
    brief = create_confirmed_brief(
        client, token, secretary_chat["id"], secretary_agent["id"], "官网改版"
    )

    # Create task with consensus_brief_id
    task = client.post(
        "/api/tasks",
        headers=auth_header(token),
        json={
            "title": "官网改版执行",
            "description": "执行官网改版计划",
            "priority": "P1",
            "owner_agent_id": secretary_agent["id"],
            "consensus_brief_id": brief["id"],
        },
    )
    assert task.status_code == 200
    task_data = task.json()
    assert task_data["consensus_brief_id"] == brief["id"]

    # bootstrap should also include consensus_brief_id
    reloaded = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    task_in_bootstrap = next(
        t for t in reloaded["tasks"] if t["id"] == task_data["id"]
    )
    assert task_in_bootstrap["consensus_brief_id"] == brief["id"]


def test_conversation_runs_endpoint_returns_step_by_step_trace(tmp_path, monkeypatch):
    """Audit/timeline view requested independently by multiple early users
    (Product Hunt feedback, see CHANGELOG): GET .../runs must return every
    run for a conversation with its full run_steps trace, in order."""
    from app.runtime.runs import append_run_step, create_run, transition_run

    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]
    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    secretary_chat = bootstrap["conversations"][0]
    secretary_agent = bootstrap["agents"][0]

    conn = connect()
    try:
        run_id = create_run(
            conn,
            workspace_id=auth["workspace"]["id"],
            conversation_id=secretary_chat["id"],
            agent_id=secretary_agent["id"],
            input_message_id="msg_seed",
            provider="hermes",
            model="deepseek-v4-pro",
        )
        append_run_step(
            conn, run_id=run_id, type="tool_call", title="rm -rf test.txt",
            detail="recursive delete", payload={"command": "rm -rf test.txt"},
        )
        append_run_step(
            conn, run_id=run_id, type="approval_required", title="需要确认",
            detail="高风险操作", payload={"category": "high_risk"},
        )
        transition_run(conn, run_id, "running")
        transition_run(conn, run_id, "completed")
        conn.commit()
    finally:
        conn.close()

    resp = client.get(
        f"/api/conversations/{secretary_chat['id']}/runs",
        headers=auth_header(token),
    )
    assert resp.status_code == 200
    runs = resp.json()
    assert len(runs) == 1
    assert runs[0]["id"] == run_id
    assert runs[0]["agent_name"] == secretary_agent["name"]
    assert runs[0]["status"] == "completed"
    step_types = [s["type"] for s in runs[0]["steps"]]
    assert step_types == ["tool_call", "approval_required"]
    assert runs[0]["steps"][0]["payload"]["command"] == "rm -rf test.txt"
    assert runs[0]["waiting_on"] is None  # completed run — nothing pending


def test_conversation_runs_endpoint_surfaces_waiting_on(tmp_path, monkeypatch):
    """service-claw-cloud borrow: a suspended run's card should say what/who
    it's blocked on without a click-through into the step-by-step trace."""
    from app.runtime.runs import create_run, transition_run
    from app.services.workspace import new_id, now_iso

    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]
    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    secretary_chat = bootstrap["conversations"][0]
    secretary_agent = bootstrap["agents"][0]

    conn = connect()
    try:
        run_id = create_run(
            conn,
            workspace_id=auth["workspace"]["id"],
            conversation_id=secretary_chat["id"],
            agent_id=secretary_agent["id"],
            input_message_id="msg_seed2",
            provider="hermes",
        )
        transition_run(conn, run_id, "running")
        transition_run(conn, run_id, "waiting_user")
        conn.execute(
            "INSERT INTO approvals (id, workspace_id, conversation_id, agent_id, "
            "title, description, status, risk_level, type, run_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', 'high', 'high_risk', ?, ?)",
            (
                new_id("appr"), auth["workspace"]["id"], secretary_chat["id"],
                secretary_agent["id"], "高风险动作需确认：rm -rf",
                "删除 scratch 目录", run_id, now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    resp = client.get(
        f"/api/conversations/{secretary_chat['id']}/runs",
        headers=auth_header(token),
    )
    assert resp.status_code == 200
    run = resp.json()[0]
    assert run["status"] == "waiting_user"
    assert run["waiting_on"] == "等老板批准：删除 scratch 目录"


def test_bootstrap_anomaly_count_24h(tmp_path, monkeypatch):
    """service-claw-cloud borrow: a boss-facing 'did anything go wrong in the
    last 24h' count — failed runs + expired (timed-out) approvals. A
    rejected approval is the owner's own call, not an anomaly, and must not
    be counted."""
    from app.runtime.runs import create_run, transition_run
    from app.services.workspace import new_id, now_iso

    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]
    ws_id = auth["workspace"]["id"]
    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    secretary_chat = bootstrap["conversations"][0]
    secretary_agent = bootstrap["agents"][0]
    assert bootstrap["anomaly_count_24h"] == 0

    conn = connect()
    try:
        failed_run = create_run(
            conn, workspace_id=ws_id, conversation_id=secretary_chat["id"],
            agent_id=secretary_agent["id"], input_message_id="msg_f",
        )
        transition_run(conn, failed_run, "running")
        transition_run(conn, failed_run, "failed", error="boom")

        rejected_run = create_run(
            conn, workspace_id=ws_id, conversation_id=secretary_chat["id"],
            agent_id=secretary_agent["id"], input_message_id="msg_r",
        )
        transition_run(conn, rejected_run, "running")
        transition_run(conn, rejected_run, "waiting_user")
        conn.execute(
            "INSERT INTO approvals (id, workspace_id, conversation_id, agent_id, "
            "title, status, risk_level, type, run_id, resolved_at, created_at) "
            "VALUES (?, ?, ?, ?, 'x', 'rejected', 'high', 'high_risk', ?, ?, ?)",
            (new_id("appr"), ws_id, secretary_chat["id"], secretary_agent["id"],
             rejected_run, now_iso(), now_iso()),
        )
        transition_run(conn, rejected_run, "completed")

        expired_run = create_run(
            conn, workspace_id=ws_id, conversation_id=secretary_chat["id"],
            agent_id=secretary_agent["id"], input_message_id="msg_e",
        )
        transition_run(conn, expired_run, "running")
        transition_run(conn, expired_run, "waiting_user")
        conn.execute(
            "INSERT INTO approvals (id, workspace_id, conversation_id, agent_id, "
            "title, status, risk_level, type, run_id, resolved_at, created_at) "
            "VALUES (?, ?, ?, ?, 'y', 'expired', 'high', 'high_risk', ?, ?, ?)",
            (new_id("appr"), ws_id, secretary_chat["id"], secretary_agent["id"],
             expired_run, now_iso(), now_iso()),
        )
        transition_run(conn, expired_run, "failed", error="approval timed out")
        conn.commit()
    finally:
        conn.close()

    bootstrap = client.get("/api/me/bootstrap", headers=auth_header(token)).json()
    # 1 failed run (the plain failed_run) + 1 expired approval, but expired_run
    # is *also* a failed run — counted once each by its own query, so total is
    # 2 failed runs (failed_run, expired_run) + 1 expired approval = 3.
    assert bootstrap["anomaly_count_24h"] == 3
