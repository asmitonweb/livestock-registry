"""The ingestion log is JSON lines and never carries submission data."""

from __future__ import annotations

import json
import logging

from openg2p_connector_service import ingest_log


def test_event_is_one_json_object_per_line(tmp_path):
    path = tmp_path / "logs" / "odk-ingest.jsonl"
    logger = logging.getLogger(ingest_log.INGEST_LOGGER_NAME)
    saved = (list(logger.handlers), logger.propagate, ingest_log._configured)
    try:
        ingest_log._configured = False
        ingest_log.configure(str(path), service="test")
        ingest_log.log_event(
            "attachments", "attachment_too_large", "WARNING",
            source_event_id="form:uuid:1", file_name="deed.jpg", size_bytes=12,
            payload={"first_name": "Desta"}, data="AAAA", password="x", skipped=None,
        )
        for handler in logger.handlers:
            handler.flush()
    finally:
        for handler in logger.handlers:
            if handler not in saved[0]:
                handler.close()
        logger.handlers[:] = saved[0]
        logger.propagate, ingest_log._configured = saved[1], saved[2]

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["event"] == "attachment_too_large"
    assert entry["severity"] == "WARNING" and entry["level"] == "WARNING"
    assert entry["source_event_id"] == "form:uuid:1"
    assert entry["size_bytes"] == 12
    assert "ts" in entry
    for forbidden in ("payload", "data", "password", "skipped"):
        assert forbidden not in entry
    assert "Desta" not in lines[0]


def test_long_values_are_cut():
    assert len(ingest_log._clean("x" * 5000)) < 1100


def test_unwritable_file_does_not_stop_logging(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    logger = logging.getLogger(ingest_log.INGEST_LOGGER_NAME)
    saved = (list(logger.handlers), logger.propagate, ingest_log._configured)
    try:
        ingest_log._configured = False
        # A path under a regular file cannot be created.
        ingest_log.configure(str(blocker / "x" / "log.jsonl"), service="test")
        ingest_log.log_event("poll", "poll_started")
    finally:
        for handler in logger.handlers:
            if handler not in saved[0]:
                handler.close()
        logger.handlers[:] = saved[0]
        logger.propagate, ingest_log._configured = saved[1], saved[2]
