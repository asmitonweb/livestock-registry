"""Unverified manual fallback never becomes a trusted verification."""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("openg2p_registry_core")
from openg2p_registry_livestock_extension.register_domain.services import (
    identity_verification as iv,
)
from openg2p_registry_livestock_extension.register_domain.services.identity_token_timeout import (
    TokenExchangeTimeout,
    timeout_message,
)
from test_identity_verification import _auth, _check, _service
from test_popup_fin import fixture

FIN_FIELD = "fayda_fin"
FIN = "001234567890"


def test_manual_fin_stays_unverified_and_normalized():
    service, session = _service(_auth())
    record = {FIN_FIELD: " " + FIN + " ", "last_authentication_status": "SUCCESS"}
    assert _check(service, [record]) == []
    assert record[FIN_FIELD] == FIN
    assert record["last_authentication_status"] is None
    assert session.added == []


@pytest.mark.parametrize(
    "value", ["12", "12345678901x", "１２３４５６７８９０１２", 123456789012]
)
def test_bad_manual_fin_cannot_bypass_server_rules(value):
    service, _ = _service(_auth())
    assert "Fayda ID" in _check(service, [{FIN_FIELD: value}])[0]


def test_failed_verification_can_use_manual_unverified_fin():
    service, session = _service(_auth(status="FAILURE"))
    record = {
        FIN_FIELD: FIN,
        "identity_verification_id": "v-1",
        "last_authentication_status": "SUCCESS",
    }
    assert _check(service, [record]) == []
    assert record["identity_verification_id"] is None
    assert record["last_authentication_status"] is None
    assert session.added == []


@pytest.mark.parametrize("value", [None, "", "999999999999"])
def test_successful_fin_cannot_be_cleared_or_changed(value):
    service, session = _service(
        _auth(internal_record_id="r-1"),
        stored={
            "r-1": {
                "identity_verification_id": "v-1",
                FIN_FIELD: FIN,
                "last_authentication_status": "SUCCESS",
            }
        },
    )
    assert _check(service, [{"internal_record_id": "r-1", FIN_FIELD: value}])
    assert not session.committed


def test_unverified_manual_id_does_not_constrain_later_esignet_identity():
    service = object.__new__(iv.OANRegistrantAuthenticationService)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    service._session = lambda: Session()

    async def stored(*args):
        return {FIN_FIELD: FIN, "last_authentication_status": None}

    async def providers(**kwargs):
        return [SimpleNamespace(provider_id="esignet")]

    async def start(**kwargs):
        holder = iv._extra_context.get()
        assert holder["context"]["foundational_id"] == ""
        assert holder["context"]["oan_bind_verified_fin"]
        assert holder["context"]["oan_unverified_fallback"]
        holder["transaction"] = SimpleNamespace(context={"authentication_id": "auth"})
        return "state", "https://esignet.example/authorize", "Fayda"

    service._stored, service.get_available_providers, service.start_authentication = (
        stored,
        providers,
        start,
    )
    asyncio.run(
        service.start_identity_verification(
            register_id=getattr(service, "REGISTER_ID", "register"),
            internal_record_id="r-1",
            staff_id="staff",
        )
    )


@pytest.mark.parametrize("bind", [True, False])
def test_token_timeout_is_explained_and_never_stores_claims(monkeypatch, bind):
    service, auth, adapter, _tx, _store = fixture(
        monkeypatch, expected=FIN if not bind else "", bind=bind
    )

    async def timeout(**kwargs):
        raise TokenExchangeTimeout(timeout_message(30))

    adapter.exchange_code_for_token = timeout
    asyncio.run(
        service.complete_authentication(state="state", authorization_code="code")
    )
    assert auth.status == "FAILURE" and auth.user_claims is None
    assert (
        "30 seconds" in auth.failure_reason
        and "token_timeout_seconds" in auth.failure_reason
    )
    assert "30 seconds" in iv.explain_failure(auth.failure_reason)


def test_fan_is_not_silently_stored_as_a_fin():
    service, _ = _service(_auth())
    assert (
        "separate 16-digit FAN" in _check(service, [{FIN_FIELD: "0012345678901234"}])[0]
    )
