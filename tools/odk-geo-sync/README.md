# odk-geo-sync: Master Data locations in ODK

ODK forms pick Region, Zone, Woreda and Kebele from **Entity Lists** in ODK Central, and
this job keeps those lists equal to the registry's Master Data. A boundary change in
Master Data reaches Collect with the next form update; no CSV is rebuilt and no form
is republished.

| Entity List | One entity per | `label` | `code` | `parent` |
| --- | --- | --- | --- | --- |
| `region`, `zone`, `woreda`, `kebele` | geo value of that level | its name | its Master Data id (what the form submits) | the parent's entity uuid (the cascade filter) |

- **Stable uuids.** The entity uuid is UUIDv5 of `<level>:<Master Data id>`, so a rerun updates the same entities and submissions keep pointing at them.
- **Mirrors exactly.** The lists follow Master Data: new values are created, renames and moves are updated, and removed values are deleted.
- **Name-linked parents.** Some Master Data link a value to its parent by the parent's name (Livestock's does). Such a parent is resolved to the parent level's id. A value whose parent is in neither form is skipped and reported.
- **Fails safe.** A run that cannot read Master Data, or reads an empty one, changes nothing, so Collect keeps the last good lists.

## Settings (environment)

| Variable | Required | Meaning |
| --- | --- | --- |
| `MDS_API_URL` | yes | Master Data API base, e.g. `http://commons-services-master-data-api` |
| `MDS_TOKEN_URL`, `MDS_CLIENT_ID`, `MDS_CLIENT_SECRET` | all or none | client-credentials sign-in, when Master Data requires one (the staff API's Master Data client works) |
| `ODK_CENTRAL_URL` | yes | e.g. `https://odk.live.openg2p.test` |
| `ODK_CENTRAL_EMAIL`, `ODK_CENTRAL_PASSWORD` | yes | a Central web user with **Project Manager** on the project (from a Secret) |
| `ODK_PROJECT_ID` | yes | the registry's ODK project |
| `ODK_GEO_LEVELS` | no | levels to publish, default `region,zone,woreda,kebele` (Master Data level names) |
| `DRY_RUN` | no | `true`: read both sides and log what would change, write nothing |
| `SYNC_TIMEOUT_SECONDS` | no | per request, default 60 |

Exit codes:
- `0`: done.
- `1`: configuration is wrong; the log names the setting.
- `2`: Master Data is unreachable or refused; the lists are unchanged.
- `3`: ODK Central refused; the log names the call and what to fix.

## Running

Run it as a CronJob in each registry namespace, since each registry owns its Master Data and its ODK project:
- nightly, plus once at install;
- before a form that uses the lists is first published, because Central links a form's `region.csv` … attachments to the Entity Lists only when the lists exist.

To get changes the same hour, run it on demand after a Master Data geography release.
Master Data's WebSub `*.version.effective` event is the planned trigger.

```sh
docker build -t odk-geo-sync tools/odk-geo-sync
docker run --rm --env-file geo-sync.env odk-geo-sync          # DRY_RUN=true first
```

A first run creates about 21,000 entities (19,535 kebeles in ETH-OAN) in batches of 1,000.
Later runs change only what Master Data changed.

## Tests

```sh
uv run --no-project --with pytest --with httpx pytest tools/odk-geo-sync/tests
```

They run against in-memory stand-ins for Master Data and Central's API:
- create, rerun, rename, move and remove;
- name-linked parents and orphans;
- Master Data down or empty;
- Central refusals;
- dry run;
- configuration errors.
