"""Small registered subclasses around native services, following Farmer 1.2.

No platform method is reassigned. CR construction, approval, history, ingestion,
outgestion and webhook authentication remain native implementations.
"""
from contextvars import ContextVar
from importlib import import_module
import os

from sqlalchemy import select
from openg2p_fastapi_common.context import get_async_session_maker
from openg2p_registry_core.models import (G2PRegisterDefinition, G2PRegisterSection, G2PRegisterUITabSection,
                                         G2PRegisterChangeRequest, G2PIntakeFormSubmission)
from openg2p_registry_core.errors import G2PRegistryErrorCodes, G2PRegistryException
from openg2p_registry_core.schemas import ChangeRequestRequestPayload, ChangePayload
from openg2p_registry_core.services.intake_form_data_service import G2PIntakeFormDataService
from openg2p_registry_core.services.g2p_register_change_request_service import G2PRegisterChangeRequestService
from openg2p_registry_core.services.g2p_change_request_worker_service import G2PChangeRequestWorkerService
from openg2p_registry_core.services.g2p_awe_webhook_service import G2PAweWebhookService

from .configuration import configured_lookup
from .models import FarmerReference, FarmerReferenceRequest, FarmerSubmissionDependency, FarmerPendingLookup
from .onboarding import OnboardingService
from .profile import FarmerReferenceError, reference_identity, intake_identity, model_identity, matches_declared_identity, PROFILE_FIELDS

_approved_callback = ContextVar("coss_farmer_approved_callback", default=None)


def domain_models():
    return import_module("openg2p_registry_extensions.register_domain.models")


async def farmer_definition(session):
    return (await session.execute(select(G2PRegisterDefinition).where(
        G2PRegisterDefinition.register_mnemonic == "Farmer"))).scalar_one()


async def create_native_request(session, local_id, profile, action, *, created_by="system"):
    definition = await farmer_definition(session)
    # Identity section UUIDs remain domain-owned; require one unambiguous native
    # section/tab binding instead of guessing a section or borrowing intake policy.
    bindings = (await session.execute(select(G2PRegisterSection, G2PRegisterUITabSection.tab_id).join(
        G2PRegisterUITabSection, G2PRegisterUITabSection.section_id == G2PRegisterSection.section_id).where(
            G2PRegisterSection.register_id == definition.register_id,
            G2PRegisterSection.section_register_id == definition.register_id,
            G2PRegisterSection.section_id.like("%farmer_identity_section_01")))).all()
    if len(bindings) != 1:
        raise FarmerReferenceError("FARMER_REFERENCE_SECTION_BINDING_REQUIRED")
    section, tab_id = bindings[0]
    if action not in {"ADD", "UPDATE"} or set(profile) - PROFILE_FIELDS:
        raise FarmerReferenceError("FARMER_REFERENCE_MANAGED_PAYLOAD_REQUIRED")
    reference_identity(profile)
    payload = {**profile, "internal_record_id": local_id, "edit_action": action}
    request = ChangeRequestRequestPayload(register_id=definition.register_id, register_mnemonic="Farmer",
        tab_id=tab_id, section_id=section.section_id, section_register_id=definition.register_id,
        internal_record_id=local_id, change_payload=[ChangePayload(**payload)])
    # Generic staff/worker creation intentionally permits only UPDATE for root
    # registers. This extension owns validated source-reference ADD orchestration
    # and uses native construction/persistence without a premature live record.
    # The same native approval/history/outgestion path applies afterwards.
    service = G2PRegisterChangeRequestService.get_component()
    await service._validate_domain_attributes([dict(profile)], "Farmer", section.section_ui_schema)
    cr = await service.construct_change_request(request, section, "Farmer", "Farmer",
        source_partner_id=os.environ.get("COSS_FARMER_SOURCE_PARTNER_ID") or None, created_by=created_by,
        change_request_source_override="INGESTION_PIPELINE", session=session)
    session.add(cr)
    session.add(cr._payload)
    await session.flush()
    return cr


def onboarding_service():
    return OnboardingService(domain_models().G2PRegisterFarmer, create_native_request)


async def record_native_decision(session, cr_id, approved):
    await onboarding_service().record_decision(session, cr_id, approved)
    request = (await session.execute(select(FarmerReferenceRequest).where(
        FarmerReferenceRequest.native_cr_id == cr_id))).scalar_one()
    submissions = (await session.execute(select(G2PIntakeFormSubmission).join(
        FarmerSubmissionDependency, FarmerSubmissionDependency.submission_id == G2PIntakeFormSubmission.submission_id).where(
            FarmerSubmissionDependency.reference_id == request.reference_id).with_for_update(
                of=G2PIntakeFormSubmission, skip_locked=True))).scalars().all()
    for submission in submissions:
        if not approved:
            if request.action == "ADD":
                submission.register_ingest_process_last_error_code = "FARMER_REFERENCE_DEPENDENCY_REJECTED"
            continue
        if (submission.approval_status == "APPROVED" and submission.draft_status == "FINAL" and
                submission.register_ingest_process_status == "NOT_APPLICABLE" and
                submission.register_ingest_process_last_error_code == "FARMER_REFERENCE_DEPENDENCY_PENDING"):
            # Release the original native ingest queue entry; don't recreate the
            # intake, change its approval, reset attempts, or replay business AWE.
            submission.register_ingest_process_status = "PENDING"
            submission.register_ingest_process_last_error_code = None
    await session.flush()


async def release_waiting_domains(session):
    """Recover a release skipped because a domain transaction held its row."""
    rows = (await session.execute(select(G2PIntakeFormSubmission, FarmerReference.state).join(
        FarmerSubmissionDependency, FarmerSubmissionDependency.submission_id == G2PIntakeFormSubmission.submission_id).join(
        FarmerReference, FarmerReference.reference_id == FarmerSubmissionDependency.reference_id).where(
            G2PIntakeFormSubmission.approval_status == "APPROVED",
            G2PIntakeFormSubmission.draft_status == "FINAL",
            G2PIntakeFormSubmission.register_ingest_process_status == "NOT_APPLICABLE",
            G2PIntakeFormSubmission.register_ingest_process_last_error_code == "FARMER_REFERENCE_DEPENDENCY_PENDING",
            FarmerReference.state.in_(["APPROVED", "REJECTED"])).with_for_update(
                of=G2PIntakeFormSubmission, skip_locked=True))).all()
    for submission, state in rows:
        if state == "APPROVED":
            submission.register_ingest_process_status = "PENDING"
            submission.register_ingest_process_last_error_code = None
        else:
            submission.register_ingest_process_last_error_code = "FARMER_REFERENCE_DEPENDENCY_REJECTED"
    await session.flush()
    return len(rows)


async def guard_farmer_approval(change_request, session):
    if _approved_callback.get() != change_request.change_request_id or not change_request.awe_request_id:
        raise FarmerReferenceError("FARMER_REFERENCE_AWE_APPROVAL_REQUIRED")
    return await onboarding_service().approval_guard(session, change_request.change_request_id)


class ReferenceIntakeService(G2PIntakeFormDataService):
    async def approve_submission(self, submission_id, approved_by):
        # The legacy staff endpoint does not call the session-aware AWE hook.
        # These registries require business decisions through their own AWE
        # policy, otherwise a manual call could enqueue an unresolved dependency.
        async with get_async_session_maker()() as session:
            submission = await self._get_submission_or_error(submission_id, session)
            definition = await self._get_register_definition(submission.register_id, session)
            if definition.register_mnemonic in {"CropSown", "Livestock", "Farmer"}:
                raise FarmerReferenceError("FARMER_REFERENCE_AWE_APPROVAL_REQUIRED")
        return await super().approve_submission(submission_id, approved_by)

    async def approve_submission_with_session(self, submission_id, session, approved_by=None):
        dependency = await session.get(FarmerSubmissionDependency, submission_id)
        reference = await session.get(FarmerReference, dependency.reference_id, with_for_update=True) if dependency else None
        submission = await super().approve_submission_with_session(submission_id, session, approved_by)
        definition = await self._get_register_definition(submission.register_id, session)
        if definition.register_mnemonic in {"CropSown", "Livestock"}:
            if not reference or reference.state != "APPROVED":
                submission.register_ingest_process_status = "NOT_APPLICABLE"
                pending_lookup = await session.get(FarmerPendingLookup, submission_id)
                suffix = "PENDING" if pending_lookup and pending_lookup.state == "PENDING" else "REQUIRED" if not reference else "REJECTED" if reference.state == "REJECTED" else "PENDING"
                submission.register_ingest_process_last_error_code = "FARMER_REFERENCE_DEPENDENCY_" + suffix
                await session.flush()
        return submission

    async def finalize_submission_with_session(self, submission_id, session, finalized_by=None,
                                                bearer_token=None, requester_sub=None):
        from .messages import explain
        # The service is a shared component: the declared IDs belong to this call.
        declared_ids = {}
        try:
            return await self._finalize_with_farmer(submission_id, session, finalized_by,
                                                    bearer_token, requester_sub, declared_ids)
        except FarmerReferenceError as error:
            raise G2PRegistryException(code=G2PRegistryErrorCodes.INVALID_REQUEST.value[1],
                                       message=explain(str(error), declared_ids)) from None

    async def _finalize_with_farmer(self, submission_id, session, finalized_by, bearer_token, requester_sub,
                                    declared_ids):
        # A person submitting (bearer present) is told at once; workers hold and retry.
        interactive = bool(bearer_token)
        submission = await self._get_submission_or_error(submission_id, session)
        await session.refresh(submission, with_for_update=True)
        definition = await self._get_register_definition(submission.register_id, session)
        if definition.register_mnemonic == "Farmer":
            raise FarmerReferenceError("FARMER_REFERENCE_CHANGE_REQUEST_REQUIRED")
        if definition.register_mnemonic in {"CropSown", "Livestock"}:
            models = domain_models()
            root = getattr(models, "G2PIntakeForm" + definition.register_mnemonic)
            rows = (await session.execute(select(root).where(root.submission_id == submission_id))).scalars().all()
            if len(rows) != 1:
                raise FarmerReferenceError("FARMER_REFERENCE_SINGLE_DOMAIN_ROOT_REQUIRED")
            row = rows[0]
            declared_ids.update({key: value for key, value in model_identity(row).items()
                                 if key in {"farmer_id", "fayda_fin", "fayda_fan_id"} and value})
            declared = intake_identity(model_identity(row))
            if not declared.get("farmer_id"):
                raise FarmerReferenceError("FARMER_REFERENCE_FARMER_ID_REQUIRED")
            for field, value in declared.items():
                setattr(row, field, value)
            sections = await self._get_form_sections(submission.form_id, session)
            farmer = await farmer_definition(session)
            if any(section.section_register_id == farmer.register_id for section in sections):
                raise FarmerReferenceError("FARMER_REFERENCE_INTAKE_METADATA_REQUIRES_ROOT_IDENTITY")
            dependency = await session.get(FarmerSubmissionDependency, submission_id)
            if dependency:
                reference = await session.get(FarmerReference, dependency.reference_id)
                if reference.state == "REJECTED":
                    raise FarmerReferenceError("FARMER_REFERENCE_DEPENDENCY_REJECTED")
                binding = dependency.identity_binding
                if binding:
                    if (binding.get("source_record_id") != reference.source_record_id or
                            binding.get("source_system") != reference.source_system):
                        raise FarmerReferenceError("FARMER_REFERENCE_SOURCE_ID_MISMATCH")
                    expected = binding["identity"]
                elif reference.state == "APPROVED":
                    local = await onboarding_service().require_approved_dependency(session, submission_id)
                    expected = model_identity(local)
                else:
                    request = await session.get(FarmerReferenceRequest, dependency.farmer_request_id)
                    expected = request.profile if request else {}
                if not matches_declared_identity(declared, expected):
                    raise FarmerReferenceError("FARMER_REFERENCE_SUBMISSION_IDENTITY_CHANGED")
                row.farmer_uuid, row.farmer_id = reference.local_farmer_id, reference.farmer_id
                row.farmer_reference_status = ("Farmer reference linked" if reference.state == "APPROVED" else
                    "Farmer reference awaiting approval; the record will be added after that approval")
            else:
                live = await session.get(getattr(models, "G2PRegister" + definition.register_mnemonic), row.internal_record_id)
                if live and live.last_approved_at and live.farmer_uuid:
                    if not matches_declared_identity(declared, model_identity(live)):
                        raise FarmerReferenceError("FARMER_REFERENCE_SUBMISSION_IDENTITY_CHANGED")
                    reference = await onboarding_service().link_approved_dependency(session, submission_id,
                        live.farmer_uuid, declared.get("farmer_id"), declared.get("fayda_fan_id"), fin=declared.get("fayda_fin"),
                        approved_domain_identity=model_identity(live), approved_domain_record_id=live.internal_record_id)
                    row.farmer_uuid, row.farmer_id = reference.local_farmer_id, reference.farmer_id
                    row.farmer_reference_status = "Farmer reference linked"
                else:
                    from .pending import resolve_or_wait
                    reference = await resolve_or_wait(session, submission, row, interactive=interactive)
                if reference is None:
                    submission.register_ingest_process_last_error_code = "FARMER_REFERENCE_DEPENDENCY_PENDING"
            await session.flush()
        if not bearer_token:
            # Native ingestion workers have no human bearer. A narrowly scoped
            # service account submits the normal intake policy; never an admin fallback.
            from .workflow import service_bearer
            bearer_token = await service_bearer()
        return await super().finalize_submission_with_session(submission_id, session, finalized_by,
            bearer_token=bearer_token, requester_sub=requester_sub)

    async def _insert_live_register_row(self, submission, section, register_definition,
                                        schema_class, register_class, intake_row, session):
        root_definition = await self._get_register_definition(submission.register_id, session)
        if root_definition.register_mnemonic in {"CropSown", "Livestock"}:
            if register_definition.register_mnemonic == "Farmer":
                raise FarmerReferenceError("FARMER_REFERENCE_LEGACY_INTAKE_SECTION_BLOCKED")
            approved = await onboarding_service().require_approved_dependency(session, submission.submission_id)
            if register_definition.register_mnemonic == root_definition.register_mnemonic:
                if intake_row.farmer_uuid != approved.internal_record_id:
                    raise FarmerReferenceError("FARMER_REFERENCE_SUBMISSION_IDENTITY_CHANGED")
        return await super()._insert_live_register_row(submission, section, register_definition,
            schema_class, register_class, intake_row, session)


class ReferenceChangeRequestService(G2PRegisterChangeRequestService):
    async def create_change_request(self, change_request_request_payload, *args, **kwargs):
        async with get_async_session_maker()() as session:
            section = await session.get(G2PRegisterSection, change_request_request_payload.section_id) if change_request_request_payload.section_id else None
            definition = await session.get(G2PRegisterDefinition, section.section_register_id) if section else None
            if definition and definition.register_mnemonic == "Farmer":
                raise FarmerReferenceError("FARMER_REFERENCE_VERIFIED_SOURCE_REQUEST_REQUIRED")
        return await super().create_change_request(change_request_request_payload, *args, **kwargs)

    async def approve_change_request_from_awe_webhook(self, change_request_id, session, approved_by=None):
        token = _approved_callback.set(change_request_id)
        try:
            return await super().approve_change_request_from_awe_webhook(change_request_id, session, approved_by)
        finally:
            _approved_callback.reset(token)

    async def approve_register(self, change_request, section, session):
        definition = await self.validate_register_definition(section.section_register_id, session)
        if definition.register_mnemonic != "Farmer":
            return await super().approve_register(change_request, section, session)
        reference, request = await guard_farmer_approval(change_request, session)
        payload = await self._get_change_request_payload(change_request.change_request_id, session)
        if len(payload.change_payload or []) != 1:
            raise FarmerReferenceError("FARMER_REFERENCE_MANAGED_PAYLOAD_REQUIRED")
        actual = payload.change_payload[0]
        profile = {field: value for field, value in actual.items() if field in PROFILE_FIELDS}
        if (profile != request.profile or actual.get("edit_action") != request.action or
                set(actual) - (PROFILE_FIELDS | {"internal_record_id", "edit_action", "documents"}) or
                actual.get("documents") not in (None, []) or
                actual.get("internal_record_id") != reference.local_farmer_id or
                change_request.internal_record_id != reference.local_farmer_id):
            raise FarmerReferenceError("FARMER_REFERENCE_MANAGED_PAYLOAD_CHANGED")
        if request.action == "ADD":
            if await session.get(domain_models().G2PRegisterFarmer, reference.local_farmer_id):
                raise FarmerReferenceError("FARMER_REFERENCE_ADD_ALREADY_RESOLVED")
            if definition.functional_id_generation_required:
                raise FarmerReferenceError("FARMER_REFERENCE_EXTERNAL_IDENTIFIER_CONFIGURATION_REQUIRED")
            await self.insert_primary_master_section_into_register(change_request, session)
            await session.flush()
            farmer = await session.get(domain_models().G2PRegisterFarmer, reference.local_farmer_id)
            # Native UPDATE classification resolves functional_record_id. For a
            # newly approved source reference, derive it from the verified pair.
            # Never rewrite an existing record's internal or functional identity.
            if farmer.functional_record_id not in (None, "", request.profile["farmer_id"]):
                raise FarmerReferenceError("FARMER_REFERENCE_LOCAL_IDENTITY_CONFLICT")
            farmer.functional_record_id = request.profile["farmer_id"]
            await session.flush()
        else:
            await super().approve_register(change_request, section, session)
        if any(field in request.profile for field in ("first_name", "middle_name", "last_name")):
            farmer = await session.get(domain_models().G2PRegisterFarmer, reference.local_farmer_id)
            farmer.farmer_name = " ".join(value.strip() for value in
                (farmer.first_name, farmer.middle_name, farmer.last_name) if value and value.strip()) or None
            await session.flush()


class ReferenceWebhookService(G2PAweWebhookService):
    async def _apply_terminal_event_for_change_request(self, event, session):
        await super()._apply_terminal_event_for_change_request(event, session)
        if event.event_type in {"request_rejected", "request_cancelled"}:
            managed = (await session.execute(select(FarmerReferenceRequest).where(
                FarmerReferenceRequest.native_cr_id == event.artifact_id))).scalar_one_or_none()
            if managed:
                await record_native_decision(session, event.artifact_id, False)


class ReferenceIngestionService(G2PChangeRequestWorkerService):
    async def create_change_request(self, change_request_request_payload, session,
                                    source_partner_id=None, created_by=None, change_request_source=None):
        payload = change_request_request_payload
        definition = await session.get(G2PRegisterDefinition, payload.section_register_id)
        if not definition or definition.register_mnemonic != "Farmer":
            return await super().create_change_request(payload, session, source_partner_id, created_by, change_request_source)
        expected_partner = os.environ.get("COSS_FARMER_SOURCE_PARTNER_ID", "").strip()
        if not expected_partner or source_partner_id != expected_partner or change_request_source != "INGESTION_PIPELINE":
            raise FarmerReferenceError("FARMER_REFERENCE_SOURCE_PARTNER_REQUIRED")
        reference = (await session.execute(select(FarmerReference).where(
            FarmerReference.local_farmer_id == payload.internal_record_id))).scalar_one_or_none()
        if not reference or reference.state != "APPROVED":
            raise FarmerReferenceError("FARMER_REFERENCE_UNREFERENCED_NOTIFICATION")
        from .notification import notification_change_identity
        notification_pair = notification_change_identity(payload)
        if notification_pair[0] != reference.farmer_id:
            raise FarmerReferenceError("FARMER_REFERENCE_IDENTITY_MISMATCH")
        section = await session.get(G2PRegisterSection, payload.section_id)
        if (payload.register_id != definition.register_id or not section or
                section.register_id != definition.register_id or section.section_register_id != definition.register_id or
                "identity" not in section.section_id):
            raise FarmerReferenceError("FARMER_REFERENCE_INVALID_NOTIFICATION_SECTION")
        client = configured_lookup()
        if client.config.source_id != reference.source_system:
            raise FarmerReferenceError("FARMER_REFERENCE_SOURCE_ID_MISMATCH")
        # Notification demographic values are deliberately not read.
        lookup_identifier = (notification_pair[1] if len(notification_pair[1]) == 12 and len(reference.uid) == 16
                             else reference.uid)
        current = await client.lookup_current(reference.farmer_id, lookup_identifier, reference.source_record_id)
        # An approved UID correction may be notified using the new UID. Only
        # the signed, consented lookup pinned to this same immutable source
        # person can establish that correction; the event cannot authorize it.
        if notification_pair[1] not in {reference.uid, current.profile.get("fayda_fan_id"), current.profile.get("fayda_fin")}:
            raise FarmerReferenceError("FARMER_REFERENCE_IDENTITY_MISMATCH")
        request = await onboarding_service().queue_current_update(session, current)
        if request is None:
            request = (await session.execute(select(FarmerReferenceRequest).where(
                FarmerReferenceRequest.reference_id == reference.reference_id,
                FarmerReferenceRequest.source_version == current.source_version,
                FarmerReferenceRequest.state == "APPROVED"))).scalar_one_or_none()
            if not request:
                raise FarmerReferenceError("FARMER_REFERENCE_STALE_SOURCE_VERSION")
        return await session.get(G2PRegisterChangeRequest, request.native_cr_id)
