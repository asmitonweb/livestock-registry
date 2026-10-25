"""Native worker finalization holds bad IDs without creating a trusted Farmer or CR."""
import importlib
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import func, select

PACKAGE = os.environ.get("REGISTRY_EXTENSION_MODULE", "openg2p_registry_livestock_extension")


@unittest.skipUnless(os.environ.get("FARMER_TEST_DATABASE_URL"), "Requires disposable PostgreSQL")
class WorkerHeldTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = importlib.import_module("test_farmer_reference_native").NativeReferenceTests()
        await self.fixture.asyncSetUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def _finalize_held(self, code):
        from openg2p_registry_core.models import (
            G2PIntakeFormDefinition,
            G2PIntakeFormSubmission,
            G2PIntakeFormUITab,
            G2PIntakeFormUITabSection,
            G2PRegisterChangeRequest,
            G2PRegisterDefinition,
            G2PRegisterSection,
        )
        from openg2p_registry_core.services.g2p_awe_integration_service import (
            G2PAweIntegrationService,
        )
        native = importlib.import_module("openg2p_registry_extensions.farmer_reference.native")
        pending = importlib.import_module("openg2p_registry_extensions.farmer_reference.pending")
        models = importlib.import_module("openg2p_registry_extensions.farmer_reference.models")
        messages = importlib.import_module("openg2p_registry_extensions.farmer_reference.messages")
        error = importlib.import_module("openg2p_registry_extensions.farmer_reference.profile").FarmerReferenceError
        mnemonic = "CropSown" if "cropsown" in PACKAGE else "Livestock"
        identity = {"farmer_id":"FR-000000000001", "fayda_fin":"001234567890"}
        submission_id = str(uuid4())
        async with self.fixture.sessions.begin() as session:
            session.add(G2PRegisterDefinition(register_id="worker-domain",register_mnemonic=mnemonic,
                register_subject="Synthetic",register_purpose="REGISTER",register_rank=1,
                functional_id_generation_required=False))
            submission = G2PIntakeFormSubmission(submission_id=submission_id,form_id="worker-form",
                register_id="worker-domain",draft_status="DRAFT",approval_status="PENDING",
                created_by="synthetic-odk",first_created_at=datetime.now(),last_updated_at=datetime.now(),
                submission_source="PARTNER",register_ingest_process_status="NOT_APPLICABLE",
                number_of_verifications_required=0,number_of_verifications_done=0)
            row = getattr(native.domain_models(),"G2PIntakeForm"+mnemonic)(internal_record_id=str(uuid4()),
                submission_id=submission_id,**identity,created_by="synthetic-odk",created_at=datetime.now(),
                last_approved_at=datetime.now(),last_approved_by="synthetic-odk")
            session.add_all([submission,row,
                G2PIntakeFormDefinition(form_id="worker-form",register_id="worker-domain",
                    form_mnemonic="Synthetic ODK",number_of_verifications=0,used_only_in_ingestion_pipeline=False),
                G2PRegisterSection(register_id="worker-domain",section_id="worker-root",
                    section_register_id="worker-domain",section_mnemonic="worker-root",section_ui_schema={"panels":[]},
                    no_of_verifications_required=0,is_list=False),
                G2PIntakeFormUITab(tab_id="worker-tab",form_id="worker-form",tab_label="Synthetic",tab_order=1),
                G2PIntakeFormUITabSection(tab_section_id="worker-binding",tab_id="worker-tab",
                    section_id="worker-root",section_order=1)])
            await session.flush()
            lookup = SimpleNamespace(lookup=AsyncMock(side_effect=error(code)))
            business = AsyncMock()
            with patch.object(pending,"configured_lookup",return_value=lookup), \
                 patch.object(G2PAweIntegrationService.get_component(),"start_intake_submission_workflow",business), \
                 patch("openg2p_registry_extensions.farmer_reference.workflow.service_bearer",
                       AsyncMock(return_value="synthetic-worker-token")):
                result = await native.G2PIntakeFormDataService.get_component().finalize_submission_with_session(
                    submission_id, session)
            self.assertEqual(result.draft_status,"FINAL")
            self.assertEqual(result.approval_status,"PENDING")
            self.assertEqual(result.register_ingest_process_status,"NOT_APPLICABLE")
            self.assertEqual(result.register_ingest_process_last_error_code,"FARMER_REFERENCE_DEPENDENCY_PENDING")
            self.assertEqual(row.farmer_reference_status,messages.explain(code,identity,held=True))
            self.assertNotIn("FARMER_REFERENCE_",row.farmer_reference_status)
            self.assertIsNone(row.farmer_uuid)
            marker = await session.get(models.FarmerPendingLookup,submission_id)
            self.assertEqual(marker.state,"BLOCKED" if "MISMATCH" in code else "PENDING")
            self.assertEqual(marker.last_error_code,code)
            for model in (models.FarmerReference,models.FarmerReferenceRequest,
                          models.FarmerWorkflowOutbox,G2PRegisterChangeRequest,native.domain_models().G2PRegisterFarmer):
                self.assertEqual(await session.scalar(select(func.count()).select_from(model)),0)
            business.assert_awaited_once()

    async def test_not_found_worker_finalizes_and_holds_with_explanation_without_cr(self):
        await self._finalize_held("FARMER_REFERENCE_NOT_FOUND")

    async def test_mismatched_worker_finalizes_and_holds_with_explanation_without_cr(self):
        await self._finalize_held("FARMER_REFERENCE_IDENTITY_MISMATCH")
