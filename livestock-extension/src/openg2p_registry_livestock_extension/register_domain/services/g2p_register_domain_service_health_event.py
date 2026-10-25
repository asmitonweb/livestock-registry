import logging
from datetime import date

from openg2p_registry_core.models import G2PRegisterChangeRequest
from openg2p_registry_core.services import G2PRegisterDomainService
from sqlalchemy import select

from .audit_snapshot import AuditSnapshotMixin

from .domain_validation_utils import (
    normalize_event_ear_tag,
    validate_known_code,
    animal_identified_by,
    _animal_models, ear_tag_exists, is_blank, parse_date, validate_species_matches, validation_error,
    fill_species_and_age_from_animal,
    first_application_reference,
    ensure_ear_tags_belong_to_submission,
    intake_rows_as_records,
    tables_for,
    submission_ids_of,
    live_rows,
)

# Kept as its own import line rather than folded into the block above: the
# block is edited by other in-flight work on this file, and a separate
# statement keeps the two changes from landing on the same lines.
from .domain_validation_utils import event_already_recorded, first_repeated_key

_logger = logging.getLogger("g2p-register-domain-service")

# Health Event's own register_id, from g2p_register_definitions.sql. Used by
# post_approve/post_ingest, below, to auto-sync the linked animal's Health
# Status — mirrors VITAL_EVENT_REGISTER_ID in
# g2p_register_domain_service_vital_event.py.
HEALTH_EVENT_REGISTER_ID = "a40e4a02-1b82-5b31-89df-71624bd96545"

# event_type -> the linked animal's new Health Status. Mirrors the Old
# System's create()-time side effect (g2p_livestock_registry/models/
# livestock_event.py G2PLivestockHealthEvent._sync_health_status: disease/
# injury -> 'sick', recovery -> 'healthy'), which Gen2 had no equivalent of —
# recording one of these events left the animal's own Health Status field on
# Livestock Details completely untouched. TREATMENT is deliberately absent
# (not in gen1 either) — administering treatment doesn't by itself say
# whether the animal is now well; a separate DISEASE/INJURY or RECOVERY
# event still carries that call.
_HEALTH_STATUS_BY_EVENT_TYPE = {
    "DISEASE": "SICK",
    "INJURY": "SICK",
    "RECOVERY": "HEALTHY",
}

# field -> human label used in the "Please provide the ... " message, mirroring
# the fields marked "widget-required" on the Health Event Details form.
#
# disease_type is NOT here even though the form shows it as required-looking:
# it is only mandatory for a DISEASE event (see _validate_disease_type below,
# mirroring vital_event's _validate_offspring_count) — an INJURY/TREATMENT/
# RECOVERY event hides the field entirely and must not be blocked on it.
_REQUIRED_FIELDS = {
    "ear_tag_id": "livestock ear tag or secondary identifier",
    "species": "species",
    "event_type": "event type",
}


class G2PRegisterDomainServiceHealthEvent(AuditSnapshotMixin, G2PRegisterDomainService):

    async def validate_domain_attributes(self, records: list[dict]):
        from .field_rules import validate_record_fields
        await validate_record_fields(records, table=True)
        records = live_rows(records)
        # The platform hands us this section's rows with no submission context;
        # rows it has already saved carry their application_reference, which
        # scopes the ear-tag check to this submission's own animals.
        batch_reference = first_application_reference(records)
        for record in records:
            # Ear tag is typed (no animal picker inside dialogs on the official
            # staff-ui): check the tag itself first so a typo is reported as a
            # typo, then derive the read-only Species from that animal.
            await self._validate_ear_tag_exists(record, batch_reference)
            await fill_species_and_age_from_animal(record)
            self._validate_required_fields(record)
            self._validate_disease_type(record)
            await validate_known_code(record, "disease_type", "LIVESTOCK_DISEASE", "Disease")
            self._validate_event_dates_required(record)
            await validate_species_matches(record)
            self._validate_not_in_future(record, "date_onset")
            self._validate_date_order(record, "date_onset", "date_resolution")
            self._validate_not_in_future(record, "date_resolution")
        await self._validate_no_duplicate_events(records)

    def _validate_required_fields(self, record: dict) -> None:
        for field, label in _REQUIRED_FIELDS.items():
            if is_blank(record.get(field)):
                validation_error(f"Please provide the {label} before saving the record.")

    def _validate_event_dates_required(self, record: dict) -> None:
        """Date of Onset / Date of Resolution are mandatory exactly when the
        Health Event Details dialog shows them for the chosen event_type
        (see patch_ls_health_event_details_sync_ui_schema.sql):
            DISEASE    onset + resolution required
            INJURY     onset + resolution required
            TREATMENT  onset required (the form hides resolution)
            RECOVERY   resolution required (the form hides onset)
        Mirrors _validate_disease_type below -- the form's conditional
        "require" action alone can't be trusted for API / bulk-import
        clients that bypass it.
        """
        event_type = str(record.get("event_type") or "").upper()
        if event_type != "RECOVERY" and is_blank(record.get("date_onset")):
            validation_error("Please provide the date of onset before saving the record.")
        if event_type != "TREATMENT" and is_blank(record.get("date_resolution")):
            validation_error("Please provide the date of resolution before saving the record.")

    def _validate_disease_type(self, record: dict) -> None:
        # Only a DISEASE event carries a disease — the form hides disease_type
        # for INJURY/TREATMENT/RECOVERY, so it must not be required there.
        if str(record.get("event_type") or "").upper() != "DISEASE":
            return
        if is_blank(record.get("disease_type")):
            validation_error("Please provide the disease before saving the record.")

    async def _validate_ear_tag_exists(self, record: dict, application_reference: str | None = None) -> None:
        value = normalize_event_ear_tag(record)
        if value is None:
            return
        if not await ear_tag_exists(value, application_reference=application_reference):
            validation_error(
                f"'{str(value).strip()}' does not match any registered or drafted animal's "
                "ear tag or secondary identifier. Add it under Livestock Details first, "
                "or check for a typo."
            )

    def _validate_not_in_future(self, record: dict, field: str) -> None:
        value = parse_date(record.get(field))
        if value is not None and value > date.today():
            validation_error(f"{field} must not be in the future")

    def _validate_date_order(self, record: dict, earlier: str, later: str) -> None:
        start = parse_date(record.get(earlier))
        end = parse_date(record.get(later))
        if start and end and end < start:
            validation_error(f"{later} must not be before {earlier}")

    async def post_approve(self, change_request: G2PRegisterChangeRequest, session) -> None:
        """DISEASE/INJURY event -> the linked animal's Health Status becomes
        SICK; RECOVERY -> HEALTHY. Covers the CHANGE REQUEST path: a Health
        Event added/edited against a Livestock record that is already an
        approved register entry. See post_ingest, below, for the other way
        a Health Event reaches this table — a still-draft record's *first*
        approval. Mirrors G2PRegisterDomainServiceVitalEvent.post_approve.
        """
        if change_request.section_register_id != HEALTH_EVENT_REGISTER_ID:
            return

        # Resolved through the "openg2p_registry_extensions" alias, not a
        # relative "..models" import — same reasoning as
        # G2PRegisterDomainServiceVitalEvent.post_approve and
        # domain_validation_utils._animal_models.
        import importlib

        G2PRegisterHealthEvent = importlib.import_module(
            "openg2p_registry_extensions.register_domain.models"
        ).G2PRegisterHealthEvent

        health_event = (
            await session.execute(
                select(G2PRegisterHealthEvent).where(
                    G2PRegisterHealthEvent.internal_record_id == change_request.internal_record_id
                )
            )
        ).scalar_one_or_none()

        if not health_event:
            # change_request.internal_record_id is the PARENT Livestock
            # record's id here, not the Health Event row's own id — for a
            # "new table row" change request (adding a Health Event to a
            # Livestock record that's already an approved register entry),
            # the platform records no id for the specific child row that
            # was added (confirmed empirically: g2p_register_verifications
            # carries no row for these change requests either). Fall back
            # to the most recently created Health Event under that parent —
            # the row this approval is almost certainly about. Without this
            # fallback, a Health Event added via "Edit Details" on an
            # existing record silently never syncs Health Status at all.
            health_event = (
                await session.execute(
                    select(G2PRegisterHealthEvent)
                    .where(G2PRegisterHealthEvent.link_internal_record_id == change_request.internal_record_id)
                    .order_by(G2PRegisterHealthEvent.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

        if not health_event:
            return
        await self._sync_animal_health_status(health_event, session)

    async def post_ingest(self, register_id: str, register_row, session) -> None:
        """Same Health Status sync as post_approve, above, but for a Health
        Event that reaches the register by a still-draft submission's FIRST
        approval (intake_form_register_ingest_worker converting the whole
        submission's rows from intake-form drafts into real register rows).
        register_row here IS the just-inserted G2PRegisterHealthEvent — no
        lookup needed, unlike post_approve.
        """
        if register_id != HEALTH_EVENT_REGISTER_ID:
            return
        await self._sync_animal_health_status(register_row, session)

    async def _sync_animal_health_status(self, health_event, session) -> None:
        event_type = str(health_event.event_type or "").upper()
        new_status = _HEALTH_STATUS_BY_EVENT_TYPE.get(event_type)
        if new_status is None:
            return

        G2PRegisterAnimal, _ = _animal_models()
        animal = (
            await session.execute(
                select(G2PRegisterAnimal).where(
                    animal_identified_by(G2PRegisterAnimal, health_event.ear_tag_id),
                    G2PRegisterAnimal.link_internal_record_id == health_event.link_internal_record_id,
                )
            )
        ).scalar()
        if not animal:
            return
        animal.health_status = new_status
        await session.flush()
        _logger.info(
            "%s health event %s: set animal %s health_status to %s",
            event_type, health_event.internal_record_id, animal.ear_tag_id or animal.secondary_identifier, new_status,
        )

    def construct_search_text(self, payload: dict, extra: list[str] = None) -> str:
        _logger.info("Constructing search text for health event record")

        keys = [
            "functional_record_id",
            "ear_tag_id",
            "species",
            "event_type",
            "disease_type",
            "treatment",
            "veterinarian_name",
            "location",
            "location_details",
        ]
        search_text = []
        if extra:
            search_text.extend(str(item).strip() for item in extra if str(item).strip())
        search_text.extend(
            str(payload.get(key) or "").strip()
            for key in keys
            if str(payload.get(key) or "").strip()
        )

        return " ".join(search_text).strip()

    def construct_record_name(self, payload: dict, extra: list[str] = None) -> str:
        _logger.info("Constructing record name for health event record")

        keys = ["event_type", "ear_tag_id"]
        record_name = []
        if extra:
            record_name.extend(str(item).strip() for item in extra if str(item).strip())
        record_name.extend(
            str(payload.get(key) or "").strip()
            for key in keys
            if str(payload.get(key) or "").strip()
        )

        return " ".join(record_name).strip()

    async def _validate_no_duplicate_events(
        self,
        records: list[dict],
        *,
        search: tuple[str, ...] | None = None,
        exclude_submission_ids: set[str] | None = None,
    ) -> None:
        """The same health event must not be recorded twice for one animal:
        same ear tag, same event type, same disease and same onset date —
        the Old System's _check_duplicate_health_event. Two layers, like
        _validate_no_duplicate_ear_tags on the Animal section: the rows of
        this save first, then the register plus every intake draft
        (excluding this save's own rows, so editing an already-approved
        event isn't flagged against itself). A RECOVERY is keyed on its
        resolution date instead of the onset it does not have; a row with
        neither date is left alone — there is nothing to say it is the same
        event.

        `search` / `exclude_submission_ids`: which tables to look in and which
        submission to ignore — see exists_in_tables in domain_validation_utils.
        Left unset (validate_domain_attributes) each row picks its own tables
        through tables_for(); post_intake_upsert passes the intake half with
        the submission itself excluded.
        """

        def key_of(record: dict):
            if is_blank(record.get("ear_tag_id")) or is_blank(record.get("event_type")):
                return None
            event_type = str(record["event_type"]).strip().upper()
            # A RECOVERY has no onset date (the form hides it) -- its date is
            # the resolution date, so that is what identifies it. The Old
            # System keyed a recovery on its empty onset, which made a second
            # recovery for the same animal a duplicate however far apart the
            # two were; here an animal that falls ill and recovers twice in a
            # year keeps both, and only the same day twice is refused.
            date_column = "date_resolution" if event_type == "RECOVERY" else "date_onset"
            on = parse_date(record.get(date_column))
            if on is None:
                return None
            disease = record.get("disease_type")
            return (
                str(record["ear_tag_id"]).strip(),
                event_type,
                None if is_blank(disease) else str(disease).strip(),
                date_column,
                on,
            )

        repeated = first_repeated_key(records, key_of)
        if repeated:
            ear_tag_id, event_type, _disease, _date_column, onset = repeated
            validation_error(
                f"The {event_type} health event for animal '{ear_tag_id}' on {onset} "
                "is entered more than once in this record."
            )

        self_ids = {
            str(record["internal_record_id"])
            for record in records
            if record.get("internal_record_id")
        }
        for record in records:
            key = key_of(record)
            if key is None:
                continue
            ear_tag_id, event_type, disease, date_column, onset = key
            if await event_already_recorded(
                "HealthEvent",
                {
                    "ear_tag_id": ear_tag_id,
                    "event_type": event_type,
                    "disease_type": disease,
                    date_column: onset,
                },
                exclude_internal_record_ids=self_ids,
                exclude_submission_ids=exclude_submission_ids,
                search=search or tables_for(record),
            ):
                validation_error(
                    f"A {event_type} health event for animal '{ear_tag_id}' on {onset} "
                    "is already recorded."
                )

    async def post_intake_upsert(self, rows: list, session) -> None:
        """Scoped ear-tag check (see ensure_ear_tags_belong_to_submission)
        and the intake-side duplicate check: only here do the rows carry the
        submission they belong to, which the search must leave out."""
        await ensure_ear_tags_belong_to_submission(rows)
        await self._validate_no_duplicate_events(
            intake_rows_as_records(rows), search=("intake",), exclude_submission_ids=submission_ids_of(rows)
        )
