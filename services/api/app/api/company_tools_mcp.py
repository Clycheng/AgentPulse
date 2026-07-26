"""Streamable HTTP MCP surface for per-run AgentPulse company tools."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.core.config import settings
from app.core.database import connect
from app.runtime.company_tools_auth import decode_company_tool_token
from app.services import company_tools
from app.tools.registry import ToolCall, execute_tool


class CompanyTokenVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            payload = decode_company_tool_token(token)
        except ValueError:
            return None
        return AccessToken(
            token=token,
            client_id=payload["agent_id"],
            scopes=["company-tools"],
            expires_at=int(payload["exp"]),
        )


company_mcp = FastMCP(
    "AgentPulse Company Tools",
    instructions="Use these tools to read company context and report durable task state.",
    token_verifier=CompanyTokenVerifier(),
    auth=AuthSettings(
        issuer_url="http://agentpulse.local",
        resource_server_url="http://agentpulse.local/mcp/company-tools",
        required_scopes=["company-tools"],
    ),
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=settings.mcp_allowed_hosts,
        allowed_origins=settings.mcp_allowed_origins,
    ),
)


def _claims() -> dict:
    access_token = get_access_token()
    if access_token is None:
        raise company_tools.CompanyToolError("missing company tool token")
    return decode_company_tool_token(access_token.token)


def _call(operation, **kwargs):
    conn = connect()
    try:
        result = operation(conn, _claims(), **kwargs)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def _run_chat_management_tool(name: str, arguments: dict) -> dict:
    """Execute a chat-management tool through the receipt-bearing registry.

    This keeps employee creation and group mutations on the same validated
    handlers used elsewhere, while requiring a live Hermes chat Run rather
    than allowing an HTTP route to infer an action from model text.
    """
    claims = _claims()
    if claims.get("run_kind") != "chat":
        raise company_tools.CompanyToolError(
            "该公司管理工具只能由当前聊天 Hermes Run 调用"
        )
    conn = connect()
    try:
        company_tools.authorize_run(conn, claims)
        agent = conn.execute(
            "SELECT * FROM agents WHERE id = ? AND workspace_id = ?",
            (claims["agent_id"], claims["workspace_id"]),
        ).fetchone()
        if agent is None:
            raise company_tools.CompanyToolError("当前员工不存在或不属于此公司")
        result = await execute_tool(
            conn,
            claims["workspace_id"],
            agent,
            ToolCall(id=f"mcp_{name}", name=name, arguments=arguments),
            run_id=claims["run_id"],
        )
        conn.commit()
        try:
            return json.loads(result.content)
        except (TypeError, json.JSONDecodeError):
            return {
                "ok": result.ok,
                "tool": name,
                "result": {"text": result.content},
                "execution_receipt_id": result.execution_receipt_id,
            }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@company_mcp.tool(description="Search the current workspace knowledge base first.")
async def search_company_knowledge(query: str, limit: int = 5) -> list[dict]:
    return _call(company_tools.search_company_knowledge, query=query, limit=limit)


@company_mcp.tool(description="List human-readable colleagues available for internal collaboration.")
async def list_colleagues() -> list[dict]:
    return _call(company_tools.list_colleagues)


@company_mcp.tool(description="Inspect your current work, request inbox, queue, blockers and resources.")
async def get_my_workbench() -> dict:
    return _call(company_tools.get_my_workbench)


@company_mcp.tool(
    description=(
        "Decide the one work request assigned to this triage run. Quick questions "
        "use answered; sustained work uses accepted/deferred with Fibonacci story points."
    )
)
async def decide_work_request(
    request_id: str,
    decision: str,
    response_content: str,
    decision_reason: str = "",
    title: str | None = None,
    expected_output: str = "",
    output_type: str = "markdown",
    story_points: int | None = None,
    business_value: int = 3,
    urgency: int = 2,
    unblock_score: int = 0,
    risk_reduction: int = 0,
    switching_cost: float = 0,
    risk_level: str = "low",
    review_required: bool = False,
) -> dict:
    return _call(
        company_tools.decide_my_work_request,
        request_id=request_id,
        decision=decision,
        response_content=response_content,
        decision_reason=decision_reason,
        title=title,
        expected_output=expected_output,
        output_type=output_type,
        story_points=story_points,
        business_value=business_value,
        urgency=urgency,
        unblock_score=unblock_score,
        risk_reduction=risk_reduction,
        switching_cost=switching_cost,
        risk_level=risk_level,
        review_required=review_required,
    )


@company_mcp.tool(
    description=(
        "Resolve the coordination case assigned to this independent Hermes run. "
        "Use confirmed goals and evidence; use needs_goal only when the goal itself is unclear."
    )
)
async def decide_coordination_case(
    case_id: str,
    decision: str,
    reason: str,
) -> dict:
    return _call(
        company_tools.decide_my_coordination_case,
        case_id=case_id,
        decision=decision,
        reason=reason,
    )


@company_mcp.tool(
    description=(
        "Create one real company employee. Use only after the company needs a "
        "new colleague; the result includes an execution receipt."
    )
)
async def create_employee(
    name: str,
    role: str,
    description: str,
    department: str = "新员工部",
    skills: list[str] | None = None,
    responsibilities: list[str] | None = None,
    capability_keys: list[str] | None = None,
) -> dict:
    return await _run_chat_management_tool(
        "create_employee",
        {
            "name": name,
            "role": role,
            "description": description,
            "department": department,
            "skills": skills or [],
            "responsibilities": responsibilities or [],
            "capability_keys": capability_keys or [],
        },
    )


@company_mcp.tool(
    description="List the company capability catalog before staffing a role that needs a concrete ability."
)
async def list_capabilities() -> dict:
    return await _run_chat_management_tool("list_capabilities", {})


@company_mcp.tool(
    description="Create a collaboration group using colleague names, not internal identifiers."
)
async def create_group(name: str, member_names: list[str]) -> dict:
    claims = _claims()
    if not member_names:
        raise company_tools.CompanyToolError("至少需要一位群成员")
    conn = connect()
    try:
        company_tools.authorize_run(conn, claims)
        members = [
            company_tools._resolve_colleague_name(conn, claims["workspace_id"], member_name)
            for member_name in member_names
        ]
    finally:
        conn.close()
    return await _run_chat_management_tool(
        "create_group",
        {"name": name, "member_agent_ids": [member["id"] for member in members]},
    )


@company_mcp.tool(
    description="Send a factual internal message to an existing collaboration group by its name."
)
async def send_group_message(group_name: str, content: str) -> dict:
    claims = _claims()
    conn = connect()
    try:
        company_tools.authorize_run(conn, claims)
        rows = conn.execute(
            """SELECT id FROM conversations
            WHERE workspace_id = ? AND kind = 'group' AND name = ?""",
            (claims["workspace_id"], group_name.strip()),
        ).fetchall()
        if len(rows) != 1:
            raise company_tools.CompanyToolError("请使用唯一的现有群名")
        conversation_id = rows[0]["id"]
    finally:
        conn.close()
    return await _run_chat_management_tool(
        "send_group_message",
        {"conversation_id": conversation_id, "content": content},
    )


@company_mcp.tool(description="Search shared company events and the current employee's evidence-backed memories.")
async def search_company_memory(query: str, limit: int = 8) -> list[dict]:
    return _call(company_tools.search_company_memory_for_run, query=query, limit=limit)


@company_mcp.tool(description="Send one durable internal message to a colleague; repeated no-fact pings are blocked.")
async def ping_colleague(to_colleague_name: str, content: str) -> dict:
    return _call(
        company_tools.ping_colleague_by_name,
        to_colleague_name=to_colleague_name,
        content=content,
    )


@company_mcp.tool(description="Send an internal message that is recorded as a company conversation.")
async def send_internal_message(to_colleague_name: str, content: str) -> dict:
    return _call(
        company_tools.ping_colleague_by_name,
        to_colleague_name=to_colleague_name,
        content=content,
    )


@company_mcp.tool(description="Propose a bounded internal subtask within the confirmed plan.")
async def propose_internal_task(
    title: str, description: str, owner_colleague_name: str, expected_output: str
) -> dict:
    return _call(
        company_tools.propose_internal_task_by_name,
        title=title,
        description=description,
        owner_colleague_name=owner_colleague_name,
        expected_output=expected_output,
    )


@company_mcp.tool(description="Record an observation with evidence from this run.")
async def record_observation(title: str, content: str, promoted: bool = False) -> dict:
    return _call(
        company_tools.record_observation,
        title=title,
        content=content,
        promoted=promoted,
    )


@company_mcp.tool(description="Record a factual collaboration lesson about a colleague.")
async def report_relationship_fact(colleague_name: str, fact: str) -> dict:
    return _call(
        company_tools.report_relationship_fact_by_name,
        colleague_name=colleague_name,
        fact=fact,
    )


@company_mcp.tool(description="Persist task progress and a short factual summary.")
async def report_progress(progress: int, summary: str) -> dict:
    return _call(company_tools.report_progress, progress=progress, summary=summary)


@company_mcp.tool(description="Submit the task's contracted Markdown or content_package_v1 output.")
async def submit_output(
    title: str, output_type: str, content: str | dict
) -> dict:
    return _call(
        company_tools.submit_output,
        title=title,
        output_type=output_type,
        content=content,
    )


@company_mcp.tool(description="Add a subtask within the confirmed brief scope.")
async def create_subtask(
    title: str,
    description: str,
    owner_colleague_name: str,
    expected_output: str,
    output_type: str = "markdown",
    depends_on_task_ids: list[str] | None = None,
) -> dict:
    return _call(
        company_tools.create_subtask_by_name,
        title=title,
        description=description,
        owner_colleague_name=owner_colleague_name,
        expected_output=expected_output,
        output_type=output_type,
        depends_on_task_ids=depends_on_task_ids,
    )


@company_mcp.tool(description="Request a brief participant to produce supporting work.")
async def request_support(
    colleague_name: str, request: str, expected_output: str
) -> dict:
    return _call(
        company_tools.request_support_by_name,
        colleague_name=colleague_name,
        request=request,
        expected_output=expected_output,
    )


@company_mcp.tool(description="Block the task with the exact missing information or failure reason.")
async def block_task(reason: str) -> dict:
    return _call(company_tools.block_task, reason=reason)


company_tools_app = company_mcp.streamable_http_app()


@asynccontextmanager
async def company_tools_lifespan():
    async with company_mcp.session_manager.run():
        yield
