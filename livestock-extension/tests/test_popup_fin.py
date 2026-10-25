"""FIN is entered on eSignet, then bound through native completion."""

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("openg2p_registry_core")
from openg2p_registry_livestock_extension.register_domain.services import (
    identity_verification as iv,
)
from openg2p_registry_livestock_extension.register_domain.services.identity_completion import (
    ReplayAdapters,
    completion_context,
)

FIN = "001234567890"
OTHER = "009876543210"


class Session:
    def __init__(self, auth, provider):
        self.auth, self.provider = auth, provider

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, cls, key):
        if cls.__name__ == "G2PRegistrantAuthentication":
            return self.auth
        if cls.__name__ == "G2PRegistrantAuthenticationProvider":
            return self.provider
        return SimpleNamespace(
            registrant_authentication_validity_days=365, register_mnemonic="Livestock"
        )

    def add(self, value):
        pass

    async def commit(self):
        pass

    async def refresh(self, value):
        pass


def fixture(monkeypatch, subject=FIN, expected="", bind=True, bad_token=False):
    auth = SimpleNamespace(
        authentication_id="auth",
        provider_id="esignet",
        register_id="register",
        internal_record_id=iv.UNBOUND + "fixture",
        status="PENDING",
        user_claims=None,
    )
    provider = SimpleNamespace(keymanager_sign_app_id=None)
    session = Session(auth, provider)

    class Adapter:
        calls = 0

        async def exchange_code_for_token(self, **kwargs):
            self.calls += 1
            assert self.calls == 1, "The authorization code was exchanged twice"
            return {"id_token": "fixture-token", "access_token": "fixture-access"}

        async def validate_callback_id_token(self, **kwargs):
            if bad_token:
                raise ValueError("Invalid token fixture")

        async def decode_jwt(self, **kwargs):
            return {"individual_id": subject}

        async def enrich_claims_from_userinfo(self, claims, **kwargs):
            return claims

        def normalize_claims(self, claims, **kwargs):
            return claims

        def validate_claims(self, claims, **kwargs):
            pass

        def registrant_subject(self, claims, **kwargs):
            return claims["individual_id"]

        def get_authentication_method(self, *args, **kwargs):
            return "esignet"

        def get_claim_verifications(self, *args, **kwargs):
            return {}

    adapter = Adapter()
    adapter.oidc_client = adapter
    transaction = SimpleNamespace(
        context={
            "authentication_id": "auth",
            "provider_id": "esignet",
            "foundational_id": expected,
            "oan_bind_verified_fin": bind,
        },
        code_verifier="pkce",
        nonce="nonce",
    )

    class Store:
        item = transaction

        def get_and_pop(self, state):
            item = self.item
            self.item = None
            return item

    store = Store()
    service = object.__new__(iv.OANRegistrantAuthenticationService)
    service._session = lambda: session
    service._auth_tx_store = iv._ContextStore(store)
    service._adapters = ReplayAdapters(
        SimpleNamespace(resolve_for_provider=lambda provider: adapter)
    )
    service._provider_to_login_provider = lambda provider: SimpleNamespace(
        id="esignet", token_endpoint_auth_method="fixture"
    )
    service._crypto = SimpleNamespace(encrypt_json=json.dumps)
    monkeypatch.setattr(iv._core, "get_async_session_maker", lambda: lambda: session)
    return service, auth, adapter, transaction, store


def test_complete_without_fin_binds_verified_subject_and_core_stores_claims_once(
    monkeypatch,
):
    service, auth, adapter, tx, store = fixture(monkeypatch)
    result = asyncio.run(
        service.complete_authentication(state="state", authorization_code="code")
    )
    assert result.status == "SUCCESS"
    assert tx.context["foundational_id"] == FIN
    assert json.loads(auth.user_claims)["individual_id"] == FIN
    assert adapter.calls == 1 and store.item is None
    assert completion_context.get() is None


def test_a_sign_in_for_another_fin_than_the_typed_one_is_refused(monkeypatch):
    service, auth, adapter, tx, _store = fixture(monkeypatch, subject=OTHER)
    tx.context["oan_form_fin"] = FIN
    asyncio.run(
        service.complete_authentication(state="state", authorization_code="code")
    )
    assert auth.status == "FAILURE" and auth.user_claims is None
    assert f"a different FIN than {FIN} on the form" in auth.failure_reason
    assert OTHER not in auth.failure_reason


def test_a_sign_in_for_the_typed_fin_binds(monkeypatch):
    service, auth, _adapter, tx, _store = fixture(monkeypatch)
    tx.context["oan_form_fin"] = FIN
    result = asyncio.run(
        service.complete_authentication(state="state", authorization_code="code")
    )
    assert result.status == "SUCCESS" and tx.context["foundational_id"] == FIN


def test_reverify_saved_fin_mismatch_is_refused(monkeypatch):
    service, auth, adapter, _tx, _store = fixture(
        monkeypatch, subject=OTHER, expected=FIN, bind=False
    )
    asyncio.run(
        service.complete_authentication(state="state", authorization_code="code")
    )
    assert auth.status == "FAILURE" and auth.user_claims is None
    assert "does not match" in auth.failure_reason
    assert adapter.calls == 1


@pytest.mark.parametrize(
    "subject", ["", "123", "1234567890123456", "12345678901x", None, 123456789012]
)
def test_non_12_digit_verified_subject_cannot_bind(monkeypatch, subject):
    service, auth, _adapter, _tx, _store = fixture(monkeypatch, subject=subject)
    asyncio.run(
        service.complete_authentication(state="state", authorization_code="code")
    )
    assert auth.status == "FAILURE" and auth.user_claims is None
    assert "12-digit FIN" in auth.failure_reason


def test_failed_token_validation_does_not_bind_or_store_claims(monkeypatch):
    service, auth, _adapter, tx, _store = fixture(monkeypatch, bad_token=True)
    asyncio.run(
        service.complete_authentication(state="state", authorization_code="code")
    )
    assert auth.status == "FAILURE" and auth.user_claims is None
    assert tx.context["foundational_id"] == ""


def test_start_without_fin_and_browser_fin_is_not_trusted():
    service = object.__new__(iv.OANRegistrantAuthenticationService)

    async def providers(**kwargs):
        return [SimpleNamespace(provider_id="esignet")]

    async def start(**kwargs):
        holder = iv._extra_context.get()
        assert holder["context"]["foundational_id"] == ""
        assert holder["context"]["oan_bind_verified_fin"] is True
        assert holder["context"]["oan_form_fin"] in (None, OTHER)
        holder["transaction"] = SimpleNamespace(context={"authentication_id": "auth"})
        return "state", "https://esignet.example/authorize", "Fayda"

    service.get_available_providers = providers
    service.start_authentication = start
    for supplied in [None, "", OTHER]:
        result = asyncio.run(
            service.start_identity_verification(
                register_id=getattr(service, "REGISTER_ID", "register"),
                foundational_id=supplied,
                internal_record_id=None,
                staff_id="staff",
            )
        )
        assert result["verification_id"] == "auth"


def test_reverify_uses_saved_fin_instead_of_browser_value():
    service = object.__new__(iv.OANRegistrantAuthenticationService)
    session = Session(None, None)
    service._session = lambda: session

    async def stored(*args):
        return {"fayda_fin": FIN, "last_authentication_status": "SUCCESS"}

    async def providers(**kwargs):
        return [SimpleNamespace(provider_id="esignet")]

    async def start(**kwargs):
        holder = iv._extra_context.get()
        assert holder["context"]["foundational_id"] == FIN
        assert not holder["context"]["oan_bind_verified_fin"]
        holder["transaction"] = SimpleNamespace(context={"authentication_id": "auth"})
        return "state", "https://esignet.example/authorize", "Fayda"

    service._stored = stored
    service.get_available_providers = providers
    service.start_authentication = start
    asyncio.run(
        service.start_identity_verification(
            register_id=getattr(service, "REGISTER_ID", "register"),
            foundational_id=OTHER,
            internal_record_id="record",
            staff_id="staff",
        )
    )


@pytest.mark.parametrize("saved_fin", [None, "", "   "])
def test_saved_draft_with_blank_fin_can_start(saved_fin):
    service = object.__new__(iv.OANRegistrantAuthenticationService)
    session = Session(None, None)
    service._session = lambda: session

    async def stored(*args):
        return {"fayda_fin": saved_fin}

    async def providers(**kwargs):
        return [SimpleNamespace(provider_id="esignet")]

    async def start(**kwargs):
        holder = iv._extra_context.get()
        assert holder["context"]["foundational_id"] == ""
        assert holder["context"]["oan_bind_verified_fin"]
        holder["transaction"] = SimpleNamespace(context={"authentication_id": "auth"})
        return "state", "https://esignet.example/authorize", "Fayda"

    service._stored = stored
    service.get_available_providers = providers
    service.start_authentication = start
    asyncio.run(
        service.start_identity_verification(
            register_id=getattr(service, "REGISTER_ID", "register"),
            internal_record_id="draft",
            staff_id="staff",
        )
    )


def test_native_expected_identity_is_not_erased_by_a_concurrent_new_record():
    created = {}

    class Store:
        def create(self, **kwargs):
            created.update(kwargs)
            return SimpleNamespace(context=kwargs["context"])

    holder = {"context": {"foundational_id": "", "oan_bind_verified_fin": True}}
    token = iv._extra_context.set(holder)
    try:
        iv._ContextStore(Store()).create(context={"foundational_id": FIN})
    finally:
        iv._extra_context.reset(token)
    assert created["context"]["foundational_id"] == FIN
    assert not created["context"]["oan_bind_verified_fin"]


@pytest.mark.parametrize("enabled", [True, False])
def test_actual_native_start_requires_the_verify_enable_flag(monkeypatch, enabled):
    """Use native start, rather than mocking away its register configuration check."""
    service = object.__new__(iv.OANRegistrantAuthenticationService)

    class NativeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, cls, key):
            if cls.__name__ == "G2PRegisterDefinition":
                return SimpleNamespace(
                    requires_registrant_authentication=enabled,
                    register_mnemonic="NoLiveRowFixture",
                )
            return SimpleNamespace(is_active=True, provider_name="Fayda")

        def add(self, auth):
            auth.authentication_id = "native-auth"

        async def commit(self):
            pass

        async def refresh(self, auth):
            pass

    class Store:
        def create(self, **kwargs):
            return SimpleNamespace(
                context=kwargs["context"],
                state="native-state",
                nonce="native-nonce",
                code_verifier="pkce",
            )

    class Adapter:
        async def build_authorize_redirect(self, **kwargs):
            assert kwargs["nonce"] == "native-nonce"
            return "https://esignet.example/authorize", "native-state"

    async def providers(**kwargs):
        return [SimpleNamespace(provider_id="esignet")]

    monkeypatch.setattr(
        iv._core, "get_async_session_maker", lambda: lambda: NativeSession()
    )
    service.get_available_providers = providers
    service._provider_to_login_provider = lambda provider: SimpleNamespace(id="esignet")
    service._auth_tx_store = iv._ContextStore(Store())
    service._adapters = SimpleNamespace(resolve_for_provider=lambda login: Adapter())

    async def start():
        return await service.start_identity_verification(
            register_id=getattr(service, "REGISTER_ID", "register"),
            internal_record_id=None,
            staff_id="staff",
        )

    if enabled:
        assert (
            asyncio.run(start())["authorization_url"]
            == "https://esignet.example/authorize"
        )
    else:
        with pytest.raises(iv.G2PRegistryException, match="not enabled"):
            asyncio.run(start())
