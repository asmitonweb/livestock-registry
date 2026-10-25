"""Notification envelope validation; authoritative lookup stays in the native CR extension."""
from .profile import FarmerReferenceError, reference_identity, intake_identity

EVENT_TYPE = "farmer_reference.changed.v1"
EVENT_FIELDS = frozenset({"event_id", "event_type", "farmer_id", "fayda_fan_id", "fayda_fin"})
REQUIRED_EVENT_FIELDS = frozenset({"event_id", "event_type", "farmer_id"})


def notification_identity(data):
    if (not isinstance(data, dict) or not REQUIRED_EVENT_FIELDS <= set(data) or set(data) - EVENT_FIELDS or data.get("event_type") != EVENT_TYPE or
            not isinstance(data.get("event_id"), str) or not data["event_id"].strip() or len(data["event_id"]) > 256):
        raise FarmerReferenceError("FARMER_REFERENCE_INVALID_NOTIFICATION")
    return reference_identity(data)


def notification_change_identity(payload):
    rows = payload.change_payload
    if not isinstance(rows, list) or len(rows) != 1:
        raise FarmerReferenceError("FARMER_REFERENCE_INVALID_NOTIFICATION")
    row = rows[0].model_dump(exclude_unset=True)
    if row.get("edit_action") != "UPDATE" or row.get("internal_record_id") != payload.internal_record_id:
        raise FarmerReferenceError("FARMER_REFERENCE_INVALID_NOTIFICATION")
    # Native workers add action/internal ID. Legacy deployed templates may still
    # include demographic keys; those values are never used to build the CR.
    return reference_identity(row)


def notification_fields(data):
    # The receiving connector adds a routing hint inside its signed envelope.
    # Accept only this registry target; it never becomes an identity/profile.
    if isinstance(data, dict) and "register_mnemonic" in data:
        if data["register_mnemonic"] != "Farmer":
            raise FarmerReferenceError("FARMER_REFERENCE_INVALID_NOTIFICATION")
        data = {key: value for key, value in data.items() if key != "register_mnemonic"}
    notification_identity(data)
    return intake_identity(data, require_farmer=True)
