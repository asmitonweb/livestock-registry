"""Approved identity mirrors through native domain hooks, in their transaction.

The complete profile stays in the local Farmer reference. Only existing domain
identity/display columns are mirrored; plot/holding geography and business fields
are owned by the domain. This service never discovers or creates a relationship
from a mutable UID, a matching name, or a sibling submission.
"""
from sqlalchemy import select
from .profile import FarmerReferenceError, reference_identity, model_identity, normalize_farmer_id


async def _approved_farmer(session, farmer_model, reference_id):
    if not isinstance(reference_id, str) or not reference_id.strip():
        raise FarmerReferenceError("FARMER_REFERENCE_APPROVED_DEPENDENCY_REQUIRED")
    # Flush pending native approval fields before re-reading. The caller owns
    # commit/rollback; no nested independent commit may expose partial projection.
    await session.flush()
    farmer = (await session.execute(
        select(farmer_model).where(farmer_model.internal_record_id == reference_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if (farmer is None or farmer.record_status != "ACTIVE" or
            not farmer.last_approved_at or not farmer.last_approved_by):
        raise FarmerReferenceError("FARMER_REFERENCE_APPROVED_DEPENDENCY_REQUIRED")
    reference_identity(model_identity(farmer))
    return farmer


def _mirror(domain_row, farmer):
    if domain_row.farmer_uuid != farmer.internal_record_id:
        raise FarmerReferenceError("FARMER_REFERENCE_LINK_MISMATCH")
    farmer_id, uid = reference_identity(model_identity(farmer))
    if domain_row.farmer_id not in (None, ""):
        # A UID rotation on the same stable reference is allowed. A different
        # Farmer ID is an identity correction and cannot be silently relinked.
        existing_id = normalize_farmer_id(domain_row.farmer_id)
        if existing_id != farmer_id:
            raise FarmerReferenceError("FARMER_REFERENCE_LINK_IDENTITY_MISMATCH")
    parts = (farmer.first_name, farmer.middle_name, farmer.last_name)
    if any(value is not None and not isinstance(value, str) for value in parts):
        raise FarmerReferenceError("FARMER_REFERENCE_INVALID_PROFILE_VALUE")
    name = " ".join(value.strip() for value in parts if value and value.strip()) or None
    proposed = {"farmer_id": farmer_id, "fayda_fan_id": farmer.fayda_fan_id,
                "link_foundational_id": uid, "farmer_name": name}
    if hasattr(domain_row, "farmer_reference_status"):
        proposed["farmer_reference_status"] = "Farmer reference linked"
    fin = getattr(farmer, "fayda_fin", None)
    if fin and getattr(domain_row, "last_authentication_status", None) != "SUCCESS":
        proposed["fayda_fin"] = fin
    changed = any(getattr(domain_row, key) != value for key, value in proposed.items())
    for key, value in proposed.items():
        setattr(domain_row, key, value)
    if changed:
        domain_row.search_text = domain_row.get_search_text_fields()
        domain_row.record_name = domain_row.get_record_name_fields()
    return int(changed)


async def refresh_domain_on_ingest(session, farmer_model, domain_row):
    """Block activation without the already approved stable local dependency.

    The native ingester invokes post_ingest before committing. A failure rolls
    back its new live row. Onboarding must supply farmer_uuid; a reviewed link
    migration must handle old records rather than this hook choosing an owner.
    """
    farmer = await _approved_farmer(session, farmer_model, domain_row.farmer_uuid)
    changed = _mirror(domain_row, farmer)
    await session.flush()
    return changed
