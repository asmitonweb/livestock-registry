# Livestock ODK form

| | |
| --- | --- |
| Form | `livestock_registry.xlsx`: form id `livestock_registry`, "Livestock Registry" |
| Published on | ODK Central dev, `https://odk-central-development.oanstaging.com`, project 14, as version `v2` |
| Media | `media/region_odk.csv`, `zone_odk.csv`, `woreda_odk.csv`, `kebele_odk.csv` (the location lookups the form reads) |
| Connector pipeline | `Livestock ODK Central`: form `livestock_registry`, data model `MY_DATA_MODEL`, partner `livestock-partner` |
| Transform | `livestock-extension/.../templates/ls_odk_transform.j2` |

The XLSX is the file as uploaded to Central, exported from there
(`GET /v1/projects/14/forms/livestock_registry.xlsx`). Central set the published
version (`v2`) at publish time, so the `version` cell in the file's settings
sheet (`v1.1.0`) is not what Collect shows. To publish a change, bump that cell
to something Central has not seen, upload it as a new draft of
`livestock_registry` (keep the form id: the connector polls it by name), attach
the four CSVs, test the draft, publish, and commit the new XLSX here.

`test/test_odk_forms.py` checks that the form id matches the connector pipeline,
that every CSV the form reads is in `media/`, and that every photo question is
one the transform maps.

## Photos

The form asks no photo today. The pipeline is ready for one:

1. The connector downloads a submission's attachments from Central and sends
   each inline as `{"__type": "File", "name", "type", "data": <base64>}`
   (`embed_attachments`, on by default; files over 10 MiB are skipped).
2. `ls_odk_transform.j2` maps an inline photo named **`livestock_photo`** (top
   level, or in the `farmer` group) to the Livestock record's
   `record_image_document_id`. A bare file name means the file did not come
   along, and it is left out.
3. On save, the Livestock service uploads it to the documents bucket and stores
   the document id (`register_domain/services/embedded_files.py`), which the
   record's profile picture reads.

So adding an `image` question named `livestock_photo` to the form is the only
step left. A photo question under any other name fails `test_odk_forms.py`
until the transform maps it.

## Following a submission

The connector and the registry both write JSON-lines ingestion logs: the
connector to `/app/logs/odk-ingest.jsonl`, the registry's partner API and celery
workers to `logs/odk-ingest.jsonl` (`REGISTRY_EXTENSIONS_ODK_INGEST_LOG_FILE`).
All of them also go to stdout.

| Where | Events |
| --- | --- |
| connector | `poll_started` / `poll_finished` / `poll_failed`, `record_received`, `attachment_embedded` / `attachment_not_uploaded` / `attachment_too_large` / `attachment_download_failed`, `sent`, `send_failed`, `duplicate_ignored` |
| partner API | `ingest_request_failed` |
| transformation worker | `submission_received` (files inline / name only), `file_missing`, `transformed`, `transform_failed` |
| ingest worker | `file_stored` / `file_rejected`, `ingest_succeeded`, `ingest_retry_scheduled`, `ingest_failed` (the error, the attempt, and what to do next) |

```sh
kubectl -n live logs deploy/livestock-connector-worker | grep '"source_event_id": "livestock_registry:uuid:<id>"'
kubectl -n live logs deploy/livestock-registry-celery-worker | grep '"ingest_id": "<ingest id>"'
```

The registry events carry `ingest_id` (`incoming_classified_data.ingest_id`);
`submission_received` also has the ODK `instance_id`, which links the two.
