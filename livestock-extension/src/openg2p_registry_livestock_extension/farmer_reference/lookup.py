"""Signed, consent-enforced DCI lookup. Notifications are never profile inputs.

The record mapping is server configuration, not an API request parameter. A
source template that omits provenance or the declared identity fails closed.
FIN12 and FAN16 are separate alternatives and are never converted.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import jwt
from jwt.utils import base64url_encode

from .profile import (FarmerReferenceError, intake_identity, local_profile,
                      matches_declared_identity, normalize_farmer_id, normalize_fin, normalize_uid)


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def detached_sign(value: bytes, key: str, algorithm: str, kid: str) -> str:
    signed = jwt.api_jws.PyJWS().encode(value, key, algorithm=algorithm, headers={"kid": kid})
    protected, _, signature = signed.split(".")
    return f"{protected}..{signature}"


@dataclass(frozen=True)
class RecordContract:
    source_id: str = "internal_record_id"
    farmer_id: str = "functional_record_id"
    uid: str = "foundational_id"
    approved_at: str = "last_approved_at"
    status: str = "record_status"
    source_query_field: str = "internal_record_id"
    # Only fields in the receiving profile allowlist can be selected.
    profile_paths: Mapping[str, str] = field(default_factory=dict)
    fin: str = "foundational_id"
    uid_query_field: str = "foundational_id"
    fin_query_field: str = "foundational_id"


@dataclass(frozen=True)
class LookupConfig:
    url: str
    sender_id: str
    source_id: str
    signing_key: str = field(repr=False)
    signing_kid: str
    source_public_key: str = field(repr=False)
    source_kid: str
    consent_audience: str
    consent_controller: str
    consent_purpose: str
    consent_scopes: tuple[str, ...]
    algorithm: str = "RS256"
    source_algorithm: str = "RS256"
    timeout_seconds: float = 8.0
    max_age_seconds: int = 300
    clock_skew_seconds: int = 30

    def __post_init__(self):
        parsed = urlsplit(self.url)
        # Like the platform clients, accept private service-network HTTP and
        # externally routed HTTPS. Only deployment configuration supplies a URL.
        if (parsed.username or parsed.password or parsed.query or parsed.fragment or
                not parsed.hostname or parsed.scheme not in {"https", "http"}):
            raise FarmerReferenceError("FARMER_REFERENCE_INVALID_SOURCE_URL")
        if self.algorithm not in {"RS256", "EdDSA"} or self.source_algorithm not in {"RS256", "EdDSA"}:
            raise FarmerReferenceError("FARMER_REFERENCE_UNSAFE_SIGNATURE_ALGORITHM")
        if not all((self.sender_id, self.source_id, self.signing_key, self.signing_kid,
                    self.source_public_key, self.source_kid, self.consent_audience,
                    self.consent_controller, self.consent_purpose, self.consent_scopes)):
            raise FarmerReferenceError("FARMER_REFERENCE_LOOKUP_CONFIGURATION_REQUIRED")
        if not 0 < self.timeout_seconds <= 30 or not 0 < self.max_age_seconds <= 600:
            raise FarmerReferenceError("FARMER_REFERENCE_INVALID_LOOKUP_LIMITS")
        if not 0 <= self.clock_skew_seconds <= 120:
            raise FarmerReferenceError("FARMER_REFERENCE_INVALID_LOOKUP_LIMITS")


@dataclass(frozen=True)
class VerifiedFarmer:
    source_system: str
    source_record_id: str
    source_version: int
    profile: Mapping
    fetched_at: datetime
    response_digest: str


def _at(record: Mapping, path: str):
    value = record
    for segment in path.split("."):
        if not isinstance(value, Mapping) or segment not in value:
            raise FarmerReferenceError("FARMER_REFERENCE_SOURCE_CONTRACT_INCOMPLETE")
        value = value[segment]
    return value


def _timestamp(value) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise FarmerReferenceError("FARMER_REFERENCE_INVALID_SOURCE_TIMESTAMP") from None


class FarmerLookup:
    def __init__(self, config: LookupConfig, contract: RecordContract):
        self.config, self.contract = config, contract

    def build_request(self, farmer_id: str | None, uid: str | None = None, *, fin: str | None = None, now: datetime | None = None,
                      source_record_id: str | None = None) -> dict:
        declared = intake_identity({"farmer_id": farmer_id, "fayda_fan_id": uid, "fayda_fin": fin})
        identifier_field = "fayda_fan_id" if declared.get("fayda_fan_id") else "fayda_fin"
        identifier = declared[identifier_field]
        cfg = self.config
        now = now or datetime.now(timezone.utc)
        timestamp = now.isoformat()
        consent = {
            "@context": "https://openg2p.org/contexts/consent_object.jsonld",
            "@type": "ConsentObject", "jti": uuid4().hex, "aud": cfg.consent_audience,
            "data_controller": cfg.consent_controller, "data_scopes": list(cfg.consent_scopes),
            "fetch_type": "oneshot", "issued_at": timestamp,
            "purpose": {"code": cfg.consent_purpose, "text": "Farmer reference approval"},
            "subject_id": {"type": identifier_field, "value": identifier},
            "validity": {"valid_from": timestamp, "valid_until": (now + timedelta(minutes=5)).isoformat()},
        }
        consent_jws = jwt.api_jws.PyJWS().encode(canonical(consent), cfg.signing_key,
            algorithm=cfg.algorithm, headers={"kid": cfg.signing_kid})
        header = {"version": "1.0.0", "message_id": uuid4().hex, "message_ts": timestamp,
                  "action": "search", "sender_id": cfg.sender_id, "receiver_id": cfg.source_id,
                  "total_count": 1, "is_msg_encrypted": False}
        message = {"transaction_id": uuid4().hex, "search_request": [{"reference_id": uuid4().hex,
            "timestamp": timestamp, "locale": "eng", "search_criteria": {
                "version": "1.0.0", "reg_type": "Farmer", "reg_record_type": "Farmer",
                "query_type": "expression", "query": {"type": "expression", "value": {
                    "expression": {"query": {"functional_record_id": {"$eq": farmer_id},
                    "foundational_id": {"$eq": uid}, "record_status": {"$eq": "ACTIVE"}}}}},
                "authorize": {"consent_jws": consent_jws}}}]}
        query = message["search_request"][0]["search_criteria"]["query"]["value"]["expression"]["query"]
        query.pop("foundational_id")
        if not declared.get("farmer_id"):
            query.pop("functional_record_id")
        else:
            query["functional_record_id"] = {"$eq": declared["farmer_id"]}
        if source_record_id:
            # Same immutable source person; approval may have rotated its ID.
            query[self.contract.source_query_field] = {"$eq": source_record_id}
        else:
            field = self.contract.uid_query_field if identifier_field == "fayda_fan_id" else self.contract.fin_query_field
            query[field] = {"$eq": identifier}
        return {"signature": detached_sign(canonical({"header": header, "message": message}),
                    cfg.signing_key, cfg.algorithm, cfg.signing_kid), "header": header, "message": message}

    async def lookup(self, farmer_id: str | None, uid: str | None = None, *, fin: str | None = None) -> VerifiedFarmer:
        declared = intake_identity({"farmer_id": farmer_id, "fayda_fan_id": uid, "fayda_fin": fin})
        request = self.build_request(declared.get("farmer_id"), declared.get("fayda_fan_id"), fin=declared.get("fayda_fin"))
        return await self._send(request, declared.get("farmer_id"), declared.get("fayda_fan_id"), fin=declared.get("fayda_fin"))

    async def lookup_current(self, farmer_id: str, approved_uid: str, source_record_id: str) -> VerifiedFarmer:
        if not source_record_id:
            raise FarmerReferenceError("FARMER_REFERENCE_SOURCE_ID_REQUIRED")
        use_fin = isinstance(approved_uid, str) and len(approved_uid) == 12
        uid, fin = (None, approved_uid) if use_fin else (approved_uid, None)
        request = self.build_request(farmer_id, uid, fin=fin, source_record_id=source_record_id)
        return await self._send(request, farmer_id, uid, fin=fin, source_record_id=source_record_id)

    async def _send(self, request, farmer_id, uid, *, fin=None, source_record_id=None):
        try:
            # Redirects may leak signed identity/consent to another endpoint.
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds, follow_redirects=False) as client:
                async with client.stream("POST", self.config.url, json=request) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 1024 * 1024:
                            raise FarmerReferenceError("FARMER_REFERENCE_RESPONSE_TOO_LARGE")
            body = json.loads(raw)
        except (httpx.HTTPError, ValueError):
            raise FarmerReferenceError("FARMER_REFERENCE_LOOKUP_UNAVAILABLE") from None
        return self.verify_response(body, request, farmer_id, uid, fin=fin, expected_source_record_id=source_record_id)

    def verify_response(self, body: object, request: dict, farmer_id: str, uid: str,
                        *, fin: str | None = None, now: datetime | None = None, expected_source_record_id: str | None = None) -> VerifiedFarmer:
        now = now or datetime.now(timezone.utc)
        cfg, contract = self.config, self.contract
        try:
            if not isinstance(body, dict):
                raise ValueError
            signature = body["signature"]
            if not isinstance(signature, str) or len(signature.split(".")) != 3 or signature.split(".")[1]:
                raise ValueError
            protected = jwt.get_unverified_header(signature)
            if protected.get("kid") != cfg.source_kid or protected.get("alg") != cfg.source_algorithm:
                raise ValueError
            if not isinstance(protected.get("crit", []), list) or set(protected.get("crit", [])) - {"b64"}:
                raise ValueError
            # Ignore untrusted jku/x5u/key claims; only this server-pinned key is used.
            signed = canonical({"header": body["header"], "message": body["message"]})
            if protected.get("b64", True) is False:
                jwt.api_jws.PyJWS().decode_complete(signature, cfg.source_public_key,
                    algorithms=[cfg.source_algorithm], detached_payload=signed)
            else:
                left, _, right = signature.split(".")
                attached = f"{left}.{base64url_encode(signed).decode('ascii')}.{right}"
                jwt.api_jws.PyJWS().decode_complete(attached, cfg.source_public_key,
                    algorithms=[cfg.source_algorithm])
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise FarmerReferenceError("FARMER_REFERENCE_RESPONSE_SIGNATURE_INVALID") from None
        try:
            header, message = body["header"], body["message"]
            if (header.get("version") != "1.0.0" or header.get("is_msg_encrypted", False) is not False or
                    header["status"] != "succ" or header["sender_id"] != cfg.source_id or
                    header["receiver_id"] != cfg.sender_id or header["action"] != "search" or
                    header["message_id"] != request["header"]["message_id"] or
                    message["transaction_id"] != request["message"]["transaction_id"] or
                    header["meta"]["signature_validation"] != "enabled" or
                    header["meta"]["consent_enforcement"] != "enabled"):
                raise ValueError
            age = (now - _timestamp(header["message_ts"])).total_seconds()
            if not -cfg.clock_skew_seconds <= age <= cfg.max_age_seconds:
                raise ValueError
            responses = message["search_response"]
            if not isinstance(responses, list) or len(responses) != 1:
                raise ValueError
            item = responses[0]
            if (item["status"] != "succ" or item["reference_id"] !=
                    request["message"]["search_request"][0]["reference_id"]):
                raise ValueError
            records = item["data"]["reg_records"]
            if isinstance(records, list) and not records:
                raise FarmerReferenceError("FARMER_REFERENCE_NOT_FOUND")
            if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
                raise ValueError
            record = records[0]
        except FarmerReferenceError:
            raise
        except (TypeError, KeyError, ValueError):
            raise FarmerReferenceError("FARMER_REFERENCE_RESPONSE_REJECTED") from None
        declared = intake_identity({"farmer_id": farmer_id, "fayda_fan_id": uid, "fayda_fin": fin})
        actual = {"farmer_id": normalize_farmer_id(_at(record, contract.farmer_id))}
        # Collect both source identifiers when supplied, so the profile does not
        # change merely because the caller chose FIN rather than FAN as its query.
        for local_field, path, normalizer in (("fayda_fan_id", contract.uid, normalize_uid),
                                               ("fayda_fin", contract.fin, normalize_fin)):
            try:
                value = _at(record, path)
                actual[local_field] = normalizer(value)
            except FarmerReferenceError:
                if declared.get(local_field):
                    raise
        if (declared.get("farmer_id") and actual["farmer_id"] != declared["farmer_id"]) or (
                not expected_source_record_id and not matches_declared_identity(declared, actual)):
            raise FarmerReferenceError("FARMER_REFERENCE_IDENTITY_MISMATCH")
        source_id = _at(record, contract.source_id)
        if expected_source_record_id and source_id != expected_source_record_id:
            raise FarmerReferenceError("FARMER_REFERENCE_SOURCE_ID_MISMATCH")
        approved_at = _timestamp(_at(record, contract.approved_at))
        if (not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 200 or
                _at(record, contract.status) != "ACTIVE" or approved_at > now + timedelta(seconds=cfg.clock_skew_seconds)):
            raise FarmerReferenceError("FARMER_REFERENCE_SOURCE_NOT_APPROVED")
        source_profile = {name: _at(record, path) for name, path in contract.profile_paths.items()
                          if _has_path(record, path)}
        # Identity fields cannot be redirected through optional profile mappings.
        source_profile["farmer_id"] = actual["farmer_id"]
        if "fayda_fan_id" in actual:
            source_profile["approved_uid"] = actual["fayda_fan_id"]
        if "fayda_fin" in actual:
            source_profile["approved_fin"] = actual["fayda_fin"]
        profile = local_profile(source_profile)
        return VerifiedFarmer(cfg.source_id, source_id, int(approved_at.timestamp() * 1_000_000),
                              profile, now, sha256(signed).hexdigest())


def _has_path(record: Mapping, path: str) -> bool:
    try:
        _at(record, path)
        return True
    except FarmerReferenceError:
        return False
