"""Committed outbox -> native AWE integration, with server-owned context."""
import os
import logging
from pathlib import Path
import httpx
from sqlalchemy import select

from openg2p_registry_core.models import G2PRegisterChangeRequest
from openg2p_registry_core.services.g2p_awe_integration_service import G2PAweIntegrationService, REGISTRY_CHANGE_REQUEST_ARTIFACT
from openg2p_registry_core.services.g2p_awe_policy_configuration_service import G2PAwePolicyConfigurationService

from .directory import PolicyDirectory
from .geography import approver_context
from .models import FarmerReference, FarmerReferenceRequest, FarmerWorkflowOutbox
from .profile import FarmerReferenceError

_logger = logging.getLogger("coss-farmer-reference-workflow")


async def service_bearer():
    try:
        url = os.environ["COSS_FARMER_AWE_TOKEN_URL"]
        # Match platform service-network URL configuration. Credentials go in
        # an OAuth request body, never a URL or a resolver query string.
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        if (parsed.username or parsed.password or parsed.query or parsed.fragment or
                not parsed.hostname or parsed.scheme not in {"http", "https"}):
            raise ValueError
        secret = Path(os.environ["COSS_FARMER_AWE_CLIENT_SECRET_FILE"]).read_text().strip()
        async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
            response = await client.post(url, data={"grant_type": "client_credentials",
                "client_id": os.environ["COSS_FARMER_AWE_CLIENT_ID"], "client_secret": secret})
            response.raise_for_status()
            token = response.json()["access_token"]
        if not isinstance(token, str) or not token.strip():
            raise ValueError
        return token
    except (KeyError, ValueError, OSError, httpx.HTTPError):
        raise FarmerReferenceError("FARMER_REFERENCE_WORKFLOW_IDENTITY_UNAVAILABLE") from None


async def deliver_workflow(session, entry, *, directory=None, bearer_provider=None, integration=None):
    request = await session.get(FarmerReferenceRequest, entry.request_id)
    if not request or request.state != "PENDING":
        entry.state = "CLOSED"
        return
    reference = await session.get(FarmerReference, request.reference_id, with_for_update=True)
    change_request = await session.get(G2PRegisterChangeRequest, entry.native_cr_id)
    if not change_request or change_request.approval_status != "PENDING":
        raise FarmerReferenceError("FARMER_REFERENCE_NATIVE_REQUEST_NOT_PENDING")
    from .native import domain_models
    approved_local = None
    if request.action == "UPDATE":
        farmer = await session.get(domain_models().G2PRegisterFarmer, reference.local_farmer_id)
        if not farmer or reference.state != "APPROVED" or not farmer.last_approved_at:
            raise FarmerReferenceError("FARMER_REFERENCE_APPROVED_LOCAL_REQUIRED")
        approved_local = {"woreda_id": farmer.woreda_id}
    candidates = await (directory or PolicyDirectory()).candidates(session, change_request.register_id)
    context = approver_context(request.action, candidates, verified_source=request.profile, approved_local=approved_local)
    policy = await G2PAwePolicyConfigurationService.get_component().find_effective_policy_configuration(session,
        register_id=change_request.register_id, policy_type=REGISTRY_CHANGE_REQUEST_ARTIFACT,
        section_id=change_request.section_id)
    from importlib import import_module
    variant = import_module("openg2p_registry_extensions").__variant__
    if (not policy or policy.policy_key != f"coss.{variant}.farmer-reference.v1" or
            not set(context) <= set(policy.context_field_names or [])):
        raise FarmerReferenceError("FARMER_REFERENCE_AWE_POLICY_CONTEXT_REQUIRED")
    service = integration or G2PAweIntegrationService.get_component()
    if not service._awe_enabled():
        raise FarmerReferenceError("FARMER_REFERENCE_AWE_REQUIRED")
    callback_url, callback_secret_id = service._callback_params()
    if not callback_url or not callback_secret_id:
        # AWE accepts callback-less requests. Such a decision can never reach
        # this registry, so retain a recoverable blocked outbox instead.
        raise FarmerReferenceError("FARMER_REFERENCE_AWE_CALLBACK_REQUIRED")
    token = await (bearer_provider or service_bearer)()
    # Only this server-produced context reaches native _build_context. Client
    # payloads cannot inject assignees, a scope, or internal source versions.
    await service.start_change_request_workflow(session, change_request, [context], bearer_token=token,
        requester=change_request.created_by or "system")
    if not change_request.awe_request_id:
        raise FarmerReferenceError("FARMER_REFERENCE_AWE_REQUEST_NOT_CREATED")
    entry.state, entry.last_error_code = "DELIVERED", None


async def dispatch_pending(session, limit=25):
    from .native import release_waiting_domains
    from .pending import retry_approved_pending
    await retry_approved_pending(session)
    await release_waiting_domains(session)
    entries = (await session.execute(select(FarmerWorkflowOutbox).where(
        FarmerWorkflowOutbox.state == "PENDING").order_by(FarmerWorkflowOutbox.native_cr_id)
        .limit(limit).with_for_update(skip_locked=True))).scalars().all()
    for entry in entries:
        entry.attempts += 1
        try:
            async with session.begin_nested():
                await deliver_workflow(session, entry)
        except Exception as error:
            # Never persist provider response bodies/tokens or identity values.
            code = str(error) if isinstance(error, FarmerReferenceError) else "FARMER_REFERENCE_WORKFLOW_DELIVERY_FAILED"
            entry.last_error_code = code
            cr = await session.get(G2PRegisterChangeRequest, entry.native_cr_id)
            if cr and not cr.awe_request_id:
                cr.awe_request_status_summary = "BLOCKED: Farmer reference approval workflow could not be started."
            _logger.warning("Farmer reference workflow delivery blocked: change_request_id=%s code=%s attempt=%s",
                            entry.native_cr_id, code, entry.attempts)
            if entry.attempts >= 5:
                entry.state = "BLOCKED"
    await session.flush()
    return len(entries)
