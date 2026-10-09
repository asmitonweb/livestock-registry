"""ls_odk_transform.j2 on an ODK submission shaped like the connector sends it.

The payload is what reaches the registry inside {"header", "message":
{"payload": ...}}: the livestock_registry form's groups, with repeats already
expanded by the connector (resolve_nav_links) and photos inline when the
pipeline embeds attachments.
"""

import base64
import copy
import json
import pathlib

import pytest

jinja2 = pytest.importorskip("jinja2")

REPO = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = (
    REPO
    / "livestock-extension/src/openg2p_registry_livestock_extension/templates/ls_odk_transform.j2"
)

PHOTO = {
    "__type": "File",
    "name": "1791460233917.jpg",
    "type": "image/jpeg",
    "data": base64.b64encode(b"\xff\xd8\xff\xe0 not really a jpeg").decode(),
}

SUBMISSION = {
    "start": "2026-10-08T09:00:00.000+03:00",
    "farmer": {
        "farmer_id": "FR-0000000001",
        "fayda_fan_id": "1234567890123456",
        "first_name": "Abebe",
        "middle_name": "Kebede",
        "last_name": "Alemu",
        "gender": "male",
        "registration_date": "2026-10-08",
    },
    "survey_personnel": {"surveyor_name": "Surveyor", "supervisor_name": "Supervisor"},
    "location": {"region_id": "oromia", "zone_id": "ZONE_1", "woreda_id": "WOREDA_1", "kebele_id": "KEBELE_1"},
    "livestock": [
        {
            "species_id": "cattle",
            "breed": "boran",
            "ear_tag_id": "ET0000000001",
            "gender": "female",
            "health_events": [],
            "vaccinations": [],
            "vital_events": [],
            "breeding_events": [
                {
                    "breeding_event_type": "ai",
                    "breeding_date": "2026-09-25",
                    "location": "field",
                    "outcome": "successful",
                }
            ],
        }
    ],
    "__system": {"submissionDate": "2026-10-08T06:00:00.000Z"},
}


def render(submission):
    return json.loads(jinja2.Template(TEMPLATE.read_text(encoding="utf-8")).render(expanded=submission))


def test_without_a_photo_the_record_has_no_image():
    out = render(SUBMISSION)
    assert "record_image_document_id" not in out["ls_livestock_record"][0]


@pytest.mark.parametrize("where", ["top", "farmer"])
def test_an_inline_photo_becomes_the_record_image(where):
    submission = copy.deepcopy(SUBMISSION)
    (submission if where == "top" else submission["farmer"])["livestock_photo"] = PHOTO
    record = render(submission)["ls_livestock_record"][0]
    # The Livestock service uploads it on save and stores the document id.
    assert record["record_image_document_id"] == PHOTO


def test_a_photo_that_arrived_as_a_file_name_only_is_left_out():
    submission = copy.deepcopy(SUBMISSION)
    submission["livestock_photo"] = "1791460233917.jpg"
    assert "record_image_document_id" not in render(submission)["ls_livestock_record"][0]


def test_breeding_location_reads_the_breeding_events_own_location():
    # It used to test a health event's location here, so a breeding event in
    # the field came out as OTHER.
    out = render(SUBMISSION)
    assert out["ls_breeding_details"][0]["location"] == "FIELD"
