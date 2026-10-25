"""Native AWE integration with the record's geography approvers in the context.

A policy binding that lists geo_woreda_approver_ids (or the zone / region
ones) in context_field_names gets them computed here, on the server, and its
stage picks them with an expression rule {"var": "geo_woreda_approver_ids"}.
Bindings that do not list them are untouched, and a submitted payload can never
supply them: the computed values are applied last.

A change request routes by the record's approved place, so proposing a new
location cannot pick its own approver; an intake routes by its submitted place.
"""
from __future__ import annotations

from importlib import import_module

from openg2p_fastapi_common.context import component_registry
from openg2p_registry_core.errors import G2PRegistryErrorCodes, G2PRegistryException
from openg2p_registry_core.models import G2PRegisterDefinition
from openg2p_registry_core.services.g2p_awe_integration_service import (
    REGISTRY_CHANGE_REQUEST_ARTIFACT,
    REGISTRY_INTAKE_FORM_ARTIFACT,
    G2PAweIntegrationService,
)
from openg2p_registry_core.services.g2p_awe_policy_configuration_service import (
    G2PAwePolicyConfigurationService,
)

from .directory import GeoPolicyDirectory
from .scopes import (
    CONTEXT_FIELDS,
    LEVELS,
    GeoApprovalError,
    approver_context,
    record_place,
)


def _wanted(policy) -> set[str]:
    return set(CONTEXT_FIELDS) & set((policy.context_field_names or []) if policy else [])


def _refused(error: GeoApprovalError) -> G2PRegistryException:
    return G2PRegistryException(code=G2PRegistryErrorCodes.INVALID_REQUEST.value[1], message=str(error))


async def _approved_record(session, register_id, internal_record_id):
    definition = await session.get(G2PRegisterDefinition, register_id)
    model = definition and getattr(import_module("openg2p_registry_extensions.register_domain.models"),
                                   f"G2PRegister{definition.register_mnemonic}", None)
    record = model and await session.get(model, internal_record_id)
    return record.to_dict() if record is not None else None


class GeoAweIntegrationService(G2PAweIntegrationService):
    directory = GeoPolicyDirectory()

    async def _geo_context(self, session, register_id, wanted, *sources, label):
        try:
            place = record_place(*sources)
            levels = tuple(level for level in LEVELS if f"geo_{level}_approver_ids" in wanted)
            candidates = await self.directory.candidates(session, register_id, levels)
            return approver_context(place, candidates, wanted, record_label=label)
        except GeoApprovalError as error:
            raise _refused(error) from None

    async def start_change_request_workflow(self, session, change_request, change_payload, *,
                                            bearer_token, requester):
        if self._awe_enabled():
            policy = await G2PAwePolicyConfigurationService.get_component().find_effective_policy_configuration(
                session, register_id=change_request.register_id,
                policy_type=REGISTRY_CHANGE_REQUEST_ARTIFACT, section_id=change_request.section_id)
            wanted = _wanted(policy)
            if wanted:
                approved = await _approved_record(session, change_request.register_id,
                                                  change_request.internal_record_id)
                context = await self._geo_context(session, change_request.register_id, wanted,
                                                  approved, *(change_payload or []), label="This record")
                change_payload = [*(change_payload or []), context]
        await super().start_change_request_workflow(session, change_request, change_payload,
                                                    bearer_token=bearer_token, requester=requester)

    async def start_intake_submission_workflow(self, session, submission, *, bearer_token, requester,
                                               record_name=None, register_mnemonic=None,
                                               intake_form_mnemonic=None, source_data=None):
        if self._awe_enabled():
            policy = await G2PAwePolicyConfigurationService.get_component().find_effective_policy_configuration(
                session, register_id=submission.register_id,
                policy_type=REGISTRY_INTAKE_FORM_ARTIFACT, intake_form_id=submission.form_id)
            wanted = _wanted(policy)
            if wanted:
                context = await self._geo_context(session, submission.register_id, wanted,
                                                  *(source_data or []), label="This submission")
                source_data = [*(source_data or []), context]
        await super().start_intake_submission_workflow(
            session, submission, bearer_token=bearer_token, requester=requester, record_name=record_name,
            register_mnemonic=register_mnemonic, intake_form_mnemonic=intake_form_mnemonic,
            source_data=source_data)


def install() -> GeoAweIntegrationService:
    """Put the service ahead of core's in the component registry (call-time lookups)."""
    current = G2PAweIntegrationService.get_component()
    if isinstance(current, GeoAweIntegrationService):
        return current
    service = GeoAweIntegrationService()
    component_registry.remove(service)
    index = component_registry.index(current) if current in component_registry else len(component_registry)
    component_registry.insert(index, service)
    return service
