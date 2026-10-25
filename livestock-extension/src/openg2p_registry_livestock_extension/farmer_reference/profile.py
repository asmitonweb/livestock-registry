"""Identity/contact/location contract shared by receiving registries.

These functions accept already authenticated source data. They do not verify a
signature, consent assertion or source approval. The transport adapter must do
that before calling them; no public endpoint accepts a client-supplied trust flag.
No function here creates a Farmer, changes a registry row, or changes IAM grants.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime
import re


class FarmerReferenceError(ValueError):
    """Stable diagnostic without Farmer identifiers or personal values."""


# Keys are canonical source fields, not notification fields or an unfiltered
# DCI response. A transport adapter must map its documented source contract.
SOURCE_TO_LOCAL_FIELDS = {
    "farmer_id": "farmer_id",
    "approved_uid": "fayda_fan_id",
    "approved_fin": "fayda_fin",
    "first_name": "first_name",
    "middle_name": "middle_name",
    "last_name": "last_name",
    "first_name_amh": "first_name_amh",
    "middle_name_amh": "middle_name_amh",
    "last_name_amh": "last_name_amh",
    "first_name_other": "first_name_other",
    "middle_name_other": "middle_name_other",
    "last_name_other": "last_name_other",
    "gender": "gender",
    "birth_date": "date_of_birth",
    "mobile_number": "mobile_number",
    "email": "email",
    "address_line_1": "address_line_1",
    "address_line_2": "address_line_2",
    "postal_code": "postal_code",
    "country_code": "country_code",
    "geo_lowest_level_value_id": "geo_lowest_level_value_id",
    "geo_code_hierarchy_json": "geo_code_hierarchy_json",
    "region_id": "region_id",
    "zone_id": "zone_id",
    "woreda_id": "woreda_id",
    "kebele_id": "kebele_id",
    "region_name": "region_name",
    "zone_name": "zone_name",
    "woreda_name": "woreda_name",
    "kebele_name": "kebele_name",
}
PROFILE_FIELDS = frozenset(SOURCE_TO_LOCAL_FIELDS.values()) | {"farmer_name"}
NAME_FIELDS = ("first_name", "middle_name", "last_name")
IDENTITY_FIELDS = frozenset({"farmer_id", "fayda_fan_id", "fayda_fin"})


def normalize_farmer_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"FR-[0-9]{12}", value.strip()):
        raise FarmerReferenceError("FARMER_REFERENCE_INVALID_FARMER_ID")
    return value.strip()


def normalize_uid(value: object) -> str:
    # Whitespace used for display is permitted; numeric coercion would discard
    # leading zeros, so neither floats nor integers are accepted.
    if not isinstance(value, str):
        raise FarmerReferenceError("FARMER_REFERENCE_INVALID_UID")
    uid = "".join(value.split())
    if not re.fullmatch(r"[0-9]{16}", uid):
        raise FarmerReferenceError("FARMER_REFERENCE_INVALID_UID")
    return uid


def identity_pair(farmer_id: object, uid: object) -> tuple[str, str]:
    return normalize_farmer_id(farmer_id), normalize_uid(uid)


def normalize_fin(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{12}", value.strip()):
        raise FarmerReferenceError("FIN (Fayda ID): enter exactly 12 digits. Use the FAN field for a 16-digit FAN")
    return value.strip()


def intake_identity(record: Mapping, *, required=True, require_farmer=False) -> dict:
    """Validate declared IDs without asserting source approval or onboarding.

    FIN and FAN remain separate. A signed source response may resolve an omitted
    Farmer ID; a supplied Farmer ID must still match that same source person.
    """
    result = {}
    for field, normalizer in (("farmer_id", normalize_farmer_id),
                              ("fayda_fan_id", normalize_uid), ("fayda_fin", normalize_fin)):
        value = record.get(field)
        if value is not None and (not isinstance(value, str) or value.strip()):
            result[field] = normalizer(value)
    if required and not (result.get("fayda_fan_id") or result.get("fayda_fin")):
        raise FarmerReferenceError("Fayda ID: enter a 12-digit FIN or a 16-digit FAN, or verify with Fayda")
    if require_farmer and not result.get("farmer_id"):
        raise FarmerReferenceError("FARMER_REFERENCE_SOURCE_IDENTITY_REQUIRED")
    return result


def reference_identity(record: Mapping, *, preferred_identifier=None) -> tuple[str, str]:
    """The approved source Farmer ID and exact identifier held by its ledger.

    Existing ledgers retain their FIN/FAN kind when the source supplies both;
    identity rotation still requires a signed current-source UPDATE and approval.
    """
    values = intake_identity(record, require_farmer=True)
    preferred_field = "fayda_fin" if isinstance(preferred_identifier, str) and len(preferred_identifier) == 12 else "fayda_fan_id"
    identifier = values.get(preferred_field) or values.get("fayda_fan_id") or values.get("fayda_fin")
    return values["farmer_id"], identifier


def matches_declared_identity(declared: Mapping, approved: Mapping) -> bool:
    wanted = intake_identity(declared)
    actual = intake_identity(approved, require_farmer=True)
    return all(actual.get(field) == value for field, value in wanted.items())


def validate_identity_fields(record: dict) -> None:
    """Normalize only fields present in a partial draft; never start onboarding.

    Blank draft fields stay blank. Final submission still needs the separate
    authenticated pair lookup and dependency service, not just format checks.
    """
    for field, normalizer in (("farmer_id", normalize_farmer_id), ("fayda_fan_id", normalize_uid), ("fayda_fin", normalize_fin)):
        if field in record and record[field] not in (None, ""):
            record[field] = normalizer(record[field])


def local_profile(source: Mapping, *, today: date | None = None) -> dict:
    """Copy the explicit allowlist; omissions remain absent, including on UPDATE.

    None represents an explicit permitted clear. Source workflow/status,
    household/economic values, documents and business fields are never copied.
    Identity validation happens separately against a fully resolved pair.
    """
    if not isinstance(source, Mapping):
        raise FarmerReferenceError("FARMER_REFERENCE_INVALID_PROFILE")
    result = {local: deepcopy(source[key]) for key, local in SOURCE_TO_LOCAL_FIELDS.items() if key in source}
    validate_identity_fields(result)
    for field, value in result.items():
        if field == "geo_code_hierarchy_json":
            if value is not None and not isinstance(value, dict):
                raise FarmerReferenceError("FARMER_REFERENCE_INVALID_GEOGRAPHY")
        elif field == "date_of_birth":
            if value is not None:
                try:
                    if isinstance(value, datetime):
                        raise ValueError
                    parsed = value if isinstance(value, date) else date.fromisoformat(value)
                except (ValueError, TypeError):
                    raise FarmerReferenceError("FARMER_REFERENCE_INVALID_BIRTH_DATE") from None
                if parsed > (today or date.today()):
                    raise FarmerReferenceError("FARMER_REFERENCE_INVALID_BIRTH_DATE")
                result[field] = parsed
        elif value is not None and not isinstance(value, str):
            raise FarmerReferenceError("FARMER_REFERENCE_INVALID_PROFILE_VALUE")
    return result


def merge_profile(approved: Mapping, proposed: Mapping) -> dict:
    """Prepare a profile patch without touching domain-owned fields.

    Omitted values preserve approved values. Explicit clears survive and the
    composed display name uses the merged names, not a partial notification.
    """
    forbidden = set(proposed) - PROFILE_FIELDS
    if forbidden:
        raise FarmerReferenceError("FARMER_REFERENCE_FIELD_OUTSIDE_ALLOWLIST")
    result = {field: deepcopy(value) for field, value in approved.items() if field in PROFILE_FIELDS}
    result.update(deepcopy(dict(proposed)))
    validate_identity_fields(result)
    reference_identity(result)
    if any(field in proposed for field in NAME_FIELDS):
        parts = [result.get(field) for field in NAME_FIELDS]
        if any(value is not None and not isinstance(value, str) for value in parts):
            raise FarmerReferenceError("FARMER_REFERENCE_INVALID_PROFILE_VALUE")
        result["farmer_name"] = " ".join(value.strip() for value in parts if value and value.strip()) or None
    return result


def prepare_update(approved: Mapping, source: Mapping, *, expected_source_id: str,
                   fetched_source_id: str, applied_version: int, fetched_version: int) -> dict:
    """Recheck immutable identity and fetched ordering before native approval.

    The caller must lock/read current persisted provenance in the approval
    transaction. This pure rule neither persists versions nor verifies transport.
    """
    if not expected_source_id or fetched_source_id != expected_source_id:
        raise FarmerReferenceError("FARMER_REFERENCE_SOURCE_ID_MISMATCH")
    if (type(applied_version) is not int or applied_version < 0 or
            type(fetched_version) is not int or fetched_version <= applied_version):
        raise FarmerReferenceError("FARMER_REFERENCE_STALE_SOURCE_VERSION")
    if "farmer_id" not in source or not (source.get("approved_uid") or source.get("approved_fin")):
        raise FarmerReferenceError("FARMER_REFERENCE_SOURCE_IDENTITY_REQUIRED")
    # Farmer ID remains stable; an approved UID rotation is on the same source
    # person and must not change the local reference or recreate domain intakes.
    if normalize_farmer_id(source["farmer_id"]) != normalize_farmer_id(approved.get("farmer_id")):
        raise FarmerReferenceError("FARMER_REFERENCE_FARMER_ID_MISMATCH")
    return merge_profile(approved, local_profile(source))


def model_identity(row) -> dict:
    return {field: getattr(row, field, None) for field in IDENTITY_FIELDS}
