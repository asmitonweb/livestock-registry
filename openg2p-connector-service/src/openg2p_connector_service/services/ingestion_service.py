"""Idempotent ingestion pipeline: dedupe -> validate -> map -> wrap -> send -> record."""

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import jmespath
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import metrics as connector_metrics
from ..clients import PartnerIngestClient
from ..config import get_settings
from ..ingest_log import log_event
from ..mappers import JmesPathMapper, PassthroughMapper
from ..models import (
    ConnectorDefinition,
    DeadLetterEntry,
    IdempotencyKey,
    IngestionRun,
    RunStatus,
)

_logger = logging.getLogger("connector.service.ingestion")


class IngestionService:
    def __init__(self):
        self._client = PartnerIngestClient()
        self._jmespath_mapper = JmesPathMapper()
        self._passthrough_mapper = PassthroughMapper()

    def _attach_run_payload(
        self,
        run: IngestionRun,
        *,
        source: dict[str, Any] | None = None,
        mapped: dict[str, Any] | None = None,
        outbound: dict[str, Any] | None = None,
    ) -> None:
        """Serialize debug payload onto *run* when CONNECTOR_STORE_RUN_PAYLOADS is on."""
        settings = get_settings()
        if not settings.store_run_payloads:
            return
        doc: dict[str, Any] = {}
        if source is not None:
            doc["source"] = source
        if mapped is not None:
            doc["mapped"] = mapped
        if outbound is not None:
            doc["outbound"] = outbound
        if not doc:
            return
        raw = json.dumps(doc, default=str)
        b = raw.encode("utf-8")
        max_b = settings.run_payload_max_bytes
        if len(b) > max_b:
            run.run_payload_json = json.dumps(
                {
                    "_truncated": True,
                    "_approx_bytes": len(b),
                    "_max_bytes": max_b,
                },
                default=str,
            )
        else:
            run.run_payload_json = raw

    async def process_record(
        self,
        connector: ConnectorDefinition,
        source_event_id: str | None,
        raw_data: dict[str, Any],
        session: AsyncSession,
    ) -> IngestionRun:
        """Full idempotent pipeline for a single record."""
        connector_id = connector.connector_id
        log_ctx = {
            "connector_id": connector_id,
            "source_event_id": source_event_id,
            "platform": connector.platform,
        }
        t0 = time.perf_counter()
        ids = {"connector_id": connector_id, "source_event_id": source_event_id}

        # --- idempotency: return canonical run, bump counter — no new IngestionRun row ---
        if source_event_id:
            ik_row = await session.execute(
                select(IdempotencyKey).where(
                    IdempotencyKey.connector_id == connector_id,
                    IdempotencyKey.source_key == source_event_id,
                )
            )
            ik = ik_row.scalar_one_or_none()
            if ik is not None:
                canonical = await session.get(IngestionRun, ik.run_id)
                if canonical is None:
                    _logger.error(
                        "Idempotency key references missing run %s — skipping increment",
                        ik.run_id,
                        extra=log_ctx,
                    )
                else:
                    ik.redelivery_count += 1
                    canonical.redelivery_count = ik.redelivery_count
                    await session.flush()
                    _logger.info(
                        "Duplicate idempotent delivery redelivery_count=%s canonical_run=%s",
                        ik.redelivery_count,
                        ik.run_id,
                        extra=log_ctx,
                    )
                    log_event(
                        "result", "duplicate_ignored", **ids,
                        run_id=canonical.run_id, redelivery_count=ik.redelivery_count,
                        original_status=canonical.status.value,
                    )
                    connector_metrics.ingest_duration.labels(
                        connector_id=connector_id,
                    ).observe(time.perf_counter() - t0)
                    return canonical

        run = IngestionRun(
            run_id=uuid.uuid4().hex,
            connector_id=connector_id,
            source_event_id=source_event_id,
            status=RunStatus.IN_PROGRESS,
            attempt_count=1,
        )
        session.add(run)
        ids["run_id"] = run.run_id
        log_event("map", "record_received", **ids, top_level_keys=len(raw_data))

        # --- optional envelope extraction ---
        data = self._extract_envelope(connector, raw_data)

        # --- map ---
        try:
            mapped = self._apply_mapper(connector, data)
        except Exception as exc:
            self._attach_run_payload(run, source=raw_data, mapped=None, outbound=None)
            return await self._fail_run(
                run, exc, connector, source_event_id, raw_data, session,
                connector_id=connector_id,
                error_category="permanent",
                t_pipeline_start=t0,
                stage="map",
            )
        log_event("map", "mapped", **ids)

        # --- optional pre-ingest validation ---
        if get_settings().validate_mapped_payload and connector.validation_schema_json:
            try:
                self._validate_payload(connector, mapped)
            except Exception as exc:
                self._attach_run_payload(run, source=raw_data, mapped=mapped, outbound=None)
                return await self._fail_run(
                    run, exc, connector, source_event_id, raw_data, session,
                    connector_id=connector_id,
                    error_category="validation",
                    t_pipeline_start=t0,
                    stage="validate",
                )

        route = self._resolve_route_targets(
            connector=connector,
            mapped_data=mapped,
            raw_data=raw_data,
        )

        try:
            wrapped = self._wrap_in_g2p_envelope(
                connector,
                mapped,
                source_event_id,
                register_mnemonic_override=route["register_mnemonic"],
            )
        except ValueError as exc:
            self._attach_run_payload(run, source=raw_data, mapped=mapped, outbound=None)
            return await self._fail_run(
                run, exc, connector, source_event_id, raw_data, session,
                connector_id=connector_id,
                error_category="config",
                t_pipeline_start=t0,
                stage="envelope",
            )

        self._attach_run_payload(
            run, source=raw_data, mapped=mapped, outbound=wrapped,
        )

        # --- send to registry ---
        try:
            correlation_id = await self._client.send(
                payload=wrapped,
                data_model=route["data_model_mnemonic"],
                connector_id=connector_id,
            )
        except Exception as exc:
            category = self._classify_send_error(exc)
            return await self._fail_run(
                run, exc, connector, source_event_id, raw_data, session,
                connector_id=connector_id,
                error_category=category,
                t_pipeline_start=t0,
                stage="send",
            )

        # --- record success ---
        run.status = RunStatus.SUCCESS
        run.registry_correlation_id = correlation_id
        _logger.info("Ingestion succeeded", extra={**log_ctx, "correlation_id": correlation_id})
        log_event(
            "send", "sent", **ids, correlation_id=correlation_id,
            data_model=route["data_model_mnemonic"],
            register=route["register_mnemonic"],
            duration_ms=int((time.perf_counter() - t0) * 1000),
        )

        if source_event_id:
            try:
                async with session.begin_nested():
                    session.add(
                        IdempotencyKey(
                            connector_id=connector_id,
                            source_key=source_event_id,
                            run_id=run.run_id,
                        )
                    )
            except IntegrityError:
                _logger.info(
                    "Duplicate idempotency key on insert (race)",
                    extra=log_ctx,
                )

        await session.flush()
        connector_metrics.ingest_duration.labels(
            connector_id=connector_id,
        ).observe(time.perf_counter() - t0)
        return run

    def _extract_envelope(
        self, connector: ConnectorDefinition, raw_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Apply optional JMESPath envelope extraction from source_config_json."""
        cfg = connector.get_source_config()
        envelope = cfg.get("webhook_payload_envelope") or cfg.get("data_path")
        if envelope:
            result = jmespath.search(envelope, raw_data)
            if isinstance(result, dict):
                return result
        return raw_data

    def _wrap_in_g2p_envelope(
        self,
        connector: ConnectorDefinition,
        data: dict[str, Any],
        source_event_id: str | None,
        register_mnemonic_override: str | None = None,
    ) -> dict[str, Any]:
        """Wrap *data* in the G2P request envelope expected by Partner API."""
        if isinstance(data.get("header"), dict) and data["header"].get("sender_id"):
            return data

        sender_id = (connector.g2p_sender_id or "").strip()
        register_mnemonic = (
            register_mnemonic_override or connector.g2p_register_mnemonic or ""
        ).strip()
        if not sender_id:
            raise ValueError(
                "Connector is missing g2p_sender_id. Set a partner mnemonic on "
                "the connector before polling."
            )
        if not register_mnemonic:
            raise ValueError(
                "Connector is missing g2p_register_mnemonic. Set a target "
                "register on the connector before polling."
            )

        message_id = source_event_id or uuid.uuid4().hex
        payload: dict[str, Any] = {**data, "register_mnemonic": register_mnemonic}

        return {
            "header": {
                "message_id": message_id,
                "sender_id": sender_id,
                "signature": "stub-sig",
                "signature_algorithm": "none",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            "message": {
                "payload": payload,
            },
        }

    def _resolve_route_targets(
        self,
        *,
        connector: ConnectorDefinition,
        mapped_data: dict[str, Any],
        raw_data: dict[str, Any],
    ) -> dict[str, str | None]:
        """Resolve target data model/register, with registration_type overrides.

        If the record carries registration_type as household/houshold or individual,
        route to the corresponding register regardless of the connector's fixed
        register mnemonic. This supports mixed ODK submissions from the same form.
        """
        registration_type = self._extract_registration_type(mapped_data, raw_data)
        normalized = (registration_type or "").strip().lower()

        register_mnemonic = (connector.g2p_register_mnemonic or "").strip() or None
        data_model_mnemonic = (connector.data_model_mnemonic or "").strip() or None

        if normalized in {"household", "houshold"}:
            register_mnemonic = "Household"
        elif normalized == "individual":
            register_mnemonic = "Individual"

        return {
            "register_mnemonic": register_mnemonic,
            "data_model_mnemonic": data_model_mnemonic,
        }

    @staticmethod
    def _extract_registration_type(
        mapped_data: dict[str, Any],
        raw_data: dict[str, Any],
    ) -> str | None:
        """Best-effort extraction for registration type from mapped/raw payloads."""
        candidates: list[Any] = [
            mapped_data.get("registration_type"),
            (mapped_data.get("message") or {}).get("payload", {}).get("registration_type")
            if isinstance(mapped_data.get("message"), dict)
            else None,
            raw_data.get("registration_type"),
            (raw_data.get("message") or {}).get("payload", {}).get("registration_type")
            if isinstance(raw_data.get("message"), dict)
            else None,
        ]
        for value in candidates:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _apply_mapper(
        self, connector: ConnectorDefinition, raw_data: dict[str, Any]
    ) -> dict[str, Any]:
        if connector.mapper_expression:
            return self._jmespath_mapper.map(raw_data, connector.mapper_expression)
        return self._passthrough_mapper.map(raw_data)

    def _validate_payload(
        self, connector: ConnectorDefinition, mapped: dict[str, Any]
    ) -> None:
        """Validate mapped payload against optional JSON schema."""
        schema = json.loads(connector.validation_schema_json)  # type: ignore[arg-type]
        try:
            import jsonschema
            jsonschema.validate(mapped, schema)
        except ImportError:
            _logger.debug("jsonschema not installed, skipping validation")
        except jsonschema.ValidationError as exc:
            raise ValueError(f"Validation failed: {exc.message}") from exc

    @staticmethod
    def _classify_send_error(exc: Exception) -> str:
        """Classify errors from the registry call for DLQ triage."""
        msg = str(exc).lower()
        if any(code in msg for code in ("timeout", "connect", "503", "502", "429")):
            return "transient"
        return "permanent"

    async def _fail_run(
        self,
        run: IngestionRun,
        exc: Exception,
        connector: ConnectorDefinition,
        source_event_id: str | None,
        raw_data: dict[str, Any],
        session: AsyncSession,
        *,
        connector_id: str | None = None,
        error_category: str = "permanent",
        t_pipeline_start: float | None = None,
        stage: str = "ingest",
    ) -> IngestionRun:
        cid = connector_id or connector.connector_id
        _logger.error(
            "Ingestion failed",
            extra={
                "connector_id": cid,
                "source_event_id": source_event_id,
                "error_category": error_category,
                "error": str(exc)[:500],
            },
            exc_info=True,
        )
        run.status = RunStatus.FAILED
        run.last_error = str(exc)[:2000]
        log_event(
            stage, f"{stage}_failed", "ERROR",
            connector_id=cid, source_event_id=source_event_id, run_id=run.run_id,
            error_category=error_category,
            error_type=type(exc).__name__, error=str(exc)[:1000],
            http_status=getattr(getattr(exc, "response", None), "status_code", None),
            attempt=run.attempt_count,
            outcome="dead_lettered",
            retryable=error_category == "transient",
            next_step="replay from the connector DLQ once the cause is fixed",
        )
        if not run.run_payload_json:
            self._attach_run_payload(run, source=raw_data, mapped=None, outbound=None)

        session.add(
            DeadLetterEntry(
                connector_id=cid,
                source_event_id=source_event_id,
                payload=raw_data,
                error=str(exc)[:2000],
                error_category=error_category,
                attempt_count=run.attempt_count,
            )
        )
        await session.flush()
        connector_metrics.dlq_entries_total.labels(
            connector_id=cid,
        ).inc()
        if t_pipeline_start is not None:
            connector_metrics.ingest_duration.labels(
                connector_id=cid,
            ).observe(time.perf_counter() - t_pipeline_start)
        return run
