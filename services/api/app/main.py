from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.admin import router as admin_router
from app.api.routes.auth import router as auth_router
from app.api.routes.briefs import router as briefs_router
from app.api.routes.catalog import router as catalog_router
from app.api.routes.channels import router as channels_router
from app.api.routes.health import router as health_router
from app.api.routes.ideas import router as ideas_router
from app.api.routes.model_settings import router as model_settings_router
from app.api.routes.memory import router as memory_router
from app.api.routes.runs import router as runs_router
from app.api.routes.team_compiler import router as team_compiler_router
from app.api.routes.task_plans import router as task_plans_router
from app.api.routes.telemetry import router as telemetry_router
from app.api.routes.webhooks import router as webhooks_router
from app.api.company_tools_mcp import company_tools_app, company_tools_lifespan
from app.api.business_tools_mcp import business_tools_app, business_tools_lifespan
from app.api.routes.business_tools import router as business_tools_router
from app.api.routes.workspace import router as workspace_router
from app.core.database import connect, init_db, shutdown_db
from app.core.config import settings
from app.core.logging import get_logger, setup_logging
from app.core.http_security import RequestSecurityMiddleware

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    setup_logging(json_output=os.environ.get("AGENTPULSE_LOG_JSON", "").lower() in ("1", "true"))
    logger.info("server_starting", version=settings.app_version)
    init_db()
    logger.info("database_initialized", kind=database_kind_label())
    _check_hermes_binary_if_provisioning_enabled()

    cron_task = None
    scheduler_task = None
    business_worker_task = None
    _app.state.task_worker = None
    _app.state.business_worker = None
    if settings.idle_thinking_cron:
        import asyncio

        cron_task = asyncio.create_task(_idle_cron_loop())
        logger.info("idle_cron_started", interval_s=settings.idle_cron_interval_seconds)

    if settings.task_worker_enabled:
        import asyncio

        scheduler_task = asyncio.create_task(_task_worker_loop())
        _app.state.task_worker = scheduler_task
        logger.info("task_worker_started", interval_s=settings.task_worker_poll_seconds)

    if settings.business_worker_enabled:
        import asyncio

        business_worker_task = asyncio.create_task(_business_worker_loop())
        _app.state.business_worker = business_worker_task
        logger.info(
            "business_worker_started", interval_s=settings.business_worker_poll_seconds
        )

    async with company_tools_lifespan(), business_tools_lifespan():
        yield

    if cron_task is not None:
        cron_task.cancel()
    if scheduler_task is not None:
        scheduler_task.cancel()
    if business_worker_task is not None:
        business_worker_task.cancel()
    shutdown_db()
    logger.info("server_stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url=None if settings.environment == "production" else "/docs",
        redoc_url=None if settings.environment == "production" else "/redoc",
        openapi_url=None if settings.environment == "production" else "/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(
        RequestSecurityMiddleware,
        max_body_bytes=settings.max_request_body_bytes,
    )

    app.include_router(health_router, prefix="/api")
    app.include_router(telemetry_router, prefix="/api")
    app.include_router(auth_router, prefix="/api")
    app.include_router(admin_router, prefix="/api")
    app.include_router(workspace_router, prefix="/api")
    app.include_router(briefs_router, prefix="/api")
    app.include_router(runs_router, prefix="/api")
    app.include_router(ideas_router, prefix="/api")
    app.include_router(model_settings_router, prefix="/api")
    app.include_router(memory_router, prefix="/api")
    app.include_router(channels_router, prefix="/api")
    app.include_router(catalog_router, prefix="/api")
    app.include_router(team_compiler_router, prefix="/api")
    app.include_router(task_plans_router, prefix="/api")
    app.include_router(business_tools_router, prefix="/api")
    app.include_router(webhooks_router)
    app.mount("/mcp/company-tools", company_tools_app)
    app.mount("/mcp/business-tools", business_tools_app)

    return app


def database_kind_label() -> str:
    from app.core.database import safe_database_label
    return safe_database_label()


def _check_hermes_binary_if_provisioning_enabled() -> None:
    """Fail loud at startup, not at the first chat message.

    Borrowed from service-claw-cloud's lifespan smoke test (SELECT count(*)
    against its own tables so a broken DB connection fails at boot, not on
    first request) — same idea applied to our own "is the thing we depend on
    actually there" check. Without this, turning on
    AGENTPULSE_HERMES_PROVISIONING with a missing/misconfigured `hermes`
    binary silently degrades every employee to the DeepSeek fallback until
    someone notices runs never reach `ready`.
    """
    if not settings.hermes_provisioning:
        return
    import shutil
    import sys

    if shutil.which(settings.hermes_bin) is None:
        logger.error(
            "hermes_binary_not_found",
            hermes_bin=settings.hermes_bin,
            hint="AGENTPULSE_HERMES_PROVISIONING=true but the `hermes` CLI "
            "isn't on PATH — every employee will silently fall back to "
            "DeepSeek instead of becoming real Hermes employees.",
        )
        print(
            f"ERROR: AGENTPULSE_HERMES_PROVISIONING=true but "
            f"`{settings.hermes_bin}` was not found on PATH.\n"
            f"       Install Hermes, or set AGENTPULSE_HERMES_BIN to its "
            f"path, or unset AGENTPULSE_HERMES_PROVISIONING for local dev "
            f"without real Hermes.",
            file=sys.stderr,
        )
        sys.exit(1)


async def _idle_cron_loop() -> None:
    import asyncio

    from app.runtime.hermes_client import HermesBackend
    from app.runtime.idle_think import run_idle_tick
    from app.runtime.profile_provisioner import build_provisioner_from_settings
    from app.runtime.reflection import run_reflection_tick
    from app.services.company_memory import run_memory_reflection_tick

    while True:
        await asyncio.sleep(settings.idle_cron_interval_seconds)
        try:
            conn = connect()
            try:
                backend = HermesBackend(hermes_bin=settings.hermes_bin)
                idle_result = await run_idle_tick(
                    conn, backend=backend, hermes_work_root=settings.hermes_work_root
                )
                reflection_result = await run_reflection_tick(
                    conn,
                    backend=backend,
                    provisioner=build_provisioner_from_settings(),
                    hermes_work_root=settings.hermes_work_root,
                )
                memory_reflections = run_memory_reflection_tick(conn)
                conn.commit()
                logger.info(
                    "cron_tick",
                    ideas=idle_result["ideas_created"],
                    skills=reflection_result["skills_learned"],
                    memory_reflections=memory_reflections,
                )
            finally:
                conn.close()
        except Exception:
            continue


async def _task_worker_loop() -> None:
    import asyncio

    from app.runtime.task_scheduler import TaskScheduler

    scheduler = TaskScheduler()
    await scheduler.recover_expired_runs()
    try:
        while True:
            await scheduler.tick()
            await asyncio.sleep(settings.task_worker_poll_seconds)
    finally:
        await scheduler.close()


async def _business_worker_loop() -> None:
    import asyncio

    from app.services.business_actions import BusinessActionWorker

    worker = BusinessActionWorker()
    try:
        while True:
            await worker.tick()
            await asyncio.sleep(settings.business_worker_poll_seconds)
    finally:
        await worker.close()


app = create_app()
