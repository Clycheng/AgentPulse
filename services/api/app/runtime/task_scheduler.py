"""Database-backed task dispatcher for TD-11."""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Callable

from app.core.config import settings
from app.core.database import Database, connect
from app.core.logging import get_logger
from app.runtime.company_tools_auth import create_company_tool_token
from app.runtime.hermes_client import HermesBackend, RunContext
from app.runtime.runner import start_run
from app.runtime.runs import RunStatus
from app.schemas.content_package import ContentPackageV1
from app.services.content_packages import parse_content_package
from app.services.task_plans import enqueue_task_run
from app.services.workspace import add_task_event, add_task_output, now_iso


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

    def _claim_runs(self, conn: Database) -> list[str]:
        workspaces = conn.execute(
            """SELECT DISTINCT workspace_id FROM runs
            WHERE status = 'queued' AND (lease_expires_at IS NULL OR lease_expires_at < ?)
            ORDER BY workspace_id""",
            (now_iso(),),
        ).fetchall()
        claimed: list[str] = []
        for workspace in workspaces:
            workspace_id = workspace["workspace_id"]
            occupied = conn.execute(
                """SELECT COUNT(*) AS count FROM runs WHERE workspace_id = ? AND (
                  status IN ('running','waiting_user','waiting_clarify') OR
                  (status = 'queued' AND lease_expires_at >= ?)
                )""",
                (workspace_id, now_iso()),
            ).fetchone()["count"]
            slots = max(0, settings.task_workspace_concurrency - int(occupied))
            if slots == 0:
                continue
            candidates = conn.execute(
                """SELECT id FROM runs WHERE workspace_id = ? AND status = 'queued'
                AND (lease_expires_at IS NULL OR lease_expires_at < ?)
                ORDER BY created_at, id LIMIT ?""",
                (workspace_id, now_iso(), slots),
            ).fetchall()
            for candidate in candidates:
                conn.execute(
                    """UPDATE runs SET lease_owner = ?, lease_expires_at = ?
                    WHERE id = ? AND status = 'queued'
                    AND (lease_expires_at IS NULL OR lease_expires_at < ?)""",
                    (
                        self.worker_id,
                        _iso_after(settings.task_run_lease_seconds),
                        candidate["id"],
                        now_iso(),
                    ),
                )
                row = conn.execute(
                    "SELECT lease_owner FROM runs WHERE id = ?", (candidate["id"],)
                ).fetchone()
                if row and row["lease_owner"] == self.worker_id:
                    claimed.append(candidate["id"])
        return claimed

    async def _execute_run(self, run_id: str) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(run_id))
        conn = connect()
        try:
            row = conn.execute(
                """SELECT r.*, t.task_plan_id, t.title AS task_title,
                t.description AS task_description, t.expected_output,
                t.output_type, t.plan_item_key, t.status AS task_status,
                p.brief_id
                FROM runs r JOIN tasks t ON t.id = r.task_id
                JOIN task_plans p ON p.id = t.task_plan_id
                WHERE r.id = ? AND r.lease_owner = ?""",
                (run_id, self.worker_id),
            ).fetchone()
            if row is None:
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
                mcp_servers=[
                    {
                        "name": "agentpulse-company",
                        "url": settings.company_tools_url,
                        "headers": {"Authorization": f"Bearer {token}"},
                    }
                ],
            )
            conn.execute(
                "UPDATE tasks SET status = '进行中', progress = 10, updated_at = ? WHERE id = ?",
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
                      'queued','running','waiting_user','waiting_clarify'
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
        if task["status"] == "阻塞":
            return
        if not latest or latest["status"] != RunStatus.COMPLETED:
            self._retry_or_block(conn, run["task_id"], latest or run, latest["error"] if latest else "run failed")
            return

        unmet = conn.execute(
            """SELECT COUNT(*) AS count FROM task_dependencies d
            JOIN tasks dependency ON dependency.id = d.depends_on_task_id
            WHERE d.task_id = ? AND dependency.status <> '已完成'""",
            (run["task_id"],),
        ).fetchone()["count"]
        if int(unmet):
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
                """UPDATE runs SET status = 'failed', error = ?, completed_at = ?
                WHERE id = ?""",
                (invalid_reason, now_iso(), run["id"]),
            )
            failed = dict(latest)
            failed["status"] = "failed"
            failed["error"] = invalid_reason
            self._retry_or_block(conn, run["task_id"], failed, invalid_reason)
            return

        conn.execute(
            "UPDATE tasks SET status = '已完成', progress = 100, updated_at = ? WHERE id = ?",
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
        self._enqueue_ready_tasks(conn, plan_id=run["task_plan_id"])
        self._refresh_plan(conn, run["task_plan_id"])

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
            spec = conn.execute(
                "SELECT hermes_profile, status FROM agent_specs WHERE agent_id = ?",
                (task["owner_agent_id"],),
            ).fetchone()
            if spec and spec["status"] == "ready" and spec["hermes_profile"]:
                enqueue_task_run(
                    conn,
                    task=task,
                    profile=spec["hermes_profile"],
                    attempt_no=attempt + 1,
                )
                conn.execute(
                    "UPDATE tasks SET status = '待执行', progress = 0, updated_at = ? WHERE id = ?",
                    (now_iso(), task_id),
                )
                return
        self._block_after_failure(conn, task, reason)

    def _block_after_failure(self, conn: Database, task: dict, reason: str) -> None:
        conn.execute(
            "UPDATE tasks SET status = '阻塞', updated_at = ? WHERE id = ?",
            (now_iso(), task["id"]),
        )
        conn.execute(
            """UPDATE task_plans SET status = 'blocked', blocked_reason = ?, updated_at = ?
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

    def _enqueue_ready_tasks(
        self, conn: Database, *, plan_id: str | None = None
    ) -> None:
        params: tuple = () if plan_id is None else (plan_id,)
        filter_sql = "" if plan_id is None else "AND t.task_plan_id = ?"
        tasks = conn.execute(
            f"""SELECT t.* FROM tasks t JOIN task_plans p ON p.id = t.task_plan_id
            WHERE t.status = '待执行' AND t.plan_item_key <> '__root__'
            AND p.status IN ('active','blocked') {filter_sql}
            ORDER BY t.created_at, t.id""",
            params,
        ).fetchall()
        for task in tasks:
            active = conn.execute(
                """SELECT id FROM runs WHERE task_id = ? AND status IN (
                  'queued','running','waiting_user','waiting_clarify'
                ) LIMIT 1""",
                (task["id"],),
            ).fetchone()
            if active:
                continue
            unmet = conn.execute(
                """SELECT COUNT(*) AS count FROM task_dependencies d
                JOIN tasks dependency ON dependency.id = d.depends_on_task_id
                WHERE d.task_id = ? AND dependency.status <> '已完成'""",
                (task["id"],),
            ).fetchone()["count"]
            if int(unmet):
                continue
            spec = conn.execute(
                "SELECT hermes_profile, status FROM agent_specs WHERE agent_id = ?",
                (task["owner_agent_id"],),
            ).fetchone()
            if not spec or spec["status"] != "ready" or not spec["hermes_profile"]:
                self._block_after_failure(conn, task, "task owner is not ready")
                continue
            latest = conn.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) AS attempt FROM runs WHERE task_id = ?",
                (task["id"],),
            ).fetchone()
            enqueue_task_run(
                conn,
                task=task,
                profile=spec["hermes_profile"],
                attempt_no=int(latest["attempt"]) + 1,
            )

    def _refresh_plan(self, conn: Database, plan_id: str) -> None:
        plan = conn.execute("SELECT * FROM task_plans WHERE id = ?", (plan_id,)).fetchone()
        children = conn.execute(
            """SELECT status, progress FROM tasks
            WHERE task_plan_id = ? AND plan_item_key <> '__root__'""",
            (plan_id,),
        ).fetchall()
        if not children:
            return
        progress = sum(int(row["progress"]) for row in children) // len(children)
        all_done = all(row["status"] == "已完成" for row in children)
        blocked = any(row["status"] == "阻塞" for row in children)
        root_status = "已完成" if all_done else "阻塞" if blocked else "进行中"
        conn.execute(
            "UPDATE tasks SET status = ?, progress = ?, updated_at = ? WHERE id = ?",
            (root_status, 100 if all_done else progress, now_iso(), plan["root_task_id"]),
        )
        if all_done:
            conn.execute(
                """UPDATE task_plans SET status = 'completed', completed_at = ?,
                updated_at = ?, blocked_reason = '' WHERE id = ?""",
                (now_iso(), now_iso(), plan_id),
            )
        elif not blocked:
            conn.execute(
                """UPDATE task_plans SET status = 'active', blocked_reason = '',
                updated_at = ? WHERE id = ?""",
                (now_iso(), plan_id),
            )

    async def recover_expired_runs(self) -> None:
        conn = connect()
        try:
            rows = conn.execute(
                """SELECT * FROM runs WHERE status IN ('running','waiting_user','waiting_clarify')
                AND lease_expires_at IS NOT NULL AND lease_expires_at < ?""",
                (now_iso(),),
            ).fetchall()
            for run in rows:
                conn.execute(
                    """UPDATE runs SET status = 'failed', error = 'worker lease expired',
                    completed_at = ?, lease_owner = NULL, lease_expires_at = NULL WHERE id = ?""",
                    (now_iso(), run["id"]),
                )
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
            if run and run["status"] not in ("completed", "failed"):
                conn.execute(
                    """UPDATE runs SET status = 'failed', error = ?, completed_at = ?,
                    lease_owner = NULL, lease_expires_at = NULL WHERE id = ?""",
                    (error[:2000], now_iso(), run_id),
                )
                self._retry_or_block(conn, run["task_id"], run, error)
                conn.commit()
        finally:
            conn.close()
