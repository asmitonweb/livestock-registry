#!/usr/bin/env python3
"""Build the livestock form's location lists from the livestock Master Data seed.

The form picks region > zone > woreda > kebele from media/region_odk.csv,
zone_odk.csv, woreda_odk.csv and kebele_odk.csv. Livestock's Master Data is
seeded from docker/db-seed/geo/ethiopia_geo_seed.sql.gz, the shared Ethiopia
hierarchy loaded into a geo table whose place *names* are unique, and whose
parent link is the parent's name (see the seed's header): rows load region
first, a repeated name is skipped, and a place whose own parent was skipped
hangs under the surviving place of that name. That leaves 14 regions, 121
zones, 1,240 woredas and 17,598 kebeles. The lists are built by the same
rules, so the form offers exactly those places, each under the parent Master
Data gives it, and every one can be reached through the cascade.

Values stay as the form and ls_odk_transform.j2 use them: regions by key
(tigray, central_ethiopia, ... from the current region_odk.csv, which the
transform's region_map reads), zones and woredas as ET codes, kebeles as the
P-code number (10101101001).

    python docs/odk/build_location_media.py          # rewrite media/*_odk.csv
    python docs/odk/build_location_media.py --check  # exit 1 if they are stale

test/test_odk_location_media.py runs the check.
"""

from __future__ import annotations

import csv
import gzip
import io
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
SEED = REPO / "docker/db-seed/geo/ethiopia_geo_seed.sql.gz"
MEDIA = HERE / "media"

_ROW = re.compile(
    r"\('(?:region|zone|woreda|kebele)-ET(\d+)', 'level-(\w+)', '((?:[^']|'')*)', (NULL|'((?:[^']|'')*)')\)"
)


def loaded_places(seed: pathlib.Path = SEED) -> dict[str, list[tuple[str, str, str | None]]]:
    """{level: [(code, name, parent name)]} in load order, as Master Data keeps them."""
    seen: set[str] = set()
    places: dict[str, list[tuple[str, str, str | None]]] = {"region": [], "zone": [], "woreda": [], "kebele": []}
    for code, level, name, _, parent in _ROW.findall(gzip.open(seed, "rt", encoding="utf-8").read()):
        name = name.replace("''", "'")
        if name in seen:
            continue
        seen.add(name)
        places[level].append((code, name, parent.replace("''", "'") if parent else None))
    return places


def _region_keys() -> dict[str, str]:
    """Region label -> the key the form and the transform use."""
    rows = csv.DictReader((MEDIA / "region_odk.csv").open(encoding="utf-8"))
    return {row["label"]: row["name"] for row in rows}


def build(places, region_keys: dict[str, str]) -> dict[str, str]:
    def table(header, rows):
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator="\n").writerows([header, *rows])
        return buffer.getvalue()

    code_of = {level: {name: code for code, name, _ in rows} for level, rows in places.items()}

    def parent_code(level: str, parent: str, code: str, width: int) -> str:
        # Master Data's parent: the surviving place of that name one level up.
        # When the name exists only at another level (a zone called Oromia lost
        # to the region), Master Data cannot cascade to it either; keep the
        # place's own parent code, as the form always did.
        return f"ET{code_of[level].get(parent, code[:width])}"

    return {
        "region_odk.csv": table(
            ["list_name", "name", "label"],
            [("region", region_keys[name], name) for _, name, _ in places["region"]],
        ),
        "zone_odk.csv": table(
            ["list_name", "name", "label", "region"],
            [("zone", f"ET{code}", name, region_keys[parent]) for code, name, parent in places["zone"]],
        ),
        "woreda_odk.csv": table(
            ["list_name", "name", "label", "zone"],
            [("woreda", f"ET{code}", name, parent_code("zone", parent, code, 4)) for code, name, parent in places["woreda"]],
        ),
        "kebele_odk.csv": table(
            ["list_name", "name", "label", "woreda"],
            [("kebele", str(int(code)), name, parent_code("woreda", parent, code, 6)) for code, name, parent in places["kebele"]],
        ),
    }


def main(argv: list[str]) -> int:
    files = build(loaded_places(), _region_keys())
    stale = [name for name, text in files.items() if (MEDIA / name).read_bytes().decode("utf-8") != text]
    if "--check" in argv:
        if stale:
            print(f"stale, rerun docs/odk/build_location_media.py: {stale}", file=sys.stderr)
            return 1
        print("location lists match the livestock Master Data seed")
        return 0
    for name, text in files.items():
        (MEDIA / name).write_bytes(text.encode("utf-8"))
    print("updated: " + (", ".join(stale) if stale else "nothing, already current"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
