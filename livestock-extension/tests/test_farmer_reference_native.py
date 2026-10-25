"""Pinned native CR construction/approval against a disposable PostgreSQL schema."""
import importlib
import os
import sys
import unittest
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

INTAKE_ID = "9b72d0b5-043e-4f5d-9f9f-75e40688faae"
PACKAGE = os.environ.get("REGISTRY_EXTENSION_MODULE", "openg2p_registry_livestock_extension")
if os.environ.get("FARMER_TEST_DATABASE_URL"):
    if os.environ.get("FARMER_WORKFLOW_SIMULATION_URL"):
        # Native helpers capture configuration at initialization; configure
        # them before the app constructs its singleton components.
        for prefix in ("registry_core_", "registry_staff_portal_api_"):
            os.environ[prefix + "awe_base_url"] = os.environ["FARMER_WORKFLOW_SIMULATION_URL"] + "/awe"
            os.environ[prefix + "awe_enabled"] = "true"
    # Standalone integration runner: don't initialize a second domain alias
    # merely because ordinary CI discovers this dependency-gated test module.
    sys.modules["openg2p_registry_extensions"] = importlib.import_module(PACKAGE)
    importlib.import_module("openg2p_registry_staff_api.config").Settings.get_config()
    app = importlib.import_module("openg2p_registry_extensions.app")
    app.Initializer()
    native = importlib.import_module("openg2p_registry_extensions.farmer_reference.native")
    ledger = importlib.import_module("openg2p_registry_extensions.farmer_reference.models")
    lookup = importlib.import_module("openg2p_registry_extensions.farmer_reference.lookup")
    Error = importlib.import_module("openg2p_registry_extensions.farmer_reference.profile").FarmerReferenceError
    from openg2p_fastapi_common.models import BaseORMModel
    from openg2p_registry_core.models import (G2PRegisterDefinition, G2PRegisterSection, G2PRegisterUITab,
        G2PRegisterUITabSection, G2PRegisterChangeRequest, G2PRegisterChangeRequestPayload, G2PRegisterSchema)


@unittest.skipUnless(os.environ.get("FARMER_TEST_DATABASE_URL"), "Requires isolated PostgreSQL")
class NativeReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url = os.environ["FARMER_TEST_DATABASE_URL"]
        if not url.startswith("postgresql+asyncpg://"):
            self.fail("Native acceptance requires PostgreSQL")
        self.schema = "coss_ref_test_" + uuid4().hex
        control = create_async_engine(url)
        async with control.begin() as connection:
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public"))
            await connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
        await control.dispose()
        self.engine = create_async_engine(url, connect_args={"server_settings":{"search_path":self.schema + ",public"}},
                                          execution_options={"schema_translate_map": {None:self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(BaseORMModel.metadata.create_all)
        self.farmer_id = ("fa120001-0000-4000-8000-000000000001" if "cropsown" in PACKAGE else
                          "f9c6a359-9563-5a43-b0fe-6c7e452037a3")
        self.section_id = "test_farmer_identity_section_01"
        async with self.sessions.begin() as session:
            definition = G2PRegisterDefinition(register_id=self.farmer_id, register_mnemonic="Farmer",
                register_subject="Farmers", register_description="Synthetic Farmer references",
                register_purpose="REGISTER", register_rank=1, functional_id_generation_required=False,
                outgest_applicable=False, dedup_is_enabled=False, completion_score_required=False,
                has_image=False, requires_registrant_authentication=False)
            section = G2PRegisterSection(register_id=self.farmer_id, section_id=self.section_id,
                section_register_id=self.farmer_id, section_mnemonic="test_farmer_identity",
                section_ui_schema={"panels":[]}, no_of_verifications_required=0)
            tab = G2PRegisterUITab(tab_id="test_farmer_tab", register_id=self.farmer_id, tab_label="farmer", tab_order=1, is_active=True)
            binding = G2PRegisterUITabSection(tab_section_id="test-farmer-binding", register_id=self.farmer_id,
                tab_id=tab.tab_id, section_id=self.section_id, section_order=1)
            session.add_all([definition, section, tab, binding])
            await session.flush()
        from datetime import datetime, timezone
        self.snapshot = lookup.VerifiedFarmer("synthetic-source", "source-person-1", 100,
            {"farmer_id":"FR-000000000001", "fayda_fan_id":"0000000000000001",
             "first_name":"Verified", "last_name":"Farmer", "woreda_id":"woreda-1"},
            datetime.now(timezone.utc), "synthetic-digest")
        self.service = native.onboarding_service()

    async def asyncTearDown(self):
        await self.engine.dispose()
        # The only recursive drop target is the schema generated above, never
        # public or an environment-owned database/schema.
        self.assertRegex(self.schema, r"^coss_ref_test_[0-9a-f]{32}$")
        control = create_async_engine(os.environ["FARMER_TEST_DATABASE_URL"])
        async with control.begin() as connection:
            await connection.execute(text('DROP SCHEMA "' + self.schema + '" CASCADE'))
        await control.dispose()

    async def test_native_constructor_creates_real_cr_payload_without_live_farmer(self):
        async with self.sessions.begin() as session:
            reference = await self.service.ensure_dependency(session, INTAKE_ID, self.snapshot, requester="synthetic-creator")
            request = (await session.execute(select(ledger.FarmerReferenceRequest))).scalar_one()
            cr = await session.get(G2PRegisterChangeRequest, request.native_cr_id)
            payload = await session.get(G2PRegisterChangeRequestPayload, request.native_cr_id)
            self.assertEqual(cr.approval_status, "PENDING")
            self.assertEqual(cr.created_by, "synthetic-creator")
            self.assertEqual(cr.internal_record_id, reference.local_farmer_id)
            self.assertEqual(payload.change_payload[0]["edit_action"], "ADD")
            self.assertEqual(payload.change_payload[0]["farmer_id"], "FR-000000000001")
            self.assertIsNone(await session.get(native.domain_models().G2PRegisterFarmer, reference.local_farmer_id))

    async def test_manual_approval_of_managed_reference_is_blocked(self):
        async with self.sessions.begin() as session:
            await self.service.ensure_dependency(session, INTAKE_ID, self.snapshot)
            request = (await session.execute(select(ledger.FarmerReferenceRequest))).scalar_one()
            cr = await session.get(G2PRegisterChangeRequest, request.native_cr_id)
            with self.assertRaisesRegex(Error, "AWE_APPROVAL_REQUIRED"):
                await native.guard_farmer_approval(cr, session)

    async def test_legacy_staff_approval_cannot_enqueue_an_unapproved_domain_dependency(self):
        from datetime import datetime
        from unittest.mock import patch
        from openg2p_registry_core.models import G2PIntakeFormSubmission
        submission_id = str(uuid4())
        async with self.sessions.begin() as session:
            session.add(G2PRegisterDefinition(register_id="synthetic-domain", register_mnemonic=
                "CropSown" if "cropsown" in PACKAGE else "Livestock", register_subject="Synthetic",
                register_purpose="REGISTER", register_rank=1, functional_id_generation_required=False))
            session.add(G2PIntakeFormSubmission(submission_id=submission_id, form_id="synthetic-form",
                register_id="synthetic-domain", draft_status="FINAL", approval_status="PENDING",
                created_by="synthetic-creator", first_created_at=datetime.now(), last_updated_at=datetime.now(),
                submission_source="STAFF_PORTAL", register_ingest_process_status="NOT_APPLICABLE",
                number_of_verifications_required=0, number_of_verifications_done=0))
        with patch.object(native, "get_async_session_maker", return_value=self.sessions):
            with self.assertRaisesRegex(Error, "AWE_APPROVAL_REQUIRED"):
                await native.G2PIntakeFormDataService.get_component().approve_submission(submission_id, "manual-user")
        async with self.sessions() as session:
            submission = await session.get(G2PIntakeFormSubmission, submission_id)
            self.assertEqual(submission.approval_status, "PENDING")
            self.assertEqual(submission.register_ingest_process_status, "NOT_APPLICABLE")

    async def test_native_approved_callback_adds_farmer_history_and_releases_dependency(self):
        async with self.sessions.begin() as session:
            await self.service.ensure_dependency(session, INTAKE_ID, self.snapshot)
            request = (await session.execute(select(ledger.FarmerReferenceRequest))).scalar_one()
            cr = await session.get(G2PRegisterChangeRequest, request.native_cr_id)
            cr.awe_request_id = "synthetic-awe-request"
            await session.flush()
            await native.G2PRegisterChangeRequestService.get_component().approve_change_request_from_awe_webhook(
                cr.change_request_id, session, approved_by="synthetic-reviewer")
            farmer = await self.service.require_approved_dependency(session, INTAKE_ID)
            self.assertEqual(farmer.first_name, "Verified")
            self.assertEqual(farmer.farmer_name, "Verified Farmer")
            self.assertEqual(farmer.last_approved_by, "synthetic-reviewer")
            self.assertEqual(farmer.functional_record_id, self.snapshot.profile["farmer_id"])
            history = (await session.execute(select(native.domain_models().G2PRegisterHistoryFarmer))).scalars().all()
            self.assertEqual(len(history), 1)

    async def _approved_reference(self):
        async with self.sessions.begin() as session:
            reference = await self.service.ensure_dependency(session, INTAKE_ID, self.snapshot)
            request = (await session.execute(select(ledger.FarmerReferenceRequest))).scalar_one()
            cr = await session.get(G2PRegisterChangeRequest, request.native_cr_id)
            cr.awe_request_id = "synthetic-awe-add"
            await session.flush()
            await native.G2PRegisterChangeRequestService.get_component().approve_change_request_from_awe_webhook(
                cr.change_request_id, session, approved_by="synthetic-reviewer")
            return reference.local_farmer_id

    async def test_native_farmer_update_preserves_omissions_and_does_not_write_linked_domain(self):
        from dataclasses import replace
        from datetime import date, datetime
        local_id = await self._approved_reference()
        models = native.domain_models()
        domain = getattr(models, "G2PRegisterCropSown", None) or models.G2PRegisterLivestock
        domain_geo = "woreda_id" if hasattr(domain, "woreda_id") else "woreda"
        async with self.sessions.begin() as session:
            farmer = await session.get(models.G2PRegisterFarmer, local_id)
            farmer.email = "approved@example.invalid"
            farmer.date_of_birth = date(1990, 1, 1)
            session.add(domain(internal_record_id="synthetic-domain", farmer_uuid=local_id,
                farmer_id=farmer.farmer_id, fayda_fan_id=farmer.fayda_fan_id, farmer_name="Verified Farmer",
                record_status="ACTIVE", created_by="synthetic-creator", created_at=datetime.now(),
                last_approved_at=datetime.now(), last_approved_by="business-reviewer", **{domain_geo:"domain-woreda"}))
            proposed = replace(self.snapshot, source_version=200, profile={**self.snapshot.profile, "first_name":"Approved new", "woreda_id":"new-source-woreda"})
            request = await self.service.queue_current_update(session, proposed)
            self.assertEqual((await session.get(domain, "synthetic-domain")).farmer_name, "Verified Farmer")
            cr = await session.get(G2PRegisterChangeRequest, request.native_cr_id)
            cr.awe_request_id = "synthetic-awe-update"
            await session.flush()
            await native.G2PRegisterChangeRequestService.get_component().approve_change_request_from_awe_webhook(
                cr.change_request_id, session, approved_by="source-update-reviewer")
            await session.refresh(farmer)
            linked = await session.get(domain, "synthetic-domain")
            self.assertEqual(farmer.email, "approved@example.invalid")
            self.assertEqual(farmer.date_of_birth, date(1990, 1, 1))
            self.assertEqual(farmer.farmer_name, "Approved new Farmer")
            self.assertEqual(linked.farmer_name, "Verified Farmer")
            self.assertEqual(getattr(linked, domain_geo), "domain-woreda")
            self.assertEqual(linked.last_approved_by, "business-reviewer")

    async def test_native_rejection_closes_managed_dependency_and_duplicate_callback_is_idempotent(self):
        from types import SimpleNamespace
        async with self.sessions.begin() as session:
            await self.service.ensure_dependency(session, INTAKE_ID, self.snapshot)
            request = (await session.execute(select(ledger.FarmerReferenceRequest))).scalar_one()
            cr = await session.get(G2PRegisterChangeRequest, request.native_cr_id)
            cr.awe_request_id = "synthetic-awe-rejected"
            event = SimpleNamespace(event_type="request_rejected", artifact_id=cr.change_request_id,
                                    request_id=cr.awe_request_id, actor="synthetic-reviewer")
            service = native.G2PAweWebhookService.get_component()
            await service._apply_terminal_event_for_change_request(event, session)
            await service._apply_terminal_event_for_change_request(event, session)
            self.assertEqual(cr.approval_status, "REJECTED")
            self.assertEqual(request.state, "REJECTED")
            with self.assertRaisesRegex(Error, "DEPENDENCY_REJECTED"):
                await self.service.require_approved_dependency(session, INTAKE_ID)

    async def test_changed_native_payload_cannot_bypass_verified_snapshot(self):
        async with self.sessions.begin() as session:
            await self.service.ensure_dependency(session, INTAKE_ID, self.snapshot)
            request = (await session.execute(select(ledger.FarmerReferenceRequest))).scalar_one()
            cr_id = request.native_cr_id
            cr = await session.get(G2PRegisterChangeRequest, cr_id)
            cr.awe_request_id = "synthetic-awe"
            payload = await session.get(G2PRegisterChangeRequestPayload, cr_id)
            payload.change_payload = [{**payload.change_payload[0], "first_name":"Forged"}]
        with self.assertRaisesRegex(Error, "MANAGED_PAYLOAD_CHANGED"):
            async with self.sessions.begin() as session:
                await native.G2PRegisterChangeRequestService.get_component().approve_change_request_from_awe_webhook(
                    cr_id, session, approved_by="synthetic-reviewer")
        async with self.sessions() as session:
            self.assertEqual((await session.get(G2PRegisterChangeRequest, cr_id)).approval_status, "PENDING")

    async def test_domain_approval_waits_without_worker_attempts_then_releases_same_native_submission(self):
        from datetime import datetime
        from openg2p_registry_core.models import G2PIntakeFormSubmission
        submission_id = str(uuid4())
        async with self.sessions.begin() as session:
            mnemonic = "CropSown" if "cropsown" in PACKAGE else "Livestock"
            session.add(G2PRegisterDefinition(register_id="synthetic-domain-register", register_mnemonic=mnemonic,
                register_subject="Synthetic domain", register_purpose="REGISTER", register_rank=1,
                functional_id_generation_required=False))
            submission = G2PIntakeFormSubmission(submission_id=submission_id, form_id="synthetic-form",
                register_id="synthetic-domain-register", draft_status="FINAL", approval_status="PENDING",
                created_by="synthetic-creator", first_created_at=datetime.now(), last_updated_at=datetime.now(),
                submission_source="STAFF_PORTAL", register_ingest_process_status="NOT_APPLICABLE",
                number_of_verifications_required=0, number_of_verifications_done=0)
            session.add(submission)
            await self.service.ensure_dependency(session, submission_id, self.snapshot)
            await native.G2PIntakeFormDataService.get_component().approve_submission_with_session(
                submission_id, session, approved_by="business-reviewer")
            self.assertEqual(submission.approval_status, "APPROVED")
            self.assertEqual(submission.register_ingest_process_status, "NOT_APPLICABLE")
            self.assertEqual(submission.register_ingest_process_attempts, 0)
            request = (await session.execute(select(ledger.FarmerReferenceRequest))).scalar_one()
            cr = await session.get(G2PRegisterChangeRequest, request.native_cr_id)
            cr.awe_request_id = "synthetic-awe-add"
            await session.flush()
            await native.G2PRegisterChangeRequestService.get_component().approve_change_request_from_awe_webhook(
                cr.change_request_id, session, approved_by="reference-reviewer")
            self.assertEqual(submission.register_ingest_process_status, "PENDING")
            self.assertEqual(submission.approved_by, "business-reviewer")
            self.assertEqual(submission.register_ingest_process_attempts, 0)
            self.assertIsNone(submission.register_ingest_process_last_error_code)

    async def test_workflow_context_uses_approved_old_geography_and_server_resolved_ids(self):
        from dataclasses import replace
        from openg2p_registry_core.models import G2PRegistryAwePolicyConfiguration
        workflow = importlib.import_module("openg2p_registry_extensions.farmer_reference.workflow")
        geo = importlib.import_module("openg2p_registry_extensions.farmer_reference.geography")
        local_id = await self._approved_reference()
        class Directory:
            async def candidates(self, session, register_id):
                return (geo.ApproverEligibility("old-reviewer", frozenset({geo.FARMER_APPROVER_ROLE}), frozenset({"woreda-1"})),
                        geo.ApproverEligibility("proposed-reviewer", frozenset({geo.FARMER_APPROVER_ROLE}), frozenset({"proposed-woreda"})))
        contexts = []
        class Integration:
            def _awe_enabled(self):
                return True
            def _callback_params(self):
                return "http://synthetic-registry.invalid/callback", "synthetic-callback"
            async def start_change_request_workflow(self, session, cr, context, **kwargs):
                contexts.append(context)
                cr.awe_request_id = "synthetic-awe-update"
        async def bearer():
            return "synthetic-test-token"
        async with self.sessions.begin() as session:
            session.add(G2PRegistryAwePolicyConfiguration(awe_policy_config_id=str(uuid4()),
                policy_scope="SECTION", register_id=self.farmer_id, section_id=self.section_id,
                policy_type="registry.change_request", policy_key=f"coss.{sys.modules['openg2p_registry_extensions'].__variant__}.farmer-reference.v1",
                context_field_names=["farmer_reference_woreda_id", "farmer_reference_approver_ids"]))
            request = await self.service.queue_current_update(session, replace(self.snapshot, source_version=200,
                profile={**self.snapshot.profile, "woreda_id":"proposed-woreda", "first_name":"Proposed"}))
            entry = await session.get(ledger.FarmerWorkflowOutbox, request.native_cr_id)
            from unittest.mock import patch
            for callback in ((None, "synthetic-callback"),
                             ("http://synthetic-registry.invalid/callback", None)):
                with patch.object(Integration, "_callback_params", return_value=callback):
                    with self.assertRaisesRegex(Error, "FARMER_REFERENCE_AWE_CALLBACK_REQUIRED"):
                        await workflow.deliver_workflow(session, entry, directory=Directory(), bearer_provider=bearer, integration=Integration())
                self.assertEqual(contexts, [])
                self.assertEqual(entry.state, "PENDING")
            await workflow.deliver_workflow(session, entry, directory=Directory(), bearer_provider=bearer, integration=Integration())
            self.assertEqual(contexts, [[{"farmer_reference_woreda_id":"woreda-1", "farmer_reference_approver_ids":["old-reviewer"]}]])
            self.assertEqual(entry.state, "DELIVERED")
            farmer = await session.get(native.domain_models().G2PRegisterFarmer, local_id)
            self.assertEqual(farmer.woreda_id, "woreda-1")
            self.assertEqual(farmer.first_name, "Verified")

    async def _workflow_http_setup(self, mode="valid"):
        """Use real native HTTP helpers; only URLs/configuration point at Postman."""
        import httpx
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from openg2p_registry_core.config import Settings as CoreSettings
        from openg2p_registry_staff_api.config import Settings as StaffSettings
        from openg2p_registry_core.helpers import AweHelper
        from openg2p_registry_core.models import G2PRegistryAwePolicyConfiguration, G2PRegistryDataPolicy
        base = os.environ["FARMER_WORKFLOW_SIMULATION_URL"]
        async with httpx.AsyncClient() as client:
            response = await client.post(base + "/workflow-configure", json={"mode":mode,"reset":True})
            response.raise_for_status()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        secret_file = Path(temporary.name) / "synthetic-client-secret"
        secret_file.write_text("synthetic-test-secret")
        env = patch.dict(os.environ, {"COSS_FARMER_AWE_TOKEN_URL":base + "/iam/realms/test/protocol/openid-connect/token",
            "COSS_FARMER_AWE_CLIENT_ID":"synthetic-client", "COSS_FARMER_AWE_CLIENT_SECRET_FILE":str(secret_file)})
        env.start()
        self.addCleanup(env.stop)
        for config in (CoreSettings.get_config(strict=False), StaffSettings.get_config(strict=False)):
            for name, value in {"awe_enabled":True, "awe_base_url":base + "/awe",
                    "awe_default_callback_url":"http://synthetic-registry.invalid/callback",
                    "awe_callback_secret_id":"synthetic-callback"}.items():
                setting = patch.object(config, name, value)
                setting.start()
                self.addCleanup(setting.stop)
        core = CoreSettings.get_config(strict=False)
        for name, value in {"keycloak_admin_url":base + "/iam", "keycloak_admin_realm":"test",
                "keycloak_admin_client_id":"synthetic-directory", "keycloak_admin_client_secret":"synthetic-secret",
                "keycloak_client_id":"synthetic-registry", "keycloak_realm":"test"}.items():
            setting = patch.object(core, name, value)
            setting.start()
            self.addCleanup(setting.stop)
        self.assertEqual(AweHelper.get_component()._base_url, base + "/awe")
        async with self.sessions.begin() as session:
            session.add(G2PRegistryAwePolicyConfiguration(awe_policy_config_id=str(uuid4()),
                policy_scope="SECTION", register_id=self.farmer_id, section_id=self.section_id,
                policy_type="registry.change_request", policy_key=f"coss.{sys.modules['openg2p_registry_extensions'].__variant__}.farmer-reference.v1",
                context_field_names=["farmer_reference_woreda_id", "farmer_reference_approver_ids"]))
            for mnemonic, woreda in (("old_woreda", "woreda-1"), ("foreign_woreda", "proposed-woreda")):
                session.add(G2PRegistryDataPolicy(policy_id=str(uuid4()), policy_mnemonic=mnemonic,
                    policy_target="REGISTER_RECORD", register_id=self.farmer_id, policy_type="ALLOW",
                    policy_filter_expression={"type":"CONDITION","field_id":"woreda_id","operator":"eq","value":woreda}))

    async def _workflow_stats(self):
        import httpx
        async with httpx.AsyncClient() as client:
            response = await client.get(os.environ["FARMER_WORKFLOW_SIMULATION_URL"] + "/workflow-stats")
            response.raise_for_status()
            return response.json()

    @unittest.skipUnless(os.environ.get("FARMER_WORKFLOW_SIMULATION_URL"), "Requires local Postman dependencies")
    async def test_native_http_add_routes_only_functional_woreda_approver_without_identity_writes(self):
        await self._workflow_http_setup()
        workflow = importlib.import_module("openg2p_registry_extensions.farmer_reference.workflow")
        async with self.sessions.begin() as session:
            await self.service.ensure_dependency(session, INTAKE_ID, self.snapshot, requester="synthetic-creator")
        async with self.sessions.begin() as session:
            entry = (await session.execute(select(ledger.FarmerWorkflowOutbox))).scalar_one()
            await workflow.deliver_workflow(session, entry)
            self.assertEqual(entry.state, "DELIVERED")
            cr = await session.get(G2PRegisterChangeRequest, entry.native_cr_id)
            self.assertTrue(cr.awe_request_id)
        stats = await self._workflow_stats()
        self.assertEqual(stats["requests"], 1)
        self.assertEqual(stats["identity_writes"], 0)
        self.assertEqual(stats["contexts"][0]["farmer_reference_approver_ids"], ["old-reviewer"])

    @unittest.skipUnless(os.environ.get("FARMER_WORKFLOW_SIMULATION_URL"), "Requires local Postman dependencies")
    async def test_native_http_update_routes_old_geography_and_retry_reuses_remote_request(self):
        from dataclasses import replace
        import httpx
        await self._workflow_http_setup("lost-response")
        local_id = await self._approved_reference()
        workflow = importlib.import_module("openg2p_registry_extensions.farmer_reference.workflow")
        async with self.sessions.begin() as session:
            request = await self.service.queue_current_update(session, replace(self.snapshot, source_version=200,
                profile={**self.snapshot.profile,"woreda_id":"proposed-woreda","first_name":"Proposed"}))
            cr_id = request.native_cr_id
        async with self.sessions.begin() as session:
            await workflow.dispatch_pending(session)
            entry = await session.get(ledger.FarmerWorkflowOutbox, cr_id)
            self.assertEqual(entry.state, "PENDING")
            self.assertEqual(entry.attempts, 1)
            self.assertEqual(entry.last_error_code, "FARMER_REFERENCE_WORKFLOW_DELIVERY_FAILED")
            self.assertIsNone((await session.get(G2PRegisterChangeRequest, cr_id)).awe_request_id)
        async with httpx.AsyncClient() as client:
            response = await client.post(os.environ["FARMER_WORKFLOW_SIMULATION_URL"] + "/workflow-configure", json={"mode":"valid"})
            response.raise_for_status()
        async with self.sessions.begin() as session:
            await workflow.dispatch_pending(session)
            entry = await session.get(ledger.FarmerWorkflowOutbox, cr_id)
            self.assertEqual(entry.state, "DELIVERED")
            self.assertEqual(entry.attempts, 2)
            farmer = await session.get(native.domain_models().G2PRegisterFarmer, local_id)
            self.assertEqual(farmer.woreda_id, "woreda-1")
            self.assertEqual(farmer.first_name, "Verified")
        stats = await self._workflow_stats()
        self.assertEqual(stats["requests"], 1)
        self.assertEqual(stats["attempts"], 2)
        self.assertEqual(stats["identity_writes"], 0)

    @unittest.skipUnless(os.environ.get("FARMER_WORKFLOW_SIMULATION_URL"), "Requires local Postman dependencies")
    async def test_native_http_directory_failure_blocks_with_visible_diagnostic_and_bounded_retries(self):
        await self._workflow_http_setup("iam-unavailable")
        workflow = importlib.import_module("openg2p_registry_extensions.farmer_reference.workflow")
        async with self.sessions.begin() as session:
            await self.service.ensure_dependency(session, INTAKE_ID, self.snapshot)
        for _ in range(5):
            async with self.sessions.begin() as session:
                await workflow.dispatch_pending(session)
        async with self.sessions.begin() as session:
            entry = (await session.execute(select(ledger.FarmerWorkflowOutbox))).scalar_one()
            self.assertEqual(entry.state, "BLOCKED")
            self.assertEqual(entry.attempts, 5)
            cr = await session.get(G2PRegisterChangeRequest, entry.native_cr_id)
            self.assertTrue(cr.awe_request_status_summary.startswith("BLOCKED:"))
            self.assertIsNone(cr.awe_request_id)
            self.assertEqual(await workflow.dispatch_pending(session), 0)
        stats = await self._workflow_stats()
        self.assertEqual(stats["requests"], 0)
        self.assertEqual(stats["identity_writes"], 0)


    async def test_fin_only_native_add_stores_exact_fin_and_history_without_inventing_fan(self):
        from dataclasses import replace
        self.snapshot = replace(self.snapshot, profile={key: value for key, value in self.snapshot.profile.items() if key != "fayda_fan_id"})
        self.snapshot = replace(self.snapshot, profile={**self.snapshot.profile, "fayda_fin": "001234567890"})
        local_id = await self._approved_reference()
        async with self.sessions() as session:
            farmer = await session.get(native.domain_models().G2PRegisterFarmer, local_id)
            self.assertEqual(farmer.fayda_fin, "001234567890")
            self.assertIsNone(farmer.fayda_fan_id)
            history = (await session.execute(select(native.domain_models().G2PRegisterHistoryFarmer))).scalars().all()
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0].fayda_fin, "001234567890")

    async def test_unresolved_fin_business_approval_holds_then_releases_after_real_reference_approval(self):
        from dataclasses import replace
        from datetime import datetime
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from openg2p_registry_core.models import G2PIntakeFormSubmission, G2PIntakeFormDefinition, G2PIntakeFormUITab, G2PIntakeFormUITabSection
        from openg2p_registry_core.services.g2p_awe_integration_service import G2PAweIntegrationService
        from openg2p_registry_core.errors import G2PRegistryException
        pending = importlib.import_module("openg2p_registry_extensions.farmer_reference.pending")
        submission_id = str(uuid4())
        mnemonic = "CropSown" if "cropsown" in PACKAGE else "Livestock"
        domain_id = "6b06a95a-9a6c-5a33-a33d-c1625716c59c" if mnemonic == "CropSown" else "997676d3-7008-59f9-b23e-613ad79bbb08"
        snapshot = replace(self.snapshot, profile={key: value for key, value in self.snapshot.profile.items() if key != "fayda_fan_id"})
        snapshot = replace(snapshot, profile={**snapshot.profile, "fayda_fin": "001234567890"})
        async with self.sessions.begin() as session:
            session.add(G2PRegisterDefinition(register_id=domain_id,register_mnemonic=mnemonic,register_subject="Synthetic",register_purpose="REGISTER",register_rank=1,functional_id_generation_required=False))
            submission=G2PIntakeFormSubmission(submission_id=submission_id,form_id="synthetic-form",register_id=domain_id,draft_status="DRAFT",approval_status="PENDING",created_by="synthetic-creator",first_created_at=datetime.now(),last_updated_at=datetime.now(),submission_source="STAFF_PORTAL",register_ingest_process_status="NOT_APPLICABLE",number_of_verifications_required=0,number_of_verifications_done=0)
            root=getattr(native.domain_models(),"G2PIntakeForm"+mnemonic)(internal_record_id=str(uuid4()),submission_id=submission_id,fayda_fin="001234567890",farmer_id="FR-000000000001",created_by="synthetic-creator",created_at=datetime.now(),last_approved_at=datetime.now(),last_approved_by="synthetic-creator")
            section_id="synthetic-domain-root"
            session.add_all([submission,root,
                G2PIntakeFormDefinition(form_id="synthetic-form",register_id=domain_id,form_mnemonic="synthetic",number_of_verifications=0,used_only_in_ingestion_pipeline=False),
                G2PRegisterSection(register_id=domain_id,section_id=section_id,section_register_id=domain_id,section_mnemonic="synthetic-root",section_ui_schema={"panels":[]},no_of_verifications_required=0,is_list=False),
                G2PIntakeFormUITab(tab_id="synthetic-intake-tab",form_id="synthetic-form",tab_label="Synthetic",tab_order=1),
                G2PIntakeFormUITabSection(tab_section_id="synthetic-intake-binding",tab_id="synthetic-intake-tab",section_id=section_id,section_order=1)])
            await session.flush()
            unavailable=SimpleNamespace(lookup=AsyncMock(side_effect=Error("FARMER_REFERENCE_NOT_FOUND")))
            submit_business_workflow=AsyncMock()
            service=native.G2PIntakeFormDataService.get_component()
            with patch.object(pending,"configured_lookup",return_value=unavailable), patch.object(G2PAweIntegrationService.get_component(),"start_intake_submission_workflow",submit_business_workflow):
                # A staff submit is refused with the reason and the fix; nothing is created.
                with self.assertRaises(G2PRegistryException) as refused:
                    await service.finalize_submission_with_session(submission_id,session,bearer_token="synthetic-staff-token")
                self.assertIn("No farmer with Farmer ID FR-000000000001 and FIN 001234567890 is in the Farmer Registry",refused.exception.message)
                self.assertIn("register them in the Farmer Registry first",refused.exception.message)
                submit_business_workflow.assert_not_awaited()
                self.assertEqual((await session.execute(select(ledger.FarmerReferenceRequest))).scalars().all(),[])
                # The ingestion worker (ODK, no staff token) holds the intake instead.
                with patch("openg2p_registry_extensions.farmer_reference.workflow.service_bearer",AsyncMock(return_value="synthetic-service-token")):
                    result=await service.finalize_submission_with_session(submission_id,session)
            self.assertEqual(result.draft_status,"FINAL")
            submit_business_workflow.assert_awaited_once()
            self.assertIn("No farmer with Farmer ID",root.farmer_reference_status)
            self.assertIsNone(root.farmer_uuid)
            await native.G2PIntakeFormDataService.get_component().approve_submission_with_session(submission_id,session,approved_by="business-reviewer")
            self.assertEqual(submission.approval_status,"APPROVED")
            self.assertEqual(submission.register_ingest_process_status,"NOT_APPLICABLE")
            self.assertEqual(len((await session.execute(select(native.domain_models().G2PRegisterFarmer))).scalars().all()),0)
            available=SimpleNamespace(lookup=AsyncMock(return_value=snapshot))
            reference=await pending.resolve_or_wait(session,submission,root,lookup=available)
            self.assertEqual(reference.state,"PENDING")
            self.assertEqual(submission.register_ingest_process_status,"NOT_APPLICABLE")
            request=(await session.execute(select(ledger.FarmerReferenceRequest))).scalar_one()
            cr=await session.get(G2PRegisterChangeRequest,request.native_cr_id)
            cr.awe_request_id="synthetic-fin-reference-approval"
            await session.flush()
            await native.G2PRegisterChangeRequestService.get_component().approve_change_request_from_awe_webhook(cr.change_request_id,session,approved_by="reference-reviewer")
            self.assertEqual(submission.register_ingest_process_status,"PENDING")
            self.assertEqual(submission.approved_by,"business-reviewer")
            self.assertEqual(submission.register_ingest_process_attempts,0)
            self.assertEqual(root.farmer_uuid,reference.local_farmer_id)


    async def test_fin_alias_reuses_approved_fan_reference_without_rewriting_profile(self):
        from dataclasses import replace
        local_id=await self._approved_reference()
        source=replace(self.snapshot,profile={key:value for key,value in self.snapshot.profile.items() if key!="fayda_fan_id"})
        source=replace(source,profile={**source.profile,"fayda_fin":"001234567890"})
        other=str(uuid4())
        async with self.sessions.begin() as session:
            reference=await self.service.ensure_dependency(session,other,source)
            self.assertEqual(reference.local_farmer_id,local_id)
            self.assertEqual(reference.uid,self.snapshot.profile["fayda_fan_id"])
            farmer=await session.get(native.domain_models().G2PRegisterFarmer,local_id)
            self.assertIsNone(farmer.fayda_fin)
            binding=await session.get(ledger.FarmerSubmissionDependency,other)
            self.assertEqual(binding.identity_binding["identity"]["fayda_fin"],"001234567890")
            self.assertEqual(len((await session.execute(select(ledger.FarmerReferenceRequest))).scalars().all()),1)


def load_tests(loader, tests, pattern):
    group=os.environ.get("FARMER_NATIVE_GROUP")
    names=loader.getTestCaseNames(NativeReferenceTests)
    if group=="core": names=[name for name in names if not name.startswith("test_native_http_")]
    elif group=="workflow": names=[name for name in names if name.startswith("test_native_http_")]
    return unittest.TestSuite(NativeReferenceTests(name) for name in names)
