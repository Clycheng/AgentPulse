"""Deterministic employee SOUL.md construction shared by every runtime."""

from __future__ import annotations


def build_employee_soul(
    *,
    name: str,
    role: str,
    prompt: str | None,
    responsibilities: list[str] | None,
) -> str:
    """Build one runtime-neutral employee persona.

    The server profile and the desktop-owned profile must receive the same
    behavioural boundary.  Keeping it pure also makes the persona reviewable
    without depending on a particular provisioning implementation.
    """
    responsibility_lines = "\n".join(
        f"- {item.strip()}"
        for item in responsibilities or []
        if isinstance(item, str) and item.strip()
    ) or "- 根据岗位专业判断推进公司目标"
    work_prompt = (prompt or "").strip() or "基于公司目标和专业判断推进工作。"
    return (
        f"# {name} · {role}\n\n"
        f"你是公司中的{name}，岗位是{role}。\n\n"
        "## 职责\n"
        f"{responsibility_lines}\n\n"
        "## 工作方式\n"
        f"{work_prompt}\n\n"
        "## 铁律\n"
        "- 先基于公司目标、证据和当前任务判断，再推进工作。\n"
        "- 需求不清楚或关键信息缺失时，直接在对话中说明缺少什么并等待回复，绝不臆测执行。\n"
        "- 高风险动作（写文件、运行命令、对外发布、部署上线、花钱或不可逆操作）必须尊重审批结果。\n"
        "- 因缺少工具、MCP 连接或权限而无法完成时，如实说明阻塞原因；不要猜测绕路或自行申请能力升级。\n"
        "- 工具未返回真实结果时，只能报告尚未执行或失败，不能把计划说成完成。\n\n"
        "## 自我进步\n"
        "- 每完成一项任务，记录可复用的经验、有效工具顺序或公司偏好，供后续工作参考。\n"
    )
