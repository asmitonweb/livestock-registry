"""Tests for odk_geo_sync against in-memory stand-ins for Master Data and ODK Central.

    uv run --no-project --with pytest --with httpx pytest tools/odk-geo-sync/tests
"""

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import odk_geo_sync as sync

LEVELS = [
    {"level_id": "l0", "level_mnemonic": "country", "parent_level_id": None},
    {"level_id": "l1", "level_mnemonic": "region", "parent_level_id": "l0"},
    {"level_id": "l2", "level_mnemonic": "zone", "parent_level_id": "l1"},
]


def _geo(region_parent="ET", zones=None):
    return {
        "l1": [{"level_value_id": "ET01", "level_value_mnemonic": "Tigray", "parent_level_value_id": region_parent},
               {"level_value_id": "ET03", "level_value_mnemonic": "Amhara", "parent_level_value_id": region_parent}],
        "l2": zones if zones is not None else [
            {"level_value_id": "ET0101", "level_value_mnemonic": "North Western", "parent_level_value_id": "ET01"},
            {"level_value_id": "ET0301", "level_value_mnemonic": "North Gondar", "parent_level_value_id": "ET03"}],
    }


class FakeMasterData:
    def __init__(self, geo, down=False):
        self.geo, self.down = geo, down

    def __call__(self, request: httpx.Request):
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        payload = json.loads(request.content)["request_body"]["request_payload"]
        if request.url.path.endswith("/geo/get_all_geo_levels"):
            data = LEVELS
        else:
            data = self.geo.get(payload["level_id"], [])
        return httpx.Response(200, json={"response_header": {"response_status": "SUCCESS"},
                                         "response_body": {"response_payload": data}})


class FakeCentral:
    """Sessions, datasets, properties, the OData feed and entity writes."""

    def __init__(self, refuse=None):
        self.datasets: dict[str, dict] = {}
        self.writes = 0
        self.refuse = refuse

    def __call__(self, request: httpx.Request):
        path, method = request.url.path, request.method
        if self.refuse:
            return httpx.Response(self.refuse, json={"message": "The authenticated actor does not have rights."})
        if path == "/v1/sessions":
            return httpx.Response(200, json={"token": "t"})
        assert request.headers.get("Authorization") == "Bearer t"
        base = "/v1/projects/6/datasets"
        if path == base and method == "GET":
            return httpx.Response(200, json=[{"name": n} for n in self.datasets])
        if path == base and method == "POST":
            self.writes += 1
            self.datasets[json.loads(request.content)["name"]] = {"properties": [], "entities": {}}
            return httpx.Response(200, json={})
        rest = path[len(base) + 1:]
        if rest.endswith(".svc/Entities"):
            ds = self.datasets[rest.split(".svc")[0]]
            rows = [{"__id": k, "label": e["label"], "__system": {"version": e["version"]}, **e["data"]}
                    for k, e in ds["entities"].items()]
            top, skip = int(request.url.params["$top"]), int(request.url.params["$skip"])
            return httpx.Response(200, json={"value": rows[skip:skip + top]})
        parts = rest.split("/")
        ds = self.datasets[parts[0]]
        if len(parts) == 1:
            return httpx.Response(200, json={"properties": [{"name": p} for p in ds["properties"]]})
        if parts[1] == "properties":
            self.writes += 1
            ds["properties"].append(json.loads(request.content)["name"])
            return httpx.Response(200, json={})
        if parts[1] == "entities" and len(parts) == 2:
            self.writes += 1
            body = json.loads(request.content)
            assert body["source"]["size"] == len(body["entities"])
            for e in body["entities"]:
                assert set(e["data"]) <= set(ds["properties"]), "property must exist first"
                ds["entities"][e["uuid"]] = {"label": e["label"], "data": e["data"], "version": 1}
            return httpx.Response(200, json={"success": True})
        key = parts[2]
        self.writes += 1
        if method == "PATCH":
            current = ds["entities"][key]
            if int(request.url.params["baseVersion"]) != current["version"]:
                return httpx.Response(409, json={"message": "version conflict"})
            body = json.loads(request.content)
            ds["entities"][key] = {"label": body["label"], "data": body["data"], "version": current["version"] + 1}
            return httpx.Response(200, json={})
        if method == "DELETE":
            del ds["entities"][key]
            return httpx.Response(200, json={"success": True})
        raise AssertionError(f"unexpected {method} {path}")


ENV = {"MDS_API_URL": "http://mds", "ODK_CENTRAL_URL": "http://odk", "ODK_CENTRAL_EMAIL": "sync@example.org",
       "ODK_CENTRAL_PASSWORD": "x", "ODK_PROJECT_ID": "6", "ODK_GEO_LEVELS": "region,zone"}


def _run(mds, central, **env):
    settings = sync.Settings.from_env({**ENV, **env})

    def route(request):
        return (mds if request.url.host == "mds" else central)(request)

    return sync.run(settings, httpx.Client(transport=httpx.MockTransport(route)))


def _counts(plans):
    return {p.level: (len(p.create), len(p.update), len(p.delete), p.unchanged) for p in plans}


def test_first_run_creates_every_level_and_a_rerun_changes_nothing():
    central = FakeCentral()
    assert _counts(_run(FakeMasterData(_geo()), central)) == {"region": (2, 0, 0, 0), "zone": (2, 0, 0, 0)}
    writes = central.writes
    assert _counts(_run(FakeMasterData(_geo()), central)) == {"region": (0, 0, 0, 2), "zone": (0, 0, 0, 2)}
    assert central.writes == writes


def test_entities_carry_the_master_data_id_and_their_parent_entity():
    central = FakeCentral()
    _run(FakeMasterData(_geo()), central)
    zone = central.datasets["zone"]["entities"][sync.entity_uuid("zone", "ET0101")]
    assert zone["label"] == "North Western"
    assert zone["data"] == {"code": "ET0101", "parent": sync.entity_uuid("region", "ET01")}
    assert central.datasets["region"]["entities"][sync.entity_uuid("region", "ET01")]["data"]["parent"] == ""


def test_a_rename_a_move_and_a_removal_are_mirrored():
    central = FakeCentral()
    _run(FakeMasterData(_geo()), central)
    changed = [{"level_value_id": "ET0101", "level_value_mnemonic": "North West", "parent_level_value_id": "ET03"}]
    assert _counts(_run(FakeMasterData(_geo(zones=changed)), central))["zone"] == (0, 1, 1, 0)
    zone = central.datasets["zone"]["entities"][sync.entity_uuid("zone", "ET0101")]
    assert zone["label"] == "North West" and zone["data"]["parent"] == sync.entity_uuid("region", "ET03")
    assert zone["version"] == 2


def test_parents_linked_by_name_resolve_to_the_parent_entity():
    by_name = [{"level_value_id": "zone-1", "level_value_mnemonic": "North Western", "parent_level_value_id": "Tigray"}]
    central = FakeCentral()
    _run(FakeMasterData(_geo(zones=by_name)), central)
    zone = central.datasets["zone"]["entities"][sync.entity_uuid("zone", "zone-1")]
    assert zone["data"]["parent"] == sync.entity_uuid("region", "ET01")


def test_a_value_with_an_unknown_parent_is_skipped_and_reported():
    zones = _geo()["l2"] + [{"level_value_id": "ET9901", "level_value_mnemonic": "Lost", "parent_level_value_id": "ET99"}]
    plans = _run(FakeMasterData(_geo(zones=zones)), FakeCentral())
    zone = next(p for p in plans if p.level == "zone")
    assert zone.orphans == ["ET9901"] and len(zone.create) == 2


def test_master_data_unreachable_changes_nothing_and_says_so():
    central = FakeCentral()
    with pytest.raises(sync.MasterDataError) as error:
        _run(FakeMasterData(_geo(), down=True), central)
    assert "Master Data Service (mds) is unreachable" in str(error.value)
    assert "ODK locations not refreshed" in str(error.value)
    assert central.writes == 0 and error.value.exit_code == 2


def test_an_empty_master_data_never_empties_the_lists():
    central = FakeCentral()
    _run(FakeMasterData(_geo()), central)
    with pytest.raises(sync.MasterDataError, match="returned no geo values"):
        _run(FakeMasterData({"l1": [], "l2": []}), central)
    assert len(central.datasets["region"]["entities"]) == 2


def test_an_unknown_level_names_the_levels_master_data_has():
    with pytest.raises(sync.MasterDataError, match=r"no geo level kebele \(it has: country, region, zone\)"):
        _run(FakeMasterData(_geo()), FakeCentral(), ODK_GEO_LEVELS="region,kebele")


def test_a_central_refusal_explains_the_fix():
    with pytest.raises(sync.CentralError) as error:
        _run(FakeMasterData(_geo()), FakeCentral(refuse=403))
    assert "Give the account Project Manager on project 6" in str(error.value)
    assert error.value.exit_code == 3


def test_a_dry_run_reads_but_never_writes():
    central = FakeCentral()
    _run(FakeMasterData(_geo()), central)
    writes = central.writes
    changed = [{"level_value_id": "ET0101", "level_value_mnemonic": "North West", "parent_level_value_id": "ET01"}]
    plans = _run(FakeMasterData(_geo(zones=changed)), central, DRY_RUN="true")
    assert _counts(plans)["zone"] == (0, 1, 1, 0) and central.writes == writes


@pytest.mark.parametrize("env,message", [
    ({"ODK_PROJECT_ID": ""}, "Not configured: ODK_PROJECT_ID"),
    ({"ODK_PROJECT_ID": "six"}, "is not a number"),
    ({"MDS_TOKEN_URL": "http://kc/token"}, "partly configured"),
])
def test_configuration_errors_name_the_setting(env, message):
    with pytest.raises(sync.ConfigError, match=message):
        sync.Settings.from_env({**ENV, **env})
