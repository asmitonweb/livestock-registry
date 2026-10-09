"""Structured per-submission ingestion log, registry side.

The same JSON-lines format as the connector's (openg2p_connector_service.ingest_log),
on the same ``odk.ingest`` logger name, so the two files join on ``source_event_id``
(the connector's message id) and a submission can be followed from the poll to
the registry:

    jq 'select(.source_event_id == "<form_id>:<instance_id>")' odk-ingest.jsonl

Configured lazily on the first event, from the environment:

    REGISTRY_EXTENSIONS_ODK_INGEST_LOG_FILE   default logs/odk-ingest.jsonl ("" = stdout only)

Events carry ids, file names, sizes, mime types, counts and error text only.
The submission body, file contents and credentials are never logged: keys that
name them are dropped, and long strings are cut.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

INGEST_LOGGER_NAME = "odk.ingest"

_logger = logging.getLogger(INGEST_LOGGER_NAME)
_configured = False

# Fields a caller must not be able to put in the log, even by mistake.
_DENIED_KEYS = frozenset({
    "payload", "data", "body", "content", "raw", "base64",
    "password", "secret", "token", "authorization", "headers",
})
_MAX_VALUE_LEN = 1000

_LEVELS = {"INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= _MAX_VALUE_LEN else value[:_MAX_VALUE_LEN] + "..."
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple, set)):
        return [_clean(item) for item in list(value)[:50]]
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items() if str(k).lower() not in _DENIED_KEYS}
    return _clean(str(value))


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = dict(getattr(record, "ingest", None) or {})
        entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            **event,
        }
        if record.exc_info:
            entry["exception"] = _clean(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure(
    path: str = "",
    *,
    service: str = "registry",
    max_bytes: int = 20 * 1024 * 1024,
    backups: int = 10,
) -> None:
    """Send ingestion events to stdout and, when *path* is set, a rotating file.

    Safe to call more than once. A file that cannot be opened is reported once
    on stdout and the log carries on there: logging must not stop the poll.
    """
    global _configured
    if _configured:
        return
    _configured = True

    formatter = JsonLineFormatter()
    _logger.setLevel(logging.INFO)
    # Own handlers only: the lines are already JSON and would otherwise be
    # written a second time by the root handler in a different format.
    _logger.propagate = False

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    _logger.addHandler(stream)

    if path:
        try:
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8",
            )
            handler.setFormatter(formatter)
            _logger.addHandler(handler)
        except OSError as exc:
            log_event(
                "logging", "ingest_log_file_unavailable", "WARNING",
                service=service, path=path, error=str(exc),
            )


def log_event(stage: str, event: str, severity: str = "INFO", **fields: Any) -> None:
    """Write one event. *stage* groups events (enrich, files, ingest, ...)."""
    if not _configured:
        configure(os.environ.get(
            "REGISTRY_EXTENSIONS_ODK_INGEST_LOG_FILE", "logs/odk-ingest.jsonl"
        ))
    severity = severity.upper()
    entry: dict[str, Any] = {"severity": severity, "stage": stage, "event": event}
    for key, value in fields.items():
        if key.lower() in _DENIED_KEYS or value is None:
            continue
        entry[key] = _clean(value)
    _logger.log(_LEVELS.get(severity, logging.INFO), event, extra={"ingest": entry})


_FILE_NAME = re.compile(r"\.(jpe?g|png|gif|webp|pdf)$", re.IGNORECASE)


def summarize_files(value: Any) -> tuple[list[dict], list[str]]:
    """Split the files in a submission into those that arrived and those that did not.

    Returns ``(embedded, bare_names)``: embedded ``{"__type": "File"}`` values as
    name / type / decoded size, and plain strings that look like a file name
    (an attachment the connector did not, or could not, download). Names only;
    the file contents are never returned.
    """
    embedded: list[dict] = []
    bare: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("__type") == "File":
                data = node.get("data") or ""
                embedded.append({
                    "file_name": node.get("name"),
                    "mime_type": node.get("type"),
                    "size_bytes": max(0, len(data) * 3 // 4 - data[-2:].count("=")),
                })
                return
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str) and _FILE_NAME.search(node.strip()):
            bare.append(node.strip())

    walk(value)
    return embedded, bare
