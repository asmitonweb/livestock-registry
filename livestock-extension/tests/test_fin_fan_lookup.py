"""Cryptographic source contract for FIN/FAN resolution, using synthetic keys in memory."""

import importlib
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

lookup = importlib.import_module(
    "openg2p_registry_livestock_extension.farmer_reference.lookup"
)
profile = importlib.import_module(
    "openg2p_registry_livestock_extension.farmer_reference.profile"
)
FIN = "001234567890"
FAN = "0012345678901234"
FARMER = "FR-000000000001"
NOW = datetime.now(UTC)


@pytest.fixture(scope="module")
def client():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    ).decode()
    public = (
        key.public_key()
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    cfg = lookup.LookupConfig(
        url="https://synthetic-source.invalid/search",
        sender_id="synthetic-receiver",
        source_id="synthetic-source",
        signing_key=private,
        signing_kid="synthetic-caller",
        source_public_key=public,
        source_kid="synthetic-source-key",
        consent_audience="synthetic-consent",
        consent_controller="synthetic-controller",
        consent_purpose="synthetic-purpose",
        consent_scopes=("farmer_identity",),
    )
    return lookup.FarmerLookup(cfg, lookup.RecordContract())


def signed(client, request, **changes):
    record = {
        "internal_record_id": "synthetic-person",
        "functional_record_id": FARMER,
        "foundational_id": FIN,
        "record_status": "ACTIVE",
        "last_approved_at": (NOW - timedelta(seconds=10)).isoformat(),
    }
    record.update(changes)
    body = {
        "header": {
            "version": "1.0.0",
            "message_ts": NOW.isoformat(),
            "status": "succ",
            "sender_id": client.config.source_id,
            "receiver_id": client.config.sender_id,
            "action": "search",
            "message_id": request["header"]["message_id"],
            "meta": {
                "signature_validation": "enabled",
                "consent_enforcement": "enabled",
            },
        },
        "message": {
            "transaction_id": request["message"]["transaction_id"],
            "search_response": [
                {
                    "status": "succ",
                    "reference_id": request["message"]["search_request"][0][
                        "reference_id"
                    ],
                    "data": {"reg_records": [record]},
                }
            ],
        },
    }
    body["signature"] = lookup.detached_sign(
        lookup.canonical(body),
        client.config.signing_key,
        client.config.source_algorithm,
        client.config.source_kid,
    )
    return body


def resign(client, body):
    unsigned = {"header": body["header"], "message": body["message"]}
    body["signature"] = lookup.detached_sign(
        lookup.canonical(unsigned),
        client.config.signing_key,
        client.config.source_algorithm,
        client.config.source_kid,
    )


def test_fin_only_query_and_consent_do_not_invent_farmer_id_or_fan(client):
    request = client.build_request(None, fin=FIN, now=NOW)
    criteria = request["message"]["search_request"][0]["search_criteria"]
    query = criteria["query"]["value"]["expression"]["query"]
    assert query == {
        "foundational_id": {"$eq": FIN},
        "record_status": {"$eq": "ACTIVE"},
    }
    consent = jwt.decode(
        criteria["authorize"]["consent_jws"],
        client.config.source_public_key,
        algorithms=["RS256"],
        audience=client.config.consent_audience,
    )
    assert consent["subject_id"] == {"type": "fayda_fin", "value": FIN}
    result = client.verify_response(
        signed(client, request), request, None, None, fin=FIN, now=NOW
    )
    assert result.profile == {"farmer_id": FARMER, "fayda_fin": FIN}


def test_legacy_fan_response_and_query_are_unchanged(client):
    request = client.build_request(FARMER, FAN, now=NOW)
    result = client.verify_response(
        signed(client, request, foundational_id=FAN), request, FARMER, FAN, now=NOW
    )
    assert result.profile == {"farmer_id": FARMER, "fayda_fan_id": FAN}


def test_both_declared_ids_must_match_separate_signed_source_fields(client):
    both = lookup.FarmerLookup(
        client.config, lookup.RecordContract(uid="fan", fin="foundational_id")
    )
    request = both.build_request(FARMER, FAN, fin=FIN, now=NOW)
    result = both.verify_response(
        signed(both, request, fan=FAN), request, FARMER, FAN, fin=FIN, now=NOW
    )
    assert result.profile["fayda_fin"] == FIN and result.profile["fayda_fan_id"] == FAN
    with pytest.raises(profile.FarmerReferenceError, match="IDENTITY_MISMATCH"):
        both.verify_response(
            signed(both, request, fan="0099999999999999"),
            request,
            FARMER,
            FAN,
            fin=FIN,
            now=NOW,
        )


@pytest.mark.parametrize(
    "changes",
    [{"foundational_id": "009999999999"}, {"functional_record_id": "FR-999999999999"}],
)
def test_valid_signature_cannot_redirect_a_declared_identity(client, changes):
    request = client.build_request(FARMER, fin=FIN, now=NOW)
    with pytest.raises(profile.FarmerReferenceError, match="IDENTITY_MISMATCH"):
        client.verify_response(
            signed(client, request, **changes), request, FARMER, None, fin=FIN, now=NOW
        )


def test_untrusted_body_cannot_create_a_verified_fin(client):
    request = client.build_request(None, fin=FIN, now=NOW)
    body = signed(client, request)
    body["message"]["search_response"][0]["data"]["reg_records"][0][
        "functional_record_id"
    ] = "FR-999999999999"
    with pytest.raises(profile.FarmerReferenceError, match="SIGNATURE_INVALID"):
        client.verify_response(body, request, None, None, fin=FIN, now=NOW)


def test_authenticated_no_match_is_distinct_from_a_trusted_reference(client):
    request = client.build_request(None, fin=FIN, now=NOW)
    body = signed(client, request)
    body["message"]["search_response"][0]["data"]["reg_records"] = []
    resign(client, body)
    with pytest.raises(profile.FarmerReferenceError, match="NOT_FOUND"):
        client.verify_response(body, request, None, None, fin=FIN, now=NOW)


def test_update_can_rotate_fin_only_on_the_same_immutable_source_person(client):
    request = client.build_request(
        FARMER, fin=FIN, source_record_id="synthetic-person", now=NOW
    )
    changed = "009999999999"
    body = signed(client, request, foundational_id=changed)
    result = client.verify_response(
        body,
        request,
        FARMER,
        None,
        fin=FIN,
        now=NOW,
        expected_source_record_id="synthetic-person",
    )
    assert result.profile["fayda_fin"] == changed
    with pytest.raises(profile.FarmerReferenceError, match="SOURCE_ID_MISMATCH"):
        client.verify_response(
            body,
            request,
            FARMER,
            None,
            fin=FIN,
            now=NOW,
            expected_source_record_id="another-person",
        )


def test_record_contract_old_positional_arguments_keep_their_meaning():
    contract = lookup.RecordContract(
        "id", "farmer", "fan", "approved", "status", "source", {}
    )
    assert (
        contract.approved_at == "approved"
        and contract.status == "status"
        and contract.fin == "foundational_id"
    )
