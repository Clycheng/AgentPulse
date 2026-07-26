"""Tests for the team compiler: one paragraph -> N role drafts -> N real
employees + one group conversation.

Unit tests cover parse_team_draft's parsing/validation (no LLM call needed —
feed it raw JSON strings). Integration tests cover the two HTTP endpoints:
draft-team (mocked LLM) and create-team (real DB, mocked provisioner so no
real Hermes CLI gets shelled out to)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api.routes import team_compiler as team_compiler_routes
from app.core.config import settings
from app.core.database import connect, init_db
from app.main import app
from app.orchestration.team_compiler import TeamDraftError, parse_team_draft
from app.runtime.profile_provisioner import RecordOnlyProvisioner


# --------------------------------------------------------------- parse_team_draft


def test_parse_team_draft_happy_path():
    raw = json.dumps(
        {
            "members": [
                {
                    "name": "阿工",
                    "role": "质检专员",
                    "department": "质检部",
                    "description": "核查打卡照片",
                    "responsibilities": ["逐一核查上门打卡照片"],
                    "suggested_capability_keys": ["data_analysis", "made_up_key"],
                },
                {
                    "name": "阿测",
                    "role": "机动备援",
                    "department": "总办",
                    "description": "哪里需要去哪里",
                    "responsibilities": ["持续关注同事进度，主动支援"],
                    "suggested_capability_keys": [],
                },
            ]
        },
        ensure_ascii=False,
    )
    members = parse_team_draft(raw, source_request="老板的原始描述")
    assert len(members) == 2
    assert members[0]["name"] == "阿工"
    # unknown key silently stripped (draft_role_spec's existing behavior)
    assert members[0]["capability_keys"] == ["data_analysis"]
    assert members[1]["capability_keys"] == []


def test_parse_team_draft_strips_markdown_fences():
    raw = "```json\n" + json.dumps({"members": [{"name": "小明", "role": "工程师"}]}) + "\n```"
    members = parse_team_draft(raw, source_request="x")
    assert members[0]["name"] == "小明"


def test_parse_team_draft_skips_malformed_entries_not_whole_batch():
    raw = json.dumps(
        {
            "members": [
                {"name": "", "role": "缺名字的岗位"},  # malformed, skipped
                {"name": "小王", "role": "文案"},
            ]
        }
    )
    members = parse_team_draft(raw, source_request="x")
    assert len(members) == 1
    assert members[0]["name"] == "小王"


def test_parse_team_draft_raises_on_invalid_json():
    with pytest.raises(TeamDraftError):
        parse_team_draft("这不是 JSON", source_request="x")


def test_parse_team_draft_raises_on_no_members():
    with pytest.raises(TeamDraftError):
        parse_team_draft(json.dumps({"members": []}), source_request="x")


# --------------------------------------------------------------- HTTP: draft-team


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings, "database_url", f"sqlite:///{tmp_path / 'team_compiler.sqlite3'}"
    )
    monkeypatch.setattr(settings, "password_iterations", 1_000)
    init_db()
    client = TestClient(app)
    resp = client.post(
        "/api/auth/register",
        json={"email": "boss@ex.com", "password": "agentpulse123",
              "display_name": "老板", "workspace_name": "公司"},
    )
    token = resp.json()["access_token"]
    return client, token


def test_draft_team_endpoint_returns_parsed_members(tmp_path, monkeypatch):
    client, token = _client(tmp_path, monkeypatch)

    async def fake_hermes_control(_conn, **kwargs):
        assert kwargs["purpose"] == "team-draft"
        assert "团队需求" in kwargs["prompt"]
        return json.dumps(
            {
                "members": [
                    {
                        "name": "阿工",
                        "role": "质检专员",
                        "department": "质检部",
                        "description": "核查打卡照片",
                        "responsibilities": ["核查真实性"],
                        "suggested_capability_keys": ["data_analysis"],
                    }
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(
        team_compiler_routes, "run_hermes_control", fake_hermes_control
    )

    resp = client.post(
        "/api/agents/draft-team",
        headers={"Authorization": f"Bearer {token}"},
        json={"description": "我需要一个质检专员核查打卡照片"},
    )
    assert resp.status_code == 200
    members = resp.json()["members"]
    assert len(members) == 1
    assert members[0]["name"] == "阿工"
    assert members[0]["capability_keys"] == ["data_analysis"]


def test_draft_team_endpoint_422_on_unparseable_output(tmp_path, monkeypatch):
    client, token = _client(tmp_path, monkeypatch)

    async def fake_hermes_control(_conn, **_kwargs):
        return "不是JSON"

    monkeypatch.setattr(
        team_compiler_routes, "run_hermes_control", fake_hermes_control
    )
    resp = client.post(
        "/api/agents/draft-team",
        headers={"Authorization": f"Bearer {token}"},
        json={"description": "随便"},
    )
    assert resp.status_code == 422


# --------------------------------------------------------------- HTTP: create-team


def test_create_team_creates_agents_and_one_group(tmp_path, monkeypatch):
    """The core real-world scenario: several roles, one of them a mixed
    bundle (some capabilities need credentials nobody configured) — every
    member still gets created, the group still gets everyone in it."""
    monkeypatch.setattr(
        settings, "database_url", f"sqlite:///{tmp_path / 'create_team.sqlite3'}"
    )
    monkeypatch.setattr(settings, "password_iterations", 1_000)
    monkeypatch.setattr(settings, "hermes_provisioning", True)
    import app.orchestration.supply as supply_module

    monkeypatch.setattr(
        supply_module, "build_provisioner_from_settings", lambda: RecordOnlyProvisioner()
    )
    init_db()
    client = TestClient(app)
    resp = client.post(
        "/api/auth/register",
        json={"email": "boss2@ex.com", "password": "agentpulse123",
              "display_name": "老板", "workspace_name": "公司2"},
    )
    token = resp.json()["access_token"]

    resp = client.post(
        "/api/agents/create-team",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "group_name": "养老+抖音项目组",
            "members": [
                {
                    "name": "护理质检小李",
                    "role": "质检专员",
                    "department": "质检部",
                    "description": "核查打卡照片",
                    "responsibilities": ["逐一核查上门打卡照片"],
                    "capability_keys": ["data_analysis", "content_writing"],
                },
                {
                    "name": "运营老周",
                    "role": "运营负责人",
                    "department": "运营部",
                    "description": "统筹进度",
                    "responsibilities": ["统筹项目进度"],
                    # mixed bundle: ad_bidding needs AD_API_KEY (not configured)
                    "capability_keys": ["data_analysis", "ad_bidding"],
                },
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["agents"]) == 2
    assert data["conversation_id"] is not None

    conn = connect()
    conv = conn.execute(
        "SELECT name FROM conversations WHERE id = ?", (data["conversation_id"],)
    ).fetchone()
    assert conv["name"] == "养老+抖音项目组"
    members = conn.execute(
        "SELECT agent_id FROM conversation_members WHERE conversation_id = ?",
        (data["conversation_id"],),
    ).fetchall()
    assert {m["agent_id"] for m in members} == {a["id"] for a in data["agents"]}

    for agent in data["agents"]:
        spec = conn.execute(
            "SELECT status, hermes_profile FROM agent_specs WHERE agent_id = ?",
            (agent["id"],),
        ).fetchone()
        assert spec is not None
        assert spec["status"] == "ready" and spec["hermes_profile"]

    zhi_jian = next(a for a in data["agents"] if a["name"] == "护理质检小李")
    caps = {
        row["capability_key"]: row["status"]
        for row in conn.execute(
            "SELECT capability_key, status FROM agent_capabilities WHERE agent_id = ?",
            (zhi_jian["id"],),
        ).fetchall()
    }
    assert caps == {"data_analysis": "enabled", "content_writing": "enabled"}

    yun_ying = next(a for a in data["agents"] if a["name"] == "运营老周")
    caps2 = {
        row["capability_key"]: row["status"]
        for row in conn.execute(
            "SELECT capability_key, status FROM agent_capabilities WHERE agent_id = ?",
            (yun_ying["id"],),
        ).fetchall()
    }
    assert caps2 == {"data_analysis": "enabled", "ad_bidding": "credential_missing"}


def test_create_team_single_member_gets_no_group(tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings, "database_url", f"sqlite:///{tmp_path / 'create_team_solo.sqlite3'}"
    )
    monkeypatch.setattr(settings, "password_iterations", 1_000)
    init_db()
    client = TestClient(app)
    resp = client.post(
        "/api/auth/register",
        json={"email": "boss3@ex.com", "password": "agentpulse123",
              "display_name": "老板", "workspace_name": "公司3"},
    )
    token = resp.json()["access_token"]

    resp = client.post(
        "/api/agents/create-team",
        headers={"Authorization": f"Bearer {token}"},
        json={"members": [{"name": "小明", "role": "文案", "description": "写文案"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["conversation_id"] is None
