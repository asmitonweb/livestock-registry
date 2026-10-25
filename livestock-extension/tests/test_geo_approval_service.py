"""The AWE integration adds server-computed place approvers only where a binding asks."""
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from openg2p_registry_core.errors import G2PRegistryException
    from openg2p_registry_core.services.g2p_awe_integration_service import (
        G2PAweIntegrationService,
    )

    from openg2p_registry_livestock_extension.geo_approval import directory, service
    from openg2p_registry_livestock_extension.geo_approval.scopes import Candidate
except ImportError:  # registry platform not installed
    service = None

GEO_FIELDS = ["geo_woreda_id", "geo_woreda_approver_ids"]
APPROVED = {"woreda_id": "woreda-A", "zone_id": "zone-A", "region_id": "region-A"}


class FakeDirectory:
    def __init__(self, *candidates):
        self.calls = 0
        self.items = candidates

    async def candidates(self, session, register_id, levels):
        self.calls += 1
        self.levels = levels
        return self.items


@unittest.skipUnless(service, "Requires the registry platform packages")
class GeoAweIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.policy = SimpleNamespace(context_field_names=GEO_FIELDS)
        finder = SimpleNamespace(find_effective_policy_configuration=mock.AsyncMock(side_effect=lambda *a, **k: self.policy))
        self.service = service.GeoAweIntegrationService.__new__(service.GeoAweIntegrationService)
        self.service.directory = FakeDirectory(Candidate("woreda.a", {"woreda": frozenset({"woreda-A"})}),
                                               Candidate("zone.a", {"zone": frozenset({"zone-A"})}),
                                               Candidate("woreda.b", {"woreda": frozenset({"woreda-B"})}))
        self.approved = dict(APPROVED)
        patches = [
            mock.patch.object(service.G2PAwePolicyConfigurationService, "get_component", return_value=finder),
            mock.patch.object(service.GeoAweIntegrationService, "_awe_enabled", return_value=True),
            mock.patch.object(service, "_approved_record", mock.AsyncMock(side_effect=lambda *a: self.approved)),
            mock.patch.object(G2PAweIntegrationService, "start_change_request_workflow", mock.AsyncMock()),
            mock.patch.object(G2PAweIntegrationService, "start_intake_submission_workflow", mock.AsyncMock()),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.change_request = SimpleNamespace(register_id="livestock", section_id="s1", internal_record_id="r1")

    async def change(self, payload):
        await self.service.start_change_request_workflow(None, self.change_request, payload,
                                                         bearer_token="t", requester="u")
        return G2PAweIntegrationService.start_change_request_workflow.await_args.args[2]

    async def test_change_request_routes_by_the_approved_place(self):
        payload = await self.change([{"woreda_id": "woreda-B", "geo_woreda_approver_ids": ["woreda.b"]}])
        context = payload[-1]
        self.assertEqual(context, {"geo_woreda_id": "woreda-A", "geo_woreda_approver_ids": ["woreda.a"]})
        self.assertEqual(self.service.directory.levels, ("woreda",))
        # Computed last, so a submitted value never reaches AWE.
        flat = {}
        for item in payload:
            flat.update(item)
        self.assertEqual(flat["geo_woreda_approver_ids"], ["woreda.a"])

    async def test_record_without_an_approved_place_uses_the_proposed_one(self):
        self.approved = {"woreda_id": None}
        payload = await self.change([{"woreda_id": "woreda-B"}])
        self.assertEqual(payload[-1]["geo_woreda_approver_ids"], ["woreda.b"])

    async def test_binding_without_geo_fields_is_untouched(self):
        self.policy = SimpleNamespace(context_field_names=["farmer_reference_approver_ids"])
        payload = [{"farmer_reference_approver_ids": ["x"]}]
        self.assertEqual(await self.change(payload), payload)
        self.assertEqual(self.service.directory.calls, 0)

    async def test_no_approver_refuses_with_an_explanation(self):
        self.service.directory = FakeDirectory()
        with self.assertRaises(G2PRegistryException) as raised:
            await self.change([])
        self.assertIn("No Woreda Approver is assigned for Woreda woreda-A", raised.exception.message)
        G2PAweIntegrationService.start_change_request_workflow.assert_not_awaited()

    async def test_intake_routes_by_the_submitted_place(self):
        submission = SimpleNamespace(register_id="livestock", form_id="f1")
        await self.service.start_intake_submission_workflow(
            None, submission, bearer_token="t", requester="u",
            source_data=[{"farmer_name": "x"}, {"woreda_id": "woreda-B", "zone_id": "zone-A"}])
        context = G2PAweIntegrationService.start_intake_submission_workflow.await_args.kwargs["source_data"][-1]
        self.assertEqual(context["geo_woreda_approver_ids"], ["woreda.b"])

    async def test_intake_without_a_place_is_explained(self):
        submission = SimpleNamespace(register_id="livestock", form_id="f1")
        with self.assertRaises(G2PRegistryException) as raised:
            await self.service.start_intake_submission_workflow(None, submission, bearer_token="t",
                                                                requester="u", source_data=[{}])
        self.assertIn("This submission has no Woreda", raised.exception.message)


class FakeKeycloak:
    """Role users and composite roles, as Keycloak's admin API returns them."""
    def __init__(self):
        self.holders = {"Woreda Approver": [{"id": "1", "username": "with.role", "enabled": True},
                                            {"id": "3", "username": "disabled", "enabled": False}]}
        self.roles = {"1": ["Woreda Approver", "DP_WOREDA_A"], "2": ["DP_WOREDA_A"],
                      "3": ["Woreda Approver", "DP_WOREDA_A"]}
        self._config = SimpleNamespace(keycloak_admin_url="k", keycloak_admin_client_id="c",
                                       keycloak_admin_client_secret="s", keycloak_client_id="r", keycloak_realm="staff")

    async def _resolve_client_uuid(self, _):
        return "uuid"

    def _registry_client_id(self):
        return "r"

    async def _admin_request(self, method, path, params=None):
        if "/roles/" in path:
            role = path.split("/roles/")[1].split("/")[0].replace("%20", " ")
            if role not in self.holders:
                raise G2PRegistryException(code="KC", message="Could not find role")
            return self.holders[role] if params["first"] == 0 else []
        return [{"name": name} for name in self.roles[path.split("/users/")[1].split("/")[0]]]


class FakeSession:
    async def execute(self, _):
        policy = SimpleNamespace(policy_mnemonic="WOREDA_A", policy_type="ALLOW", policy_filter_expression={
            "type": "CONDITION", "field_id": "woreda_id", "operator": "eq", "value": "woreda-A"})
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [policy]))


@unittest.skipUnless(service, "Requires the registry platform packages")
class GeoPolicyDirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_scope_needs_the_levels_role(self):
        with mock.patch.object(directory, "DataPolicyKeycloakHelper", FakeKeycloak):
            found = await directory.GeoPolicyDirectory().candidates(FakeSession(), "livestock", ("woreda",))
        self.assertEqual(found, (Candidate("with.role", {"woreda": frozenset({"woreda-A"})}),))

    async def test_missing_role_is_explained(self):
        with mock.patch.object(directory, "DataPolicyKeycloakHelper", FakeKeycloak),                 self.assertRaises(service.GeoApprovalError) as raised:
            await directory.GeoPolicyDirectory().candidates(FakeSession(), "livestock", ("zone",))
        self.assertIn('Keycloak has no client role "Zone Approver"', str(raised.exception))


if __name__ == "__main__":
    unittest.main()
