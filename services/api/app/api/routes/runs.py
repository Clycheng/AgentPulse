from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/runs", tags=["runs"])


@router.post("/llm-chat", include_in_schema=False)
async def retired_direct_llm_chat() -> None:
    """Reject the temporary direct-provider endpoint.

    All employee and control work is now a persisted Hermes ACP Run. Keeping a
    visible 410 helps old desktop builds fail honestly instead of silently
    bypassing profile, tool, approval, and receipt enforcement.
    """
    raise HTTPException(
        status_code=410,
        detail="直接模型聊天已停用；请通过 Hermes Run 执行员工工作。",
    )
