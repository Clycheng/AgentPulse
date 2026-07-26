"""Database-backed task dispatcher for TD-11."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Callable

from app.core.config import settings
from app.core.database import Database, connect
from app.core.logging import get_logger
from app.orchestration.workforce import (
    claim_server_runs,
    dependency_blockers,
    refresh_plan_state,
    schedule_coordination_work,
    schedule_ready_tasks,
    schedule_work_request_triage,
    sync_agent_work_state,
)
from app.runtime.company_tools_auth import create_company_tool_token
from app.runtime.hermes_client import HermesBackend, RunContext
from app.runtime.runner import resolve_hermes_profile, start_run
from app.runtime.runs import RunStatus, create_run
from app.schemas.content_package import ContentPackageV1
from app.services.content_packages import parse_content_package
from app.services.task_plans import enqueue_task_run
from app.services.workspace import add_task_event, add_task_output, now_iso
from app.services.workforce import (
    acquire_task_resources,
    decide_coordination_case,
    release_run_resources,
    resume_preempted_task_after,
)


logger = get_logger(__name__)


def _iso_after(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


class TaskScheduler:
    def __init__(self, *, backend_factory: Callable[[], object] | None = None) -> None:
        self.worker_id = f"worker_{secrets.token_hex(6)}"
        self.backend_factory = backend_factory or (
            lambda: HermesBackend(hermes_bin=settings.hermes_bin)
        )
        self._active: dict[str, asyncio.Task] = {}

    async def close(self) -> None:
        tasks = list(self._active.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()

    async def tick(self) -> None:
        self._collect_finished()
        conn = connect()
        try:
            self._enqueue_coordination_runs(conn)
            self._enqueue_work_request_triage(conn)
            self._enqueue_ready_tasks(conn)
            claimed = self._claim_runs(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        for run_id in claimed:
            self._active[run_id] = asyncio.create_task(self._execute_run(run_id))

    def _collect_finished(self) -> None:
        for run_id, task in list(self._active.items()):
            if task.done():
                try:
                    task.result()
                except (Exception, asyncio.CancelledError):
                    pass
                self._active.pop(run_id, None)

    def _enqueue_work_request_triage(self, conn: Database) -> None:
        schedule_work_request_triage(
            conn,
            resolve_profile=resolve_hermes_profile,
            create_special_run=self._create_special_run,
            timestamp=now_iso(),
        )

    def _enqueue_coordination_runs(self, conn: Database) -> None:
        schedule_coordination_work(
            conn,
            resolve_profile=resolve_hermes_profile,
            create_special_run=self._create_special_run,
            timestamp=now_iso(),
        )

    def _create_special_run(
        self, conn: Database, item: dict, profile: str, run_kind: str
    ) -> str:
        agent_id = (
            item["target_agent_id"]
            if run_kind == "triage"
            else item["coordinator_agent_id"]
        )
        run_id = create_run(
            conn,
            workspace_id=item["workspace_id"],
            conversation_id=item["conversation_id"],
            agent_id=agent_id,
            task_id=None,
            input_message_id=item.get("source_message_id"),
            hermes_profile_id=profile,
            workdir="",
            status=RunStatus.QUEUED,
            run_kind=run_kind,
        )
        work_root = os.path.abspath(settings.hermes_work_root or ".hermes-data")
        workdir = os.path.join(work_root, profile, "work", run_kind, run_id)
        conn.execute("UPDATE runs SET workdir = ? WHERE id = ?", (workdir, run_id))
        return run_id

    async def _execute_triage_run(self, conn: Database, run: dict) -> None:
        request = conn.execute(
            "SELECT * FROM work_requests WHERE triage_run_id = ?", (run["id"],)
        ).fetchone()
        if request is None:
            raise ValueError("triage run has no work request")
        queue = conn.execute(
            """SELECT title, workflow_status, story_points, priority_score,
            waiting_reason FROM tasks WHERE owner_agent_id = ?
            AND workflow_status NOT IN ('completed','cancelled')
            ORDER BY priority_score DESC, created_at LIMIT 20""",
            (run["agent_id"],),
        ).fetchall()
        token = create_company_tool_token(
            workspace_id=run["workspace_id"],
            run_id=run["id"],
            agent_id=run["agent_id"],
            conversation_id=run["conversation_id"],
            run_kind="triage",
        )
        prompt = f"""你正在处理一条已经送达的内部工作请求。

【请求 ID】{request['id']}
【请求内容】{request['content']}
【当前个人队列】{json.dumps([dict(row) for row in queue], ensure_ascii=False)}

先判断这是可立即回答的问题，还是需要持续工作和交付物。
- 快速确认：调用 decide_work_request，decision=answered。
- 接受为正式任务：decision=accepted，并使用 1/2/3/5/8/13 Story Point。
- 暂后处理：decision=deferred；无法执行：rejected；信息不足：needs_info。
- 排序只看业务价值、紧急度、解锁同事、风险降低、等待时长和工作量，
  不因请求人的身份加权。超过 13 SP 必须要求拆分。
必须调用 decide_work_request 留下结构化决定，不能只输出自然语言。
"""
        ctx = RunContext(
            run_id=run["id"],
            prompt=prompt,
            workdir=run["workdir"],
            profile=run["hermes_profile_id"],
            agent_id=run["agent_id"],
            workspace_id=run["workspace_id"],
            conversation_id=run["conversation_id"],
            resume_session_id=run.get("hermes_session_id"),
            mcp_servers=[
                {
                    "name": "agentpulse-company",
                    "url": settings.company_tools_url,
                    "headers": {"Authorization": f"Bearer {token}"},
                }
            ],
        )
        result = await start_run(
            conn,
            ctx=ctx,
            backend=self.backend_factory(),
            input_message_id=run.get("input_message_id"),
            persist_message=False,
            existing_run_id=run["id"],
        )
        latest_request = conn.execute(
            "SELECT * FROM work_requests WHERE id = ?", (request["id"],)
        ).fetchone()
        if latest_request and latest_request["status"] == "evaluating":
            response = result.get("text") or "本次评估未形成结构化决定，需要补充信息后重试。"
            conn.execute(
                """UPDATE work_requests SET status = 'needs_info', response_content = ?,
                decision_reason = 'Hermes 未调用结构化决策工具', decided_at = ?,
                updated_at = ? WHERE id = ?""",
                (response[:4000], now_iso(), now_iso(), request["id"]),
            )
            if result.get("text"):
                from app.services.workspace import add_message

                add_message(
                    conn,
                    conversation_id=run["conversation_id"],
                    sender_type="agent",
                    sender_id=run["agent_id"],
                    content=result["text"],
                    provider="hermes",
                    model="",
                )
        conn.execute(
            "UPDATE runs SET lease_owner = NULL, lease_expires_at = NULL WHERE id = ?",
            (run["id"],),
        )
        sync_agent_work_state(conn, run["agent_id"], now_iso=now_iso())

    async def _execute_coordination_run(self, conn: Database, run: dict) -> None:
        case = conn.execute(
            """SELECT c.*, w.content AS request_content, w.status AS request_status,
            w.target_agent_id, w.requester_type, w.requester_id, w.consensus_brief_id
            FROM coordination_cases c JOIN work_requests w ON w.id = c.work_request_id
            WHERE c.run_id = ?""",
            (run["id"],),
        ).fetchone()
        if case is None:
            raise ValueError("coordination run has no case")
        brief = None
        if case.get("consensus_brief_id"):
            brief = conn.execute(
                "SELECT * FROM consensus_briefs WHERE id = ?",
                (case["consensus_brief_id"],),
            ).fetchone()
        assessments = conn.execute(
            """SELECT * FROM priority_assessments WHERE work_request_id = ?
            ORDER BY created_at, id""",
            (case["work_request_id"],),
        ).fetchall()
        token = create_company_tool_token(
            workspace_id=run["workspace_id"],
            run_id=run["id"],
            agent_id=run["agent_id"],
            conversation_id=run["conversation_id"],
            run_kind="coordination",
        )
        prompt = f"""你是这次争议的独立协调员工，不代表请求方、执行方或老板。

【协调 case】{case['id']}
【争议原因】{case['reason']}
【原请求】{case['request_content']}
【原请求状态】{case['request_status']}
【已确认目标】{json.dumps(dict(brief) if brief else None, ensure_ascii=False)}
【排序证据】{json.dumps([dict(row) for row in assessments], ensure_ascii=False)}
【补充证据 ID】{case['evidence_json']}

只依据已确认目标、可审计证据和统一排序规则裁决，不因任何人的身份加权。
- 请求应重新评估：uphold；合理延后：defer；不应执行：reject。
- 只有目标本身不清楚时才用 needs_goal，请老板补充目标。
必须调用 decide_coordination_case，不能只输出自然语言。
"""
        ctx = RunContext(
            run_id=run["id"],
            prompt=prompt,
            workdir=run["workdir"],
            profile=run["hermes_profile_id"],
            agent_id=run["agent_id"],
            workspace_id=run["workspace_id"],
            conversation_id=run["conversation_id"],
            resume_session_id=run.get("hermes_session_id"),
            mcp_servers=[
                {
                    "name": "agentpulse-company",
                    "url": settings.company_tools_url,
                    "headers": {"Authorization": f"Bearer {token}"},
                }
            ],
        )
        result = await start_run(
            conn,
            ctx=ctx,
            backend=self.backend_factory(),
            input_message_id=None,
            persist_message=False,
            existing_run_id=run["id"],
        )
        latest = conn.execute(
            "SELECT status FROM coordination_cases WHERE id = ?", (case["id"],)
        ).fetchone()
        if latest and latest["status"] == "evaluating":
            decide_coordination_case(
                conn,
                workspace_id=run["workspace_id"],
                case_id=case["id"],
                coordinator_agent_id=run["agent_id"],
                run_id=run["id"],
                decision="needs_goal",
                reason=result.get("text")
                or "Hermes 未调用结构化协调工具，需要补充目标后重试。",
            )
        conn.execute(
            "UPDATE runs SET lease_owner = NULL, lease_expires_at = NULL WHERE id = ?",
            (run["id"],),
        )
        sync_agent_work_state(conn, run["agent_id"], now_iso=now_iso())

    def _claim_runs(self, conn: Database) -> list[str]:
        return claim_server_runs(
            conn,
            worker_id=self.worker_id,
            configured_slots=settings.task_server_slots,
            lease_seconds=settings.task_run_lease_seconds,
            timestamp=now_iso(),
            lease_expires_at=_iso_after(settings.task_run_lease_seconds),
            acquire_resources=acquire_task_resources,
            release_resources=release_run_resources,
        )

    async def _execute_run(self, run_id: str) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(run_id))
        conn = connect()
        try:
            row = conn.execute(
                """SELECT r.*, t.task_plan_id, t.title AS task_title,
                t.description AS task_description, t.expected_output,
                t.output_type, t.plan_item_key, t.status AS task_status,
                p.brief_id
                FROM runs r LEFT JOIN tasks t ON t.id = r.task_id
                LEFT JOIN task_plans p ON p.id = t.task_plan_id
                WHERE r.id = ? AND r.lease_owner = ?""",
                (run_id, self.worker_id),
            ).fetchone()
            if row is None:
                return
            if row.get("run_kind") == "triage":
                await self._execute_triage_run(conn, row)
                conn.commit()
                return
            if row.get("run_kind") == "coordination":
                await self._execute_coordination_run(conn, row)
                conn.commit()
                return
            prompt = self._build_task_prompt(conn, row)
            workspace = conn.execute(
                "SELECT * FROM workspaces WHERE id = ?", (row["workspace_id"],)
            ).fetchone()
            agent = conn.execute(
                "SELECT a.*, d.name AS department_name FROM agents a "
                "LEFT JOIN departments d ON d.id = a.department_id WHERE a.id = ?",
                (row["agent_id"],),
            ).fetchone()
            conversation = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (row["conversation_id"],)
            ).fetchone()
            context_manifest = None
            if workspace and agent and conversation:
                from app.services.company_memory import build_context_manifest

                context_manifest = build_context_manifest(
                    conn,
                    workspace=workspace,
                    conversation=conversation,
                    agent=agent,
                    current_text=prompt,
                    task_id=row["task_id"],
                )
                prompt += "\n\n【本次工作相关的公司记忆】\n" + context_manifest["text"]
            token = create_company_tool_token(
                workspace_id=row["workspace_id"],
                plan_id=row["task_plan_id"],
                task_id=row["task_id"],
                run_id=row["id"],
                agent_id=row["agent_id"],
                conversation_id=row["conversation_id"],
            )
            ctx = RunContext(
                run_id=run_id,
                prompt=prompt,
                workdir=row["workdir"],
                profile=row["hermes_profile_id"],
                agent_id=row["agent_id"],
                workspace_id=row["workspace_id"],
                conversation_id=row["conversation_id"],
                task_id=row["task_id"],
                context_manifest_id=context_manifest["id"] if context_manifest else None,
                resume_session_id=row.get("hermes_session_id"),
                mcp_servers=[
                    {
                        "name": "agentpulse-company",
                        "url": settings.company_tools_url,
                        "headers": {"Authorization": f"Bearer {token}"},
                    }
                ],
            )
            conn.execute(
                """UPDATE tasks SET status = '进行中', workflow_status = 'in_progress',
                progress = 10, updated_at = ? WHERE id = ?""",
                (now_iso(), row["task_id"]),
            )
            conn.commit()
            result = await start_run(
                conn,
                ctx=ctx,
                backend=self.backend_factory(),
                input_message_id=row["input_message_id"],
                persist_message=False,
                existing_run_id=run_id,
            )
            self._finalize_run(conn, row, result)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            self._record_execution_crash(run_id, str(exc))
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            conn.close()

    async def _heartbeat(self, run_id: str) -> None:
        while True:
            await asyncio.sleep(settings.task_run_heartbeat_seconds)
            conn = connect()
            try:
                conn.execute(
                    """UPDATE runs SET lease_expires_at = ?
                    WHERE id = ? AND lease_owner = ? AND status IN (
                      'leased','running','pausing','waiting_user','waiting_clarify',
                      'waiting_approval','waiting_information','waiting_colleague'
                    )""",
                    (
                        _iso_after(settings.task_run_lease_seconds),
                        run_id,
                        self.worker_id,
                    ),
                )
                conn.commit()
            except Exception as exc:
                conn.rollback()
                logger.warning(
                    "task_run_heartbeat_failed", run_id=run_id, error=str(exc)
                )
            finally:
                conn.close()

    def _build_task_prompt(self, conn: Database, run: dict) -> str:
        brief = conn.execute(
            "SELECT * FROM consensus_briefs WHERE id = ?", (run["brief_id"],)
        ).fetchone()
        dependency_outputs = conn.execute(
            """SELECT t.title, o.output_type, o.content
            FROM task_dependencies d
            JOIN tasks t ON t.id = d.depends_on_task_id
            JOIN task_outputs o ON o.task_id = t.id
            WHERE d.task_id = ? ORDER BY o.created_at""",
            (run["task_id"],),
        ).fetchall()
        knowledge = conn.execute(
            """SELECT id, title, category, content FROM knowledge_sources
            WHERE workspace_id = ? ORDER BY updated_at DESC LIMIT 20""",
            (run["workspace_id"],),
        ).fetchall()
        input_message = None
        if run["input_message_id"]:
            input_message = conn.execute(
                "SELECT content FROM messages WHERE id = ?", (run["input_message_id"],)
            ).fetchone()
        schema = (
            json.dumps(ContentPackageV1.model_json_schema(), ensure_ascii=False)
            if run["output_type"] == "content_package_v1"
            else "Markdown"
        )
        return f"""你正在执行 AgentPulse 已确认计划中的一项任务。

【共识 brief】
目标：{brief['goal']}
范围：{brief['scope']}
约束：{brief['constraints']}
成功标准：{brief['success_criteria']}

【当前任务】
标题：{run['task_title']}
说明：{run['task_description']}
预期交付：{run['expected_output']}
交付类型：{run['output_type']}

【补充信息】
{input_message['content'] if input_message else '无'}

【前置任务产出】
{json.dumps([dict(row) for row in dependency_outputs], ensure_ascii=False)}

【公司资料】
{json.dumps([dict(row) for row in knowledge], ensure_ascii=False)}

【执行规则】
1. 资料库优先；需要网页信息时可以检索。
2. 事实性内容必须引用资料 ID 或 URL；无法验证的内容放入 assumptions。
3. 过程中用 report_progress 汇报。完成时必须调用 submit_output。
4. 缺失关键信息时调用 block_task；范围内调整可用 create_subtask/request_support，
   不得修改 brief 的目标、范围或成功标准。
5. 首版只交付待发布内容，不得真实发布或发送。
6. 内容研究不得调用 terminal、shell 或执行下载脚本；只使用公司资料检索和网页读取工具。

【交付 schema】
{schema}
"""

    def _finalize_run(self, conn: Database, run: dict, result: dict) -> None:
        latest = conn.execute("SELECT * FROM runs WHERE id = ?", (run["id"],)).fetchone()
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (run["task_id"],)).fetchone()
        conn.execute(
            "UPDATE runs SET lease_owner = NULL, lease_expires_at = NULL WHERE id = ?",
            (run["id"],),
        )
        release_run_resources(conn, run_id=run["id"])
        if latest and latest["status"] == RunStatus.PAUSED:
            sync_agent_work_state(conn, run["agent_id"], now_iso=now_iso())
            return
        if task["status"] == "阻塞":
            return
        if not latest or latest["status"] != RunStatus.COMPLETED:
            self._retry_or_block(conn, run["task_id"], latest or run, latest["error"] if latest else "run failed")
            return

        if dependency_blockers(conn, run["task_id"]):
            conn.execute(
                "UPDATE tasks SET status = '待执行', progress = 0, updated_at = ? WHERE id = ?",
                (now_iso(), run["task_id"]),
            )
            return

        outputs = conn.execute(
            "SELECT * FROM task_outputs WHERE task_id = ? ORDER BY created_at",
            (run["task_id"],),
        ).fetchall()
        matching = [row for row in outputs if row["output_type"] == run["output_type"]]
        if not matching and run["output_type"] != "content_package_v1" and result.get("text"):
            self._save_markdown_fallback(conn, run, result["text"])
            matching = [True]
        if run["output_type"] == "content_package_v1" and matching:
            try:
                parse_content_package(matching[-1]["content"])
            except Exception as exc:
                matching = []
                invalid_reason = f"invalid content_package_v1: {exc}"
            else:
                invalid_reason = ""
        else:
            invalid_reason = "required output was not submitted"
        if not matching:
            conn.execute(
                """UPDATE runs SET status = 'failed', runtime_status = 'failed',
                error = ?, completed_at = ?
                WHERE id = ?""",
                (invalid_reason, now_iso(), run["id"]),
            )
            failed = dict(latest)
            failed["status"] = "failed"
            failed["error"] = invalid_reason
            self._retry_or_block(conn, run["task_id"], failed, invalid_reason)
            return

        conn.execute(
            """UPDATE tasks SET status = '已完成', workflow_status = 'completed',
            waiting_reason = '', progress = 100, updated_at = ? WHERE id = ?""",
            (now_iso(), run["task_id"]),
        )
        add_task_event(
            conn,
            workspace_id=run["workspace_id"],
            task_id=run["task_id"],
            conversation_id=run["conversation_id"],
            agent_id=run["agent_id"],
            kind="task_completed",
            title="任务自动完成",
            content=run["expected_output"],
        )
        resume_preempted_task_after(conn, task_id=run["task_id"])
        self._enqueue_ready_tasks(conn, plan_id=run["task_plan_id"])
        self._refresh_plan(conn, run["task_plan_id"])
        sync_agent_work_state(conn, run["agent_id"], now_iso=now_iso())

    def _save_markdown_fallback(self, conn: Database, run: dict, text: str) -> None:
        add_task_output(
            conn,
            workspace_id=run["workspace_id"],
            task_id=run["task_id"],
            conversation_id=run["conversation_id"],
            agent_id=run["agent_id"],
            title=run["task_title"],
            output_type="markdown",
            content=text,
        )

    def _retry_or_block(
        self, conn: Database, task_id: str, run: dict, reason: str
    ) -> None:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        attempt = int(run["attempt_no"])
        if attempt < 2:
            profile = resolve_hermes_profile(conn, task["owner_agent_id"])
            if profile:
                enqueue_task_run(
                    conn,
                    task=task,
                    profile=profile,
                    attempt_no=attempt + 1,
                )
                conn.execute(
                    """UPDATE tasks SET status = '待执行', workflow_status = 'ready',
                    progress = 0, updated_at = ? WHERE id = ?""",
                    (now_iso(), task_id),
                )
                return
        self._block_after_failure(conn, task, reason)

    def _block_after_failure(self, conn: Database, task: dict, reason: str) -> None:
        conn.execute(
            """UPDATE tasks SET status = '阻塞', workflow_status = 'waiting_information',
            waiting_reason = 'execution_failed', updated_at = ? WHERE id = ?""",
            (now_iso(), task["id"]),
        )
        conn.execute(
            """UPDATE task_plans SET status = 'blocked', workflow_status = 'degraded',
            blocked_reason = ?, updated_at = ?
            WHERE id = ?""",
            (reason[:2000], now_iso(), task["task_plan_id"]),
        )
        add_task_event(
            conn,
            workspace_id=task["workspace_id"],
            task_id=task["id"],
            conversation_id=task["conversation_id"],
            agent_id=task["owner_agent_id"],
            kind="task_blocked",
            title="自动执行两次失败",
            content=reason[:2000],
        )
        resume_preempted_task_after(conn, task_id=task["id"])

    def _enqueue_ready_tasks(
        self, conn: Database, *, plan_id: str | None = None
    ) -> None:
        schedule_ready_tasks(
            conn,
            resolve_profile=resolve_hermes_profile,
            enqueue_run=lambda db, task, profile, attempt: enqueue_task_run(
                db, task=task, profile=profile, attempt_no=attempt
            ),
            block_unready=self._block_after_failure,
            timestamp=now_iso(),
            plan_id=plan_id,
        )

    def _refresh_plan(self, conn: Database, plan_id: str) -> None:
        refresh_plan_state(conn, plan_id=plan_id, timestamp=now_iso())

    async def recover_expired_runs(self) -> None:
        conn = connect()
        try:
            rows = conn.execute(
                """SELECT * FROM runs WHERE status IN (
                  'leased','running','pausing','waiting_user','waiting_clarify',
                  'waiting_approval','waiting_information','waiting_colleague'
                )
                AND lease_expires_at IS NOT NULL AND lease_expires_at < ?""",
                (now_iso(),),
            ).fetchall()
            for run in rows:
                conn.execute(
                    """UPDATE runs SET status = 'failed', runtime_status = 'failed',
                    error = 'worker lease expired',
                    completed_at = ?, lease_owner = NULL, lease_expires_at = NULL WHERE id = ?""",
                    (now_iso(), run["id"]),
                )
                release_run_resources(conn, run_id=run["id"])
                if run.get("task_id"):
                    self._retry_or_block(conn, run["task_id"], run, "worker lease expired")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _record_execution_crash(self, run_id: str, error: str) -> None:
        conn = connect()
        try:
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run and run["status"] not in RunStatus.TERMINAL:
                conn.execute(
                    """UPDATE runs SET status = 'failed', runtime_status = 'failed',
                    error = ?, completed_at = ?,
                    lease_owner = NULL, lease_expires_at = NULL WHERE id = ?""",
                    (error[:2000], now_iso(), run_id),
                )
                release_run_resources(conn, run_id=run_id)
                if run.get("task_id"):
                    self._retry_or_block(conn, run["task_id"], run, error)
                elif run.get("run_kind") == "coordination":
                    conn.execute(
                        """UPDATE coordination_cases SET status = 'needs_goal',
                        decision_reason = ?, updated_at = ?
                        WHERE run_id = ? AND status = 'evaluating'""",
                        (error[:2000], now_iso(), run_id),
                    )
                elif run.get("run_kind") == "triage":
                    conn.execute(
                        """UPDATE work_requests SET status = 'needs_info',
                        decision_reason = ?, updated_at = ?
                        WHERE triage_run_id = ? AND status = 'evaluating'""",
                        (error[:2000], now_iso(), run_id),
                    )
                else:
                    conn.execute(
                        """UPDATE work_requests SET status = 'needs_info',
                        response_content = ?, updated_at = ? WHERE triage_run_id = ?
                        AND status = 'evaluating'""",
                        (error[:2000], now_iso(), run_id),
                    )
                sync_agent_work_state(conn, run["agent_id"], now_iso=now_iso())
                conn.commit()
        finally:
            conn.close()
