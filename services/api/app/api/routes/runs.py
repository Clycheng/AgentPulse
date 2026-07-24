from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_workspace_id
from app.core.database import Database, get_db
from app.runtime.deepseek import (
    DeepSeekChatClient,
    DeepSeekAPIError,
    DeepSeekNotConfigured,
)
from app.schemas.run import LlmChatRequest, LlmChatResponse
from app.services.model_credentials import deepseek_client_for_workspace

router = APIRouter(prefix="/runs", tags=["runs"])


@router.post("/llm-chat", response_model=LlmChatResponse)
async def run_llm_chat(
    payload: LlmChatRequest,
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> LlmChatResponse:
    try:
        return await deepseek_client_for_workspace(conn, workspace_id).complete(payload)
    except DeepSeekNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except DeepSeekAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
