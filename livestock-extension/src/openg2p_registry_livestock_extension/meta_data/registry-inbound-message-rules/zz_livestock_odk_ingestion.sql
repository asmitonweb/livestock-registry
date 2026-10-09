-- ODK submissions from the connector service become Livestock intakes.
--
-- The connector (openg2p-connector-service) polls ODK Central and posts each
-- livestock_registry submission to the Partner API as
--
--   POST /partner/ingest_data?data_model=MY_DATA_MODEL
--   {"header":  {"message_id": "livestock_registry:<instance id>", "sender_id": ..., ...},
--    "message": {"payload": {<the ODK submission, repeat groups expanded>}}}
--
-- The Partner API wraps the request as {"body": <request>}, so every path below
-- starts at $.body. The celery worker then classifies it onto the Livestock
-- register's ODK intake form and renders ls_odk_transform.j2 (odk_ingest_hooks
-- renders the copy packaged with the extension; the catalogue row below names
-- the same file in the templates bucket).
--
-- These are the rows the dev environment was configured with by hand, with the
-- same ids, so a fresh environment comes up able to ingest without that manual
-- step. Every row is upserted on its fixed id, so a re-run changes nothing.
-- The partner the connector posts as (livestock-partner) lives in Master Data,
-- which db-seed does not write: see ci/connector/README.md.

INSERT INTO "public"."data_models" (
    "data_model_id", "data_model_mnemonic", "pattern_for_data_model",
    "response_template_document_id", "is_active"
) VALUES (
    'MY_DATA_MODEL', 'MY_DATA_MODEL', '$.body.header.sender_id=>^.*$', NULL, TRUE
) ON CONFLICT ("data_model_id") DO UPDATE SET
    "data_model_mnemonic" = EXCLUDED."data_model_mnemonic",
    "pattern_for_data_model" = EXCLUDED."pattern_for_data_model",
    "response_template_document_id" = EXCLUDED."response_template_document_id",
    "is_active" = EXCLUDED."is_active";

INSERT INTO "public"."incoming_model_key_paths" (
    "key_path_id", "data_model_id", "key_path_for_message_id", "key_path_for_sender",
    "key_path_for_signature", "key_path_for_signature_payload", "is_list",
    "key_path_for_list_elements"
) VALUES (
    'my_key_path', 'MY_DATA_MODEL',
    '$.body.header.message_id', '$.body.header.sender_id',
    '$.body.header.signature', '$.body.message', FALSE, ''
) ON CONFLICT ("key_path_id") DO UPDATE SET
    "data_model_id" = EXCLUDED."data_model_id",
    "key_path_for_message_id" = EXCLUDED."key_path_for_message_id",
    "key_path_for_sender" = EXCLUDED."key_path_for_sender",
    "key_path_for_signature" = EXCLUDED."key_path_for_signature",
    "key_path_for_signature_payload" = EXCLUDED."key_path_for_signature_payload",
    "is_list" = EXCLUDED."is_list",
    "key_path_for_list_elements" = EXCLUDED."key_path_for_list_elements";

-- Every connector submission is a livestock registration: Livestock register,
-- its ODK intake form.
INSERT INTO "public"."incoming_model_semantic_patterns" (
    "semantic_pattern_id", "data_model_id", "register_id", "intake_form_id",
    "section_id", "pattern_for_register", "pattern_for_intake_form",
    "pattern_for_section", "key_path_for_business_payload",
    "raw_payload_enricher_class"
) VALUES (
    'SP-ODK-LIVESTOCK-1', 'MY_DATA_MODEL',
    '997676d3-7008-59f9-b23e-613ad79bbb08', 'e92e8be1-207f-518e-99c2-8bc21cc1f112',
    NULL, NULL, NULL, NULL, '$.body.message.payload',
    'G2PDciLivestockCreateEnricherService'
) ON CONFLICT ("semantic_pattern_id") DO UPDATE SET
    "data_model_id" = EXCLUDED."data_model_id",
    "register_id" = EXCLUDED."register_id",
    "intake_form_id" = EXCLUDED."intake_form_id",
    "pattern_for_intake_form" = EXCLUDED."pattern_for_intake_form",
    "key_path_for_business_payload" = EXCLUDED."key_path_for_business_payload",
    "raw_payload_enricher_class" = EXCLUDED."raw_payload_enricher_class";

-- The template catalogue row; db-seed uploads the object (LOAD_TEMPLATES)
-- under the same key.
INSERT INTO "public"."g2p_registry_documents" (
    "document_id", "document_store_id", "bucket", "source_filename",
    "created_by", "created_at"
) VALUES (
    '8d92ac11-b93b-4a13-9f06-3f4ed20399cc', 'ls_odk_transform.j2', 'templates',
    'ls_odk_transform.j2', 'seeder', '2026-09-29 20:35:05'
) ON CONFLICT ("document_id") DO UPDATE SET
    "document_store_id" = EXCLUDED."document_store_id",
    "bucket" = EXCLUDED."bucket";

INSERT INTO "public"."incoming_templates" (
    "template_id", "register_id", "data_model_id", "template_document_id",
    "jsonld_expansion_required", "created_at", "updated_at"
) VALUES (
    'IN-TMPL-ODK-1', '997676d3-7008-59f9-b23e-613ad79bbb08',
    'MY_DATA_MODEL', '8d92ac11-b93b-4a13-9f06-3f4ed20399cc',
    FALSE, '2026-09-29 20:35:05', NULL
) ON CONFLICT ("template_id") DO UPDATE SET
    "register_id" = EXCLUDED."register_id",
    "data_model_id" = EXCLUDED."data_model_id",
    "template_document_id" = EXCLUDED."template_document_id",
    "jsonld_expansion_required" = EXCLUDED."jsonld_expansion_required";
