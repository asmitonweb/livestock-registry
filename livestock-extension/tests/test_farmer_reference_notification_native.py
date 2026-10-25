"""Isolated native receiving-notification lifecycle tests; separate bounded group."""
import importlib
import os
from sqlalchemy import select
import unittest
from test_farmer_reference_native import NativeReferenceTests

if os.environ.get("FARMER_TEST_DATABASE_URL"):
    native=importlib.import_module("openg2p_registry_extensions.farmer_reference.native")
    ledger=importlib.import_module("openg2p_registry_extensions.farmer_reference.models")
    Error=importlib.import_module("openg2p_registry_extensions.farmer_reference.profile").FarmerReferenceError
    from openg2p_registry_core.models import G2PRegisterChangeRequestPayload


class NativeNotificationTests(NativeReferenceTests):
    def _notification_payload(self, local_id, **changes):
        from openg2p_registry_core.schemas import ChangePayload, ChangeRequestRequestPayload
        row = {"edit_action":"UPDATE", "internal_record_id":local_id,
               "farmer_id":self.snapshot.profile["farmer_id"],
               **{key:self.snapshot.profile[key] for key in ("fayda_fan_id","fayda_fin") if key in self.snapshot.profile},
               "first_name":"Untrusted notification name", **changes}
        return ChangeRequestRequestPayload(register_id=self.farmer_id, section_register_id=self.farmer_id,
            section_id=self.section_id, tab_id="test_farmer_tab", internal_record_id=local_id,
            change_payload=[ChangePayload.model_validate(row)])

    async def test_notification_update_reuses_cr_and_keeps_approved_values_until_native_approval(self):
        from dataclasses import replace
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        local_id = await self._approved_reference()
        current = replace(self.snapshot, source_version=200, profile={**self.snapshot.profile,"first_name":"Current source"})
        client = SimpleNamespace(config=SimpleNamespace(source_id=self.snapshot.source_system),
            lookup_current=AsyncMock(return_value=current))
        with patch.dict(os.environ,{"COSS_FARMER_SOURCE_PARTNER_ID":"synthetic-source-partner"}), patch.object(native,"configured_lookup",return_value=client):
            async with self.sessions.begin() as session:
                service = native.G2PChangeRequestWorkerService.get_component()
                payload = self._notification_payload(local_id)
                first = await service.create_change_request(payload,session,source_partner_id="synthetic-source-partner",change_request_source="INGESTION_PIPELINE")
                second = await service.create_change_request(payload,session,source_partner_id="synthetic-source-partner",change_request_source="INGESTION_PIPELINE")
                self.assertEqual(first.change_request_id,second.change_request_id)
                body = await session.get(G2PRegisterChangeRequestPayload,first.change_request_id)
                self.assertEqual(body.change_payload[0]["first_name"],"Current source")
                self.assertEqual(first.approval_status,"PENDING")
                self.assertEqual((await session.get(native.domain_models().G2PRegisterFarmer,local_id)).first_name,"Verified")
                self.assertEqual(client.lookup_current.await_args.args,(self.snapshot.profile["farmer_id"],self.snapshot.profile["fayda_fan_id"],self.snapshot.source_record_id))
                self.assertEqual(len((await session.execute(select(ledger.FarmerWorkflowOutbox).where(
                    ledger.FarmerWorkflowOutbox.native_cr_id==first.change_request_id))).scalars().all()),1)
                first.awe_request_id="synthetic-notification-approval"
                await session.flush()
                await native.G2PRegisterChangeRequestService.get_component().approve_change_request_from_awe_webhook(
                    first.change_request_id,session,approved_by="target-reviewer")
                self.assertEqual((await session.get(native.domain_models().G2PRegisterFarmer,local_id)).first_name,"Current source")

    async def test_notification_identity_mismatch_fails_before_lookup_or_cr(self):
        from unittest.mock import patch
        local_id=await self._approved_reference()
        with patch.dict(os.environ,{"COSS_FARMER_SOURCE_PARTNER_ID":"synthetic-source-partner"}), patch.object(native,"configured_lookup") as lookup_factory:
            async with self.sessions.begin() as session:
                with self.assertRaisesRegex(Error,"IDENTITY_MISMATCH"):
                    await native.G2PChangeRequestWorkerService.get_component().create_change_request(
                        self._notification_payload(local_id,farmer_id="FR-000000000002"),session,
                        source_partner_id="synthetic-source-partner",change_request_source="INGESTION_PIPELINE")
                lookup_factory.assert_not_called()
                self.assertEqual(len((await session.execute(select(ledger.FarmerReferenceRequest))).scalars().all()),1)

    async def test_notification_for_unreferenced_farmer_cannot_onboard(self):
        from unittest.mock import patch
        with patch.dict(os.environ,{"COSS_FARMER_SOURCE_PARTNER_ID":"synthetic-source-partner"}), patch.object(native,"configured_lookup") as lookup_factory:
            async with self.sessions.begin() as session:
                with self.assertRaisesRegex(Error,"UNREFERENCED_NOTIFICATION"):
                    await native.G2PChangeRequestWorkerService.get_component().create_change_request(
                        self._notification_payload("unknown-local"),session,
                        source_partner_id="synthetic-source-partner",change_request_source="INGESTION_PIPELINE")
                lookup_factory.assert_not_called()
                self.assertEqual(len((await session.execute(select(ledger.FarmerReference))).scalars().all()),0)

    async def test_notification_from_wrong_partner_cannot_lookup_or_create_request(self):
        from unittest.mock import patch
        local_id=await self._approved_reference()
        with patch.dict(os.environ,{"COSS_FARMER_SOURCE_PARTNER_ID":"synthetic-source-partner"}), patch.object(native,"configured_lookup") as lookup_factory:
            async with self.sessions.begin() as session:
                with self.assertRaisesRegex(Error,"SOURCE_PARTNER_REQUIRED"):
                    await native.G2PChangeRequestWorkerService.get_component().create_change_request(
                        self._notification_payload(local_id),session,source_partner_id="wrong-partner",change_request_source="INGESTION_PIPELINE")
                lookup_factory.assert_not_called()

    async def test_notification_lookup_outage_keeps_local_data_and_has_no_pending_update(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock,patch
        local_id=await self._approved_reference()
        client=SimpleNamespace(config=SimpleNamespace(source_id=self.snapshot.source_system),
            lookup_current=AsyncMock(side_effect=Error("FARMER_REFERENCE_LOOKUP_UNAVAILABLE")))
        with patch.dict(os.environ,{"COSS_FARMER_SOURCE_PARTNER_ID":"synthetic-source-partner"}), patch.object(native,"configured_lookup",return_value=client):
            async with self.sessions.begin() as session:
                with self.assertRaisesRegex(Error,"LOOKUP_UNAVAILABLE"):
                    await native.G2PChangeRequestWorkerService.get_component().create_change_request(
                        self._notification_payload(local_id),session,source_partner_id="synthetic-source-partner",change_request_source="INGESTION_PIPELINE")
                self.assertEqual((await session.get(native.domain_models().G2PRegisterFarmer,local_id)).first_name,"Verified")
                self.assertEqual(len((await session.execute(select(ledger.FarmerReferenceRequest))).scalars().all()),1)


    @unittest.skipUnless(os.environ.get("FARMER_WORKFLOW_SIMULATION_URL"), "Requires local Postman dependency")
    async def test_notification_uses_actual_configured_signed_lookup_over_http(self):
        from dataclasses import replace
        from pathlib import Path
        import json
        import tempfile
        from unittest.mock import patch
        import httpx
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import Encoding,PrivateFormat,NoEncryption,PublicFormat
        base=os.environ["FARMER_WORKFLOW_SIMULATION_URL"]
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        private=key.private_bytes(Encoding.PEM,PrivateFormat.PKCS8,NoEncryption()).decode()
        public=key.public_key().public_bytes(Encoding.PEM,PublicFormat.SubjectPublicKeyInfo).decode()
        async with httpx.AsyncClient() as http:
            response=await http.get(base+"/public-key")
            response.raise_for_status()
            source_public=response.json()["public_key"]
            (await http.post(base+"/configure",json={"public_key":public})).raise_for_status()
        self.snapshot=replace(self.snapshot,source_system="synthetic-farmer",source_record_id="synthetic-source-person")
        local_id=await self._approved_reference()
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)
            (directory/"private.pem").write_text(private)
            (directory/"public.pem").write_text(source_public)
            (directory/"record.json").write_text(json.dumps({"profile_paths":{"first_name":"first_name","last_name":"last_name","woreda_id":"woreda_id"}}))
            settings={"DCI_URL":base+"/valid","SENDER_ID":"synthetic-crop","SOURCE_ID":"synthetic-farmer",
                "SIGNING_KEY_FILE":str(directory/"private.pem"),"SIGNING_KID":"synthetic-caller",
                "SOURCE_PUBLIC_KEY_FILE":str(directory/"public.pem"),"SOURCE_KID":"synthetic-source",
                "RECORD_CONTRACT_FILE":str(directory/"record.json"),"CONSENT_AUDIENCE":"synthetic-consent",
                "CONSENT_CONTROLLER":"synthetic-controller","CONSENT_PURPOSE":"synthetic-purpose",
                "CONSENT_SCOPES":"farmer_personal_details","SOURCE_PARTNER_ID":"synthetic-source-partner"}
            with patch.dict(os.environ,{"COSS_FARMER_"+name:value for name,value in settings.items()}):
                async with self.sessions.begin() as session:
                    cr=await native.G2PChangeRequestWorkerService.get_component().create_change_request(
                        self._notification_payload(local_id),session,source_partner_id="synthetic-source-partner",change_request_source="INGESTION_PIPELINE")
                    body=await session.get(G2PRegisterChangeRequestPayload,cr.change_request_id)
                    self.assertEqual(body.change_payload[0]["first_name"],"Synthetic")
                    self.assertNotIn("household_income",body.change_payload[0])
                    self.assertEqual(cr.approval_status,"PENDING")
                    self.assertEqual((await session.get(native.domain_models().G2PRegisterFarmer,local_id)).first_name,"Verified")
                    self.assertIsNotNone(await session.get(ledger.FarmerWorkflowOutbox,cr.change_request_id))


    async def test_notification_unverified_uid_mismatch_cannot_create_update(self):
        from dataclasses import replace
        from types import SimpleNamespace
        from unittest.mock import AsyncMock,patch
        local_id=await self._approved_reference()
        client=SimpleNamespace(config=SimpleNamespace(source_id=self.snapshot.source_system),
            lookup_current=AsyncMock(return_value=replace(self.snapshot,source_version=200)))
        with patch.dict(os.environ,{"COSS_FARMER_SOURCE_PARTNER_ID":"synthetic-source-partner"}),patch.object(native,"configured_lookup",return_value=client):
            async with self.sessions.begin() as session:
                with self.assertRaisesRegex(Error,"IDENTITY_MISMATCH"):
                    await native.G2PChangeRequestWorkerService.get_component().create_change_request(
                        self._notification_payload(local_id,fayda_fan_id="0000000000000002"),session,
                        source_partner_id="synthetic-source-partner",change_request_source="INGESTION_PIPELINE")
                self.assertEqual(client.lookup_current.await_count,1)
                self.assertEqual(len((await session.execute(select(ledger.FarmerReferenceRequest))).scalars().all()),1)

    async def test_notification_source_approved_uid_rotation_keeps_stable_local_identity(self):
        from dataclasses import replace
        from types import SimpleNamespace
        from unittest.mock import AsyncMock,patch
        local_id=await self._approved_reference()
        current=replace(self.snapshot,source_version=200,profile={**self.snapshot.profile,"fayda_fan_id":"0000000000000002"})
        client=SimpleNamespace(config=SimpleNamespace(source_id=self.snapshot.source_system),lookup_current=AsyncMock(return_value=current))
        with patch.dict(os.environ,{"COSS_FARMER_SOURCE_PARTNER_ID":"synthetic-source-partner"}),patch.object(native,"configured_lookup",return_value=client):
            async with self.sessions.begin() as session:
                cr=await native.G2PChangeRequestWorkerService.get_component().create_change_request(
                    self._notification_payload(local_id,fayda_fan_id="0000000000000002"),session,
                    source_partner_id="synthetic-source-partner",change_request_source="INGESTION_PIPELINE")
                farmer=await session.get(native.domain_models().G2PRegisterFarmer,local_id)
                self.assertEqual(farmer.fayda_fan_id,"0000000000000001")
                cr.awe_request_id="synthetic-rotation-approval"
                await session.flush()
                await native.G2PRegisterChangeRequestService.get_component().approve_change_request_from_awe_webhook(
                    cr.change_request_id,session,approved_by="target-reviewer")
                self.assertEqual(farmer.fayda_fan_id,"0000000000000002")
                self.assertEqual(farmer.functional_record_id,"FR-000000000001")
                self.assertEqual(farmer.internal_record_id,local_id)


    async def test_notification_fin_reference_creates_pending_update_then_native_approval(self):
        from dataclasses import replace
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        self.snapshot=replace(self.snapshot,profile={key:value for key,value in self.snapshot.profile.items() if key!="fayda_fan_id"})
        self.snapshot=replace(self.snapshot,profile={**self.snapshot.profile,"fayda_fin":"001234567890"})
        local_id=await self._approved_reference()
        current=replace(self.snapshot,source_version=200,profile={**self.snapshot.profile,"first_name":"Current signed FIN source"})
        client=SimpleNamespace(config=SimpleNamespace(source_id=self.snapshot.source_system),lookup_current=AsyncMock(return_value=current))
        with patch.dict(os.environ,{"COSS_FARMER_SOURCE_PARTNER_ID":"synthetic-source-partner"}),patch.object(native,"configured_lookup",return_value=client):
            async with self.sessions.begin() as session:
                cr=await native.G2PChangeRequestWorkerService.get_component().create_change_request(self._notification_payload(local_id),session,source_partner_id="synthetic-source-partner",change_request_source="INGESTION_PIPELINE")
                farmer=await session.get(native.domain_models().G2PRegisterFarmer,local_id)
                self.assertEqual(farmer.first_name,"Verified")
                self.assertEqual(farmer.fayda_fin,"001234567890")
                self.assertIsNone(farmer.fayda_fan_id)
                cr.awe_request_id="synthetic-fin-update-approval"
                await session.flush()
                await native.G2PRegisterChangeRequestService.get_component().approve_change_request_from_awe_webhook(cr.change_request_id,session,approved_by="target-reviewer")
                self.assertEqual(farmer.first_name,"Current signed FIN source")
                self.assertIsNone(farmer.fayda_fan_id)



def load_tests(loader, tests, pattern):
    names=[name for name in loader.getTestCaseNames(NativeNotificationTests) if name.startswith("test_notification_")]
    return unittest.TestSuite(NativeNotificationTests(name) for name in names)
