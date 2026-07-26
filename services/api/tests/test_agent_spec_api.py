"""Tests for agent spec API routes (TD-04-T5).

Covers:
- POST /api/agents with role_spec → creates agent + spec + provisions
- GET /api/agents/{id}/spec → returns spec with capabilities
- POST /api/agents/{id}/credentials → provides credential, enables capability
- POST /api/agents/{id}/provision → idempotent re-provision
"""

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import init_db
from app.main import app


def make_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(
        settings,
        "database_url",
        f"sqlite:///{tmp_path / 'test_agent_spec.sqlite3'}",
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
            "workspace_name": "测试公司",
        },
    )
    assert response.status_code == 200
    return response.json()


def test_create_agent_without_role_spec(tmp_path, monkeypatch):
    """POST /api/agents without role_spec works as before (no spec created)."""
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    response = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "普通员工",
            "description": "测试",
            "department_name": "技术部",
            "prompt": "你是一个测试员工",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "普通员工"
    assert "spec" not in data or data.get("spec") is None


def test_create_agent_uses_shared_provisioning_entry(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    calls: list[dict] = []

    def provision_stub(_conn, **kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(
        "app.api.routes.workspace.provision_new_agent", provision_stub
    )
    response = client.post(
        "/api/agents",
        headers=auth_header(auth["access_token"]),
        json={
            "name": "统一入口员工",
            "description": "测试",
            "department_name": "技术部",
            "prompt": "你是测试员工",
        },
    )
    assert response.status_code == 200
    assert calls and calls[0]["role_name"] == "测试"
    assert calls[0]["capability_keys"] == []


def test_create_agent_with_role_spec_auto_capabilities(tmp_path, monkeypatch):
    """POST /api/agents with role_spec + auto capabilities → spec status = ready."""
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    response = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "前端工程师",
            "description": "写代码的",
            "department_name": "技术部",
            "prompt": "你是一个前端工程师",
            "role_spec": {
                "role_name": "前端工程师",
                "source_request": "需要一个能写代码、运行测试的员工",
                "responsibilities": ["编写前端代码", "运行测试"],
                "capability_keys": ["write_code", "run_tests"],
            },
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "前端工程师"
    assert "spec" in data
    spec = data["spec"]
    assert spec["role_name"] == "前端工程师"
    assert spec["status"] == "ready"
    assert len(spec["capabilities"]) == 2
    cap_keys = {c["capability_key"] for c in spec["capabilities"]}
    assert cap_keys == {"write_code", "run_tests"}
    # Auto capabilities should be enabled immediately (no credentials needed)
    for cap in spec["capabilities"]:
        assert cap["status"] == "enabled"


def test_create_agent_with_credential_capabilities(tmp_path, monkeypatch):
    """POST /api/agents with capability needing credentials → blocked_on_credentials."""
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    response = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "全栈工程师",
            "description": "写代码+推送",
            "department_name": "技术部",
            "prompt": "你是一个全栈工程师",
            "role_spec": {
                "role_name": "全栈工程师",
                "source_request": "需要一个能写代码和推送代码的员工",
                "capability_keys": ["write_code", "git_push"],
            },
        },
    )
    assert response.status_code == 200
    spec = response.json()["spec"]
    assert spec["status"] == "blocked_on_credentials"

    # write_code should be enabled, git_push should need credentials
    for cap in spec["capabilities"]:
        if cap["capability_key"] == "write_code":
            assert cap["status"] == "enabled"
        elif cap["capability_key"] == "git_push":
            assert cap["status"] == "credential_missing"
            assert "GITHUB_TOKEN" in cap["required_credentials"]


def test_get_agent_spec(tmp_path, monkeypatch):
    """GET /api/agents/{id}/spec returns spec with capabilities."""
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    # Create agent with spec
    create_resp = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "测试员工",
            "description": "测试",
            "department_name": "技术部",
            "prompt": "你是测试员工",
            "role_spec": {
                "role_name": "测试员工",
                "capability_keys": ["run_tests"],
            },
        },
    )
    assert create_resp.status_code == 200
    agent_id = create_resp.json()["id"]

    # Get spec
    spec_resp = client.get(
        f"/api/agents/{agent_id}/spec",
        headers=auth_header(token),
    )
    assert spec_resp.status_code == 200
    spec = spec_resp.json()
    assert spec["role_name"] == "测试员工"
    assert spec["status"] == "ready"
    assert len(spec["capabilities"]) == 1
    assert spec["capabilities"][0]["capability_key"] == "run_tests"


def test_get_agent_spec_not_found(tmp_path, monkeypatch):
    """GET /api/agents/{id}/spec returns 404 if no spec."""
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    # Create agent without spec
    create_resp = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "无规格员工",
            "description": "测试",
            "department_name": "技术部",
            "prompt": "你是测试员工",
        },
    )
    agent_id = create_resp.json()["id"]

    # Get spec → 404
    spec_resp = client.get(
        f"/api/agents/{agent_id}/spec",
        headers=auth_header(token),
    )
    assert spec_resp.status_code == 404


def test_provide_credential_enables_capability(tmp_path, monkeypatch):
    """POST /api/agents/{id}/credentials enables credential_missing capability."""
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    # Create agent with git_push (needs GITHUB_TOKEN)
    create_resp = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "工程师",
            "description": "测试",
            "department_name": "技术部",
            "prompt": "你是工程师",
            "role_spec": {
                "role_name": "工程师",
                "capability_keys": ["write_code", "git_push"],
            },
        },
    )
    agent_id = create_resp.json()["id"]
    spec = create_resp.json()["spec"]
    assert spec["status"] == "blocked_on_credentials"

    # Provide GITHUB_TOKEN
    cred_resp = client.post(
        f"/api/agents/{agent_id}/credentials",
        headers=auth_header(token),
        json={
            "credential_name": "GITHUB_TOKEN",
            "value": "ghp_test_token_12345",
        },
    )
    assert cred_resp.status_code == 200
    updated_spec = cred_resp.json()

    # git_push should now be enabled
    git_push_cap = next(
        c for c in updated_spec["capabilities"] if c["capability_key"] == "git_push"
    )
    assert git_push_cap["status"] == "enabled"

    # Spec should now be ready (all capabilities enabled)
    assert updated_spec["status"] == "ready"


def test_provide_wrong_credential(tmp_path, monkeypatch):
    """POST /api/agents/{id}/credentials with wrong credential name → 400."""
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    create_resp = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "工程师",
            "description": "测试",
            "department_name": "技术部",
            "prompt": "你是工程师",
            "role_spec": {
                "role_name": "工程师",
                "capability_keys": ["write_code"],
            },
        },
    )
    agent_id = create_resp.json()["id"]

    # Try to provide credential that no capability needs
    cred_resp = client.post(
        f"/api/agents/{agent_id}/credentials",
        headers=auth_header(token),
        json={
            "credential_name": "NONEXISTENT_TOKEN",
            "value": "test",
        },
    )
    assert cred_resp.status_code == 400


def test_credential_value_not_in_response(tmp_path, monkeypatch):
    """Credential value must never appear in API responses."""
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    create_resp = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "工程师",
            "description": "测试",
            "department_name": "技术部",
            "prompt": "你是工程师",
            "role_spec": {
                "role_name": "工程师",
                "capability_keys": ["git_push"],
            },
        },
    )
    agent_id = create_resp.json()["id"]
    secret_value = "ghp_super_secret_token_xyz"

    cred_resp = client.post(
        f"/api/agents/{agent_id}/credentials",
        headers=auth_header(token),
        json={
            "credential_name": "GITHUB_TOKEN",
            "value": secret_value,
        },
    )
    assert cred_resp.status_code == 200
    # Verify the secret value does NOT appear in the response
    resp_text = cred_resp.text
    assert secret_value not in resp_text


def test_provision_idempotent(tmp_path, monkeypatch):
    """POST /api/agents/{id}/provision is idempotent — re-provision doesn't change ready state."""
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    create_resp = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "工程师",
            "description": "测试",
            "department_name": "技术部",
            "prompt": "你是工程师",
            "role_spec": {
                "role_name": "工程师",
                "capability_keys": ["write_code"],
            },
        },
    )
    agent_id = create_resp.json()["id"]
    assert create_resp.json()["spec"]["status"] == "ready"

    # Re-provision
    prov_resp = client.post(
        f"/api/agents/{agent_id}/provision",
        headers=auth_header(token),
    )
    assert prov_resp.status_code == 200
    assert prov_resp.json()["status"] == "ready"


def test_provision_nonexistent_agent(tmp_path, monkeypatch):
    """POST /api/agents/{id}/provision with nonexistent agent → 404."""
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    prov_resp = client.post(
        "/api/agents/agent_nonexistent/provision",
        headers=auth_header(token),
    )
    assert prov_resp.status_code == 404


def test_prohibited_auto_capability_always_blocked(tmp_path, monkeypatch):
    """domain_register (prohibited_auto) always stays credential_missing even after provision."""
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    response = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "运维",
            "description": "测试",
            "department_name": "技术部",
            "prompt": "你是运维",
            "role_spec": {
                "role_name": "运维工程师",
                "capability_keys": ["write_code", "domain_register"],
            },
        },
    )
    assert response.status_code == 200
    spec = response.json()["spec"]

    # domain_register should be credential_missing
    domain_cap = next(
        c for c in spec["capabilities"] if c["capability_key"] == "domain_register"
    )
    assert domain_cap["status"] == "credential_missing"
    assert domain_cap["risk_gate"] == "prohibited_auto"


def test_full_flow_create_provision_credential_ready(tmp_path, monkeypatch):
    """End-to-end: create → blocked → provide credential → ready."""
    client = make_client(tmp_path, monkeypatch)
    auth = register_user(client)
    token = auth["access_token"]

    # 1. Create agent with deploy_preview (needs PLATFORM_TOKEN)
    create_resp = client.post(
        "/api/agents",
        headers=auth_header(token),
        json={
            "name": "前端部署专员",
            "description": "部署预览环境",
            "department_name": "技术部",
            "prompt": "你负责部署预览",
            "role_spec": {
                "role_name": "前端部署专员",
                "source_request": "能写代码和部署预览的员工",
                "responsibilities": ["编写代码", "部署到预览环境"],
                "capability_keys": ["write_code", "deploy_preview"],
            },
        },
    )
    assert create_resp.status_code == 200
    agent_id = create_resp.json()["id"]
    spec = create_resp.json()["spec"]
    assert spec["status"] == "blocked_on_credentials"

    # 2. Check spec via GET
    spec_resp = client.get(
        f"/api/agents/{agent_id}/spec",
        headers=auth_header(token),
    )
    assert spec_resp.status_code == 200
    assert spec_resp.json()["status"] == "blocked_on_credentials"

    # 3. Provide credential
    cred_resp = client.post(
        f"/api/agents/{agent_id}/credentials",
        headers=auth_header(token),
        json={
            "credential_name": "PLATFORM_TOKEN",
            "value": "plat_test_token",
        },
    )
    assert cred_resp.status_code == 200
    assert cred_resp.json()["status"] == "ready"

    # 4. Re-provision (idempotent)
    prov_resp = client.post(
        f"/api/agents/{agent_id}/provision",
        headers=auth_header(token),
    )
    assert prov_resp.status_code == 200
    assert prov_resp.json()["status"] == "ready"
