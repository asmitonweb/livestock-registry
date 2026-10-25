"""Loaded by native run_celery.py CELERY_IMPORTS in worker and beat processes."""
import asyncio
from celery import current_app, shared_task
from openg2p_fastapi_common.config import Settings
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from .runtime import install
from .workflow import dispatch_pending

install()

# The native beat process also runs its producer tasks on the default queue.
# Reference delivery uses the worker's async engine and must go to the native
# worker queue, whose process owns REGISTRY_CELERY_WORKERS_* configuration.
_worker_queue = getattr(Settings.get_config(strict=False), "worker_queue", "registry_worker_queue")


@shared_task(name="coss.farmer_reference.deliver_workflows", queue=_worker_queue)
def deliver_workflows():
    async def run():
        from openg2p_registry_celery_worker.engine import Engine
        # The native worker owns a different engine from fastapi-common. Keep
        # this extension task's short-lived loop from reusing either loop's pool.
        engine = create_async_engine(Engine.construct_async_db_datasource(), poolclass=NullPool)
        try:
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions.begin() as session:
                return await dispatch_pending(session)
        finally:
            await engine.dispose()
    return asyncio.run(run())


current_app.conf.beat_schedule = {
    **(current_app.conf.beat_schedule or {}),
    "coss-farmer-reference-workflows": {
        "task": "coss.farmer_reference.deliver_workflows", "schedule": 30.0,
        "options": {"queue": _worker_queue},
    },
}
