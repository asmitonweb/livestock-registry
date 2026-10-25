"""PostgreSQL transaction/concurrency acceptance for the extension ledger.

Uses a disposable database and a real persisted CR stand-in to check atomicity.
Native CR schema/AWE delivery are covered separately, not inferred from this.
"""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import os
import unittest
from uuid import uuid4, uuid5, NAMESPACE_URL

from sqlalchemy import String, select, func
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from openg2p_fastapi_common.models import BaseORMModel

PACKAGE = os.environ.get("REGISTRY_EXTENSION_MODULE", "openg2p_registry_cropsown_extension")
ledger = importlib.import_module(PACKAGE + ".farmer_reference.models")
onboarding = importlib.import_module(PACKAGE + ".farmer_reference.onboarding")
VerifiedFarmer = importlib.import_module(PACKAGE + ".farmer_reference.lookup").VerifiedFarmer
Error = importlib.import_module(PACKAGE + ".farmer_reference.profile").FarmerReferenceError


class Farmer(BaseORMModel):
    __tablename__ = "test_approved_farmers"
    internal_record_id: Mapped[str] = mapped_column(String, primary_key=True)
    farmer_id: Mapped[str] = mapped_column(String)
    fayda_fan_id: Mapped[str | None] = mapped_column(String, nullable=True)
    fayda_fin: Mapped[str | None] = mapped_column(String, nullable=True)
    record_status: Mapped[str] = mapped_column(String, default="ACTIVE")
    last_approved_at: Mapped[str] = mapped_column(String, default="2026-10-24")
    last_approved_by: Mapped[str] = mapped_column(String, default="target-reviewer")


class NativeCr(BaseORMModel):
    __tablename__ = "test_native_cr"
    change_request_id: Mapped[str] = mapped_column(String, primary_key=True)
    local_farmer_id: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)


TABLES = [model.__table__ for model in (*ledger.REFERENCE_MODELS, Farmer, NativeCr)]


def sid(value):
    return str(uuid5(NAMESPACE_URL, "https://coss-test.invalid/intake/" + value))


@unittest.skipUnless(os.environ.get("FARMER_TEST_DATABASE_URL"), "Requires isolated PostgreSQL; run ci/refactor/verify-onboarding")
class OnboardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url = os.environ.get("FARMER_TEST_DATABASE_URL")
        if not url or not url.startswith("postgresql+asyncpg://"):
            self.fail("An isolated PostgreSQL test database is required; SQLite cannot prove concurrency")
        self.engine = create_async_engine(url, pool_size=12, max_overflow=12)
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda conn: BaseORMModel.metadata.drop_all(conn, tables=TABLES))
            await connection.run_sync(lambda conn: BaseORMModel.metadata.create_all(conn, tables=TABLES))
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)

        async def create_native(session, local_id, profile, action, **kwargs):
            row = NativeCr(change_request_id=str(uuid4()), local_farmer_id=local_id, action=action)
            session.add(row)
            await session.flush()
            return row
        self.service = onboarding.OnboardingService(Farmer, create_native)
        self.snapshot = VerifiedFarmer("central-farmer", "immutable-source-person-1", 100,
            {"farmer_id": "FR-000000000001", "fayda_fan_id": "0000000000000001", "first_name": "Verified",
             "woreda_id": "verified-woreda"}, datetime.now(timezone.utc), "synthetic-response-digest")

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def submit(self, submission="intake-1", snapshot=None):
        async with self.sessions.begin() as session:
            return await self.service.ensure_dependency(session, sid(submission), snapshot or self.snapshot)

    async def count(self, model):
        async with self.sessions() as session:
            return await session.scalar(select(func.count()).select_from(model))

    async def approve(self, cr_id):
        async with self.sessions.begin() as session:
            ref, request = await self.service.approval_guard(session, cr_id)
            farmer = await session.get(Farmer, ref.local_farmer_id)
            if not farmer:
                farmer = Farmer(internal_record_id=ref.local_farmer_id, farmer_id=request.profile["farmer_id"],
                                fayda_fan_id=request.profile["fayda_fan_id"])
                session.add(farmer)
            else:
                farmer.fayda_fan_id = request.profile["fayda_fan_id"]
            await session.flush()
            await self.service.record_decision(session, cr_id, True)

    async def request(self, action="ADD", version=100):
        async with self.sessions() as session:
            return (await session.execute(select(ledger.FarmerReferenceRequest).where(
                ledger.FarmerReferenceRequest.action == action,
                ledger.FarmerReferenceRequest.source_version == version))).scalar_one()

    async def test_twenty_concurrent_submissions_reuse_one_reference_cr_and_outbox(self):
        refs = await asyncio.gather(*(self.submit(f"intake-{index}") for index in range(20)))
        self.assertEqual(len({row.local_farmer_id for row in refs}), 1)
        for model in (ledger.FarmerReference, ledger.FarmerReferenceRequest, NativeCr, ledger.FarmerWorkflowOutbox):
            self.assertEqual(await self.count(model), 1)
        self.assertEqual(await self.count(ledger.FarmerSubmissionDependency), 20)
        self.assertEqual(await self.count(Farmer), 0, "a pending request must not create a trusted shell")

    async def test_concurrent_fin_fan_aliases_reuse_one_pending_reference_without_identity_conversion(self):
        fin_source=replace(self.snapshot,profile={**{key:value for key,value in self.snapshot.profile.items() if key != "fayda_fan_id"},"fayda_fin":"001234567890"})
        references=await asyncio.gather(*(self.submit(f"alternate-{index}",fin_source if index % 2 else self.snapshot) for index in range(20)))
        self.assertEqual(len({reference.reference_id for reference in references}),1)
        self.assertEqual(await self.count(ledger.FarmerReferenceRequest),1)
        self.assertEqual(await self.count(NativeCr),1)
        self.assertEqual(await self.count(ledger.FarmerSubmissionDependency),20)
        self.assertEqual(await self.count(Farmer),0)
        async with self.sessions() as session:
            bindings=(await session.execute(select(ledger.FarmerSubmissionDependency))).scalars().all()
            self.assertEqual(sum("fayda_fin" in row.identity_binding["identity"] for row in bindings),10)
            self.assertEqual(sum("fayda_fan_id" in row.identity_binding["identity"] for row in bindings),10)

    async def test_concurrent_duplicate_submission_and_restart_are_idempotent(self):
        refs = await asyncio.gather(*(self.submit() for _ in range(12)))
        self.assertEqual(await self.count(ledger.FarmerSubmissionDependency), 1)
        self.service = onboarding.OnboardingService(Farmer, self.service.create_native_cr)
        self.assertEqual((await self.submit()).reference_id, refs[0].reference_id)
        self.assertEqual(await self.count(NativeCr), 1)

    async def test_failure_rolls_back_cr_ledger_links_and_outbox_together(self):
        with self.assertRaisesRegex(RuntimeError, "downstream failure"):
            async with self.sessions.begin() as session:
                await self.service.ensure_dependency(session, sid("intake-1"), self.snapshot)
                raise RuntimeError("downstream failure")
        for model in (*ledger.REFERENCE_MODELS, NativeCr, Farmer):
            self.assertEqual(await self.count(model), 0)

    async def test_no_draft_side_effect_and_pending_dependency_cannot_activate(self):
        self.assertEqual(await self.count(NativeCr), 0)
        await self.submit()
        async with self.sessions.begin() as session:
            with self.assertRaisesRegex(Error, "DEPENDENCY_PENDING"):
                await self.service.require_approved_dependency(session, sid("intake-1"))

    async def test_approval_releases_existing_dependency_without_recreating_intake(self):
        ref = await self.submit()
        await self.approve((await self.request()).native_cr_id)
        async with self.sessions.begin() as session:
            farmer = await self.service.require_approved_dependency(session, sid("intake-1"))
            self.assertEqual(farmer.internal_record_id, ref.local_farmer_id)
        await self.submit("intake-2")
        self.assertEqual(await self.count(NativeCr), 1)

    async def test_rejected_add_blocks_retries_and_other_submissions(self):
        await self.submit()
        async with self.sessions.begin() as session:
            await self.service.record_decision(session, (await self.request()).native_cr_id, False)
        for intake in ("intake-1", "intake-2"):
            with self.assertRaisesRegex(Error, "DEPENDENCY_REJECTED"):
                await self.submit(intake)
        self.assertEqual(await self.count(Farmer), 0)

    async def test_existing_approved_exact_reference_is_adopted_without_overwrite(self):
        async with self.sessions.begin() as session:
            session.add(Farmer(internal_record_id="existing", farmer_id="FR-000000000001", fayda_fan_id="0000000000000001"))
        ref = await self.submit()
        self.assertEqual(ref.local_farmer_id, "existing")
        self.assertEqual(ref.applied_version, 0)
        self.assertEqual(await self.count(NativeCr), 0)

    async def test_ambiguous_or_mismatched_existing_identity_fails_closed(self):
        async with self.sessions.begin() as session:
            session.add(Farmer(internal_record_id="wrong-pair", farmer_id="FR-000000000002", fayda_fan_id="0000000000000001"))
        with self.assertRaisesRegex(Error, "EXISTING_IDENTITY_AMBIGUOUS"):
            await self.submit()
        self.assertEqual(await self.count(NativeCr), 0)

    async def test_source_person_cannot_be_replaced_for_same_farmer_id(self):
        await self.submit()
        with self.assertRaisesRegex(Error, "SOURCE_ID_MISMATCH"):
            await self.submit("intake-2", replace(self.snapshot, source_record_id="different-source-person"))

    async def test_expired_lookup_and_unverified_objects_rejected(self):
        with self.assertRaisesRegex(Error, "LOOKUP_EXPIRED"):
            await self.submit(snapshot=replace(self.snapshot, fetched_at=datetime.now(timezone.utc) - timedelta(minutes=6)))
        with self.assertRaisesRegex(Error, "VERIFIED_LOOKUP_REQUIRED"):
            await self.submit(snapshot={"trusted": True})

    async def test_unreferenced_kafka_update_does_not_onboard(self):
        async with self.sessions.begin() as session:
            self.assertIsNone(await self.service.queue_current_update(session, self.snapshot))
        self.assertEqual(await self.count(ledger.FarmerReference), 0)
        self.assertEqual(await self.count(NativeCr), 0)

    async def test_duplicate_update_and_rejection_preserve_approved_local(self):
        await self.submit()
        await self.approve((await self.request()).native_cr_id)
        snapshot = replace(self.snapshot, source_version=200, profile={**self.snapshot.profile, "first_name": "Proposed"})
        async def deliver():
            async with self.sessions.begin() as session:
                return await self.service.queue_current_update(session, snapshot)
        requests = await asyncio.gather(*(deliver() for _ in range(10)))
        self.assertEqual(len({request.native_cr_id for request in requests}), 1)
        async with self.sessions.begin() as session:
            await self.service.record_decision(session, requests[0].native_cr_id, False)
        self.assertEqual((await deliver()).state, "REJECTED")
        async with self.sessions() as session:
            reference = (await session.execute(select(ledger.FarmerReference))).scalar_one()
            self.assertEqual(reference.applied_version, 100)

    async def test_stale_pending_update_rechecked_after_newer_approval(self):
        await self.submit()
        await self.approve((await self.request()).native_cr_id)
        async with self.sessions.begin() as session:
            old = await self.service.queue_current_update(session, replace(self.snapshot, source_version=200))
            new = await self.service.queue_current_update(session, replace(self.snapshot, source_version=300))
        await self.approve(new.native_cr_id)
        with self.assertRaisesRegex(Error, "STALE_SOURCE_VERSION"):
            await self.approve(old.native_cr_id)
        async with self.sessions.begin() as session:
            self.assertIsNone(await self.service.queue_current_update(session, replace(self.snapshot, source_version=200)))

    async def test_uid_rotation_requires_update_approval_before_new_domain_reuse(self):
        await self.submit()
        await self.approve((await self.request()).native_cr_id)
        changed = replace(self.snapshot, source_version=200, profile={**self.snapshot.profile, "fayda_fan_id": "0000000000000002"})
        with self.assertRaisesRegex(Error, "LOCAL_IDENTITY_UPDATE_REQUIRED"):
            await self.submit("intake-2", changed)
        async with self.sessions.begin() as session:
            request = await self.service.queue_current_update(session, changed)
        await self.approve(request.native_cr_id)
        await self.submit("intake-2", changed)
        self.assertEqual(await self.count(Farmer), 1)
