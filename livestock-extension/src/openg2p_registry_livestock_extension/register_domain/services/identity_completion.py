"""Request-local replay for native completion after verified-subject binding.

An authorization code is single-use. The no-FIN path validates it with the
native adapter first, then core receives the same transaction and token response
once. All token/claim checks and final persistence still run in core. Nothing is
put back into Redis and replay state is discarded at the end of this request.
"""

from contextvars import ContextVar

from iam_core.user_auth.oidc_client import OidcClient

from .identity_token_timeout import exchange_with_timeout

completion_context: ContextVar[dict | None] = ContextVar(
    "oan_fin_completion", default=None
)


class ReplayAdapter:
    def __init__(self, inner):
        self.inner = inner

    async def exchange_code_for_token(self, **kwargs):
        context = completion_context.get()
        if context and context.get("token_response") is not None:
            if (
                kwargs.get("code") != context["code"]
                or str(kwargs["login_provider"].id) != context["provider_id"]
            ):
                raise ValueError(
                    "Fayda verification: the completion context changed. Verify again"
                )
            return context["token_response"]
        timeout = getattr(kwargs["login_provider"], "oan_token_timeout_seconds", None)
        if timeout is not None and isinstance(getattr(self.inner, "oidc_client", None), OidcClient):
            return await exchange_with_timeout(self.inner.oidc_client, timeout, **kwargs)
        return await self.inner.exchange_code_for_token(**kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class ReplayAdapters:
    def __init__(self, inner):
        self.inner = inner

    def resolve_for_provider(self, provider):
        return ReplayAdapter(self.inner.resolve_for_provider(provider))

    def __getattr__(self, name):
        return getattr(self.inner, name)
