"""Real transactional projection tests against an isolated in-memory database."""
import importlib
import os
import unittest
from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

PACKAGE = os.environ.get("REGISTRY_EXTENSION_MODULE", "openg2p_registry_cropsown_extension")
projection = importlib.import_module(PACKAGE + ".farmer_reference.projection")
Error = importlib.import_module(PACKAGE + ".farmer_reference.profile").FarmerReferenceError


class Base(DeclarativeBase):
    pass


class Farmer(Base):
    __tablename__ = "farmers"
    internal_record_id: Mapped[str] = mapped_column(String, primary_key=True)
    farmer_id: Mapped[str] = mapped_column(String)
    fayda_fan_id: Mapped[str] = mapped_column(String)
    record_status: Mapped[str] = mapped_column(String)
    last_approved_at: Mapped[str] = mapped_column(String, nullable=True)
    last_approved_by: Mapped[str] = mapped_column(String, nullable=True)
    first_name: Mapped[str] = mapped_column(String, nullable=True)
    middle_name: Mapped[str] = mapped_column(String, nullable=True)
    last_name: Mapped[str] = mapped_column(String, nullable=True)


class Domain(Base):
    __tablename__ = "domains"
    internal_record_id: Mapped[str] = mapped_column(String, primary_key=True)
    farmer_uuid: Mapped[str] = mapped_column(String, nullable=True)
    farmer_id: Mapped[str] = mapped_column(String, nullable=True)
    fayda_fan_id: Mapped[str] = mapped_column(String, nullable=True)
    link_foundational_id: Mapped[str] = mapped_column(String, nullable=True)
    farmer_name: Mapped[str] = mapped_column(String, nullable=True)
    record_status: Mapped[str] = mapped_column(String)
    search_text: Mapped[str] = mapped_column(String, nullable=True)
    record_name: Mapped[str] = mapped_column(String, nullable=True)
    business_data: Mapped[str] = mapped_column(String, default="season-2026;plots-2;animals-20")
    woreda_id: Mapped[str] = mapped_column(String, default="domain-owned-woreda")
    approval_request_id: Mapped[str] = mapped_column(String, default="original-domain-task")

    def get_search_text_fields(self):
        return f"{self.farmer_name} {self.farmer_id} {self.business_data}"

    def get_record_name_fields(self):
        return self.farmer_name


class ProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite://")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        async with self.sessions.begin() as session:
            session.add_all([
                Farmer(internal_record_id="local-1", farmer_id="FR-000000000001", fayda_fan_id="0000000000000001",
                       record_status="ACTIVE", last_approved_at="2026-10-24", last_approved_by="reviewer",
                       first_name="Old", middle_name="Middle", last_name="Farmer"),
                Domain(internal_record_id="domain-1", farmer_uuid="local-1", farmer_id="FR-000000000001",
                       fayda_fan_id="0000000000000001", farmer_name="Old Middle Farmer", record_status="ACTIVE"),
                Domain(internal_record_id="unlinked", farmer_uuid=None, farmer_id="FR-000000000001",
                       fayda_fan_id="0000000000000001", farmer_name="Unlinked", record_status="ACTIVE"),
                Domain(internal_record_id="inactive", farmer_uuid="local-1", farmer_id="FR-000000000001",
                       fayda_fan_id="0000000000000001", farmer_name="Inactive", record_status="INACTIVE"),
                # Same pair on another reference must never be selected by pair.
                Domain(internal_record_id="other-reference", farmer_uuid="local-other", farmer_id="FR-000000000001",
                       fayda_fan_id="0000000000000001", farmer_name="Other", record_status="ACTIVE"),
            ])

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_own_domain_approval_projects_only_its_row_and_preserves_business(self):
        async with self.sessions.begin() as session:
            farmer = await session.get(Farmer, "local-1")
            farmer.first_name = "Approved"
            self.assertEqual(await projection.refresh_domain_on_ingest(session, Farmer, await session.get(Domain, "domain-1")), 1)
            row = await session.get(Domain, "domain-1")
            self.assertEqual(row.farmer_name, "Approved Middle Farmer")
            self.assertEqual(row.business_data, "season-2026;plots-2;animals-20")
            self.assertEqual(row.woreda_id, "domain-owned-woreda")
            self.assertEqual(row.approval_request_id, "original-domain-task")
            self.assertEqual(row.record_name, "Approved Middle Farmer")
            self.assertIn("Approved", row.search_text)
            self.assertEqual((await session.get(Domain, "unlinked")).farmer_name, "Unlinked")
            self.assertEqual((await session.get(Domain, "inactive")).farmer_name, "Inactive")
            self.assertEqual((await session.get(Domain, "other-reference")).farmer_name, "Other")
            self.assertEqual(await projection.refresh_domain_on_ingest(session, Farmer, await session.get(Domain, "domain-1")), 0)

    async def test_projection_rollback_preserves_approved_values(self):
        with self.assertRaisesRegex(RuntimeError, "downstream failure"):
            async with self.sessions.begin() as session:
                (await session.get(Farmer, "local-1")).first_name = "Pending"
                await projection.refresh_domain_on_ingest(session, Farmer, await session.get(Domain, "domain-1"))
                raise RuntimeError("downstream failure")
        async with self.sessions() as session:
            self.assertEqual((await session.get(Farmer, "local-1")).first_name, "Old")
            self.assertEqual((await session.get(Domain, "domain-1")).farmer_name, "Old Middle Farmer")

    async def test_unapproved_or_inactive_reference_blocks_projection(self):
        for field, value in (("last_approved_at", None), ("last_approved_by", None), ("record_status", "INACTIVE")):
            with self.subTest(field=field), self.assertRaisesRegex(Error, "DEPENDENCY_REQUIRED"):
                async with self.sessions.begin() as session:
                    setattr(await session.get(Farmer, "local-1"), field, value)
                    await projection.refresh_domain_on_ingest(session, Farmer, await session.get(Domain, "domain-1"))

    async def test_unlinked_ingest_rolls_back_without_guessing_owner(self):
        with self.assertRaisesRegex(Error, "DEPENDENCY_REQUIRED"):
            async with self.sessions.begin() as session:
                row = Domain(internal_record_id="new", farmer_uuid=None, farmer_id="FR-000000000001",
                             fayda_fan_id="0000000000000001", farmer_name="Typed", record_status="ACTIVE")
                session.add(row)
                await projection.refresh_domain_on_ingest(session, Farmer, row)
        async with self.sessions() as session:
            self.assertIsNone(await session.get(Domain, "new"))

    async def test_late_intake_reads_latest_approved_reference(self):
        async with self.sessions.begin() as session:
            (await session.get(Farmer, "local-1")).first_name = "Latest"
        async with self.sessions.begin() as session:
            row = Domain(internal_record_id="late", farmer_uuid="local-1", farmer_id="FR-000000000001",
                         fayda_fan_id="0000000000000001", farmer_name="Old", record_status="ACTIVE")
            session.add(row)
            self.assertEqual(await projection.refresh_domain_on_ingest(session, Farmer, row), 1)
            self.assertEqual(row.farmer_name, "Latest Middle Farmer")

    async def test_uid_rotation_keeps_local_link_and_does_not_recreate_domain(self):
        async with self.sessions.begin() as session:
            (await session.get(Farmer, "local-1")).fayda_fan_id = "0000000000000002"
            await projection.refresh_domain_on_ingest(session, Farmer, await session.get(Domain, "domain-1"))
            row = await session.get(Domain, "domain-1")
            self.assertEqual(row.farmer_uuid, "local-1")
            self.assertEqual(row.fayda_fan_id, "0000000000000002")
            self.assertEqual(row.link_foundational_id, "0000000000000002")
            self.assertEqual(len((await session.execute(select(Domain))).scalars().all()), 4)

    async def test_conflicting_farmer_id_rolls_back_instead_of_relinking(self):
        with self.assertRaisesRegex(Error, "LINK_IDENTITY_MISMATCH"):
            async with self.sessions.begin() as session:
                (await session.get(Domain, "domain-1")).farmer_id = "FR-000000000002"
                await projection.refresh_domain_on_ingest(session, Farmer, await session.get(Domain, "domain-1"))
        async with self.sessions() as session:
            self.assertEqual((await session.get(Domain, "domain-1")).farmer_id, "FR-000000000001")

    async def test_explicit_optional_name_clear_is_projected(self):
        async with self.sessions.begin() as session:
            (await session.get(Farmer, "local-1")).middle_name = None
            await projection.refresh_domain_on_ingest(session, Farmer, await session.get(Domain, "domain-1"))
            self.assertEqual((await session.get(Domain, "domain-1")).farmer_name, "Old Farmer")


if __name__ == "__main__":
    unittest.main()
