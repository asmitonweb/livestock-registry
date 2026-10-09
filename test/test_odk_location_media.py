"""The livestock form offers exactly the places the livestock Master Data holds.

The livestock registry's Master Data is seeded from
docker/db-seed/geo/ethiopia_geo_seed.sql.gz: the shared Ethiopia hierarchy
(14 regions, 125 zones, 1,379 woredas, 19,535 kebeles), loaded into a geo table
whose place *names* must be unique. Rows go in region-first and a repeated name
is skipped, so 14 regions, 121 zones, 1,240 woredas and 17,598 kebeles load
(see the seed's header). The form's lists (odk/media/*_odk.csv) must offer
those and only those: a place the form offers but Master Data lacks cannot be
resolved on ingest. This replays the seed's rule and compares.
"""

import csv
import gzip
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
SEED = REPO / "docker/db-seed/geo/ethiopia_geo_seed.sql.gz"
MEDIA = REPO / "odk/media"
_ROW = re.compile(r"\('(?:region|zone|woreda|kebele)-ET(\d+)', 'level-(\w+)', '((?:[^']|'')*)', (?:NULL|'(?:[^']|'')*')\)")


def _loaded_places():
    """{level: {code: name}} as the seed leaves them: first row of each name wins."""
    seen, places = set(), {"region": {}, "zone": {}, "woreda": {}, "kebele": {}}
    for code, level, name in _ROW.findall(gzip.open(SEED, "rt", encoding="utf-8").read()):
        name = name.replace("''", "'")
        if name in seen:
            continue
        seen.add(name)
        places[level][code] = name
    return places


def _csv(name):
    return list(csv.DictReader((MEDIA / name).open(encoding="utf-8")))


def test_the_form_offers_what_master_data_loads():
    places = _loaded_places()
    assert {level: len(codes) for level, codes in places.items()} == {
        "region": 14, "zone": 121, "woreda": 1240, "kebele": 17598,
    }
    form = {
        "zone": {row["name"].removeprefix("ET"): row["label"] for row in _csv("zone_odk.csv")},
        "woreda": {row["name"].removeprefix("ET"): row["label"] for row in _csv("woreda_odk.csv")},
        "kebele": {row["name"].zfill(12): row["label"] for row in _csv("kebele_odk.csv")},
    }
    for level, offered in form.items():
        assert offered == places[level], f"{level}: form and Master Data differ"
    assert sorted(row["label"] for row in _csv("region_odk.csv")) == sorted(places["region"].values())


def test_lists_are_what_the_generator_builds():
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_location_media", REPO / "odk/build_location_media.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    for name, text in builder.build(builder.loaded_places(), builder._region_keys()).items():
        assert (MEDIA / name).read_bytes().decode("utf-8") == text, (
            f"{name} is stale: run python odk/build_location_media.py"
        )


def test_places_the_cascade_cannot_reach_are_only_the_name_clashes():
    """Zones named like their region (Addis Ababa, Harari, Sidama, and Amhara's
    Oromia special zone, named like the Oromia region) are dropped by the
    name-unique load, and the three special woredas have no parent; neither
    Master Data's cascade nor the form's reaches these 63 woredas and 297
    kebeles. Moving livestock's Master Data to the id-keyed shared hierarchy
    (as farmer and crop use) removes this; until then, a change here means the
    seed or the lists changed."""
    zones = {row["name"] for row in _csv("zone_odk.csv")}
    regions = {row["name"] for row in _csv("region_odk.csv")}
    assert {row["region"] for row in _csv("zone_odk.csv")} <= regions
    woredas = _csv("woreda_odk.csv")
    orphan_woredas = {w["name"] for w in woredas if w["zone"] not in zones}
    reachable_woredas = {w["name"] for w in woredas} - orphan_woredas
    orphan_kebeles = [k for k in _csv("kebele_odk.csv") if k["woreda"] not in reachable_woredas]
    assert {w["zone"] for w in woredas if w["zone"] not in zones} == {
        "ET0310", "ET0700", "ET0725", "ET0726", "ET1301", "ET1401", "ET1600",
    }
    assert (len(orphan_woredas), len(orphan_kebeles)) == (63, 297)
