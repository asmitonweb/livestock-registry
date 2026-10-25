"""Fayda (eSignet) verification of a farmer during intake, on core's registrant
authentication.

Core verifies a registrant who is already in the register: it reads the FIN
from the live record and, after the eSignet sign-in, checks that the person who
signed in has that FIN. A farmer being registered has no live record yet, so
OAN starts core's flow with the FIN entered only in the popup (put into core's
transaction context, which is what core checks the sign-in against). Core's
callback, its checks, its g2p_registrant_authentications row and its provider
configuration are used unchanged.

The verification is then tied to the record by identity_verification_id. On
every save of the farmer the server checks that verification, stamps its
outcome on the record (last_authentication_status, last_authenticated_on,
authentication_expiry_date, never taken from the form) and refuses a change to a
verified value (identity_claims.map_claims).
"""

import json
import logging
import re
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import TypedDict

from cryptography.fernet import Fernet, InvalidToken
from iam_core.schemas.auth_transaction import AuthTransaction
from openg2p_fastapi_common.context import component_registry, dbengine
from openg2p_registry_core.config import Settings
from openg2p_registry_core.errors import G2PRegistryErrorCodes, G2PRegistryException
from openg2p_registry_core.models import (
    AuthenticationStatusEnum,
    G2PRegistrantAuthentication,
    G2PRegistrantAuthenticationProvider,
)
from openg2p_registry_core.services import G2PRegistrantAuthenticationService
from openg2p_registry_core.services import (
    g2p_registrant_authentication_service as _core,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..identity_claims import explain_failure, same_value

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

# The core service in the pinned platform calls get_async_session_maker without
# importing it, so every registrant-authentication call fails with a NameError.
# Upstream's source imports it; until the pinned images do, give the module the
# name it uses. A no-op once the import is there.
if not hasattr(_core, "get_async_session_maker"):
    from openg2p_fastapi_common.context import get_async_session_maker

    _core.get_async_session_maker = get_async_session_maker

# The id of a verification started before the farmer's record has an id; the
# first save of the record ties the verification to it.
UNBOUND = "oan-unbound:"

STAMPS = (
    "last_authentication_status",
    "last_authenticated_on",
    "authentication_expiry_date",
)


class _StartContext(TypedDict, total=False):
    context: dict[str, str]
    transaction: AuthTransaction


_extra_context: ContextVar[_StartContext | None] = ContextVar(
    "oan_registrant_auth_context", default=None
)


class _ContextStore:
    """Core's transaction store, adding OAN's values to the context of the
    transaction core creates (core puts the live record's FIN there)."""

    def __init__(self, inner):
        self._inner = inner

    def create(self, **kwargs):
        extra = _extra_context.get()
        if extra is not None:
            kwargs["context"] = {**(kwargs.get("context") or {}), **extra["context"]}
        transaction = self._inner.create(**kwargs)
        if extra is not None:
            extra["transaction"] = transaction
        return transaction

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _invalid(message: str) -> G2PRegistryException:
    return G2PRegistryException(
        code=G2PRegistryErrorCodes.INVALID_REQUEST.value[1], message=message
    )


def _now() -> datetime:
    return datetime.now(tz=UTC).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _day(value: datetime | None) -> str | None:
    # The stamps are dates: core turns a YYYY-MM-DD string into a date for a
    # date column, and the string also survives a change request's JSON.
    return value.date().isoformat() if value else None


class OANRegistrantAuthenticationService(G2PRegistrantAuthenticationService):
    def __init__(self):
        super().__init__()
        self._auth_tx_store = _ContextStore(self._auth_tx_store)
        key = _config.registrant_auth_claims_encryption_key
        try:
            self._fernet = (
                Fernet(key.encode() if isinstance(key, str) else key) if key else None
            )
        except ValueError:
            self._fernet = None  # core stores the claims unencrypted then, too

    @staticmethod
    def _provider_to_login_provider(provider):
        # IAM supports a canonical assertion audience separately from the
        # transport endpoint (for example, an HTTP-only local gateway).
        login = G2PRegistrantAuthenticationService._provider_to_login_provider(provider)
        audience = (provider.provider_config or {}).get("jwt_assertion_aud")
        if audience is not None:
            if not isinstance(audience, str) or not audience.strip():
                raise _invalid("Fayda provider: jwt_assertion_aud must be a nonblank canonical token URL. Ask the administrator to correct it")
            login.jwt_assertion_aud = audience.strip()
        return login

    @staticmethod
    def _session():
        return async_sessionmaker(dbengine.get(), expire_on_commit=False)()

    REGISTER_ID = "997676d3-7008-59f9-b23e-613ad79bbb08"

    async def start_identity_verification(
        self,
        *,
        register_id: str,
        foundational_id: str,
        internal_record_id: str | None,
        staff_id: str,
    ) -> dict:
        if register_id != self.REGISTER_ID:
            raise _invalid(
                "Fayda verification: choose this registry’s domain register and verify again"
            )
        foundational_id = (foundational_id or "").strip()
        if not foundational_id:
            raise _invalid(
                "FIN (Fayda ID): enter the farmer's FIN in the Fayda verification window"
            )
        if not re.fullmatch(r"[0-9]{12}", foundational_id):
            raise _invalid(
                "FIN (Fayda ID): enter all 12 digits in the Fayda verification window"
            )
        providers = await self.get_available_providers(register_id=register_id)
        if not providers:
            raise _invalid(
                "Fayda verification is not set up for this register. Ask the administrator to add an "
                "active eSignet provider for it (g2p_registrant_authentication_providers)"
            )
        extra: _StartContext = {"context": {"foundational_id": foundational_id}}
        token = _extra_context.set(extra)
        try:
            _, authorization_url, provider_name = await self.start_authentication(
                register_id=register_id,
                internal_record_id=internal_record_id or f"{UNBOUND}{uuid.uuid4()}",
                provider_id=providers[0].provider_id,
                initiated_by_staff_id=staff_id,
            )
        finally:
            _extra_context.reset(token)
        return {
            "verification_id": extra["transaction"].context["authentication_id"],
            "authorization_url": authorization_url,
            "provider_name": provider_name,
        }

    async def _verification(self, session, verification_id: str):
        auth = (
            await session.get(G2PRegistrantAuthentication, verification_id)
            if verification_id
            else None
        )
        if not auth:
            raise _invalid(
                "Fayda verification: this verification was not found. Verify the farmer again"
            )
        provider = await session.get(
            G2PRegistrantAuthenticationProvider, auth.provider_id
        )
        return auth, provider

    def _claims(self, auth) -> dict:
        raw = auth.user_claims or ""
        if self._fernet:
            try:
                raw = self._fernet.decrypt(raw.encode()).decode()
            except InvalidToken:
                pass  # stored unencrypted (core does that without a valid key)
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def _verified(self, auth, provider) -> tuple[str | None, dict]:
        """The verified FIN and the register fields the claims give."""
        claims = self._claims(auth)
        subject = None
        if provider:
            login_provider = self._provider_to_login_provider(provider)
            subject = self._adapters.resolve_for_provider(
                login_provider
            ).registrant_subject(claims, login_provider=login_provider)
        return (
            subject,
            {},
        )  # FIN evidence only; approved Farmer projections remain source-owned.

    def _state(self, auth) -> tuple[str, str | None]:
        """The verification's status for the form, and what to do about it."""
        if auth.status == AuthenticationStatusEnum.success.value:
            if auth.expiry_at and auth.expiry_at <= _now():
                return (
                    "EXPIRED",
                    "The Fayda verification has expired. Verify the farmer again",
                )
            return "SUCCESS", None
        if auth.status == AuthenticationStatusEnum.failure.value:
            return "FAILURE", explain_failure(auth.failure_reason)
        ttl = int(_config.registrant_auth_session_ttl_seconds or 300)
        if (
            auth.status == AuthenticationStatusEnum.pending.value
            and auth.initiated_at < _now() - timedelta(seconds=ttl)
        ):
            return (
                "EXPIRED",
                "The Fayda sign-in was not finished in time. Verify the farmer again",
            )
        return auth.status, None

    async def get_identity_verification(
        self, *, verification_id: str, staff_id: str
    ) -> dict:
        async with self._session() as session:
            auth, provider = await self._verification(session, verification_id)
        if auth.initiated_by_staff_id != staff_id:
            raise G2PRegistryException(
                code=G2PRegistryErrorCodes.INVALID_REQUEST.value[1],
                message="Fayda verification: it was started by another staff member. Verify the farmer yourself",
            )
        status, message = self._state(auth)
        result = {
            "verification_id": auth.authentication_id,
            "status": status,
            "message": message,
            "verified_at": _iso(auth.completed_at) if status == "SUCCESS" else None,
            "expires_at": _iso(auth.expiry_at) if status == "SUCCESS" else None,
            "foundational_id": None,
            "values": {},
        }
        if status == "SUCCESS":
            result["foundational_id"], result["values"] = self._verified(auth, provider)
        return result

    async def _stored(
        self, session, model_names: tuple[str, ...], internal_record_id: str
    ) -> dict | None:
        """The saved farmer row with this id (the intake row, else the register row)."""
        import importlib

        module = importlib.import_module(
            "openg2p_registry_extensions.register_domain.models"
        )
        for name in model_names:
            cls = getattr(module, name)
            row = (
                await session.execute(
                    select(cls)
                    .where(cls.internal_record_id == internal_record_id)
                    .limit(1)
                )
            ).scalar()
            if row is not None:
                return {
                    column.key: getattr(row, column.key) for column in cls.__table__.c
                }
        return None

    async def verify_records(
        self, records: list[dict], labels: dict[str, str], mnemonic: str
    ) -> list[str]:
        """Check each farmer record's Fayda verification, stamp its outcome and
        return what is wrong, as messages that say what to do."""
        problems: list[str] = []
        models = (f"G2PIntakeForm{mnemonic}", f"G2PRegister{mnemonic}")
        async with self._session() as session:
            for record in records:
                record_id = record.get("internal_record_id")
                stored = (
                    await self._stored(session, models, record_id)
                    if record_id
                    else None
                )
                carries_id = "identity_verification_id" in record
                verification_id = (
                    record.get("identity_verification_id")
                    if carries_id
                    else (stored or {}).get("identity_verification_id")
                )
                if not verification_id:
                    if (stored or {}).get("identity_verification_id"):
                        problems.append(
                            "Fayda verification: an existing verification cannot be removed. Verify again to replace it"
                        )
                        continue
                    if carries_id or any(field in record for field in STAMPS):
                        record.update(dict.fromkeys(STAMPS))
                    continue
                try:
                    auth, provider = await self._verification(session, verification_id)
                except G2PRegistryException as error:
                    problems.append(error.message)
                    continue
                if auth.register_id != self.REGISTER_ID:
                    problems.append(
                        "Fayda verification: this verification belongs to another register. Verify again"
                    )
                    continue
                status, message = self._state(auth)
                if status != "SUCCESS":
                    problems.append(
                        f"Fayda verification: {message or 'it is not complete. Finish the Fayda sign-in or verify again'}"
                    )
                    continue
                subject, verified = self._verified(auth, provider)
                merged = {**(stored or {}), **record}
                fin = merged.get("fayda_fin")
                if not subject or not re.fullmatch(r"[0-9]{12}", str(subject)):
                    problems.append(
                        "FIN (Fayda ID): Fayda did not return a valid FIN. Verify again or contact support"
                    )
                    continue
                if fin and str(fin).strip() != str(subject).strip():
                    problems.append(
                        f"{labels.get('foundational_id', 'FIN')}: the farmer was verified with Fayda under another FIN. "
                        "Verify the farmer again in the Fayda window"
                    )
                for field, value in verified.items():
                    if field in record and not same_value(record[field], value):
                        problems.append(
                            f'{labels.get(field, field)}: Fayda verified it as "{value}" and it cannot be changed. '
                            "Undo the change; if the farmer's Fayda record is wrong, it has to be corrected there"
                        )
                record.update(
                    last_authentication_status=status,
                    last_authenticated_on=_day(auth.completed_at),
                    authentication_expiry_date=_day(auth.expiry_at),
                )
                if not carries_id:
                    continue
                if auth.internal_record_id != record_id:
                    bound = None
                    if not auth.internal_record_id.startswith(UNBOUND):
                        bound = await self._stored(
                            session, models, auth.internal_record_id
                        )
                    if bound is not None:
                        problems.append(
                            "Fayda verification: it was used for another farmer record. Verify this farmer again"
                        )
                        continue
                    if not record_id:
                        record["internal_record_id"] = record_id = str(uuid.uuid4())
                    auth.internal_record_id = record_id
                    session.add(auth)
                record.update(
                    fayda_fin=subject,
                    last_authentication_status=status,
                    last_authenticated_on=_day(auth.completed_at),
                    authentication_expiry_date=_day(auth.expiry_at),
                )
            if not problems:
                await session.commit()
        return problems


def install_identity_verification() -> OANRegistrantAuthenticationService:
    """Put OAN's service where core's was: in the component registry, and in
    core's controller service, which looked it up when it was created."""
    from openg2p_registry_core.controller_services import (
        G2PRegistrantAuthenticationControllerService,
    )

    core = G2PRegistrantAuthenticationService.get_component()
    if isinstance(core, OANRegistrantAuthenticationService):
        return core
    service = OANRegistrantAuthenticationService()
    component_registry.remove(service)
    index = (
        component_registry.index(core)
        if core in component_registry
        else len(component_registry)
    )
    if core in component_registry:
        component_registry.remove(core)
    component_registry.insert(index, service)
    controller = G2PRegistrantAuthenticationControllerService.get_component()
    if controller:
        controller.registrant_authentication_service = service
    return service
