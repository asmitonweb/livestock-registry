# Livestock Extension

The Livestock domain package for the OpenG2P registry platform: SQLAlchemy
models, pydantic schemas, register domain services (validation, `search_text`
and `record_name` construction), the functional-ID generator, and the seed
metadata (register definitions/sections/UI tabs, lookup data, AWE policies, DCI
templates).

Installed into the registry-platform base images and selected at runtime by
`REGISTRY_EXTENSION_MODULE=openg2p_registry_livestock_extension`.

## Registers

Ported from the Odoo module `g2p_livestock_registry`: the livestock holding is
the hub, and every line links straight to it, mirroring the module's `line_ids`
and `*_event_ids` one2many fields.

| Mnemonic | Parent | Table | Odoo model |
|---|---|---|---|
| `Farmer` | — (root) | `g2p_register_farmers` | `res.partner` (farmer fields) |
| `Livestock` | — (root) | `g2p_register_livestocks` | `g2p.livestock.registry` |
| `Animal` | Livestock | `g2p_register_animals` | `g2p.livestock.registry.line` |
| `HealthEvent` | Livestock | `g2p_register_health_events` | `g2p.livestock.health.event` |
| `Vaccination` | Livestock | `g2p_register_vaccinations` | `g2p.livestock.vaccination` |
| `VitalEvent` | Livestock | `g2p_register_vital_events` | `g2p.livestock.vital.event` |
| `Breeding` | Livestock | `g2p_register_breedings` | `g2p.livestock.breeding` |
| `VaccineSchedule` | Livestock | `g2p_register_vaccine_schedules` | `g2p.livestock.vaccine.schedule` |
| `ImportBatch` | Livestock | `g2p_register_import_batches` | `g2p.livestock.import.batch` |
| `AuditLog` | Livestock | `g2p_register_audit_logs` | `g2p.livestock.audit.log` |

`live_stock_dashboard` is deliberately not ported: the staff portal renders its
own views over these registers.

### The farmer is identified, not owned

The Farmer Registry stays the system of record for people. The `Farmer` register
here carries only the identity needed to attach livestock to a person — the
externally issued `farmer_id` and `fayda_fan_id` alongside the internally
generated functional ID, plus name, sex and contact.

The livestock holding repeats those identifiers (`farmer_uuid`, `farmer_id`,
`fayda_fan_id`, and a denormalised `farmer_name`) so a holding can be read
without a join, and mirrors the Fayda FAN into the platform's
`link_foundational_id` — the column that exists for "this record belongs to a
person held elsewhere".

`farmer_id` is validated as `FR-` followed by exactly twelve digits in both
the Farmer and Livestock register services, e.g. `FR-123456789012`.

### Enriched from the Farmer Registry

When staff submit (finalize) an intake form whose Farmer section has a Farmer
ID, the registry looks that farmer up in the Farmer Registry and overwrites the
typed first / middle / last name, gender, date of birth and mobile number with
the Farmer Registry's values (`farmer_registry_enrichment.py`, called from
`G2PRegisterDomainServiceFarmer.pre_finalize`, a hook added by
`docker/staff-api/core-patches/apply_patches.py` Fix 7). Saving a section does
not call the Farmer Registry. The lookup is a DCI
`idtype-value` search signed with this registry's partner key from Partner
Management, carrying a freshly signed consent object for the Consent Manager.
Only the cleaned fields are kept; the raw response is not stored.

It applies to web intake only (ODK and File Import do not pass through that
hook), is off unless `FARMER_REGISTRY_ENRICHMENT_ENABLED=true` (see the
`FARMER_REGISTRY_*` settings in `docker-compose.yml` / the chart values), and
fails open: if the Farmer Registry is unreachable, rejects the call, or does
not return exactly one farmer, the operator's entries stand.

### Two identifiers for a holding

* **`oan_id`** is the OAN identifier built from the farmer code and first name,
  e.g. `FR-123456789012-ABEBE-001`, with a running number per farmer — the format
  `_generate_oan_id` produces. It is what an operator reads and what
  `record_name` uses.
* **`functional_record_id`** is allocated by the platform's id-generator with the
  `LS-` prefix, and is the stable key other records reference.

Animals are identified by **`ear_tag_id`**, which with species and owner forms
the uniqueness rule the module enforces as `ear_tag_species_owner_uniq`.

### Approval

Nothing reaches a register without approval. Every Livestock and Farmer change
request and every Livestock intake goes through AWE to the **Woreda Approvers of
the record's own woreda**, in one stage (Oct 2026 decision; it replaced gen1's
Kebele -> Woreda -> Zone -> Region ladder, so a record goes from DRAFT to
VERIFIED on that approval).

- **Who is a Woreda Approver for a woreda:** a staff member with the client role
  `Woreda Approver` and a registry data policy (`DP_<mnemonic>`) on the register
  that allows that `woreda_id` (EQ or IN). The same policy limits what they see.
  `Zone Approver` / `Region Approver` work the same on `zone_id` / `region_id`
  for a binding that asks for them.
- **How it is routed:** the policy binding (`zzz_geo_approval_awe.sql`) lists
  `geo_woreda_approver_ids` in `context_field_names`; `geo_approval/` computes it
  on the server and the stage's expression rule reads it (`awe_meta_data/`). A
  submitted payload cannot supply it. A change request routes by the record's
  approved woreda, so proposing a new location cannot pick its own approver; an
  intake routes by its submitted woreda.
- **When it cannot be routed** the submission is refused with the reason: no
  woreda on the record ("select the woreda"), or no Woreda Approver for it ("ask
  the administrator to give a staff member the role and a data policy"). There
  is no fallback to another level or place.
- The Farmer identity section keeps its own Farmer reference policy
  (`zzz_farmer_reference_awe.sql`).

### Catalogues and lookups

Values an administrator should be able to extend without a code change are
**attribute lookups** — String columns resolved against `g2p_attributes` /
`g2p_attribute_values`, seeded in `meta_data/lookup-data/` and refreshed from the
master-data catalogue by `catalog-sync`:

| Attribute | Hierarchy |
|---|---|
| `LIVESTOCK_SPECIES` | flat |
| `LIVESTOCK_BREED` | under species |
| `VACCINE_TYPE` | under species |
| `LIVESTOCK_DISEASE` | flat |
| `APPROVAL_STATUS` | flat |

`local/masterdata-mock/catalogs.json` is the same data in the catalogue API's
shape; the two share the `<catalog>_<code>` value-id scheme, so the seeded rows
and the synced rows are the same rows.

REGION / ZONE / WOREDA / KEBELE are not seeded here: the platform resolves the
geo hierarchy from master-data through `G2PGeo.geo_lowest_level_value_id`.

Closed value sets stay as Python enums (`register_domain/models/enums.py`):
livestock state, source system, sync status, animal state, sex, health status,
vaccination status, health-event type, vital-event type and cause, breeding event
type and outcome, event location, import source and state, and audit action type.
