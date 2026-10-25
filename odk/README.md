# Livestock ODK form and ingestion

Field officers use ODK Collect or Central's web form to record a livestock holding:
- the farmer and the location;
- the animals;
- the animals' health, vaccination, vital and breeding events.

ODK Central passes submissions to the registry's ingestion API (`POST /partner/ingest_data`).
The ingestion workers then:
1. classify the submission;
2. transform it with `ls_odk_transform.j2`;
3. save it as an intake submission.

The submission then goes through AWE approval before it reaches the register.

## Files

| File | What |
|---|---|
| `livestock_registry.xlsx` | The XLSForm (form id `livestock_registry`). v3 removed the animal Sex choice `other`. v4 adds "not in the future" to both resolution dates. **v5** takes Region, Zone, Woreda and Kebele from the Master Data Entity Lists instead of CSV attachments (see [Locations](#locations)). |
| `sample_submissions.json` | A real-shaped submission as Central's OData API returns it, with the repeat groups inlined. |
| `breed_choices.csv` | The form's breed choices (species, name), a copy of the `breed` list on the choices sheet. Update it with the form. |
| `test_transform_locations.py` | Renders the transform for a v4 (CSV) and a v5 (Entity List) submission and checks where the locations go. |

The four location CSVs (`media/*_odk.csv`) are gone. Their values were neither Master
Data ids nor names, so ingestion had to map them. A boundary change also meant rebuilding
the files and republishing the form.

## Locations

`tools/odk-geo-sync` keeps one Entity List per level (`region`, `zone`, `woreda`,
`kebele`) in the registry's ODK project equal to Master Data. It runs nightly and after
a geography release.

| In the form (group `location`) | What |
|---|---|
| `region_id` … `kebele_id` | `select_one_from_file region.csv` … `kebele.csv`; Central serves each file from the Entity List of the same name. The zone, woreda and kebele lists are filtered by `parent=${<level above>_id}`. |
| `region_code` … `kebele_code` | `instance('<level>')/root/item[name=${<level>_id}]/code`: the chosen place's Master Data id (a choice's own value is the entity's uuid). |
| `location_source` | `'mds'`: tells the transform this submission carries Master Data ids. |

For a v5 submission, the transform sends:
- the ids as `region_id` … `kebele_id`;
- no names.

The registry fills the names from the ids when the record is saved (`admin_area_sync`), the same way it does for staff entry.
A v4 or older submission is transformed as before.

## Publishing a form change

1. Run `tools/odk-geo-sync` once for the project if it has not run yet. Central links the form's `region.csv` … attachments to the Entity Lists only when the lists exist.
2. Edit `livestock_registry.xlsx` and bump `version` on the `settings` sheet.
3. In Central, open the project, then the form, then Edit Form, then Upload new Form Definition. Each of the four location attachments shows as linked to its Entity List. If one does not, link it on the draft's attachment list.
4. Test the draft (Test in browser): picking a Region narrows the Zones, and so on. Then Publish Draft.
5. Keep this folder's copy identical to what Central serves.

## Form → registry mapping

| Form (survey sheet) | Intake section | Notes |
|---|---|---|
| `farmer/*` | `ls_farmer_identity`, `ls_livestock_record` | `fayda_fan_id` 16 digits, `farmer_id` `FR-` + 10 digits; `gender` upper-cased |
| `survey_personnel/*` | `ls_survey_personnel` | |
| `location/*` | `ls_farmer_identity`, `ls_farmer_location`, `ls_livestock_location` | v5: the Master Data ids as `region_id` … `kebele_id`, names filled by the registry. v4: the region slug becomes a name in the template, and zone/woreda/kebele codes are matched against Master Data |
| `livestock` (repeat) | `ls_animal_details` | species → `LIVESTOCK_SPECIES_*`; breed → `LIVESTOCK_BREED_*`; poultry/beehive use `secondary_identifier` + `quantity` and Sex `mixed` |
| `livestock/health_events` | `ls_health_event_details` | `disease_type` → `LIVESTOCK_DISEASE_*` (DISEASE events only); `health_event_notes` → notes |
| `livestock/vaccinations` | `ls_vaccination_details` | `vaccine` → `VACCINE_TYPE_*` by species |
| `livestock/vital_events` | `ls_vital_event_details` | `birth_details/sex_of_offspring` → `offspring_gender`; `disease_details/*` → disease fields; `vital_event_details` → notes |
| `livestock/breeding_events` | `ls_breeding_details` | `natural_breeding` / `artificial_insemination` → NATURAL / AI; `artificial_insemination_tab/*` → AI fields |

Some values are neither a form choice nor an existing code, such as an unknown disease,
vaccine or breed. Those are passed through unchanged, so the same domain validation that
guards staff entry rejects the submission with its own message.

## Approval

An ODK submission is saved as a normal intake submission, with the livestock record in
state DRAFT. The ingest worker finalizes it and starts AWE approval with a service-account
token. The token comes from `REGISTRY_CELERY_WORKERS_AWE_TOKEN_URL`, `_CLIENT_ID` and
`_CLIENT_SECRET` on the celery worker.

From there it follows the same approval stages as a submission typed in the staff portal.
Without those settings the submission is finalized but has no approval request, so it sits
PENDING with no task for any approver.
