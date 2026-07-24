"""Auto-provisioning wiring (TD-03-T5): create_agent_spec -> provision.

Always-on tests use a RecordOnly provisioner to assert the wiring (SOUL +
credentials written, model, profile-name shape). The guarded e2e drives the real
`hermes` CLI to prove "hire -> a runnable Hermes profile exists".

    HERMES_E2E=1 pytest tests/test_auto_provision.py
"""

import os
import shutil
import sqlite3
import tempfile

import pytest

from app.core.config import settings
from app.core.database import Database
from app.orchestration.supply import create_agent_spec, provision
from app.runtime.profile_provisioner import (
    LocalHermesProvisioner,
    RecordOnlyProvisioner,
)


def _make_db() -> Database:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db = Database(conn, "sqlite")
    db.executescript(
        """
        CREATE TABLE workspaces (id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE agents (
            id TEXT PRIMARY KEY, workspace_id TEXT, name TEXT, role TEXT,
            description TEXT, prompt TEXT
        );
        CREATE TABLE agent_specs (
            id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            role_name TEXT NOT NULL, source_request TEXT NOT NULL DEFAULT '',
            responsibilities_json TEXT NOT NULL DEFAULT '[]', hermes_profile TEXT,
            status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE agent_capabilities (
            id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            capability_key TEXT NOT NULL, skill_refs_json TEXT NOT NULL DEFAULT '[]',
            toolset_refs_json TEXT NOT NULL DEFAULT '[]', mcp_refs_json TEXT NOT NULL DEFAULT '[]',
            required_credentials_json TEXT NOT NULL DEFAULT '[]',
            risk_gate TEXT NOT NULL DEFAULT 'auto', status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO workspaces (id, name) VALUES ('ws_1', '测试公司');
        INSERT INTO agents (id, workspace_id, name, role, description, prompt)
        VALUES ('agent_abc123', 'ws_1', '小研', '研究员', '做研究', '你负责调研并输出结论');
        """
    )
    return db


def _spec_with_auto_cap(db: Database):
    create_agent_spec(
        db,
        agent_id="agent_abc123",
        workspace_id="ws_1",
        role_name="研究员",
        responsibilities=["调研", "写报告"],
        capability_keys=["content_writing"],  # auto, no credentials
    )


def test_wiring_records_soul_and_credentials(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-fake-test")
    db = _make_db()
    _spec_with_auto_cap(db)
    rec = RecordOnlyProvisioner()
    spec = provision(db, "agent_abc123", provisioner=rec)

    assert spec["status"] == "ready"
    profile = spec["hermes_profile"]
    assert profile and profile.replace("_", "").isalnum() and profile.islower()

    actions = [a.action for a in rec.get_actions()]
    assert actions[:2] == ["create_profile", "write_soul"]
    assert "configure" in actions
    assert "write_credentials" not in actions  # injected into each ACP subprocess


def test_default_provisioner_is_record_only_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "hermes_provisioning", False)
    from app.runtime.profile_provisioner import build_provisioner_from_settings

    assert isinstance(build_provisioner_from_settings(), RecordOnlyProvisioner)


# --------------------------------------------------- secretary bootstrap default


def test_secretary_gets_default_capabilities_when_provisioning_enabled(tmp_path, monkeypatch):
    from app.core.database import connect, init_db
    from app.services.workspace import (
        SECRETARY_DEFAULT_CAPABILITIES,
        create_workspace_for_user,
        new_id,
        now_iso,
    )
    from app.orchestration.capability_catalog import CATALOG

    monkeypatch.setattr(
        settings, "database_url", f"sqlite:///{tmp_path / 'secretary_on.sqlite3'}"
    )
    monkeypatch.setattr(settings, "hermes_provisioning", True)
    # Exercise the settings.hermes_provisioning=True *code path* without
    # actually shelling out to a real `hermes` CLI — this machine happens to
    # have one installed, so without this the test would silently create a
    # real orphaned profile under ~/.hermes/profiles on every run (found the
    # hard way: 5 orphans accumulated before this fix).
    import app.orchestration.supply as supply_module

    monkeypatch.setattr(
        supply_module, "build_provisioner_from_settings", lambda: RecordOnlyProvisioner()
    )
    init_db()
    conn = connect()
    user_id = new_id("user")
    conn.execute(
        "INSERT INTO users (id, email, password_hash, display_name, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, "boss3@ex.com", "x", "老板", now_iso()),
    )
    ws = create_workspace_for_user(conn, user_id, "公司3")
    conn.commit()

    secretary = conn.execute(
        "SELECT id FROM agents WHERE workspace_id = ? AND name = '小秘'", (ws["id"],)
    ).fetchone()
    spec = conn.execute(
        "SELECT status, hermes_profile FROM agent_specs WHERE agent_id = ?",
        (secretary["id"],),
    ).fetchone()
    assert spec is not None
    assert spec["status"] == "ready" and spec["hermes_profile"]

    caps = {
        row["capability_key"]: row["status"]
        for row in conn.execute(
            "SELECT capability_key, status FROM agent_capabilities WHERE agent_id = ?",
            (secretary["id"],),
        ).fetchall()
    }
    assert set(caps) == set(SECRETARY_DEFAULT_CAPABILITIES) == set(CATALOG)
    assert {
        key for key, status in caps.items() if status == "enabled"
    } == {key for key, cap in CATALOG.items() if not cap.required_credentials}
    assert {
        key for key, status in caps.items() if status == "credential_missing"
    } == {key for key, cap in CATALOG.items() if cap.required_credentials}


def test_secretary_has_full_capability_spec_when_provisioning_disabled(tmp_path, monkeypatch):
    """Fallback mode still exposes the full authorization state to the UI."""
    from app.core.database import connect, init_db
    from app.services.workspace import (
        SECRETARY_DEFAULT_CAPABILITIES,
        create_workspace_for_user,
        new_id,
        now_iso,
    )

    monkeypatch.setattr(
        settings, "database_url", f"sqlite:///{tmp_path / 'secretary_off.sqlite3'}"
    )
    monkeypatch.setattr(settings, "hermes_provisioning", False)
    init_db()
    conn = connect()
    user_id = new_id("user")
    conn.execute(
        "INSERT INTO users (id, email, password_hash, display_name, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, "boss4@ex.com", "x", "老板", now_iso()),
    )
    ws = create_workspace_for_user(conn, user_id, "公司4")
    conn.commit()

    secretary = conn.execute(
        "SELECT id FROM agents WHERE workspace_id = ? AND name = '小秘'", (ws["id"],)
    ).fetchone()
    spec = conn.execute(
        "SELECT status, hermes_profile FROM agent_specs WHERE agent_id = ?", (secretary["id"],)
    ).fetchone()
    assert spec["status"] == "blocked_on_credentials"
    assert spec["hermes_profile"] is None
    cap_count = conn.execute(
        "SELECT COUNT(*) AS c FROM agent_capabilities WHERE agent_id = ?",
        (secretary["id"],),
    ).fetchone()["c"]
    assert cap_count == len(SECRETARY_DEFAULT_CAPABILITIES)


def test_secretary_backfills_missing_capabilities_on_bootstrap(tmp_path, monkeypatch):
    from app.core.database import connect, init_db
    from app.services.workspace import (
        SECRETARY_DEFAULT_CAPABILITIES,
        create_workspace_for_user,
        get_bootstrap,
        new_id,
        now_iso,
    )

    monkeypatch.setattr(
        settings, "database_url", f"sqlite:///{tmp_path / 'secretary_backfill.sqlite3'}"
    )
    monkeypatch.setattr(settings, "hermes_provisioning", False)
    init_db()
    conn = connect()
    user_id = new_id("user")
    conn.execute(
        "INSERT INTO users (id, email, password_hash, display_name, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, "boss4b@ex.com", "x", "老板", now_iso()),
    )
    ws = create_workspace_for_user(conn, user_id, "公司4b")
    secretary = conn.execute(
        "SELECT id FROM agents WHERE workspace_id = ? AND name = '小秘'", (ws["id"],)
    ).fetchone()
    conn.execute(
        "DELETE FROM agent_capabilities WHERE agent_id = ? AND capability_key = 'computer_use'",
        (secretary["id"],),
    )
    conn.commit()

    get_bootstrap(conn, ws["id"])
    keys = {
        row["capability_key"]
        for row in conn.execute(
            "SELECT capability_key FROM agent_capabilities WHERE agent_id = ?",
            (secretary["id"],),
        ).fetchall()
    }
    assert keys == set(SECRETARY_DEFAULT_CAPABILITIES)
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM agent_specs WHERE agent_id = ?", (secretary["id"],)
    ).fetchone()["c"] == 1


# ------------------------------------------- Talent Market hire provisioning


def _recruit_setup(tmp_path, monkeypatch, *, db_name):
    """Same real-code-path-without-real-Hermes pattern as the secretary tests."""
    from app.core.database import connect, init_db
    from app.services.workspace import create_workspace_for_user, new_id, now_iso

    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / db_name}")
    monkeypatch.setattr(settings, "hermes_provisioning", True)
    import app.orchestration.supply as supply_module

    monkeypatch.setattr(
        supply_module, "build_provisioner_from_settings", lambda: RecordOnlyProvisioner()
    )
    init_db()
    conn = connect()
    user_id = new_id("user")
    conn.execute(
        "INSERT INTO users (id, email, password_hash, display_name, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, "boss5@ex.com", "x", "老板", now_iso()),
    )
    ws = create_workspace_for_user(conn, user_id, "公司5")
    conn.commit()
    return conn, ws["id"]


def test_recruit_from_template_provisions_credential_free_role(tmp_path, monkeypatch):
    """内容主笔 (content-writer): every capability in its bundle is
    credential-free, so the hire should come out fully ready — this is
    the product's main advertised hiring flow (Talent Market), which
    previously produced an employee with no agent_specs row at all."""
    from app.services.workspace import recruit_from_template

    conn, ws_id = _recruit_setup(tmp_path, monkeypatch, db_name="recruit_clean.sqlite3")
    agent = recruit_from_template(
        conn, workspace_id=ws_id, template_id="content-writer", department_name=None
    )

    spec = conn.execute(
        "SELECT status, hermes_profile FROM agent_specs WHERE agent_id = ?",
        (agent["id"],),
    ).fetchone()
    assert spec is not None
    assert spec["status"] == "ready" and spec["hermes_profile"]

    caps = {
        row["capability_key"]: row["status"]
        for row in conn.execute(
            "SELECT capability_key, status FROM agent_capabilities WHERE agent_id = ?",
            (agent["id"],),
        ).fetchall()
    }
    assert caps == {"content_writing": "enabled", "seo_content": "enabled"}


def test_recruit_from_template_mixed_role_stays_ready(tmp_path, monkeypatch):
    """运营负责人 (ops-lead) mixes credential-free (data_analysis,
    report_generation) with credential-needing (ad_analysis, ad_bidding —
    need AD_API_KEY nobody has configured) capabilities. provision() is
    all-or-nothing, so without the credential split this bundle would leave
    the employee stuck at blocked_on_credentials with *no* real profile at
    all — worse than useless for the two capabilities that could have
    worked immediately."""
    from app.services.workspace import recruit_from_template

    conn, ws_id = _recruit_setup(tmp_path, monkeypatch, db_name="recruit_mixed.sqlite3")
    agent = recruit_from_template(
        conn, workspace_id=ws_id, template_id="ops-lead", department_name=None
    )

    spec = conn.execute(
        "SELECT status, hermes_profile FROM agent_specs WHERE agent_id = ?",
        (agent["id"],),
    ).fetchone()
    assert spec is not None
    assert spec["status"] == "ready" and spec["hermes_profile"]

    caps = {
        row["capability_key"]: row["status"]
        for row in conn.execute(
            "SELECT capability_key, status FROM agent_capabilities WHERE agent_id = ?",
            (agent["id"],),
        ).fetchall()
    }
    assert caps["data_analysis"] == "enabled"
    assert caps["report_generation"] == "enabled"
    assert caps["ad_analysis"] == "credential_missing"
    assert caps["ad_bidding"] == "credential_missing"


def test_recruit_from_template_noop_when_provisioning_disabled(tmp_path, monkeypatch):
    from app.core.database import connect, init_db
    from app.services.workspace import (
        create_workspace_for_user,
        new_id,
        now_iso,
        recruit_from_template,
    )

    monkeypatch.setattr(
        settings, "database_url", f"sqlite:///{tmp_path / 'recruit_off.sqlite3'}"
    )
    monkeypatch.setattr(settings, "hermes_provisioning", False)
    init_db()
    conn = connect()
    user_id = new_id("user")
    conn.execute(
        "INSERT INTO users (id, email, password_hash, display_name, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, "boss6@ex.com", "x", "老板", now_iso()),
    )
    ws = create_workspace_for_user(conn, user_id, "公司6")
    conn.commit()

    agent = recruit_from_template(
        conn, workspace_id=ws["id"], template_id="content-writer", department_name=None
    )
    spec = conn.execute(
        "SELECT id FROM agent_specs WHERE agent_id = ?", (agent["id"],)
    ).fetchone()
    assert spec is None


_E2E = os.environ.get("HERMES_E2E") == "1" and shutil.which("hermes") is not None


@pytest.mark.skipif(not _E2E, reason="set HERMES_E2E=1 with hermes installed")
def test_hire_creates_runnable_profile(monkeypatch):
    work_root = tempfile.mkdtemp(prefix="ap-autoprov-")
    monkeypatch.setattr(settings, "hermes_provisioning", True)
    monkeypatch.setattr(settings, "hermes_work_root", work_root)
    if not settings.deepseek_api_key:
        monkeypatch.setattr(settings, "deepseek_api_key", "sk-placeholder")

    db = _make_db()
    _spec_with_auto_cap(db)
    prov = LocalHermesProvisioner(work_root=work_root)
    try:
        spec = provision(db, "agent_abc123")  # uses config-driven LocalHermes
        assert spec["status"] == "ready"
        profile = spec["hermes_profile"]

        pdir = prov._profile_dir(profile)
        config = (pdir / "config.yaml").read_text()
        assert "deepseek-v4-flash" in config
        assert (pdir / "SOUL.md").exists()
        assert "研究员" in (pdir / "SOUL.md").read_text()
        assert "DEEPSEEK_API_KEY" in (pdir / ".env").read_text()
    finally:
        prov.delete_profile(spec["hermes_profile"] if "spec" in dir() else "")
