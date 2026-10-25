"""Durable unresolved Farmer lookups; no trusted placeholder reference.

An intake may finalize and receive business approval while a signed lookup is
unavailable or has no approved match. Native activation remains held until a
real local Farmer ADD dependency has itself been approved. Retrying after the
business decision reuses the same outbox/dependency, never replays the intake.
"""

from datetime import UTC, datetime, timedelta

from openg2p_registry_core.models import G2PIntakeFormSubmission, G2PRegisterDefinition
from sqlalchemy import select

from .configuration import configured_lookup
from .models import FarmerPendingLookup
from .profile import FarmerReferenceError, intake_identity, matches_declared_identity


def pending_message(identity, code):
    field = "fayda_fin" if identity.get("fayda_fin") else "fayda_fan_id"
    label = "FIN" if field == "fayda_fin" else "FAN"
    if code in {
        "FARMER_REFERENCE_NOT_FOUND",
        "FARMER_REFERENCE_SOURCE_CONTRACT_INCOMPLETE",
    }:
        return (
            f"Farmer reference pending: no Farmer record found for {label} {identity[field]} yet; "
            "it will be linked after approval"
        )
    return (
        "Farmer reference pending: the approved Farmer could not be looked up. "
        "This intake can be approved; linking will retry when the Farmer Registry is available"
    )


async def resolve_or_wait(session, submission, row, *, lookup=None):
    from .native import onboarding_service

    declared = intake_identity(
        {
            "farmer_id": row.farmer_id,
            "fayda_fin": row.fayda_fin,
            "fayda_fan_id": row.fayda_fan_id,
        }
    )
    pending = await session.get(FarmerPendingLookup, submission.submission_id)
    if pending and pending.identity_json != declared:
        raise FarmerReferenceError("FARMER_REFERENCE_SUBMISSION_IDENTITY_CHANGED")
    if pending is None:
        pending = FarmerPendingLookup(
            submission_id=submission.submission_id,
            identity_json=declared,
            state="PENDING",
            attempts=0,
        )
        session.add(pending)
        await session.flush()
    pending.attempts += 1
    try:
        # A failed orchestration may have flushed a partial CR. Roll it back
        # before retaining the pending marker in the caller's native transaction.
        async with session.begin_nested():
            client = lookup or configured_lookup()
            if declared.get("fayda_fin"):
                source = await client.lookup(
                    declared.get("farmer_id"),
                    declared.get("fayda_fan_id"),
                    fin=declared["fayda_fin"],
                )
            else:
                source = await client.lookup(
                    declared.get("farmer_id"), declared["fayda_fan_id"]
                )
            if not matches_declared_identity(declared, source.profile):
                raise FarmerReferenceError("FARMER_REFERENCE_IDENTITY_MISMATCH")
            reference = await onboarding_service().ensure_dependency(
                session,
                submission.submission_id,
                source,
                requester=submission.created_by,
            )
    except FarmerReferenceError as error:
        code = str(error)
        if code in {
            "FARMER_REFERENCE_IDENTITY_MISMATCH",
            "FARMER_REFERENCE_SOURCE_ID_MISMATCH",
            "FARMER_REFERENCE_EXISTING_IDENTITY_AMBIGUOUS",
            "FARMER_REFERENCE_SUBMISSION_IDENTITY_CHANGED",
        }:
            raise
        pending.state, pending.last_error_code = "PENDING", code
        delay = min(3600, 60 * (2 ** min(pending.attempts - 1, 6)))
        pending.retry_after = datetime.now(UTC).replace(tzinfo=None) + timedelta(
            seconds=delay
        )
        row.farmer_reference_status = pending_message(declared, code)
        row.farmer_uuid = None
        await session.flush()
        return None
    pending.state, pending.last_error_code, pending.retry_after = "RESOLVED", None, None
    row.farmer_uuid = reference.local_farmer_id
    row.farmer_id = source.profile["farmer_id"]
    for field in ("fayda_fin", "fayda_fan_id"):
        if source.profile.get(field):
            setattr(row, field, source.profile[field])
    row.farmer_reference_status = (
        "Farmer reference linked"
        if reference.state == "APPROVED"
        else "Farmer reference awaiting approval; the record will be added after that approval"
    )
    await session.flush()
    return reference


async def retry_approved_pending(session, limit=10):
    from .native import domain_models

    now = datetime.now(UTC).replace(tzinfo=None)
    rows = (
        await session.execute(
            select(FarmerPendingLookup, G2PIntakeFormSubmission)
            .join(
                G2PIntakeFormSubmission,
                G2PIntakeFormSubmission.submission_id
                == FarmerPendingLookup.submission_id,
            )
            .where(
                FarmerPendingLookup.state == "PENDING",
                FarmerPendingLookup.retry_after <= now,
                G2PIntakeFormSubmission.draft_status == "FINAL",
                G2PIntakeFormSubmission.approval_status == "APPROVED",
            )
            .order_by(FarmerPendingLookup.submission_id)
            .limit(limit)
            .with_for_update(of=G2PIntakeFormSubmission, skip_locked=True)
        )
    ).all()
    for pending, submission in rows:
        definition = await session.get(G2PRegisterDefinition, submission.register_id)
        model = getattr(domain_models(), "G2PIntakeForm" + definition.register_mnemonic)
        roots = (
            (
                await session.execute(
                    select(model).where(model.submission_id == submission.submission_id)
                )
            )
            .scalars()
            .all()
        )
        if len(roots) != 1:
            pending.state, pending.last_error_code = (
                "BLOCKED",
                "FARMER_REFERENCE_SINGLE_DOMAIN_ROOT_REQUIRED",
            )
            continue
        try:
            async with session.begin_nested():
                await resolve_or_wait(session, submission, roots[0])
        except FarmerReferenceError as error:
            await session.refresh(pending)
            await session.refresh(roots[0])
            pending.state, pending.last_error_code = "BLOCKED", str(error)
            roots[
                0
            ].farmer_reference_status = "Farmer reference blocked: the declared identifiers do not match the approved Farmer. Ask the administrator to review this intake"

    await session.flush()
    return len(rows)
