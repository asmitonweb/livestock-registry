# Livestock Registry — ODK form

Field agents register a farmer's animals, and each animal's health, vaccination,
vital and breeding events, with the **Livestock Registry** form in ODK Collect
(or the browser). ODK Central stores the submissions; the livestock connector
polls Central and posts each one to the registry, which turns it into a
livestock intake.

```
ODK Collect / web form → ODK Central → livestock connector (poll, photos inline)
  → partner API → celery: classify → ls_odk_transform.j2 (packaged) → livestock intake
```

## At a glance

| | |
| --- | --- |
| Form file | `odk/livestock_registry.xlsx` |
| Form id / title | `livestock_registry` / "Livestock Registry" |
| Version | published on dev as `v2` (the settings sheet says `v1.1.0`: Central set the version at publish time) |
| ODK Central (dev) | `https://odk-central-development.oanstaging.com`, project **14** |
| Media | `odk/media/region_odk.csv`, `zone_odk.csv`, `woreda_odk.csv`, `kebele_odk.csv` |
| Location lists | generated from the livestock Master Data seed: `odk/build_location_media.py` |
| Transform | `livestock-extension/.../templates/ls_odk_transform.j2`, rendered from the package by `odk_ingest_hooks.py` |
| Connector | `ci/connector/` (release `livestock-connector`, namespace `live`), data model `MY_DATA_MODEL`, partner `livestock-partner` |
| Registry routing | `zz_livestock_odk_ingestion.sql` (data model, key path, semantic pattern, template row) |

## What is where

| Path | |
| --- | --- |
| `odk/livestock_registry.xlsx` | the XLSForm, as exported from Central (`GET /v1/projects/14/forms/livestock_registry.xlsx`) |
| `odk/media/` | the location CSVs the form reads (`select_one_from_file`) |
| `odk/build_location_media.py` | rebuilds the location CSVs from the Master Data seed |
| `livestock-extension/.../templates/ls_odk_transform.j2` | ODK submission → intake sections |
| `livestock-extension/.../odk_ingest_hooks.py`, `odk_ingest_events.py`, `ingest_log.py` | registry-side ingestion behaviour and its log |
| `local/postgres/seed_connector_pipelines.sql` | a connector pipeline for local runs (ODK login passed as psql variables) |
| `test/test_odk_forms.py`, `test_odk_transform.py`, `test_odk_ingest_events.py`, `test_odk_location_media.py` | CI checks |

## Form structure

| Group | Asks |
| --- | --- |
| `farmer` | farmer id, Fayda FAN, first / middle / last name, gender, birth date, mobile, registration date |
| `survey_personnel` | surveyor and supervisor names and mobiles |
| `location` | **region → zone → woreda → kebele** (cascading) |
| `livestock` (repeat, one per animal or flock) | species, breed, ear tag, secondary identifier, quantity, weight, birth date, gender, health and vaccination status, registration date |
| → `health_events` (repeat) | event type, disease, onset / resolution, notifiable, treatment, vet, location, notes |
| → `vaccinations` (repeat) | vaccine, date, next due, batch, administered by, notes |
| → `vital_events` (repeat) | type (birth, death, …), date, location, officer, birth details (offspring count, sex), disease details, cause |
| → `breeding_events` (repeat) | type (AI / natural), date, location, sire or semen, AI technician / technique / batch, expected calving, pregnancy confirmation, outcome, notes |

The transform turns these into the intake sections `ls_farmer_identity`,
`ls_farmer_location`, `ls_survey_personnel`, `ls_livestock_record`,
`ls_livestock_location`, `ls_animal_details`, `ls_health_event_details`,
`ls_vaccination_details`, `ls_vital_event_details` and `ls_breeding_details`;
species, breed, vaccine and status names are mapped to the registry's values.

## Location lists

The livestock registry's Master Data is seeded from
`docker/db-seed/geo/ethiopia_geo_seed.sql.gz`: the shared Ethiopia hierarchy
(14 regions, 125 zones, 1,379 woredas, 19,535 kebeles), loaded into a geo table
whose place **names** must be unique and whose parent link is the parent's
name. Rows load region first and a repeated name is skipped, so Master Data
holds 14 regions, 121 zones, 1,240 woredas and 17,598 kebeles. The form's four
CSVs offer exactly those, each under the parent Master Data gives it:

| Level | CSV value | Master Data id |
| --- | --- | --- |
| region | `oromia` (key; `region_map` in the transform) | `region-ET04` |
| zone | `ET0408` | `zone-ET0408` |
| woreda | `ET040801` | `woreda-ET040801` |
| kebele | `40801101001` | `kebele-ET040801101001` |

**What the agent cannot pick.** Because of the name rule:

- 1,937 kebeles, 139 woredas and 4 zones of the shared hierarchy are not in
  livestock's Master Data (their names repeat elsewhere);
- 63 woredas and 297 kebeles are in Master Data and the form but cannot be
  reached through the cascade, in Master Data or in the form. Their zone is
  named like its region (Addis Ababa, Harari, Sidama, and Amhara's Oromia
  special zone named like the Oromia region) and was skipped, or, for the three
  special woredas (Kebena, Mareko, Tembaro), the seed gives no parent.

Farmer and crop sown use the hierarchy keyed by id, which has neither gap.
Moving livestock's Master Data onto it would let these lists become the full
hierarchy too. `test/test_odk_location_media.py` pins the current state: the
lists equal what the seed loads, and the unreachable places are exactly those.

**When the seed changes:**

```sh
python odk/build_location_media.py          # rewrites odk/media/*_odk.csv
python odk/build_location_media.py --check  # what CI runs
```

then publish the new CSVs (below).

## Photos

The form asks no photo today. The pipeline is ready for one: the connector
downloads a submission's attachments and sends them inline as
`{"__type": "File", ...}` (`embed_attachments`, on by default; over 10 MiB is
skipped), and `ls_odk_transform.j2` maps an inline photo named
**`livestock_photo`** (top level or in the `farmer` group) to the Livestock
record's `record_image_document_id`. On save the Livestock service uploads it to
the documents bucket and stores the document id
(`register_domain/services/embedded_files.py`); the record's profile picture
reads it. A file that arrived as a name only is left out and logged. Adding an
`image` question named `livestock_photo` is the only step left;
`test/test_odk_forms.py` fails on a photo question the transform does not map.

## Publishing

The connector polls the form by id, so **keep the form id `livestock_registry`**.

**New CSVs only:** ODK Central → project 14 → *Livestock Registry* → **Create a
new draft** → **Media Files** → upload the changed CSVs → test → **Publish**
with a new version. With the API:

```sh
C=https://odk-central-development.oanstaging.com; P=14; F=livestock_registry
T=$(curl -s $C/v1/sessions -H 'Content-Type: application/json' -d '{"email":"…","password":"…"}' | jq -r .token)
curl -s -X POST "$C/v1/projects/$P/forms/$F/draft" -H "Authorization: Bearer $T"     # copies the published version
for m in region_odk.csv zone_odk.csv woreda_odk.csv kebele_odk.csv; do
  curl -s -X POST "$C/v1/projects/$P/forms/$F/draft/attachments/$m" -H "Authorization: Bearer $T" \
    -H 'Content-Type: text/csv' --data-binary @odk/media/$m; done
curl -s -X POST "$C/v1/projects/$P/forms/$F/draft/publish?version=<new version>" -H "Authorization: Bearer $T"
```

**A changed form:** bump the settings `version` cell past what Central has seen,
upload the XLSX as a new draft (`POST …/forms/livestock_registry/draft` with the
XLSX body and `X-XlsForm-FormId-Fallback: livestock_registry`), attach the four
CSVs, test, publish, and commit the XLSX in `odk/`.

The transform ships inside the registry image (the hooks render the packaged
copy), so a transform change goes live with the next registry deploy.

## Connector

`ci/connector/README.md`. The pipeline: ODK Central base URL, project 14, form
`livestock_registry`, data model `MY_DATA_MODEL`, header
`partner-id: livestock-partner`, `resolve_nav_links` and `embed_attachments` on.
Each submission is sent **once**: an edit in Central is not re-sent; make a new
submission instead.

## Following a submission

The connector and the registry write JSON lines to `odk-ingest.jsonl` and stdout:

| Where | Events |
| --- | --- |
| connector | `poll_*`, `record_received`, `attachment_*` / `attachments_summary`, `sent` / `send_failed`, `duplicate_ignored` |
| partner API | `ingest_request_failed` |
| transformation worker | `submission_received` (files inline / name only), `file_missing`, `transformed`, `transform_failed` |
| ingest worker | `file_stored` / `file_rejected`, `ingest_succeeded`, `ingest_retry_scheduled`, `ingest_failed` (error, attempt, next step) |

```sh
kubectl -n live logs deploy/livestock-connector-worker | grep 'livestock_registry:uuid:<id>'
kubectl -n live logs deploy/livestock-registry-celery-worker | grep '"ingest_id": "<ingest id>"'
```

Registry events carry `ingest_id` (`incoming_classified_data.ingest_id`);
`submission_received` also has the ODK `instance_id`, which links the two.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Web form: "This form does not exist … (Attempted to access form with ID: )" | Enketo's link was made under an older Central hostname; the Central admin regenerates the form's Enketo id. Collect is unaffected. |
| An agent cannot find a woreda or kebele | It is one of the places the name rule drops or strands (see Location lists). |
| `ingest_failed` | the reason is in the event and in `incoming_classified_data.ingestion_latest_error_code`; the hooks do not retry a rejected submission |
| Nothing arrives | connector `poll_failed` / `send_failed`; an edited submission is not re-sent |
