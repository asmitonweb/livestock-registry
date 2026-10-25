"""Native domain FAN validators share the reference service's stored format."""

import importlib

import pytest

pytest.importorskip("openg2p_registry_core")
module = importlib.import_module(
    "openg2p_registry_livestock_extension.register_domain.services.g2p_register_domain_service_livestock"
)
service = object.__new__(module.G2PRegisterDomainServiceLivestock)


@pytest.mark.parametrize(
    "value", ["0012345678901234", "0012 3456 7890 1234", " 0012345678901234 "]
)
def test_domain_fan_stores_compact_ascii_without_losing_zeroes(value):
    row = {"fayda_fan_id": value}
    service._validate_fayda_fan_id(row)
    assert row["fayda_fan_id"] == "0012345678901234"


@pytest.mark.parametrize(
    "value",
    [
        "001234567890",
        "00123456789012345",
        "١٢٣٤٥٦٧٨٩٠١٢٣٤٥٦",
        1234567890123456,
        "FAN-0012345678901234",
    ],
)
def test_bad_fan_names_the_field_and_repair(value):
    with pytest.raises(Exception) as failure:
        service._validate_fayda_fan_id({"fayda_fan_id": value})
    assert "Fayda ID (FAN): enter exactly 16 digits" in str(failure.value)
