from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Header, HTTPException
from starlette.responses import StreamingResponse

from app.api.deps import get_current_user, get_workspace_id
from app.core.database import Database, Row, get_db
from app.runtime.company_tools_auth import decode_company_tool_token
from app.schemas.workforce import (
    CoordinationCaseCreate,
    CoordinationDecisionIn,
    ReviewDecisionIn,
    RunPauseIn,
    RunResumeIn,
    TaskDependencyCreate,
    TaskReviewCreate,
    WorkRequestCreate,
    WorkRequestDecisionIn,
    WorkbenchOut,
)
from app.services.company_tools import CompanyToolError, authorize_run
from app.services.workforce import (
    WorkforceError,
    add_task_dependency,
    create_work_request,
    decide_coordination_case,
    decide_task_review,
    decide_work_request,
    get_agent_workbench,
    get_workforce_overview,
    open_coordination_case,
    request_run_pause,
    request_task_review,
    resume_paused_run,
)
from app.services.workforce_events import stream_workforce_events


router = APIRouter(tags=["workforce"])


def _company_claims(authorization: str | None, conn: Database) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Hermes Run Token")
    try:
        claims = decode_company_tool_token(
            authorization.removeprefix("Bearer ").strip()
        )
        authorize_run(conn, claims)
    except (ValueError, CompanyToolError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return claims


@router.get("/workforce/overview")
def get_workforce_overview_route(
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> dict:
    return get_workforce_overview(conn, workspace_id=workspace_id)


@router.get("/workforce/events")
def stream_workforce_events_route(
    after: str | None = None,
    workspace_id: str = Depends(get_workspace_id),
) -> StreamingResponse:
    async def body():
        async for event in stream_workforce_events(
            workspace_id=workspace_id, after_event_id=after
        ):
            if event is None:
                yield ": keep-alive\n\n"
                continue
            yield (
                f"id: {event['id']}\n"
                f"event: {event['event_type']}\n"
                f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            )

    return StreamingResponse(body(), media_type="text/event-stream")


@router.post("/work-requests")
def create_work_request_route(
    payload: WorkRequestCreate,
    current_user: Row = Depends(get_current_user),
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> dict:
    try:
        return create_work_request(
            conn,
            workspace_id=workspace_id,
            requester_type="user",
            requester_id=current_user["id"],
            **payload.model_dump(),
        )
    except WorkforceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/agents/{agent_id}/workbench", response_model=WorkbenchOut)
def get_agent_workbench_route(
    agent_id: str,
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> WorkbenchOut:
    try:
        return WorkbenchOut(
            **get_agent_workbench(conn, workspace_id=workspace_id, agent_id=agent_id)
        )
    except WorkforceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/work-requests/{request_id}/decision")
def decide_work_request_route(
    request_id: str,
    payload: WorkRequestDecisionIn,
    authorization: str | None = Header(default=None),
    conn: Database = Depends(get_db),
) -> dict:
    claims = _company_claims(authorization, conn)
    if claims["run_kind"] != "triage":
        raise HTTPException(status_code=403, detail="仅工作请求评估 Run 可以决策")
    try:
        return decide_work_request(
            conn,
            workspace_id=claims["workspace_id"],
            request_id=request_id,
            target_agent_id=claims["agent_id"],
            payload=payload.model_dump(),
        )
    except WorkforceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/work-requests/{request_id}/disputes")
def open_coordination_case_route(
    request_id: str,
    payload: CoordinationCaseCreate,
    current_user: Row = Depends(get_current_user),
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> dict:
    try:
        return open_coordination_case(
            conn,
            workspace_id=workspace_id,
            work_request_id=request_id,
            raised_by_type="user",
            raised_by_id=current_user["id"],
            reason=payload.reason,
            evidence_ids=payload.evidence_ids,
        )
    except WorkforceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/coordination-cases/{case_id}/decision")
def decide_coordination_case_route(
    case_id: str,
    payload: CoordinationDecisionIn,
    authorization: str | None = Header(default=None),
    conn: Database = Depends(get_db),
) -> dict:
    claims = _company_claims(authorization, conn)
    if claims["run_kind"] != "coordination":
        raise HTTPException(status_code=403, detail="仅独立协调 Hermes Run 可以裁决")
    try:
        return decide_coordination_case(
            conn,
            workspace_id=claims["workspace_id"],
            case_id=case_id,
            coordinator_agent_id=claims["agent_id"],
            run_id=claims["run_id"],
            **payload.model_dump(),
        )
    except WorkforceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/dependencies")
def add_task_dependency_route(
    task_id: str,
    payload: TaskDependencyCreate,
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> dict:
    try:
        return add_task_dependency(
            conn, workspace_id=workspace_id, task_id=task_id, **payload.model_dump()
        )
    except WorkforceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/reviews")
def request_task_review_route(
    task_id: str,
    payload: TaskReviewCreate,
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> dict:
    try:
        return request_task_review(
            conn, workspace_id=workspace_id, task_id=task_id, **payload.model_dump()
        )
    except WorkforceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/reviews/{review_id}/decision")
def decide_task_review_route(
    review_id: str,
    payload: ReviewDecisionIn,
    authorization: str | None = Header(default=None),
    conn: Database = Depends(get_db),
) -> dict:
    claims = _company_claims(authorization, conn)
    if claims["run_kind"] not in {"review", "task"}:
        raise HTTPException(status_code=403, detail="仅评审任务 Run 可以提交结论")
    try:
        return decide_task_review(
            conn,
            workspace_id=claims["workspace_id"],
            review_id=review_id,
            reviewer_agent_id=claims["agent_id"],
            **payload.model_dump(),
        )
    except WorkforceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/runs/{run_id}/pause")
def pause_run_route(
    run_id: str,
    payload: RunPauseIn,
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> dict:
    try:
        return request_run_pause(
            conn, workspace_id=workspace_id, run_id=run_id, reason=payload.reason
        )
    except WorkforceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/runs/{run_id}/resume")
def resume_run_route(
    run_id: str,
    payload: RunResumeIn,
    workspace_id: str = Depends(get_workspace_id),
    conn: Database = Depends(get_db),
) -> dict:
    try:
        return resume_paused_run(
            conn, workspace_id=workspace_id, run_id=run_id, reason=payload.reason
        )
    except WorkforceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
