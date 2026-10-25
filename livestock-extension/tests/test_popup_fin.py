"""Popup start validates FIN and keeps the native transaction contract."""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("openg2p_registry_core")
from openg2p_registry_livestock_extension.register_domain.services import (
    identity_verification as iv,
)


def service():
    return object.__new__(iv.OANRegistrantAuthenticationService)


@pytest.mark.parametrize("fin", ["", "   ", "123", "12345678901x", "1234567890123456"])
def test_blank_or_malformed_fin_fails_before_provider_lookup(fin):
    with pytest.raises(iv.G2PRegistryException) as error:
        asyncio.run(
            service().start_identity_verification(
                register_id=iv.OANRegistrantAuthenticationService.REGISTER_ID,
                foundational_id=fin,
                internal_record_id=None,
                staff_id="staff",
            )
        )
    assert "FIN (Fayda ID):" in error.value.message
    assert "window" in error.value.message


def test_start_normalizes_fin_and_returns_native_authentication_id():
    instance = service()

    async def providers(**kwargs):
        return [SimpleNamespace(provider_id="esignet")]

    async def start(**kwargs):
        holder = iv._extra_context.get()
        assert holder["context"]["foundational_id"] == "123456789012"
        holder["transaction"] = SimpleNamespace(
            context={"authentication_id": "native-auth"}
        )
        return "state", "https://esignet.example/authorize", "Fayda"

    instance.get_available_providers = providers
    instance.start_authentication = start
    result = asyncio.run(
        instance.start_identity_verification(
            register_id=iv.OANRegistrantAuthenticationService.REGISTER_ID,
            foundational_id=" 123456789012 ",
            internal_record_id=None,
            staff_id="staff",
        )
    )
    assert result["verification_id"] == "native-auth"
    assert result["authorization_url"] == "https://esignet.example/authorize"
    assert iv._extra_context.get() is None
