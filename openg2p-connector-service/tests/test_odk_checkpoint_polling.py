"""Behavioural tests for the ODK transport's enterprise checkpoint flow.

Covers:
* Default incremental mode emits TIMESTAMP checkpoints with boundary ids.
* Same-watermark records already in ``boundary_ids`` are skipped.
* A 501 from the configured incremental field raises under
  ``strict_incremental=True`` and downgrades to a full scan when
  explicitly opted in.

The tests stub HTTPX so we never touch the network.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from openg2p_connector_service.auth.base import AuthContext
from openg2p_connector_service.config import get_settings
from openg2p_connector_service.transports import (
    Checkpoint,
    CheckpointMode,
    get_transport,
)
from openg2p_connector_service.transports.odk_central import (
    OdkCentralTransport,
    StrictIncrementalUnsupported,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

class FakeConnector:
    """Minimal stand-in for ConnectorDefinition.

    The transport only reaches into ``get_source_config`` /
    ``get_poll_state`` / a couple of attributes, so we don't need a
    full ORM instance.
    """

    def __init__(
        self,
        *,
        source_config: dict[str, Any],
        poll_state: dict[str, Any] | None = None,
    ):
        self.connector_id = "conn-test"
        self.auth_type = "odk_session"
        self._source = source_config
        self._poll_state = poll_state or {}

    def get_source_config(self) -> dict[str, Any]:
        merged = dict(self._source)
        merged.update(self._poll_state)
        return merged

    def get_poll_state(self) -> dict[str, Any]:
        return dict(self._poll_state)


class _FakeAuth:
    async def get_auth_context(self, _connector):
        return AuthContext(headers={}, base_url=None)


@pytest.fixture(autouse=True)
def _stub_auth(monkeypatch):
    """Avoid real ODK session login; transports just need an empty context."""
    monkeypatch.setattr(
        "openg2p_connector_service.transports.odk_central.get_auth_strategy",
        lambda _name: _FakeAuth(),
    )


@pytest.fixture(autouse=True)
def _enterprise_strict_default(monkeypatch):
    """Force strict_incremental defaults so tests don't depend on host env."""
    settings = get_settings()
    monkeypatch.setattr(settings, "strict_incremental", True, raising=False)
    monkeypatch.setattr(
        settings, "full_scan_on_incremental_unsupported", False, raising=False
    )


def _http_response(
    status: int,
    *,
    body: dict | None = None,
    request_url: str = "http://test/odk",
) -> httpx.Response:
    req = httpx.Request("GET", request_url)
    return httpx.Response(
        status_code=status,
        json=body if body is not None else {},
        request=req,
    )


def _patch_client_get(monkeypatch, sequence: list[httpx.Response]):
    """Make ``httpx.AsyncClient.get`` yield *sequence* in order."""
    queue = list(sequence)
    calls: list[dict] = []

    async def _fake_get(self, url, *, headers=None, params=None):
        calls.append({"url": url, "params": dict(params or {})})
        if not queue:
            raise AssertionError("Unexpected extra ODK request")
        return queue.pop(0)

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)
    return calls


# ---------------------------------------------------------------------------
# happy path: timestamp + boundary ids
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timestamp_mode_emits_structured_checkpoint(monkeypatch):
    body = {
        "value": [
            {
                "__id": "uuid:r1",
                "__system": {"submissionDate": "2026-04-19T10:00:00.000Z"},
                "name": "alice",
            },
            {
                "__id": "uuid:r2",
                "__system": {"submissionDate": "2026-04-19T10:00:01.000Z"},
                "name": "bob",
            },
        ]
    }
    _patch_client_get(monkeypatch, [_http_response(200, body=body)])

    transport: OdkCentralTransport = get_transport("odk_central")  # type: ignore[assignment]
    connector = FakeConnector(
        source_config={
            "base_url": "http://odk.test",
            "project_id": 4,
            "form_id": "f1",
            "embed_attachments": False,  # these tests are about the cursor
            "page_size": 10,
            "max_pages": 1,
        }
    )

    records = [r async for r in transport.fetch(connector)]
    assert len(records) == 2

    cps = [r.checkpoint for r in records]
    assert all(cp is not None and cp.mode is CheckpointMode.TIMESTAMP for cp in cps)
    assert cps[0].value == "2026-04-19T10:00:00.000Z"
    assert cps[0].boundary_ids == ["uuid:r1"]
    assert cps[1].value == "2026-04-19T10:00:01.000Z"


@pytest.mark.asyncio
async def test_boundary_ids_skip_already_seen_records_at_same_watermark(monkeypatch):
    body = {
        "value": [
            {
                "__id": "uuid:already",
                "__system": {"submissionDate": "2026-04-19T10:00:00.000Z"},
            },
            {
                "__id": "uuid:fresh",
                "__system": {"submissionDate": "2026-04-19T10:00:00.000Z"},
            },
        ]
    }
    _patch_client_get(monkeypatch, [_http_response(200, body=body)])

    prev_cp = Checkpoint(
        mode=CheckpointMode.TIMESTAMP,
        value="2026-04-19T10:00:00.000Z",
        boundary_ids=["uuid:already"],
    )
    connector = FakeConnector(
        source_config={
            "base_url": "http://odk.test",
            "project_id": 4,
            "form_id": "f1",
            "embed_attachments": False,  # these tests are about the cursor
            "page_size": 10,
            "max_pages": 1,
        },
        poll_state={"_checkpoint": prev_cp.to_dict()},
    )

    transport: OdkCentralTransport = get_transport("odk_central")  # type: ignore[assignment]
    records = [r async for r in transport.fetch(connector)]
    assert [r.source_event_id for r in records] == ["f1:uuid:fresh"]


# ---------------------------------------------------------------------------
# strict-fallback policy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_strict_incremental_raises_on_501(monkeypatch):
    _patch_client_get(monkeypatch, [_http_response(501)])
    transport: OdkCentralTransport = get_transport("odk_central")  # type: ignore[assignment]
    connector = FakeConnector(
        source_config={
            "base_url": "http://odk.test",
            "project_id": 4,
            "form_id": "f1",
            "embed_attachments": False,  # these tests are about the cursor
            "max_pages": 1,
            "strict_incremental": True,
            "full_scan_on_incremental_unsupported": False,
        }
    )

    with pytest.raises(StrictIncrementalUnsupported):
        async for _ in transport.fetch(connector):
            pass


@pytest.mark.asyncio
async def test_explicit_full_scan_opt_in_recovers_from_501(monkeypatch):
    body = {
        "value": [
            {"__id": "uuid:r1", "__system": {"submissionDate": "2026-04-19T10:00:00.000Z"}}
        ]
    }
    _patch_client_get(
        monkeypatch,
        [_http_response(501), _http_response(200, body=body)],
    )
    transport: OdkCentralTransport = get_transport("odk_central")  # type: ignore[assignment]
    connector = FakeConnector(
        source_config={
            "base_url": "http://odk.test",
            "project_id": 4,
            "form_id": "f1",
            "embed_attachments": False,  # these tests are about the cursor
            "max_pages": 1,
            "strict_incremental": True,
            "full_scan_on_incremental_unsupported": True,
        }
    )

    records = [r async for r in transport.fetch(connector)]
    assert [r.source_event_id for r in records] == ["f1:uuid:r1"]


# ---------------------------------------------------------------------------
# capability declaration
# ---------------------------------------------------------------------------

def test_capability_declaration_is_safe_default():
    from openg2p_connector_service.transports import (
        get_transport_capability,
        transport_registry,
    )

    cap = get_transport_capability("odk_central")
    assert CheckpointMode.TIMESTAMP in cap.modes
    assert cap.default_mode is CheckpointMode.TIMESTAMP
    assert cap.supports_strict_incremental is True

    # Webhook is push-based and exposes no poll modes.
    cap_w = get_transport_capability("webhook")
    assert cap_w.modes == ()

    # WebSub is also push-based; the hub calls the webhook endpoint.
    cap_ws = get_transport_capability("websub")
    assert cap_ws.modes == ()


def test_unknown_transport_raises():
    from openg2p_connector_service.transports import get_transport_capability

    with pytest.raises(ValueError):
        get_transport_capability("does-not-exist")


# ---------------------------------------------------------------------------
# legacy compat — sequence cursor still emits a checkpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sequence_mode_still_works_for_servers_that_support_id(monkeypatch):
    body = {
        "value": [
            {"__id": "uuid:r1", "__system": {"submissionDate": "now"}},
        ]
    }
    _patch_client_get(monkeypatch, [_http_response(200, body=body)])
    connector = FakeConnector(
        source_config={
            "base_url": "http://odk.test",
            "project_id": 4,
            "form_id": "f1",
            "embed_attachments": False,  # these tests are about the cursor
            "incremental_mode": "sequence",
            "max_pages": 1,
        }
    )

    transport: OdkCentralTransport = get_transport("odk_central")  # type: ignore[assignment]
    records = [r async for r in transport.fetch(connector)]
    assert records[0].checkpoint is not None
    assert records[0].checkpoint.mode is CheckpointMode.SEQUENCE
    assert records[0].checkpoint.value == "uuid:r1"


@pytest.mark.asyncio
async def test_sequence_cursor_is_round_tripped_to_filter(monkeypatch):
    body = {"value": []}
    calls = _patch_client_get(monkeypatch, [_http_response(200, body=body)])
    prev_cp = Checkpoint(mode=CheckpointMode.SEQUENCE, value="uuid:prev")
    connector = FakeConnector(
        source_config={
            "base_url": "http://odk.test",
            "project_id": 4,
            "form_id": "f1",
            "embed_attachments": False,  # these tests are about the cursor
            "incremental_mode": "sequence",
            "max_pages": 1,
        },
        poll_state={"_checkpoint": prev_cp.to_dict()},
    )

    transport: OdkCentralTransport = get_transport("odk_central")  # type: ignore[assignment]
    [_ async for _ in transport.fetch(connector)]
    assert calls[0]["params"]["$filter"] == "__id gt 'uuid:prev'"


# ---------------------------------------------------------------------------
# legacy persisted state — last_submission_id still respected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_legacy_last_submission_id_filters_in_sequence_mode(monkeypatch):
    body = {"value": []}
    calls = _patch_client_get(monkeypatch, [_http_response(200, body=body)])
    connector = FakeConnector(
        source_config={
            "base_url": "http://odk.test",
            "project_id": 4,
            "form_id": "f1",
            "embed_attachments": False,  # these tests are about the cursor
            "incremental_mode": "sequence",
            "max_pages": 1,
        },
        poll_state={"last_submission_id": "uuid:legacy"},
    )

    transport: OdkCentralTransport = get_transport("odk_central")  # type: ignore[assignment]
    [_ async for _ in transport.fetch(connector)]
    assert calls[0]["params"]["$filter"] == "__id gt 'uuid:legacy'"


# ---------------------------------------------------------------------------
# canonical OData params
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_poll_with_no_state_uses_orderby_only(monkeypatch):
    body = {"value": []}
    calls = _patch_client_get(monkeypatch, [_http_response(200, body=body)])
    connector = FakeConnector(
        source_config={
            "base_url": "http://odk.test",
            "project_id": 4,
            "form_id": "f1",
            "embed_attachments": False,  # these tests are about the cursor
            "max_pages": 1,
        }
    )

    transport: OdkCentralTransport = get_transport("odk_central")  # type: ignore[assignment]
    [_ async for _ in transport.fetch(connector)]
    params = calls[0]["params"]
    assert params["$orderby"] == "__system/submissionDate asc"
    # No prior checkpoint → no filter on the first poll.
    assert "$filter" not in params


@pytest.mark.asyncio
async def test_subsequent_poll_uses_ge_filter_against_watermark(monkeypatch):
    body = {"value": []}
    calls = _patch_client_get(monkeypatch, [_http_response(200, body=body)])
    prev_cp = Checkpoint(
        mode=CheckpointMode.TIMESTAMP,
        value="2026-04-19T10:00:00.000Z",
        boundary_ids=["uuid:already"],
    )
    connector = FakeConnector(
        source_config={
            "base_url": "http://odk.test",
            "project_id": 4,
            "form_id": "f1",
            "embed_attachments": False,  # these tests are about the cursor
            "max_pages": 1,
        },
        poll_state={"_checkpoint": prev_cp.to_dict()},
    )

    transport: OdkCentralTransport = get_transport("odk_central")  # type: ignore[assignment]
    [_ async for _ in transport.fetch(connector)]
    params = calls[0]["params"]
    assert (
        params["$filter"]
        == "__system/submissionDate ge 2026-04-19T10:00:00.000Z"
    )


# A handy assertion for future regressions: the JSON serializer must
# preserve enum values by their string form.
def test_checkpoint_dict_is_json_serializable():
    cp = Checkpoint(
        mode=CheckpointMode.TIMESTAMP,
        value="2026-04-19T10:00:00Z",
        boundary_ids=["uuid:1"],
    )
    blob = json.dumps(cp.to_dict())
    assert "timestamp" in blob
