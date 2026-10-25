"""FIN and FAN are alternatives, and declarations do not establish trust."""

import importlib

import pytest

p = importlib.import_module(
    "openg2p_registry_livestock_extension.farmer_reference.profile"
)
FIN = "001234567890"
FAN = "0012345678901234"
FARMER = "FR-000000000001"


def test_fin_only_declaration_preserves_kind_and_zeroes():
    assert p.intake_identity({"fayda_fin": FIN}) == {"fayda_fin": FIN}
    assert p.reference_identity({"farmer_id": FARMER, "fayda_fin": FIN}) == (
        FARMER,
        FIN,
    )


def test_fan_only_and_both_are_distinct():
    assert p.intake_identity({"fayda_fan_id": "0012 3456 7890 1234"}) == {
        "fayda_fan_id": FAN
    }
    profile = {"farmer_id": FARMER, "fayda_fin": FIN, "fayda_fan_id": FAN}
    assert p.reference_identity(profile, preferred_identifier=FIN) == (FARMER, FIN)
    assert p.reference_identity(profile, preferred_identifier=FAN) == (FARMER, FAN)


@pytest.mark.parametrize(
    "record",
    [
        {},
        {"fayda_fin": FAN},
        {"fayda_fan_id": FIN},
        {"fayda_fin": "１２３４５６７８９０１２"},
    ],
)
def test_missing_or_wrong_kind_cannot_be_coerced(record):
    with pytest.raises(p.FarmerReferenceError):
        p.intake_identity(record)


def test_every_declared_identity_must_match_approved_source():
    profile = {"farmer_id": FARMER, "fayda_fin": FIN, "fayda_fan_id": FAN}
    assert p.matches_declared_identity({"fayda_fin": FIN}, profile)
    assert not p.matches_declared_identity({"fayda_fin": "009999999999"}, profile)
    assert not p.matches_declared_identity(
        {"fayda_fin": FIN, "farmer_id": "FR-999999999999"}, profile
    )


def test_fin_allowlist_cannot_copy_source_business_or_grants():
    assert p.local_profile(
        {"approved_fin": FIN, "roles": ["admin"], "head_count": 42}
    ) == {"fayda_fin": FIN}
