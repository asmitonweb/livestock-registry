"""Celery application, polling tasks, and async webhook processing."""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from celery import Celery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .config import get_settings
from .database import get_engine
from . import ingest_log
from . import metrics as connector_metrics
from .ingest_log import log_event
from .models import ConnectorDefinition
from .services import IngestionService
from .transports import (
    CHECKPOINT_STATE_KEY,
    Checkpoint,
    CheckpointMode,
    get_transport,
)

# Ensure auth strategies and transports self-register in this worker process.
# Without this, polls raise "Unknown auth strategy: 'odk_session'" because the
# @register_auth decorators never fire. (API does this via main.py.)
from . import transports as _transports  # noqa: F401
from .auth import strategies as _auth_strategies  # noqa: F401

_settings = get_settings()
_logger = logging.getLogger("connector.worker")
# The worker process polls and ingests; it has no create_app() to set this up.
ingest_log.configure(
    _settings.ingest_log_file,
    service="connector-worker",
    max_bytes=_settings.ingest_log_max_bytes,
    backups=_settings.ingest_log_backups,
)

celery_app = Celery(
    "connector_worker",
    broker=_settings.celery_broker_url,
    backend=_settings.celery_result_backend,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)

_POLL_BEAT_INTERVAL = 60
_PUSH_TRANSPORT_TYPES = frozenset({"webhook", "websub"})

# One asyncio loop per Celery worker *process* (not per task).
# A singleton AsyncEngine/asyncpg must stay on the same loop; creating a new
# loop each task and closing it leaves the engine bound to a dead loop → OSError 9.
_worker_loop: asyncio.AbstractEventLoop | None = None


def _worker_event_loop() -> asyncio.AbstractEventLoop:
    global _worker_loop
    if _worker_loop is None:
        _worker_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_worker_loop)
    return _worker_loop


def _run_async(coro):
    """Run *coro* on this worker process's persistent loop (Celery prefork-safe)."""
    loop = _worker_event_loop()
    return loop.run_until_complete(coro)


# ---------------------------------------------------------------------------
# Poll a single connector
# ---------------------------------------------------------------------------

@celery_app.task(name="connector.poll")
def poll_connector_task(connector_id: str) -> dict:
    """Fetch records from the external source and ingest each one."""
    return _run_async(_poll_async(connector_id))


async def _poll_async(connector_id: str) -> dict:
    engine = get_engine()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    ingestion = IngestionService()
    stats = {"connector_id": connector_id, "fetched": 0, "success": 0, "failed": 0}
    t0 = time.monotonic()
    poll_error: str | None = None

    async with session_factory() as session:
        connector = await session.get(ConnectorDefinition, connector_id)
        if connector is None or not connector.is_active:
            _logger.warning("Connector %s not active, skipping poll", connector_id)
            return stats

        # Snapshot everything we need from the ORM instance up front. After any
        # internal rollback (e.g. IdempotencyKey race in process_record) the
        # instance's attributes become expired and accessing them inside this
        # async block triggers a sync lazy-load → MissingGreenlet.
        transport_type = connector.transport_type
        poll_state: dict = dict(connector.get_poll_state())
        poll_state_changed = False
        # Structured checkpoint advancement — comparison logic lives on
        # the transport-emitted Checkpoint, not in the worker.
        prev_checkpoint = Checkpoint.from_dict(poll_state.get(CHECKPOINT_STATE_KEY))
        current_checkpoint: Checkpoint | None = prev_checkpoint

        log_event("poll", "poll_started", connector_id=connector_id, transport=transport_type)
        try:
            transport = get_transport(transport_type)
            async for record in transport.fetch(connector):
                stats["fetched"] += 1
                connector_metrics.poll_fetched.labels(
                    connector_id=connector_id,
                    transport=transport_type,
                ).inc()
                run = await ingestion.process_record(
                    connector=connector,
                    source_event_id=record.source_event_id,
                    raw_data=record.data,
                    session=session,
                )
                connector_metrics.ingest_records_total.labels(
                    connector_id=connector_id,
                    status=run.status.value,
                ).inc()
                if run.status.value == "SUCCESS":
                    stats["success"] += 1
                    record_checkpoint = _record_checkpoint(record)
                    if record_checkpoint is not None:
                        merged = record_checkpoint.merged_with(current_checkpoint)
                        if merged.is_advanced_over(current_checkpoint):
                            current_checkpoint = merged
                            poll_state[CHECKPOINT_STATE_KEY] = current_checkpoint.to_dict()
                            poll_state_changed = True
                            # Mirror legacy keys so older callers/UI that
                            # read source_config (which merges poll_state)
                            # keep seeing a sensible value.
                            if record.cursor_key:
                                poll_state[record.cursor_key] = record.cursor_value
                else:
                    stats["failed"] += 1
        except Exception as exc:
            poll_error = f"{type(exc).__name__}: {exc}"[:2000]
            _logger.exception("Poll failed for %s", connector_id)
            log_event(
                "poll", "poll_failed", "ERROR",
                connector_id=connector_id, error=poll_error,
                fetched_before_failure=stats["fetched"],
            )

        # Reload the connector — the session may have been rolled back internally
        # (expiring attributes) or we may need to mutate it in a fresh tx.
        connector = await session.get(ConnectorDefinition, connector_id)
        duration_s = time.monotonic() - t0
        if connector is not None:
            connector.last_poll_at = datetime.now(timezone.utc).replace(tzinfo=None)
            connector.last_poll_duration_ms = int(duration_s * 1000)
            connector.last_poll_fetched = stats["fetched"]
            if poll_error is not None:
                connector.last_poll_status = "FAILED"
                connector.last_poll_error = poll_error
                stats["poll_error"] = poll_error
            elif stats["failed"] > 0:
                connector.last_poll_status = "PARTIAL"
                connector.last_poll_error = None
            else:
                connector.last_poll_status = "SUCCESS"
                connector.last_poll_error = None
            if poll_state_changed:
                connector.set_poll_state(poll_state)
            final_status = connector.last_poll_status
        else:
            final_status = "FAILED"
        await session.commit()

        connector_metrics.poll_duration.labels(
            connector_id=connector_id,
            transport=transport_type,
        ).observe(duration_s)
        connector_metrics.poll_status_total.labels(
            connector_id=connector_id,
            status=final_status or "UNKNOWN",
        ).inc()
        _emit_checkpoint_lag(
            connector_id=connector_id,
            transport_type=transport_type,
            checkpoint=current_checkpoint,
        )

    _logger.info("Poll complete for %s: %s", connector_id, stats)
    if poll_error is None:
        log_event(
            "poll", "poll_finished",
            "WARNING" if stats["failed"] else "INFO",
            connector_id=connector_id, fetched=stats["fetched"],
            success=stats["success"], failed=stats["failed"],
            duration_ms=int(duration_s * 1000),
        )
    return stats


# ---------------------------------------------------------------------------
# Dispatch polls for all enabled polling connectors (per-connector interval)
# ---------------------------------------------------------------------------

@celery_app.task(name="connector.poll_all")
def poll_all_connectors_task() -> list[str]:
    """Enumerate enabled polling connectors and dispatch if their interval has elapsed."""
    return _run_async(_dispatch_polls())


async def _dispatch_polls() -> list[str]:
    engine = get_engine()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    dispatched: list[str] = []

    async with session_factory() as session:
        result = await session.execute(
            select(ConnectorDefinition).where(
                ConnectorDefinition.enabled.is_(True),
                ConnectorDefinition.paused.is_(False),
                ConnectorDefinition.transport_type.notin_(list(_PUSH_TRANSPORT_TYPES)),
            )
        )
        now = datetime.now(timezone.utc)
        for connector in result.scalars().all():
            if _should_poll(connector, now):
                poll_connector_task.delay(connector.connector_id)
                dispatched.append(connector.connector_id)

    _logger.info("Dispatched polls for %d connectors", len(dispatched))
    return dispatched


def _emit_checkpoint_lag(
    *,
    connector_id: str,
    transport_type: str,
    checkpoint: Checkpoint | None,
) -> None:
    """Publish the persisted checkpoint lag (now − watermark) in seconds.

    Only meaningful for ``TIMESTAMP`` checkpoints; for other modes the
    gauge is set to -1 so dashboards can clearly distinguish "no data"
    from "running fresh".
    """
    if checkpoint is None or checkpoint.mode is not CheckpointMode.TIMESTAMP:
        connector_metrics.poll_checkpoint_lag_seconds.labels(
            connector_id=connector_id,
            transport=transport_type,
            mode=checkpoint.mode.value if checkpoint else "none",
        ).set(-1)
        return
    raw = checkpoint.value
    if not isinstance(raw, str) or not raw:
        return
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    lag = (datetime.now(timezone.utc) - ts).total_seconds()
    connector_metrics.poll_checkpoint_lag_seconds.labels(
        connector_id=connector_id,
        transport=transport_type,
        mode=checkpoint.mode.value,
    ).set(lag)


def _record_checkpoint(record) -> Checkpoint | None:
    """Resolve the structured checkpoint for *record*.

    Prefers ``record.checkpoint`` (the new contract). Falls back to the
    legacy ``cursor_key``/``cursor_value`` pair, treating it as a
    SEQUENCE checkpoint so existing transports keep advancing without
    code changes. Records with neither produce ``None`` (no progress).
    """
    if record.checkpoint is not None:
        return record.checkpoint
    if record.cursor_key and record.cursor_value:
        return Checkpoint(
            mode=CheckpointMode.SEQUENCE,
            value=record.cursor_value,
            extra={"cursor_key": record.cursor_key},
        )
    return None


def _should_poll(connector: ConnectorDefinition, now: datetime) -> bool:
    """Check per-connector poll interval against last_poll_at."""
    cfg = connector.get_source_config()
    interval = cfg.get("poll_interval_seconds", _settings.odk_poll_interval_seconds)
    if connector.last_poll_at is None:
        return True
    last = connector.last_poll_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    elapsed = (now - last).total_seconds()
    return elapsed >= interval


# ---------------------------------------------------------------------------
# Async webhook processing (fast ACK, heavy work in worker)
# ---------------------------------------------------------------------------

@celery_app.task(name="connector.process_webhook")
def process_webhook_task(
    connector_id: str, raw_body_hex: str, source_event_id: str | None
) -> dict:
    """Process a webhook payload that was accepted with 202."""
    return _run_async(
        _process_webhook_async(connector_id, raw_body_hex, source_event_id)
    )


async def _process_webhook_async(
    connector_id: str, raw_body_hex: str, source_event_id: str | None
) -> dict:
    engine = get_engine()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    ingestion = IngestionService()

    raw_body = bytes.fromhex(raw_body_hex)
    data = json.loads(raw_body)
    if isinstance(data.get("data"), dict):
        data = data["data"]

    async with session_factory() as session:
        connector = await session.get(ConnectorDefinition, connector_id)
        if connector is None or not connector.is_active:
            return {"error": "connector not active"}

        run = await ingestion.process_record(
            connector=connector,
            source_event_id=source_event_id,
            raw_data=data,
            session=session,
        )
        await session.commit()

    return {
        "run_id": run.run_id,
        "status": run.status.value,
        "registry_correlation_id": run.registry_correlation_id,
    }


# ---------------------------------------------------------------------------
# Celery beat schedule — polls every 60s and per-connector intervals gate dispatch
# ---------------------------------------------------------------------------

celery_app.conf.beat_schedule = {
    "poll-all-connectors": {
        "task": "connector.poll_all",
        "schedule": _POLL_BEAT_INTERVAL,
    },
}
