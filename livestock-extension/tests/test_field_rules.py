"""Field contracts, including actual registered domain-service entry points."""

import asyncio
import importlib
import os
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest

PACKAGE = os.environ.get(
    "REGISTRY_EXTENSION_MODULE", "openg2p_registry_livestock_extension"
)
rules = importlib.import_module(PACKAGE + ".register_domain.services.field_rules")
from openg2p_registry_core.errors import G2PRegistryException


@pytest.mark.parametrize(
    "value", ["Abebe", "O'Neil", "Jean-Luc", "አበበ", "Élodie", "  Aster  "]
)
def test_person_names_accept_letters_scripts_and_supported_punctuation(value):
    record = {"farmer_name": value}
    assert rules.clean_records([record]) == []
    assert record["farmer_name"] == value.strip()


@pytest.mark.parametrize(
    "value", ["Farmer123", "Farmer_Name", "John.Doe", "👩", "---", "x" * 101, 42]
)
def test_person_names_refuse_digits_symbols_nontext_and_overlong_values(value):
    problems = rules.clean_records([{"farmer_name": value}])
    assert len(problems) == 1
    assert problems[0].key == "coss_field_name_format"
    assert "Farmer Name:" in rules.explain(problems[0]) and "Remove" in rules.explain(
        problems[0]
    )


def test_amharic_name_field_is_ethiopic_and_other_language_names_remain_open():
    assert (
        rules.clean_records([{"first_name_amh": "አበበ", "first_name_other": "أحمد"}])
        == []
    )
    assert (
        rules.clean_records([{"first_name_amh": "Abebe"}])[0].key
        == "coss_field_amharic_name_format"
    )
    assert rules.clean_records([{"first_name_amh": "አበበ፲"}])


@pytest.mark.parametrize(
    "value", ["not-a-date", "2026-02-31", True, 20260101, "1700-01-01"]
)
def test_supplied_malformed_or_out_of_bounds_dates_are_explained(value):
    problems = rules.clean_records([{"registration_date": value}])
    assert problems
    assert "Registration Date:" in rules.explain(problems[0])


def test_date_blanks_today_date_objects_legacy_display_dates_and_future_rules():
    for value in (None, "", "   "):
        assert rules.clean_records([{"registration_date": value}]) == []
    for value in ("today", date.today()):
        record = {"registration_date": value}
        assert rules.clean_records([record]) == []
        assert record["registration_date"] == date.today().isoformat()
    record = {"registration_date": "24/10/2026"}
    assert rules.clean_records([record]) == []
    assert record["registration_date"] == "2026-10-24"
    assert (
        rules.clean_records(
            [{"date_of_birth": (date.today() + timedelta(days=1)).isoformat()}]
        )[0].key
        == "coss_field_date_future"
    )


def test_all_domain_date_columns_are_checked_without_becoming_required():
    for field in rules.DATE_FIELDS:
        problems = rules.clean_records([{field: "invalid-date"}])
        assert problems[0].field == field
    assert rules.clean_records([{}]) == []


def test_fan_fin_and_farmer_ids_normalize_without_cross_kind_conversion():
    row = {
        "farmer_id": " FR-000000000001 ",
        "fayda_fin": " 001234567890 ",
        "fayda_fan_id": "0000 0000 0000 0001",
    }
    assert rules.clean_records([row]) == []
    assert row == {
        "farmer_id": "FR-000000000001",
        "fayda_fin": "001234567890",
        "fayda_fan_id": "0000000000000001",
    }
    for field, value in (
        ("farmer_id", "fr-000000000001"),
        ("fayda_fin", "0000000000000001"),
        ("fayda_fan_id", "001234567890"),
    ):
        assert rules.clean_records([{field: value}])[0].field == field


def test_native_deleted_rows_do_not_revalidate_removed_values():
    assert (
        rules.clean_records(
            [
                {
                    "edit_action": "DELETE",
                    "registration_date": "bad",
                    "farmer_name": "Bad123",
                }
            ]
        )
        == []
    )


def test_translation_metadata_can_reword_the_explained_error():
    issue = rules.Issue("farmer_name", "coss_field_name_format")
    assert (
        rules.explain(
            issue,
            {"farmer_name": "Person", "coss_field_name_format": "{label}: correction"},
        )
        == "Person: correction"
    )


@pytest.mark.parametrize("service_name", ["Livestock", "Farmer"])
@pytest.mark.parametrize(
    "record", [{"farmer_name": "Bad123"}, {"registration_date": "not-a-date"}]
)
def test_actual_domain_entry_rejects_bad_fields_before_section_branching(
    service_name, record
):
    service_module = importlib.import_module(
        PACKAGE
        + ".register_domain.services.g2p_register_domain_service_"
        + ("crop_sown" if service_name == "CropSown" else service_name.lower())
    )
    service = getattr(service_module, "G2PRegisterDomainService" + service_name)()
    with (
        patch.object(rules, "_translations", new=AsyncMock(return_value={})),
        pytest.raises(G2PRegistryException) as caught,
    ):
        asyncio.run(service.validate_domain_attributes([dict(record)]))
    assert ":" in caught.value.message and (
        "Remove" in caught.value.message or "correct" in caught.value.message
    )


def test_managed_snapshot_name_whitespace_is_frozen_canonically_without_touching_other_values():
    onboarding = importlib.import_module(PACKAGE + ".farmer_reference.onboarding")
    source = {
        "first_name": "  Abebe  ",
        "first_name_amh": "  አበበ  ",
        "email": " keep spaces ",
    }
    snapshot = onboarding._json_profile(source)
    assert snapshot == {
        "first_name": "Abebe",
        "first_name_amh": "አበበ",
        "email": " keep spaces ",
    }
    assert source["first_name"] == "  Abebe  "


@pytest.mark.parametrize(
    "field,module_name,service_name",
    [
        ("ai_technician_name", "breeding", "Breeding"),
        ("veterinarian_name", "health_event", "HealthEvent"),
    ],
)
def test_professional_names_are_checked_by_actual_event_services(
    field, module_name, service_name
):
    service_module = importlib.import_module(
        PACKAGE + ".register_domain.services.g2p_register_domain_service_" + module_name
    )
    service = getattr(service_module, "G2PRegisterDomainService" + service_name)()
    assert rules.clean_records([{field: "አበበ"}]) == []
    with (
        patch.object(rules, "_translations", new=AsyncMock(return_value={})),
        pytest.raises(G2PRegistryException),
    ):
        asyncio.run(service.validate_domain_attributes([{field: "Doctor123"}]))


@pytest.mark.parametrize(
    "fake_delete",
    [
        {"_delete": True},
        {"is_deleted": True},
        {"_action": "DELETE"},
        {"status": "DELETED"},
        {"edit_action": "remove"},
    ],
)
def test_only_native_delete_contract_can_skip_field_validation(fake_delete):
    assert rules.clean_records(
        [{**fake_delete, "farmer_name": "Bad123", "registration_date": "invalid"}]
    )


def test_table_field_errors_identify_the_row_without_repeating_personal_values():
    with (
        patch.object(rules, "_translations", new=AsyncMock(return_value={})),
        pytest.raises(G2PRegistryException) as caught,
    ):
        asyncio.run(
            rules.validate_record_fields(
                [{"farmer_name": "Aster"}, {"farmer_name": "Bad123"}], table=True
            )
        )
    assert "Farmer Name (row 2):" in caught.value.message
    assert "Bad123" not in caught.value.message


def test_identity_section_needs_the_farmer_id_and_one_fayda_id():
    empty = {"farmer_id": " ", "fayda_fin": None, "fayda_fan_id": ""}
    keys = sorted(problem.key for problem in rules.clean_records([empty]))
    assert keys == ["coss_field_farmer_id_required", "coss_field_fayda_one_of"]
    for problem in rules.clean_records([dict(empty)]):
        assert rules.explain(problem).startswith(("Farmer ID:", "Fayda ID:"))
    for fayda in ({"fayda_fin": "001234567890"}, {"fayda_fan_id": "0000000000000001"}):
        record = {**empty, "farmer_id": "FR-000000000001", **fayda}
        assert rules.identity_issues(record) == []


def test_identity_rule_skips_records_without_the_identity_fields():
    assert rules.identity_issues({"animal_type": "Cattle"}) == []
    assert rules.identity_issues({"farmer_id": ""}) == []
