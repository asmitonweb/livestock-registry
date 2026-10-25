"""The native exchange gets a call-local timeout without shared mutation."""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("openg2p_registry_core")
from iam_core.schemas import TokenEndpointAuthMethod
from iam_core.user_auth.oidc_client import OidcClient
from openg2p_registry_livestock_extension.register_domain.services import (
    identity_token_timeout as timeout,
)


@pytest.mark.parametrize("raw", [0, -1, "no", None, True, float("inf"), float("nan")])
def test_bad_timeout_config_names_the_setting(raw):
    with pytest.raises(ValueError, match="token_timeout_seconds"):
        timeout.token_timeout_seconds(
            SimpleNamespace(provider_config={"token_timeout_seconds": raw})
        )


def test_default_and_custom_timeout():
    assert timeout.token_timeout_seconds(SimpleNamespace(provider_config={})) == 30
    assert (
        timeout.token_timeout_seconds(
            SimpleNamespace(provider_config={"token_timeout_seconds": 45})
        )
        == 45
    )


def test_isolated_native_client_timeout_pkce_and_cleanup(monkeypatch):
    calls = []
    original = OidcClient.exchange_code_for_token.__globals__["AsyncOAuth2Client"]

    class OAuth:
        def __init__(self, **kwargs):
            self.options = kwargs
            self.closed = False
            calls.append(self)

        async def fetch_token(self, url, **kwargs):
            assert url == "https://esignet.example/token"
            assert kwargs["code"] == "single-use" and kwargs["code_verifier"] == "pkce"
            await asyncio.sleep(0)
            return {"id_token": "fixture"}

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(timeout, "AsyncOAuth2Client", OAuth)
    login = SimpleNamespace(
        client_id="registry",
        client_secret="fixture",
        token_endpoint_auth_method=TokenEndpointAuthMethod.client_secret_post,
        oauth_callback_url="http://registry.example/callback",
        enable_pkce=True,
    )

    async def run():
        return await asyncio.gather(
            *(
                timeout.exchange_with_timeout(
                    OidcClient(),
                    seconds,
                    login_provider=login,
                    code="single-use",
                    code_verifier="pkce",
                    server_metadata={"token_endpoint": "https://esignet.example/token"},
                )
                for seconds in [15, 40]
            )
        )

    assert asyncio.run(run()) == [{"id_token": "fixture"}] * 2
    assert sorted(c.options["timeout"] for c in calls) == [15, 40]
    assert all(c.closed for c in calls)
    assert (
        OidcClient.exchange_code_for_token.__globals__["AsyncOAuth2Client"] is original
    )


def test_pinned_iam_timeout_bridge_assumption_is_still_supported():
    assert "AsyncOAuth2Client" in OidcClient.exchange_code_for_token.__code__.co_names


def test_unsupported_iam_bridge_falls_back_to_native(monkeypatch):
    monkeypatch.setattr(timeout, "_NATIVE_TIMEOUT_SUPPORTED", False)

    async def native(self, **kwargs):
        assert kwargs["code"] == "single-use"
        return {"id_token": "native-fallback"}

    monkeypatch.setattr(OidcClient, "exchange_code_for_token", native)
    assert asyncio.run(
        timeout.exchange_with_timeout(OidcClient(), 30, code="single-use")
    ) == {"id_token": "native-fallback"}
