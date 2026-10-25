"""The transform's locations for the CSV forms (v4 and earlier) and the
Master Data Entity List form (v5).

    uv run --no-project --with pytest --with jinja2 pytest odk/test_transform_locations.py
"""

import copy
import json
from pathlib import Path

import jinja2

HERE = Path(__file__).parent
TEMPLATE = HERE.parent / "livestock-extension/src/openg2p_registry_livestock_extension/templates/ls_odk_transform.j2"
SAMPLE = json.loads((HERE / "sample_submissions.json").read_text(encoding="utf-8"))["value"][0]
LOCATED = ("ls_farmer_identity", "ls_farmer_location", "ls_livestock_location")
IDS = ("region_id", "zone_id", "woreda_id", "kebele_id")


def _render(submission):
    return json.loads(jinja2.Template(TEMPLATE.read_text(encoding="utf-8")).render(expanded=submission))


def _v5(sample):
    sub = copy.deepcopy(sample)
    sub["location"] = {
        # Entity uuids, as Collect submits a choice from an Entity List.
        "region_id": "5b0f6a52-0000-5000-8000-000000000001",
        "zone_id": "5b0f6a52-0000-5000-8000-000000000002",
        "woreda_id": "5b0f6a52-0000-5000-8000-000000000003",
        "kebele_id": "5b0f6a52-0000-5000-8000-000000000004",
        "region_code": "ET01", "zone_code": "ET0101", "woreda_code": "ET010101", "kebele_code": "ET010101101001",
        "location_source": "mds",
    }
    return sub


def test_a_v5_submission_sends_the_master_data_ids_and_leaves_the_names_to_the_registry():
    out = _render(_v5(SAMPLE))
    for section in LOCATED:
        row = out[section][0]
        assert [row[k] for k in IDS] == ["ET01", "ET0101", "ET010101", "ET010101101001"], section
        assert [row[k] for k in ("region", "zone", "woreda", "kebele")] == [None] * 4, section


def test_an_older_csv_submission_is_unchanged():
    out = _render(SAMPLE)
    for section in LOCATED:
        row = out[section][0]
        assert not any(k in row for k in IDS), section
    assert out["ls_farmer_location"][0]["region"] is not None
