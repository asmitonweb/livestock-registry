"""Native metadata replays and ODK save into the intake-only Livestock section."""
import importlib
import json
import os
import unittest
from copy import deepcopy
from pathlib import Path

from jinja2 import Environment
from sqlalchemy import func, select

PACKAGE = "openg2p_registry_livestock_extension"
DOMAIN = "997676d3-7008-59f9-b23e-613ad79bbb08"
SECTION = "livestock_intake_identity_section_01"
OLD = "livestock_livestock_record_section_01"
ROOT = Path(importlib.import_module(PACKAGE).__file__).parent


@unittest.skipUnless(os.environ.get("FARMER_TEST_DATABASE_URL"), "Requires disposable PostgreSQL")
class IntakeIdentitySectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = importlib.import_module("test_farmer_reference_native").NativeReferenceTests()
        await self.fixture.asyncSetUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def replay(self, paths):
        async with self.fixture.engine.begin() as connection:
            raw = await connection.get_raw_connection()
            for path in paths:
                sql = path.read_text(encoding="utf-8").replace('"public".', '"'+self.fixture.schema+'".')
                await raw.driver_connection.execute(sql)

    async def test_full_metadata_replay_preserves_register_and_odk_saves_new_intake_fields(self):
        from openg2p_registry_core.models import (
            G2PIntakeFormUITabSection,
            G2PRegisterChangeRequest,
            G2PRegisterSection,
            G2PRegisterUITabSection,
        )
        native = importlib.import_module("openg2p_registry_extensions.farmer_reference.native")
        models = native.domain_models()
        paths = sorted((ROOT/"meta_data"/"register-metadata").glob("*.sql"))
        split = next(path for path in paths if path.name == "zzzzzzz_identity_section_split.sql")
        await self.replay([path for path in paths if path != split])
        async with self.fixture.sessions() as session:
            original = deepcopy((await session.get(G2PRegisterSection, OLD)).section_ui_schema)
        await self.replay([split])
        async with self.fixture.sessions() as session:
            first = deepcopy((await session.get(G2PRegisterSection, SECTION)).section_ui_schema)
            bindings = list((await session.execute(select(G2PIntakeFormUITabSection.section_id,
                G2PIntakeFormUITabSection.section_order).order_by(G2PIntakeFormUITabSection.tab_section_id))).all())
        await self.replay(paths)
        async with self.fixture.sessions.begin() as session:
            current = await session.get(G2PRegisterSection, SECTION)
            self.assertEqual(current.section_ui_schema, first)
            self.assertEqual((await session.get(G2PRegisterSection, OLD)).section_ui_schema, original)
            self.assertIn('farmer_name', json.dumps(original))
            again = list((await session.execute(select(G2PIntakeFormUITabSection.section_id,
                G2PIntakeFormUITabSection.section_order).order_by(G2PIntakeFormUITabSection.tab_section_id))).all())
            self.assertEqual(again, bindings)
            self.assertNotIn(OLD, [row[0] for row in again])
            self.assertIn(SECTION, [row[0] for row in again])
            self.assertGreater(await session.scalar(select(func.count()).select_from(G2PRegisterUITabSection)
                .where(G2PRegisterUITabSection.section_id == OLD)), 0)
            template = Environment().from_string((ROOT/"templates"/"ls_odk_transform.j2").read_text(encoding="utf-8"))
            transformed = json.loads(template.render(expanded={"farmer": {
                "farmer_id":"FR-000000000001", "fayda_id":"001234567890"}}))
            self.assertNotIn("ls_livestock_record", transformed)
            submission = await native.G2PIntakeFormDataService.get_component().save_intake_form_submission_with_session(
                None, current.section_id, transformed[current.section_mnemonic], current.section_register_id,
                "e92e8be1-207f-518e-99c2-8bc21cc1f112", DOMAIN, "synthetic-odk", session)
            row = (await session.execute(select(models.G2PIntakeFormLivestock)
                .where(models.G2PIntakeFormLivestock.submission_id == submission.submission_id))).scalar_one()
            self.assertEqual(row.farmer_id, "FR-000000000001")
            self.assertEqual(row.fayda_fin, "001234567890")
            self.assertIsNone(row.fayda_fan_id)
            self.assertIsNone(row.farmer_uuid)
            self.assertEqual(submission.draft_status, "DRAFT")
            self.assertEqual(await session.scalar(select(func.count()).select_from(G2PRegisterChangeRequest)), 0)
