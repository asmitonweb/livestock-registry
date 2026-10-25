"""Canonical client-assertion audience is distinct from local transport."""

import pytest

pytest.importorskip("openg2p_registry_core")
from iam_core.user_auth.helpers.client_assertion_helper import _jwt_payload
from openg2p_registry_core.models import G2PRegistrantAuthenticationProvider
from openg2p_registry_livestock_extension.register_domain.services.identity_verification import (
    OANRegistrantAuthenticationService,
)

HTTP = "http://esignet.example/v1/esignet/oauth/v2/token"
HTTPS = "https://esignet.example/v1/esignet/oauth/v2/token"


def provider(config):
    return G2PRegistrantAuthenticationProvider(
        provider_id="esignet",
        provider_name="Fayda",
        adapter_name="esignet",
        token_endpoint=HTTP,
        client_id="existing-client",
        oauth_callback_url="http://registry.example/registrant-auth/callback",
        token_endpoint_auth_method="private_key_jwt",
        provider_config=config,
    )


def test_canonical_audience_is_used_by_native_iam_without_changing_transport():
    login = OANRegistrantAuthenticationService._provider_to_login_provider(
        provider({"jwt_assertion_aud": HTTPS})
    )
    assert login.token_endpoint == HTTP
    assert _jwt_payload(login)["aud"] == HTTPS


def test_unconfigured_provider_keeps_native_default():
    login = OANRegistrantAuthenticationService._provider_to_login_provider(provider({}))
    assert _jwt_payload(login)["aud"] == HTTP


@pytest.mark.parametrize("value", ["", " ", 123, []])
def test_invalid_audience_has_an_actionable_error(value):
    with pytest.raises(Exception) as error:
        OANRegistrantAuthenticationService._provider_to_login_provider(
            provider({"jwt_assertion_aud": value})
        )
    assert "jwt_assertion_aud" in str(error.value.message)
