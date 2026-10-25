"""What the person submitting an intake is told when its Farmer cannot be used.

Every message says what is wrong and what to do. Codes stay in logs; the
submitter sees the sentence. A message that is already a sentence (field
format errors) is shown as it is.
"""
from __future__ import annotations

from collections.abc import Mapping

_CHECK_IDS = "Check the Farmer ID and Fayda ID against the farmer's Farmer Registry record and submit again"
_REGISTER_FIRST = ("If the farmer is not registered yet, register them in the Farmer Registry first; "
                   "this intake can be submitted once that registration is approved")
_TRY_AGAIN = "Nothing was submitted. Try again in a few minutes; if it keeps failing, tell the administrator"

MESSAGES = {
    "FARMER_REFERENCE_FARMER_ID_REQUIRED":
        "Farmer ID: enter the farmer's Farmer Registry ID (FR- followed by 12 digits)",
    "FARMER_REFERENCE_SOURCE_IDENTITY_REQUIRED":
        "Farmer ID: enter the farmer's Farmer Registry ID (FR- followed by 12 digits)",
    "FARMER_REFERENCE_INVALID_FARMER_ID":
        "Farmer ID: enter FR- followed by exactly 12 digits, preserving leading zeroes",
    "FARMER_REFERENCE_INVALID_UID":
        "Fayda FAN: enter exactly 16 digits, or enter the 12-digit FIN instead",
    "FARMER_REFERENCE_NOT_FOUND":
        "No farmer with {ids} is in the Farmer Registry. " + _CHECK_IDS + ". " + _REGISTER_FIRST,
    "FARMER_REFERENCE_SOURCE_NOT_APPROVED":
        "The farmer with {ids} is in the Farmer Registry but not approved yet. "
        "Submit this intake after the farmer's registration is approved",
    "FARMER_REFERENCE_IDENTITY_MISMATCH":
        "The Farmer ID and Fayda ID belong to different farmers in the Farmer Registry ({ids}). " + _CHECK_IDS,
    "FARMER_REFERENCE_FARMER_ID_MISMATCH":
        "The Farmer ID and Fayda ID belong to different farmers in the Farmer Registry ({ids}). " + _CHECK_IDS,
    "FARMER_REFERENCE_SOURCE_ID_MISMATCH":
        "This registry already links the farmer with {ids} to a different Farmer Registry record. "
        "Ask the administrator to check the farmer's reference before submitting",
    "FARMER_REFERENCE_EXISTING_IDENTITY_AMBIGUOUS":
        "This registry already holds more than one farmer matching {ids}. "
        "Ask the administrator to resolve the duplicate before submitting",
    "FARMER_REFERENCE_SUBMISSION_IDENTITY_CHANGED":
        "The Farmer ID or Fayda ID changed after this intake was linked to a farmer. "
        "Restore the original IDs, or start a new intake for the other farmer",
    "FARMER_REFERENCE_PENDING_IDENTITY_CHANGED":
        "The farmer with {ids} is already awaiting approval with a different Fayda ID. "
        "Use the Fayda ID of that pending registration, or wait for its approval",
    "FARMER_REFERENCE_LOCAL_IDENTITY_UPDATE_REQUIRED":
        "The farmer's Fayda ID changed in the Farmer Registry. Wait for the farmer's update to be approved "
        "in this registry, then submit again",
    "FARMER_REFERENCE_DEPENDENCY_REJECTED":
        "Adding the farmer with {ids} to this registry was rejected. "
        "Check the IDs, or ask the approver why, before submitting again",
    "FARMER_REFERENCE_LOOKUP_UNAVAILABLE":
        "The Farmer Registry could not be reached to check the farmer. " + _TRY_AGAIN,
    "FARMER_REFERENCE_LOOKUP_EXPIRED":
        "The Farmer Registry's answer arrived too late to be used. " + _TRY_AGAIN,
    "FARMER_REFERENCE_LOOKUP_CONFIGURATION_REQUIRED":
        "Farmer lookups are not configured in this registry, so the farmer cannot be checked. "
        "Ask the administrator to set the Farmer Registry connection",
}

# The lookup could not reach or trust the Farmer Registry: nothing is wrong
# with what was entered.
UNAVAILABLE = {
    "FARMER_REFERENCE_LOOKUP_UNAVAILABLE", "FARMER_REFERENCE_LOOKUP_EXPIRED",
    "FARMER_REFERENCE_RESPONSE_REJECTED", "FARMER_REFERENCE_RESPONSE_SIGNATURE_INVALID",
    "FARMER_REFERENCE_RESPONSE_TOO_LARGE", "FARMER_REFERENCE_SOURCE_CONTRACT_INCOMPLETE",
    "FARMER_REFERENCE_INVALID_SOURCE_TIMESTAMP", "FARMER_REFERENCE_INVALID_PROFILE",
    "FARMER_REFERENCE_INVALID_VERIFIED_SNAPSHOT", "FARMER_REFERENCE_UNSAFE_SIGNATURE_ALGORITHM",
}


def _ids(identity: Mapping | None) -> str:
    identity = identity or {}
    parts = []
    if identity.get("farmer_id"):
        parts.append(f"Farmer ID {identity['farmer_id']}")
    if identity.get("fayda_fin"):
        parts.append(f"FIN {identity['fayda_fin']}")
    elif identity.get("fayda_fan_id"):
        parts.append(f"FAN {identity['fayda_fan_id']}")
    return " and ".join(parts) or "these IDs"


def explain(code: str, identity: Mapping | None = None, *, held=False) -> str:
    if not code.startswith("FARMER_REFERENCE_"):
        return code
    if code in MESSAGES:
        text = MESSAGES[code].format(ids=_ids(identity))
    elif code in UNAVAILABLE:
        text = "The Farmer Registry's answer about the farmer could not be verified. " + _TRY_AGAIN
    else:
        text = ("The farmer could not be linked to this intake. Nothing was submitted. "
                f"Tell the administrator the reference {code}")
    if held:
        # Ingestion has already saved the intake; the staff refusal wording
        # must not say otherwise in the durable worker diagnostic.
        text = text.replace("Nothing was submitted.", "This intake is saved and held.")
    return text
