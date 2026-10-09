"""The ODK ingestion log events and the embedded-file normaliser.

Loads ingest_log.py, odk_ingest_events.py and register_domain/services/
embedded_files.py straight from the extension, with the platform modules they
import stubbed, so it runs on a bare checkout (and in CI) without the registry
platform installed.
"""

import asyncio
import base64
import importlib.util
import json
import logging
import pathlib
import sys
import types

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
EXTENSION = next((REPO).glob("*-extension/src/openg2p_registry_*_extension"))
PACKAGE = "odk_ext_under_test"


def _load(relative, name):
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", EXTENSION / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Status:
    PENDING = types.SimpleNamespace(value="PENDING")
    PROCESSED = types.SimpleNamespace(value="PROCESSED")
    FAILED = types.SimpleNamespace(value="FAILED")


class _Row:
    def __init__(self, **fields):
        defaults = dict(
            intake_form_id="form-1", register_id="reg-1", partner_id="partner-1",
            ingestion_number_of_attempts=1, intake_form_submission_id=None,
            ingestion_latest_error_code=None,
        )
        defaults.update(fields)
        self.__dict__.update(defaults)


@pytest.fixture
def modules(monkeypatch, tmp_path):
    """The three modules, with the platform stubbed and the log captured."""
    uploads = []

    class _Handler:
        def upload(self, data, length, bucket, content_type):
            uploads.append({"bytes": data.read(), "content_type": content_type})
            return "store-1"

    class _Session:
        def __init__(self, store):
            self.store = store

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def add(self, row):
            self.store["added"] = row

        async def commit(self):
            pass

    stubs = {
        "openg2p_fastapi_common": types.ModuleType("openg2p_fastapi_common"),
        "openg2p_fastapi_common.context": types.SimpleNamespace(dbengine=types.SimpleNamespace(get=lambda: None)),
        "openg2p_registry_core": types.ModuleType("openg2p_registry_core"),
        "openg2p_registry_core.config": types.SimpleNamespace(
            Settings=types.SimpleNamespace(get_config=lambda strict=False: None)
        ),
        "openg2p_registry_core.helpers": types.ModuleType("openg2p_registry_core.helpers"),
        "openg2p_registry_core.helpers.document": types.SimpleNamespace(get_document_handler=lambda: _Handler()),
        "openg2p_registry_core.helpers.file_validation": types.SimpleNamespace(validate_file_bytes=None),
        "openg2p_registry_core.helpers.file_validation_profiles": types.SimpleNamespace(
            get_upload_validation_profile=lambda bucket, config: None
        ),
        "openg2p_registry_core.models": types.SimpleNamespace(
            G2PRegistryDocument=lambda **kw: types.SimpleNamespace(**kw),
            IncomingClassifiedData=object,
            ProcessStatusEnum=_Status,
        ),
        "openg2p_registry_core.models.enum": types.SimpleNamespace(
            DocumentBucket=types.SimpleNamespace(DOCUMENTS="documents")
        ),
    }
    for name, module in stubs.items():
        monkeypatch.setitem(sys.modules, name, module)
    store = {}
    sqlalchemy_asyncio = types.SimpleNamespace(async_sessionmaker=lambda *a, **k: (lambda: _Session(store)))
    monkeypatch.setitem(sys.modules, "sqlalchemy.ext.asyncio", sqlalchemy_asyncio)

    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(EXTENSION)]
    monkeypatch.setitem(sys.modules, PACKAGE, package)
    for sub in ("register_domain", "register_domain.services"):
        stub = types.ModuleType(f"{PACKAGE}.{sub}")
        stub.__path__ = [str(EXTENSION / sub.replace(".", "/"))]
        monkeypatch.setitem(sys.modules, f"{PACKAGE}.{sub}", stub)

    monkeypatch.setenv("REGISTRY_EXTENSIONS_ODK_INGEST_LOG_FILE", str(tmp_path / "odk-ingest.jsonl"))
    ingest_log = _load("ingest_log.py", "ingest_log")
    events = _load("odk_ingest_events.py", "odk_ingest_events")
    embedded = _load("register_domain/services/embedded_files.py", "register_domain.services.embedded_files")
    yield types.SimpleNamespace(
        ingest_log=ingest_log, events=events, embedded=embedded, uploads=uploads, store=store,
        log=lambda: [json.loads(line) for line in (tmp_path / "odk-ingest.jsonl").read_text().splitlines()],
    )
    logger = logging.getLogger(ingest_log.INGEST_LOGGER_NAME)
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def _session_maker(row):
    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, _model, _ingest_id):
            return row

    return _Session


PHOTO = {"__type": "File", "name": "cow.jpg", "type": "image/jpeg", "data": base64.b64encode(b"jpeg").decode()}


def test_received_submission_reports_inline_and_name_only_files(modules):
    payload = {"__id": "uuid:1", "photo": PHOTO, "other": {"certificate": "deed.jpg"}}
    modules.events.log_received("ingest-1", payload)
    lines = modules.log()
    received = next(line for line in lines if line["event"] == "submission_received")
    assert received["instance_id"] == "uuid:1"
    assert (received["files_embedded"], received["files_name_only"]) == (1, 1)
    assert received["severity"] == "WARNING"
    assert [line["file_name"] for line in lines if line["event"] == "file_missing"] == ["deed.jpg"]
    assert PHOTO["data"] not in json.dumps(lines)


@pytest.mark.parametrize(
    "row, error, event, severity",
    [
        (_Row(ingestion_status="PROCESSED", intake_form_submission_id="sub-1"), None, "ingest_succeeded", "INFO"),
        (_Row(ingestion_status="PENDING", ingestion_latest_error_code="db timeout"), ValueError("db timeout"),
         "ingest_retry_scheduled", "WARNING"),
        (_Row(ingestion_status="FAILED", ingestion_latest_error_code="Fayda ID must be 16 digits"),
         ValueError("Fayda ID must be 16 digits"), "ingest_failed", "ERROR"),
    ],
)
def test_ingest_outcome_is_read_back_from_the_row(modules, row, error, event, severity):
    asyncio.run(modules.events.log_ingest_outcome("ingest-1", _session_maker(row), error=error))
    line = modules.log()[-1]
    assert (line["event"], line["severity"], line["ingest_id"]) == (event, severity, "ingest-1")
    if event != "ingest_succeeded":
        assert line["error"] == row.ingestion_latest_error_code
        assert line["next_step"]


def test_transform_failure_is_logged_with_the_error(modules):
    modules.events.log_transform_failed("ingest-1", KeyError("planning"), "fix the template")
    line = modules.log()[-1]
    assert (line["event"], line["error_type"], line["next_step"]) == ("transform_failed", "KeyError", "fix the template")


def test_embedded_photo_is_uploaded_and_replaced_by_its_document_id(modules):
    record = {"record_image_document_id": dict(PHOTO), "created_by": "odk"}
    asyncio.run(modules.embedded.persist_embedded_files(record, ("record_image_document_id",), purpose="test"))
    assert isinstance(record["record_image_document_id"], str)
    assert modules.uploads == [{"bytes": b"jpeg", "content_type": "image/jpeg"}]
    stored = next(line for line in modules.log() if line["event"] == "file_stored")
    assert stored["document_id"] == record["record_image_document_id"]


def test_a_plain_value_is_left_alone(modules):
    record = {"record_image_document_id": "doc-existing"}
    asyncio.run(modules.embedded.persist_embedded_files(record, ("record_image_document_id",), purpose="test"))
    assert record["record_image_document_id"] == "doc-existing"
    assert modules.uploads == []


def test_an_undecodable_photo_fails_the_save_and_is_logged(modules):
    record = {"record_image_document_id": dict(PHOTO, data="not base64!!")}
    with pytest.raises(ValueError):
        asyncio.run(modules.embedded.persist_embedded_files(record, ("record_image_document_id",), purpose="test"))
    rejected = next(line for line in modules.log() if line["event"] == "file_rejected")
    assert rejected["outcome"] == "save_refused"
