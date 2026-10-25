"""Tests for the save-time Fayda verification checks
(services/identity_verification.py). They need the registry platform, so they
run inside the staff API image and are skipped elsewhere:

    docker run --rm -v "$PWD:/w" -w /w --entrypoint sh <staff-api image> -c \\
      "pip install -q --no-deps ./oan-extension pytest && python -m pytest -q oan-extension/tests"
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("openg2p_registry_core")

from openg2p_registry_livestock_extension.register_domain.services import (
    identity_verification as iv,
)

NOW = datetime.now(UTC).replace(tzinfo=None)
FIN = "123456789012"
VERIFIED = {"first_name": "Abebe", "birth_date": "1987-05-12"}


def _auth(**changes):
    auth = SimpleNamespace(
        authentication_id="v-1",
        register_id=iv.OANRegistrantAuthenticationService.REGISTER_ID,
        internal_record_id=f"{iv.UNBOUND}x",
        status="SUCCESS",
        failure_reason=None,
        initiated_at=NOW,
        completed_at=NOW,
        expiry_at=NOW + timedelta(days=730),
        initiated_by_staff_id="staff-1",
    )
    auth.__dict__.update(changes)
    return auth


class _Session:
    def __init__(self):
        self.added, self.committed = [], False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        self.committed = True


def _service(auth, stored=None, subject=FIN):
    service = object.__new__(iv.OANRegistrantAuthenticationService)
    session = _Session()
    service._session = lambda: session

    async def stored_row(_session, _models, record_id):
        return (stored or {}).get(record_id)

    async def verification(_session, verification_id):
        if verification_id != auth.authentication_id:
            raise iv._invalid(
                "Fayda verification: this verification was not found. Verify the farmer again"
            )
        return auth, None

    service._stored = stored_row
    service._verification = verification
    service._verified = lambda _auth, _provider: (subject, dict(VERIFIED))
    return service, session


def _check(service, records):
    return asyncio.run(
        service.verify_records(records, {"first_name": "First Name"}, "Livestock")
    )


def test_first_save_ties_the_verification_to_a_new_record_and_stamps_it():
    auth = _auth()
    service, session = _service(auth)
    record = {
        "identity_verification_id": "v-1",
        "fayda_fin": FIN,
        "last_authentication_status": "FORGED",
    }
    assert _check(service, [record]) == []
    assert record["internal_record_id"] == auth.internal_record_id
    assert record["last_authentication_status"] == "SUCCESS"
    assert record["last_authenticated_on"] == NOW.date().isoformat()
    assert session.committed and session.added == [auth]


def test_no_verification_clears_stamps_sent_by_the_browser():
    service, _ = _service(_auth())
    record = {
        "identity_verification_id": "",
        "last_authentication_status": "SUCCESS",
        "last_authenticated_on": "2026-01-01",
    }
    assert _check(service, [record]) == []
    assert (
        record["last_authentication_status"] is None
        and record["last_authenticated_on"] is None
    )


def test_a_verified_value_cannot_be_changed_in_another_section():
    service, _ = _service(
        _auth(internal_record_id="r-1"),
        stored={"r-1": {"identity_verification_id": "v-1"}},
    )
    problems = _check(service, [{"internal_record_id": "r-1", "first_name": "Abebech"}])
    assert problems and problems[0].startswith(
        'First Name: Fayda verified it as "Abebe"'
    )


def test_the_fin_on_the_form_must_be_the_verified_one():
    service, _ = _service(_auth())
    problems = _check(
        service, [{"identity_verification_id": "v-1", "fayda_fin": "999999999999"}]
    )
    assert problems and "verified with Fayda under another FIN" in problems[0]


def test_a_verification_used_by_another_saved_farmer_is_refused():
    service, _ = _service(
        _auth(internal_record_id="r-other"),
        stored={"r-other": {"internal_record_id": "r-other"}},
    )
    problems = _check(
        service,
        [
            {
                "internal_record_id": "r-1",
                "identity_verification_id": "v-1",
                "fayda_fin": FIN,
            }
        ],
    )
    assert problems == [
        "Fayda verification: it was used for another farmer record. Verify this farmer again"
    ]


def test_a_failed_or_unknown_verification_says_what_to_do():
    service, _ = _service(
        _auth(
            status="FAILURE",
            failure_reason="Token subject (1) does not match registrant record (2).",
        )
    )
    problems = _check(service, [{"identity_verification_id": "v-1"}])
    assert "not the FIN on this form" in problems[0]
    service, _ = _service(_auth())
    assert "was not found" in _check(service, [{"identity_verification_id": "nope"}])[0]


def test_an_expired_verification_is_not_accepted():
    service, _ = _service(_auth(expiry_at=NOW - timedelta(days=1)))
    assert (
        "has expired"
        in _check(service, [{"identity_verification_id": "v-1", "fayda_fin": FIN}])[0]
    )


def test_the_transaction_context_preserves_the_server_expected_fin():
    created = {}

    class Store:
        def create(self, **kwargs):
            created.update(kwargs)
            return SimpleNamespace(context=kwargs["context"])

    store = iv._ContextStore(Store())
    holder = {"context": {"foundational_id": FIN}}
    token = iv._extra_context.set(holder)
    try:
        store.create(
            login_provider_id="p",
            redirect_uri="/",
            server_metadata=None,
            context={"authentication_id": "v-1", "foundational_id": ""},
        )
    finally:
        iv._extra_context.reset(token)
    assert created["context"] == {"authentication_id": "v-1", "foundational_id": FIN}
    assert holder["transaction"].context["authentication_id"] == "v-1"
    # Outside an OAN start, core's own context is untouched.
    iv._ContextStore(Store()).create(context={"foundational_id": "live"})
    assert created["context"] == {"foundational_id": "live"}


def test_missing_verified_subject_is_refused():
    service, session = _service(_auth(), subject=None)
    problems = _check(service, [{"identity_verification_id": "v-1"}])
    assert problems and "did not return a valid FIN" in problems[0]
    assert not session.committed


def test_existing_verification_cannot_be_removed_to_downgrade_the_record():
    service, session = _service(
        _auth(internal_record_id="r-1"),
        stored={"r-1": {"identity_verification_id": "v-1"}},
    )
    problems = _check(
        service, [{"internal_record_id": "r-1", "identity_verification_id": ""}]
    )
    assert problems and "cannot be removed" in problems[0]
    assert not session.committed


def test_verified_fin_is_saved_without_changing_the_reference_uid():
    service, _ = _service(_auth())
    record = {"identity_verification_id": "v-1", "fayda_fan_id": "0012345678901234"}
    assert _check(service, [record]) == []
    assert record["fayda_fin"] == FIN
    assert record["fayda_fan_id"] == "0012345678901234"


def test_stamps_without_a_verification_id_are_never_trusted():
    service, _ = _service(_auth())
    record = {"last_authentication_status": "SUCCESS"}
    assert _check(service, [record]) == []
    assert record["last_authentication_status"] is None


def test_partial_save_uses_server_stamps_from_the_stored_verification():
    service, _ = _service(
        _auth(internal_record_id="r-1"),
        stored={"r-1": {"identity_verification_id": "v-1"}},
    )
    record = {"internal_record_id": "r-1", "last_authentication_status": "FORGED"}
    assert _check(service, [record]) == []
    assert record["last_authentication_status"] == "SUCCESS"
