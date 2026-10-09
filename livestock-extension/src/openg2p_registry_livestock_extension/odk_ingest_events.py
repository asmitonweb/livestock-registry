"""What the ODK ingestion hooks write to the ingestion log.

One line per step a submission takes in the registry, in the JSON-lines format
of ingest_log.py (the same as the connector's and the farmer registry's):

    submission_received   the instance, and which files came inline / as a name only
    file_missing          a photo that arrived as a file name only
    transformed           the template's sections and record counts
    transform_failed      the template could not render or parse
    ingest_succeeded      saved to the intake form
    ingest_retry_scheduled / ingest_failed
                          why it was not saved, the attempt, and what to do next

Every event carries ingest_id (incoming_classified_data.ingest_id), so a
submission can be followed with  jq 'select(.ingest_id == "<id>")'.
Nothing here may change the outcome of an ingest: every helper swallows its
own errors.
"""

import logging

from .ingest_log import log_event, summarize_files

_logger = logging.getLogger(__name__)

_NEXT_STEP_FAILED = (
    "gave up: fix the cause, then set incoming_classified_data.ingestion_status "
    "back to PENDING to retry"
)


def unwrap_payload(data):
    """The ODK submission inside a partner envelope ({"body": {"message": {"payload": ...}}})."""
    if isinstance(data, dict):
        if isinstance(data.get("body"), dict):
            return (data["body"].get("message") or {}).get("payload", data)
        if isinstance(data.get("message"), dict):
            return data["message"].get("payload", data)
    return data


def log_received(ingest_id, payload) -> None:
    try:
        embedded, bare = summarize_files(payload)
        instance_id = None
        if isinstance(payload, dict):
            meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
            instance_id = payload.get("__id") or meta.get("instanceID") or payload.get("instanceID")
        log_event(
            "transform", "submission_received", "WARNING" if bare else "INFO",
            ingest_id=ingest_id, instance_id=instance_id,
            files_embedded=len(embedded), files_name_only=len(bare),
        )
        for entry in embedded:
            log_event("transform", "file_received", ingest_id=ingest_id, **entry)
        for name in bare:
            log_event(
                "transform", "file_missing", "WARNING", ingest_id=ingest_id, file_name=name,
                reason="only the file name arrived; the connector did not embed the file "
                       "(not uploaded in ODK, over the size limit, download failed, or embedding is off)",
                outcome="saved_without_this_file",
            )
    except Exception as error:  # pragma: no cover - a log must not break ingestion
        _logger.warning("Could not log the received submission: %s", error)


def log_transformed(ingest_id, transformed) -> None:
    try:
        sections = {}
        if isinstance(transformed, dict):
            for name, value in transformed.items():
                sections[name] = len(value) if isinstance(value, list) else 1
        log_event("transform", "transformed", ingest_id=ingest_id, sections=sections)
    except Exception as error:  # pragma: no cover
        _logger.warning("Could not log the transform: %s", error)


def log_transform_failed(ingest_id, error, fallback: str) -> None:
    try:
        log_event(
            "transform", "transform_failed", "ERROR", ingest_id=ingest_id,
            error_type=type(error).__name__, error=str(error), next_step=fallback,
        )
    except Exception as log_error:  # pragma: no cover
        _logger.warning("Could not log the transform failure: %s", log_error)


async def log_ingest_outcome(ingest_id, session_maker, error=None) -> None:
    """Read the ingest row after an attempt and log how it ended.

    Reads the row rather than trusting *error*: the platform worker records a
    failure (PENDING to retry, FAILED when out of attempts) before it raises,
    and that row is what staff and a retry will see."""
    try:
        from openg2p_registry_core.models import IncomingClassifiedData, ProcessStatusEnum

        async with session_maker() as session:
            row = await session.get(IncomingClassifiedData, ingest_id)
            if row is None:
                log_event(
                    "ingest", "ingest_failed", "ERROR", ingest_id=ingest_id,
                    error=str(error) if error else "incoming_classified_data row not found",
                )
                return
            fields = dict(
                ingest_id=ingest_id,
                intake_form_id=row.intake_form_id,
                register_id=row.register_id,
                partner_id=row.partner_id,
                attempt=row.ingestion_number_of_attempts,
            )
            status = row.ingestion_status
            if status == ProcessStatusEnum.PROCESSED.value and error is None:
                log_event(
                    "ingest", "ingest_succeeded",
                    submission_id=row.intake_form_submission_id, **fields,
                )
                return
            message = row.ingestion_latest_error_code or (str(error) if error else None)
            if status == ProcessStatusEnum.PENDING.value:
                log_event(
                    "ingest", "ingest_retry_scheduled", "WARNING", error=message,
                    will_retry=True, next_step="the worker retries this ingest", **fields,
                )
            else:
                log_event(
                    "ingest", "ingest_failed", "ERROR", status=status, error=message,
                    error_type=type(error).__name__ if error else None,
                    will_retry=False, next_step=_NEXT_STEP_FAILED, **fields,
                )
    except Exception as log_error:  # pragma: no cover
        _logger.warning("Could not log the ingest outcome for %s: %s", ingest_id, log_error)


def log_request_failed(error) -> None:
    try:
        log_event(
            "partner_api", "ingest_request_failed", "ERROR",
            error_type=type(error).__name__, error=str(error),
            next_step="the connector logs this send as failed (send_failed) with the same error",
        )
    except Exception as log_error:  # pragma: no cover
        _logger.warning("Could not log the failed ingest request: %s", log_error)
