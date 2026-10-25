"""Real HTTP lookup against the local Postman simulation (no real credentials)."""
import importlib
import os
from dataclasses import replace
import unittest

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption, PublicFormat

PACKAGE = os.environ.get("REGISTRY_EXTENSION_MODULE", "openg2p_registry_cropsown_extension")
lookup = importlib.import_module(PACKAGE + ".farmer_reference.lookup")
Error = importlib.import_module(PACKAGE + ".farmer_reference.profile").FarmerReferenceError
BASE = os.environ.get("FARMER_SIMULATION_URL", "http://127.0.0.1:49182/farmer")


@unittest.skipUnless(os.environ.get("FARMER_SIMULATION_URL"), "Requires local Postman simulation; run ci/refactor/verify-onboarding")
class HttpLookupTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
        public = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
        source_public = httpx.get(BASE + "/public-key").json()["public_key"]
        httpx.post(BASE + "/configure", json={"public_key": public}).raise_for_status()
        cls.config = lookup.LookupConfig(url=BASE + "/valid", sender_id="synthetic-crop",
            source_id="synthetic-farmer", signing_key=private, signing_kid="synthetic-caller",
            source_public_key=source_public, source_kid="synthetic-source", consent_audience="synthetic-consent",
            consent_controller="synthetic-controller", consent_purpose="synthetic-purpose",
            consent_scopes=("farmer_personal_details",), timeout_seconds=2.0)
        cls.contract = lookup.RecordContract(profile_paths={"first_name":"first_name", "last_name":"last_name",
                                                           "woreda_id":"woreda_id", "email":"email"})

    async def run_lookup(self, mode, **config):
        # The slow fixture is a timeout test; successful RSA verification must
        # not share its artificial sub-second deadline under Docker/host load.
        if mode == "timeout":
            config.setdefault("timeout_seconds", 0.1)
        client = lookup.FarmerLookup(replace(self.config, url=BASE + "/" + mode, **config), self.contract)
        return await client.lookup("FR-000000000001", "0000 0000 0000 0001")

    async def test_signed_pair_lookup_filters_profile_and_preserves_explicit_clear(self):
        result = await self.run_lookup("valid")
        self.assertEqual(result.source_record_id, "synthetic-source-person")
        self.assertEqual(result.profile["fayda_fan_id"], "0000000000000001")
        self.assertEqual(result.profile["woreda_id"], "synthetic-woreda")
        self.assertIsNone(result.profile["email"])
        for field in ("household_income", "documents", "animal_count", "last_approved_at", "record_status"):
            self.assertNotIn(field, result.profile)

    async def test_optional_omission_is_distinct_from_explicit_clear(self):
        self.assertNotIn("email", (await self.run_lookup("omitted-email")).profile)

    async def test_transport_failures_are_blocking_and_redirects_are_not_followed(self):
        for mode in ("unauthorized", "rate-limit", "unavailable", "html", "timeout", "invalid-json", "redirect"):
            with self.subTest(mode=mode), self.assertRaisesRegex(Error, "LOOKUP_UNAVAILABLE"):
                await self.run_lookup(mode)

    async def test_signature_correlation_consent_cardinality_and_freshness_fail_closed(self):
        for mode in ("unsigned", "tampered", "wrong-correlation", "wrong-reference", "wrong-sender",
                     "consent-disabled", "stale", "multiple", "zero", "inactive", "wrong-identity"):
            with self.subTest(mode=mode), self.assertRaises(Error):
                await self.run_lookup(mode)

    async def test_source_contract_gaps_are_explicit_and_fin_is_not_converted(self):
        for mode in ("current-template", "missing-source-id", "missing-approval", "fin-is-not-uid"):
            with self.subTest(mode=mode), self.assertRaises(Error):
                await self.run_lookup(mode)

    async def test_server_pinned_key_id_cannot_be_changed_by_source(self):
        with self.assertRaisesRegex(Error, "RESPONSE_SIGNATURE_INVALID"):
            await self.run_lookup("valid", source_kid="unrelated-key")

    async def test_current_lookup_pins_immutable_person_and_allows_source_approved_uid_rotation(self):
        client = lookup.FarmerLookup(replace(self.config, url=BASE + "/rotated"), self.contract)
        result = await client.lookup_current("FR-000000000001", "0000000000000001", "synthetic-source-person")
        self.assertEqual(result.profile["fayda_fan_id"], "0000000000000002")
        client = lookup.FarmerLookup(replace(self.config, url=BASE + "/different-source-person"), self.contract)
        with self.assertRaisesRegex(Error, "SOURCE_ID_MISMATCH"):
            await client.lookup_current("FR-000000000001", "0000000000000001", "synthetic-source-person")


    async def test_fin_only_signed_http_lookup_resolves_farmer_id_without_inventing_fan(self):
        client=lookup.FarmerLookup(replace(self.config,url=BASE+"/fin-valid"),self.contract)
        result=await client.lookup(None,fin="000000000001")
        self.assertEqual(result.profile["farmer_id"],"FR-000000000001")
        self.assertEqual(result.profile["fayda_fin"],"000000000001")
        self.assertNotIn("fayda_fan_id",result.profile)

    async def test_fin_http_no_match_is_pending_eligible_but_mismatch_is_refused(self):
        client=lookup.FarmerLookup(replace(self.config,url=BASE+"/fin-zero"),self.contract)
        with self.assertRaisesRegex(Error,"NOT_FOUND"):
            await client.lookup(None,fin="000000000001")
        client=lookup.FarmerLookup(replace(self.config,url=BASE+"/fin-wrong-identity"),self.contract)
        with self.assertRaisesRegex(Error,"IDENTITY_MISMATCH"):
            await client.lookup(None,fin="000000000001")
