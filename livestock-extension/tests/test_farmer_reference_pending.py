"""Real PostgreSQL pending-lookup/ledger checks; native AWE is verified separately."""

import importlib
import os
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import func, select

PACKAGE = os.environ.get(
    "REGISTRY_EXTENSION_MODULE", "openg2p_registry_livestock_extension"
)
pending = importlib.import_module(PACKAGE + ".farmer_reference.pending")
native = importlib.import_module(PACKAGE + ".farmer_reference.native")
ledger = importlib.import_module(PACKAGE + ".farmer_reference.models")
profile = importlib.import_module(PACKAGE + ".farmer_reference.profile")
FIN = "001234567890"


@unittest.skipUnless(
    os.environ.get("FARMER_TEST_DATABASE_URL"), "Requires owned disposable PostgreSQL"
)
class PendingLookupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixtures = importlib.import_module("test_farmer_reference_onboarding")
        self.case = self.fixtures.OnboardingTests(methodName="runTest")
        await self.case.asyncSetUp()
        self.sessions = self.case.sessions
        self.source = replace(
            self.case.snapshot,
            profile={
                "farmer_id": "FR-000000000001",
                "fayda_fin": FIN,
                "first_name": "Synthetic",
                "woreda_id": "verified-woreda",
            },
        )
        self.submission = SimpleNamespace(
            submission_id=str(uuid4()), created_by="synthetic-staff"
        )
        self.row = SimpleNamespace(
            farmer_id=None,
            fayda_fin=FIN,
            fayda_fan_id=None,
            farmer_uuid=None,
            farmer_reference_status=None,
        )

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    def client(self, found=False):
        outer = self

        class Lookup:
            async def lookup(self, farmer_id, uid=None, *, fin=None):
                assert fin == FIN and uid is None
                if not found:
                    raise profile.FarmerReferenceError("FARMER_REFERENCE_NOT_FOUND")
                return outer.source

        return Lookup()

    async def resolve(self, found=False):
        async with self.sessions.begin() as session:
            with patch.object(
                native, "onboarding_service", return_value=self.case.service
            ):
                return await pending.resolve_or_wait(
                    session, self.submission, self.row, lookup=self.client(found)
                )

    async def test_unresolved_fin_persists_diagnostic_without_a_trusted_farmer_or_cr(
        self,
    ):
        self.assertIsNone(await self.resolve())
        async with self.sessions() as session:
            marker = await session.get(
                ledger.FarmerPendingLookup, self.submission.submission_id
            )
            self.assertEqual(marker.identity_json, {"fayda_fin": FIN})
            self.assertEqual(marker.state, "PENDING")
            self.assertIsNotNone(marker.retry_after)
            self.assertEqual(
                await session.scalar(
                    select(func.count()).select_from(ledger.FarmerReference)
                ),
                0,
            )
            self.assertEqual(
                await session.scalar(
                    select(func.count()).select_from(self.fixtures.NativeCr)
                ),
                0,
            )
            with self.assertRaisesRegex(
                profile.FarmerReferenceError, "DEPENDENCY_REQUIRED"
            ):
                await self.case.service.require_approved_dependency(
                    session, self.submission.submission_id
                )
        self.assertIsNone(self.row.farmer_uuid)
        self.assertIn(
            "No farmer with FIN", self.row.farmer_reference_status
        )

    async def test_recovery_creates_one_real_pending_add_and_still_requires_reference_approval(
        self,
    ):
        await self.resolve()
        reference = await self.resolve(found=True)
        self.assertEqual(reference.state, "PENDING")
        self.assertEqual(reference.uid, FIN)
        self.assertEqual(self.row.farmer_id, "FR-000000000001")
        async with self.sessions() as session:
            marker = await session.get(
                ledger.FarmerPendingLookup, self.submission.submission_id
            )
            self.assertEqual(marker.state, "RESOLVED")
            self.assertEqual(
                await session.scalar(
                    select(func.count()).select_from(self.fixtures.NativeCr)
                ),
                1,
            )
            self.assertEqual(
                await session.scalar(
                    select(func.count()).select_from(ledger.FarmerWorkflowOutbox)
                ),
                1,
            )
            with self.assertRaisesRegex(
                profile.FarmerReferenceError, "DEPENDENCY_PENDING"
            ):
                await self.case.service.require_approved_dependency(
                    session, self.submission.submission_id
                )

    async def test_changed_declared_fin_cannot_reuse_pending_link(self):
        await self.resolve()
        self.row.fayda_fin = "009999999999"
        with self.assertRaisesRegex(profile.FarmerReferenceError, "IDENTITY_CHANGED"):
            await self.resolve(found=True)

    async def test_domain_approval_is_held_without_manufacturing_a_reference(self):
        await self.resolve()
        result = SimpleNamespace(
            register_id="domain",
            register_ingest_process_status="PENDING",
            register_ingest_process_last_error_code=None,
        )
        service = object.__new__(native.ReferenceIntakeService)

        async def base(*args, **kwargs):
            return result

        async def definition(*args):
            return SimpleNamespace(register_mnemonic="Livestock")

        service._get_register_definition = definition
        async with self.sessions.begin() as session:
            with patch.object(
                native.G2PIntakeFormDataService, "approve_submission_with_session", base
            ):
                approved = await service.approve_submission_with_session(
                    self.submission.submission_id, session, "reviewer"
                )
            self.assertEqual(approved.register_ingest_process_status, "NOT_APPLICABLE")
            self.assertEqual(
                approved.register_ingest_process_last_error_code,
                "FARMER_REFERENCE_DEPENDENCY_PENDING",
            )
