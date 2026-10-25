"""Mirror a registry's Master Data geography into ODK Central Entity Lists.

ODK forms used to carry the locations as hand-built CSV attachments
(odk/media/*_odk.csv): a boundary change meant regenerating the files and
republishing every form, and the values were neither Master Data ids nor
names, so ingestion had to guess. Master Data is the one source of the
geography; this job keeps one Entity List per level (region, zone, woreda,
kebele) in the registry's ODK project equal to it. Collect downloads Entity
List changes with the form's update; no form is republished.

Each entity is one geo value:

    uuid    a hash of "<level>:<Master Data id>" (a v4 uuid, as Central requires), so every run upserts the same entity
    label   the value's name (level_value_mnemonic)
    code    the Master Data id (level_value_id), what the form submits
    parent  the parent's entity uuid, for the cascade (choice_filter parent=${region_id})

A Master Data whose values point at their parent by name (Livestock's) is
read the same way: such a parent is resolved to the parent level's id here,
so the form never depends on how Master Data keys its links.

Entities are created, updated and deleted to match: unlike a seed, the lists
mirror Master Data exactly. A run that cannot read Master Data changes
nothing, so Collect keeps the last good lists.

Configuration is environment only (see README.md). Exit codes: 0 done,
1 configuration, 2 Master Data unreachable or refused, 3 ODK Central refused.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

_logger = logging.getLogger("odk-geo-sync")

# Fixed, so every environment and every run derives the same entity uuids.
NAMESPACE = uuid.UUID("6f1c1f4e-2b55-4d0e-9c7a-0a8d3c1e5b21")
DEFAULT_LEVELS = ("region", "zone", "woreda", "kebele")
PROPERTIES = ("code", "parent")
SOURCE = "mds-odk-geo-sync"


class SyncError(Exception):
    """A refusal with a message that names the cause and the fix."""

    exit_code = 1


class ConfigError(SyncError):
    exit_code = 1


class MasterDataError(SyncError):
    exit_code = 2


class CentralError(SyncError):
    exit_code = 3


# ---------------------------------------------------------------- pure part


def entity_uuid(level: str, value_id: str) -> str:
    # Central accepts version-4 uuids only: the name-based hash, stamped as v4,
    # stays the same for the same place on every run.
    return str(uuid.UUID(bytes=uuid.uuid5(NAMESPACE, f"{level}:{value_id}").bytes, version=4))


@dataclass
class Entity:
    uuid: str
    label: str
    data: dict[str, str]


@dataclass
class LevelPlan:
    level: str
    create: list[Entity] = field(default_factory=list)
    update: list[tuple[Entity, int]] = field(default_factory=list)  # (entity, base version)
    delete: list[str] = field(default_factory=list)
    unchanged: int = 0
    orphans: list[str] = field(default_factory=list)


def build_catalog(levels: list[dict], values: dict[str, list[dict]], wanted: tuple[str, ...]):
    """Desired entities per wanted level, and the values whose parent is unknown.

    `levels` is Master Data's level list (level_id, level_mnemonic,
    parent_level_id); `values` maps a level_id to its values.
    """
    by_mnemonic = {lv.get("level_mnemonic"): lv for lv in levels if lv.get("level_mnemonic")}
    missing = [w for w in wanted if w not in by_mnemonic]
    if missing:
        known = ", ".join(sorted(by_mnemonic)) or "none"
        raise MasterDataError(
            f"Master Data has no geo level {', '.join(missing)} (it has: {known}). "
            "Set ODK_GEO_LEVELS to the level names this Master Data uses."
        )
    level_of_id = {lv["level_id"]: lv.get("level_mnemonic") for lv in levels}

    catalog: dict[str, dict[str, Entity]] = {}
    orphans: dict[str, list[str]] = {}
    previous: str | None = None
    for name in wanted:
        level = by_mnemonic[name]
        rows = [r for r in values.get(level["level_id"], []) if r.get("level_value_id")]
        parent_level = level_of_id.get(level.get("parent_level_id"))
        parents = catalog.get(parent_level, {}) if parent_level == previous else {}
        parent_ids = {e.data["code"]: e.uuid for e in parents.values()}
        names: dict[str, list[str]] = {}
        for e in parents.values():
            names.setdefault(e.label.strip().casefold(), []).append(e.uuid)
        level_entities: dict[str, Entity] = {}
        for row in rows:
            value_id = str(row["level_value_id"])
            parent_uuid = ""
            if previous is not None:
                link = row.get("parent_level_value_id")
                parent_uuid = parent_ids.get(str(link)) if link is not None else None
                if parent_uuid is None and isinstance(link, str):
                    # A parent linked by name, unique at the parent level.
                    by_name = names.get(link.strip().casefold(), [])
                    parent_uuid = by_name[0] if len(by_name) == 1 else None
                if parent_uuid is None:
                    orphans.setdefault(name, []).append(value_id)
                    continue
            entity = Entity(
                uuid=entity_uuid(name, value_id),
                label=str(row.get("level_value_mnemonic") or value_id).strip(),
                data={"code": value_id, "parent": parent_uuid},
            )
            level_entities[entity.uuid] = entity
        catalog[name] = level_entities
        previous = name
    return catalog, orphans


def plan_level(level: str, desired: dict[str, Entity], existing: dict[str, dict], orphans=()) -> LevelPlan:
    """What to change in one Entity List. `existing` maps uuid to
    {"label", "data", "version"} as Central holds it."""
    plan = LevelPlan(level=level, orphans=list(orphans))
    for key, entity in desired.items():
        current = existing.get(key)
        if current is None:
            plan.create.append(entity)
            continue
        data = {p: str(current.get("data", {}).get(p) or "") for p in PROPERTIES}
        if current.get("label") != entity.label or data != entity.data:
            plan.update.append((entity, int(current.get("version") or 1)))
        else:
            plan.unchanged += 1
    plan.delete = [key for key in existing if key not in desired]
    return plan


# ---------------------------------------------------------------- clients


@dataclass
class Settings:
    mds_url: str
    mds_token_url: str | None
    mds_client_id: str | None
    mds_client_secret: str | None
    odk_url: str
    odk_email: str
    odk_password: str
    odk_project_id: int
    levels: tuple[str, ...]
    timeout: float
    dry_run: bool

    @classmethod
    def from_env(cls, env=os.environ) -> Settings:
        required = ("MDS_API_URL", "ODK_CENTRAL_URL", "ODK_CENTRAL_EMAIL", "ODK_CENTRAL_PASSWORD", "ODK_PROJECT_ID")
        missing = [k for k in required if not env.get(k)]
        if missing:
            raise ConfigError(f"Not configured: {', '.join(missing)}. Set them (see tools/odk-geo-sync/README.md).")
        auth = [env.get(k) for k in ("MDS_TOKEN_URL", "MDS_CLIENT_ID", "MDS_CLIENT_SECRET")]
        if any(auth) and not all(auth):
            raise ConfigError(
                "Master Data sign-in is partly configured: set all of MDS_TOKEN_URL, MDS_CLIENT_ID and "
                "MDS_CLIENT_SECRET, or none for a Master Data without sign-in."
            )
        try:
            project = int(env["ODK_PROJECT_ID"])
        except ValueError:
            raise ConfigError(f"ODK_PROJECT_ID: {env['ODK_PROJECT_ID']!r} is not a number. Use the project's id from Central.")
        levels = tuple(x.strip() for x in env.get("ODK_GEO_LEVELS", ",".join(DEFAULT_LEVELS)).split(",") if x.strip())
        return cls(
            mds_url=env["MDS_API_URL"].rstrip("/"),
            mds_token_url=auth[0], mds_client_id=auth[1], mds_client_secret=auth[2],
            odk_url=env["ODK_CENTRAL_URL"].rstrip("/"),
            odk_email=env["ODK_CENTRAL_EMAIL"], odk_password=env["ODK_CENTRAL_PASSWORD"],
            odk_project_id=project, levels=levels,
            timeout=float(env.get("SYNC_TIMEOUT_SECONDS", "60")),
            dry_run=env.get("DRY_RUN", "").lower() in ("1", "true", "yes"),
        )


class MasterData:
    def __init__(self, settings: Settings, http: httpx.Client):
        self.s, self.http = settings, http
        self.token: str | None = None

    def _host(self) -> str:
        return httpx.URL(self.s.mds_url).host or self.s.mds_url

    def _sign_in(self) -> None:
        if not self.s.mds_token_url or self.token:
            return
        try:
            resp = self.http.post(self.s.mds_token_url, data={
                "grant_type": "client_credentials",
                "client_id": self.s.mds_client_id, "client_secret": self.s.mds_client_secret})
        except httpx.HTTPError as error:
            raise MasterDataError(f"Master Data sign-in ({self.s.mds_token_url}) is unreachable ({error}); "
                                  "ODK locations not refreshed.") from error
        if resp.status_code >= 400 or not (resp.json() or {}).get("access_token"):
            raise MasterDataError(
                f"Master Data sign-in refused client {self.s.mds_client_id} (HTTP {resp.status_code}). "
                "Check MDS_CLIENT_ID / MDS_CLIENT_SECRET and that the client has service accounts enabled.")
        self.token = resp.json()["access_token"]

    def post(self, path: str, payload: dict):
        self._sign_in()
        envelope = {
            "request_header": {"sender_app_mnemonic": SOURCE, "sender_app_url": "",
                               "request_id": uuid.uuid4().hex,
                               "request_timestamp": datetime.now(UTC).isoformat()},
            "request_body": {"pagination_request": None, "request_payload": payload},
        }
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            resp = self.http.post(f"{self.s.mds_url}{path}", json=envelope, headers=headers)
        except httpx.HTTPError as error:
            raise MasterDataError(f"Master Data Service ({self._host()}) is unreachable ({error}); "
                                  "ODK locations not refreshed.") from error
        if resp.status_code >= 400:
            raise MasterDataError(f"Master Data Service ({self._host()}) refused {path} with HTTP "
                                  f"{resp.status_code}; ODK locations not refreshed. Check MDS_API_URL and the client's roles.")
        body = resp.json() or {}
        header = body.get("response_header") or {}
        if str(header.get("response_status") or "SUCCESS").upper() != "SUCCESS":
            detail = header.get("response_error_message") or header.get("response_error_code")
            raise MasterDataError(f"Master Data Service refused {path}: {detail}; ODK locations not refreshed.")
        return (body.get("response_body") or {}).get("response_payload")

    def geography(self):
        levels = self.post("/geo/get_all_geo_levels", {}) or []
        values = {}
        for level in levels:
            if level.get("level_mnemonic") in self.s.levels:
                values[level["level_id"]] = self.post(
                    "/geo/get_geo_level_values", {"level_id": level["level_id"], "parent_level_value_id": None}) or []
        return levels, values


class Central:
    PAGE = 5000
    BATCH = 1000

    def __init__(self, settings: Settings, http: httpx.Client):
        self.s, self.http = settings, http
        self.base = f"{settings.odk_url}/v1/projects/{settings.odk_project_id}"
        self.headers: dict[str, str] = {}

    def _check(self, resp: httpx.Response, what: str) -> httpx.Response:
        if resp.status_code < 400:
            return resp
        try:
            message = (resp.json() or {}).get("message") or resp.text
        except ValueError:
            message = resp.text
        hint = {401: "Check ODK_CENTRAL_EMAIL / ODK_CENTRAL_PASSWORD.",
                403: f"Give the account Project Manager on project {self.s.odk_project_id}.",
                404: f"Check ODK_PROJECT_ID ({self.s.odk_project_id}) and ODK_CENTRAL_URL."}.get(
            resp.status_code, "See Central's message.")
        raise CentralError(f"ODK Central refused {what} (HTTP {resp.status_code}: {message[:300]}). {hint}")

    def request(self, method: str, url: str, what: str, **kwargs) -> httpx.Response:
        try:
            resp = self.http.request(method, url, headers=self.headers, **kwargs)
        except httpx.HTTPError as error:
            raise CentralError(f"ODK Central ({self.s.odk_url}) is unreachable ({error}). "
                               "Check ODK_CENTRAL_URL; the Entity Lists were not changed.") from error
        return self._check(resp, what)

    def sign_in(self) -> None:
        resp = self.request("POST", f"{self.s.odk_url}/v1/sessions", "the sign-in",
                            json={"email": self.s.odk_email, "password": self.s.odk_password})
        self.headers = {"Authorization": f"Bearer {resp.json()['token']}"}

    def dataset_names(self) -> set[str]:
        return {d["name"] for d in self.request("GET", f"{self.base}/datasets", "the dataset list").json()}

    def ensure_dataset(self, name: str) -> None:
        if name not in self.dataset_names():
            self.request("POST", f"{self.base}/datasets", f"creating Entity List {name}", json={"name": name})
        info = self.request("GET", f"{self.base}/datasets/{name}", f"Entity List {name}").json()
        have = {p["name"] for p in info.get("properties", [])}
        for prop in PROPERTIES:
            if prop not in have:
                self.request("POST", f"{self.base}/datasets/{name}/properties",
                             f"adding property {prop} to {name}", json={"name": prop})

    def entities(self, name: str) -> dict[str, dict]:
        """uuid -> {label, data, version}, through the dataset's OData feed."""
        found: dict[str, dict] = {}
        skip = 0
        while True:
            page = self.request("GET", f"{self.base}/datasets/{name}.svc/Entities", f"reading Entity List {name}",
                                params={"$top": self.PAGE, "$skip": skip}).json().get("value", [])
            for row in page:
                found[row["__id"]] = {
                    "label": row.get("label"),
                    "data": {p: row.get(p) for p in PROPERTIES},
                    "version": (row.get("__system") or {}).get("version", 1),
                }
            if len(page) < self.PAGE:
                return found
            skip += self.PAGE

    def apply(self, name: str, plan: LevelPlan) -> None:
        url = f"{self.base}/datasets/{name}/entities"
        for i in range(0, len(plan.create), self.BATCH):
            batch = plan.create[i:i + self.BATCH]
            self.request("POST", url, f"creating {len(batch)} entities in {name}", json={
                "entities": [{"uuid": e.uuid, "label": e.label, "data": e.data} for e in batch],
                "source": {"name": SOURCE, "size": len(batch)}})
        for entity, version in plan.update:
            self.request("PATCH", f"{url}/{entity.uuid}", f"updating {entity.data['code']} in {name}",
                         params={"baseVersion": version}, json={"label": entity.label, "data": entity.data})
        for key in plan.delete:
            self.request("DELETE", f"{url}/{key}", f"deleting entity {key} from {name}")


# ---------------------------------------------------------------- run


def run(settings: Settings, http: httpx.Client | None = None) -> list[LevelPlan]:
    http = http or httpx.Client(timeout=settings.timeout)
    levels, values = MasterData(settings, http).geography()
    catalog, orphans = build_catalog(levels, values, settings.levels)
    if not any(catalog.values()):
        raise MasterDataError("Master Data returned no geo values; ODK locations not refreshed (an empty "
                              "Master Data would otherwise empty every list). Check the seed of this namespace.")
    central = Central(settings, http)
    central.sign_in()
    plans = []
    for level in settings.levels:
        if not settings.dry_run:
            central.ensure_dataset(level)
            existing = central.entities(level)
        else:
            # Read only: what would change in the lists that exist now.
            existing = central.entities(level) if level in central.dataset_names() else {}
        plan = plan_level(level, catalog[level], existing, orphans.get(level, ()))
        if not settings.dry_run:
            central.apply(level, plan)
        plans.append(plan)
    return plans


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
        plans = run(settings)
    except SyncError as error:
        _logger.error("%s", error)
        return error.exit_code
    for p in plans:
        _logger.info("%s: %d created, %d updated, %d deleted, %d unchanged%s", p.level, len(p.create),
                     len(p.update), len(p.delete), p.unchanged, " (dry run)" if settings.dry_run else "")
        if p.orphans:
            _logger.warning("%s: %d values skipped, their parent is not in Master Data's %s level (first: %s). "
                            "Fix the parent links in Master Data.", p.level, len(p.orphans), p.level,
                            ", ".join(p.orphans[:5]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
