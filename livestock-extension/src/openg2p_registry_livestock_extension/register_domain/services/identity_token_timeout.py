"""Call-scoped timeout for the pinned native IAM token exchange.

iam_core 1.4.2 builds AsyncOAuth2Client without a timeout (httpx 5 s);
eSignet's token call can take longer. IAM has no client-factory/timeout argument. Reuse its unchanged
exchange function with an isolated globals dictionary containing a call-owned
OAuth client factory. No IAM module, class or shared adapter is reassigned,
and the native assertion, PKCE and token-response behavior is preserved.
Retire this bridge when IAM exposes a client factory or timeout parameter.
"""

import logging
import math
from importlib.metadata import version
from types import FunctionType

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from iam_core.user_auth.oidc_client import OidcClient

_NATIVE_TIMEOUT_SUPPORTED = (
    "AsyncOAuth2Client" in OidcClient.exchange_code_for_token.__code__.co_names
)
if not _NATIVE_TIMEOUT_SUPPORTED:
    logging.getLogger(__name__).warning(
        "iam_core %s token exchange no longer builds AsyncOAuth2Client; OAN token-timeout bridge is disabled and native defaults apply. Review the IAM upgrade.",
        version("iam-core"),
    )


def token_timeout_seconds(provider):
    raw = (provider.provider_config or {}).get("token_timeout_seconds", 30)
    try:
        value = float(raw)
    except (ValueError, TypeError):
        value = 0
    if isinstance(raw, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(
            "Fayda provider: token_timeout_seconds must be a positive number of seconds. Ask the administrator to correct it"
        )
    return value


def timeout_message(seconds):
    return (
        f"Fayda (eSignet) did not answer within {seconds:g} seconds. Verify again; "
        "if it keeps happening, ask the administrator to check eSignet or raise token_timeout_seconds."
    )


class TokenExchangeTimeout(httpx.TimeoutException):
    pass


async def exchange_with_timeout(client, seconds, **kwargs):
    native = OidcClient.exchange_code_for_token
    if not _NATIVE_TIMEOUT_SUPPORTED:
        return await native(client, **kwargs)
    clients = []

    def factory(*args, **options):
        options["timeout"] = seconds
        oauth = AsyncOAuth2Client(*args, **options)
        clients.append(oauth)
        return oauth

    exchange = FunctionType(
        native.__code__,
        {**native.__globals__, "AsyncOAuth2Client": factory},
        native.__name__,
        native.__defaults__,
        native.__closure__,
    )
    exchange.__kwdefaults__ = native.__kwdefaults__
    try:
        return await exchange(client, **kwargs)
    except httpx.TimeoutException as error:
        raise TokenExchangeTimeout(timeout_message(seconds)) from error
    finally:
        for oauth in clients:
            await oauth.aclose()
