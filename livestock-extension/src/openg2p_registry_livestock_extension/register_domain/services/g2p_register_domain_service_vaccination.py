import logging
from datetime import date, timedelta

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
    intake_rows_as_records,
    submission_ids_of,
    tables_for,
    resolve_today_default,
    validate_belongs_to_species,
    ensure_ear_tags_belong_to_submission,
    live_rows,
)

# Kept as its own import line rather than folded into the block above: the
# block is edited by other in-flight work on this file, and a separate
# statement keeps the two changes from landing on the same lines.
from .domain_validation_utils import (
    event_already_recorded,
    first_repeated_key,
    humanize_attribute_value,
)
from .domain_validation_utils import get_animal_species_and_birth_date

_logger = logging.getLogger("g2p-register-domain-service")

# Vaccination's own register_id, from g2p_register_definitions.sql. Used by
# post_approve/post_ingest, below, to auto-sync the linked animal's
# Vaccination Status — mirrors HEALTH_EVENT_REGISTER_ID in
# g2p_register_domain_service_health_event.py.
VACCINATION_REGISTER_ID = "51c1f6d6-856a-5e2f-84e9-ff5abdc4fb75"

# field -> human label used in the "Please provide the ... " message, mirroring
# the fields marked "widget-required" on the Vaccination Details form.
_REQUIRED_FIELDS = {
    "ear_tag_id": "livestock ear tag or secondary identifier",
    "species": "species",
    "vaccine_type": "vaccine",
    "vaccination_date": "vaccination date",
}


class G2PRegisterDomainServiceVaccination(AuditSnapshotMixin, G2PRegisterDomainService):

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
            resolve_today_default(record, "vaccination_date")
            self._validate_required_fields(record)
            # Vaccine is a plain (unfiltered) list in the dialog; keep a goat vaccine
            # off a cattle animal here, server-side.
            await validate_known_code(record, "vaccine_type", "VACCINE_TYPE", "Vaccine")
            await validate_belongs_to_species(record.get("vaccine_type"), record.get("species"), "Vaccine")
            await validate_species_matches(record)
            self._validate_not_in_future(record, "vaccination_date")
            await self._validate_not_before_birth(record)
            await self._populate_next_due_date(record)
            self._validate_date_order(record, "vaccination_date", "next_due_date")
        await self._validate_no_duplicate_events(records)

    def _validate_required_fields(self, record: dict) -> None:
        for field, label in _REQUIRED_FIELDS.items():
            if is_blank(record.get(field)):
                validation_error(f"Please provide the {label} before saving the record.")

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

    async def _validate_not_before_birth(self, record: dict) -> None:
        """An animal cannot be vaccinated before it was born. Animals with no
        Date of Birth on file (e.g. flock species) are not checked."""
        vaccination_date = parse_date(record.get("vaccination_date"))
        ear_tag_id = record.get("ear_tag_id")
        if vaccination_date is None or is_blank(ear_tag_id):
            return
        _, birth_date = await get_animal_species_and_birth_date(str(ear_tag_id).strip())
        if birth_date is not None and vaccination_date < birth_date:
            validation_error(
                "Vaccination date cannot be before the animal's date of birth "
                f"({birth_date.strftime('%d/%m/%Y')})."
            )

    async def _populate_next_due_date(self, record: dict) -> None:
        """Derive next_due_date from vaccination_date + the matching Vaccine
        Schedule's interval_days — the same computation the Old System's
        Vaccine Schedule interval drove, and the one the now off-limits
        registry-platform widget (`widget-date-offset`) used to do purely as
        a live in-dialog preview. Done here instead so it's authoritative on
        save regardless of what the (unmodified) platform's widgets can or
        can't compute — same convention as
        G2PRegisterDomainServiceAnimal._populate_age_from_date_of_birth:
        overwrites whatever was submitted, since it's a derived value, not
        independent input.

        Looks up the APPROVED Vaccine Schedule register, not intake drafts —
        that's the operational, administrator-tuned schedule the overdue
        sweep (vaccination_status_service.py) and reminder task already
        trust. No schedule configured yet for this vaccine/species pair (or
        one not yet approved) leaves next_due_date untouched rather than
        blocking the save — a vaccination is still valid without one.
        """
        vaccination_date = parse_date(record.get("vaccination_date"))
        vaccine_type = record.get("vaccine_type")
        species = record.get("species")
        if vaccination_date is None or is_blank(vaccine_type) or is_blank(species):
            return

        import importlib

        from openg2p_fastapi_common.context import dbengine
        from sqlalchemy.ext.asyncio import async_sessionmaker

        G2PRegisterVaccineSchedule = importlib.import_module(
            "openg2p_registry_extensions.register_domain.models"
        ).G2PRegisterVaccineSchedule

        # The schedule seed is keyed by bare codes (vaccine_name "ANTHRAX_CATTLE",
        # species "CATTLE") while the form saves attribute value ids
        # ("VACCINE_TYPE_ANTHRAX_CATTLE", "LIVESTOCK_SPECIES_CATTLE"). Compare
        # both sides with the catalogue prefixes stripped so either spelling
        # matches — without this the lookup never matched a real entry and
        # next_due_date stayed NULL (no overdue flip, no due-soon/overdue mail).
        def norm(value) -> str:
            text = str(value or "").strip().upper()
            for prefix in ("VACCINE_TYPE_", "LIVESTOCK_SPECIES_", "LIVESTOCK_VACCINE_"):
                if text.startswith(prefix):
                    text = text[len(prefix):]
            return text

        session_maker = async_sessionmaker(dbengine.get(), expire_on_commit=False)
        async with session_maker() as session:
            schedules = (
                await session.execute(
                    select(
                        G2PRegisterVaccineSchedule.vaccine_name,
                        G2PRegisterVaccineSchedule.species,
                        G2PRegisterVaccineSchedule.interval_days,
                    ).where(G2PRegisterVaccineSchedule.is_active.is_not(False))
                    # Newest approved row first: when the same vaccine and
                    # species exist twice (a default and a form-saved row),
                    # the one an administrator saved last wins.
                    .order_by(G2PRegisterVaccineSchedule.last_approved_at.desc().nulls_last())
                )
            ).all()

        interval_days = None
        for name, schedule_species, days in schedules:
            if days is not None and norm(name) == norm(vaccine_type) and norm(schedule_species) == norm(species):
                interval_days = int(days)
                break
        if interval_days is None:
            return

        record["next_due_date"] = (vaccination_date + timedelta(days=interval_days)).isoformat()

    def _validate_date_order(self, record: dict, earlier: str, later: str) -> None:
        start = parse_date(record.get(earlier))
        end = parse_date(record.get(later))
        if start and end and end < start:
            validation_error(f"{later} must not be before {earlier}")

    async def post_approve(self, change_request: G2PRegisterChangeRequest, session) -> None:
        """A logged Vaccination -> the linked animal's Vaccination Status
        becomes UP_TO_DATE. Mirrors the Old System's create()-time side
        effect (g2p_livestock_registry/models/livestock_event.py:
        `rec.line_id.vaccination_status = 'up_to_date'`), covering the
        CHANGE REQUEST path: a Vaccination added/edited against a Livestock
        record that is already an approved register entry.
        """
        if change_request.section_register_id != VACCINATION_REGISTER_ID:
            return

        import importlib

        G2PRegisterVaccination = importlib.import_module(
            "openg2p_registry_extensions.register_domain.models"
        ).G2PRegisterVaccination

        vaccination = (
            await session.execute(
                select(G2PRegisterVaccination).where(
                    G2PRegisterVaccination.internal_record_id == change_request.internal_record_id
                )
            )
        ).scalar_one_or_none()

        if not vaccination:
            # change_request.internal_record_id is the PARENT Livestock
            # record's id here, not the Vaccination row's own id, for a
            # "new table row" change request — see the identical case
            # (confirmed empirically) documented on
            # G2PRegisterDomainServiceHealthEvent.post_approve. Fall back to
            # the most recently created Vaccination under that parent.
            vaccination = (
                await session.execute(
                    select(G2PRegisterVaccination)
                    .where(G2PRegisterVaccination.link_internal_record_id == change_request.internal_record_id)
                    .order_by(G2PRegisterVaccination.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

        if not vaccination:
            return
        await self._sync_animal_vaccination_status(vaccination, session)

    async def post_ingest(self, register_id: str, register_row, session) -> None:
        """Same Vaccination Status sync as post_approve, above, but for a
        Vaccination that reaches the register by a still-draft submission's
        FIRST approval. register_row here IS the just-inserted
        G2PRegisterVaccination — no lookup needed, unlike post_approve.
        """
        if register_id != VACCINATION_REGISTER_ID:
            return
        await self._sync_animal_vaccination_status(register_row, session)

    async def _sync_animal_vaccination_status(self, vaccination, session) -> None:
        G2PRegisterAnimal, _ = _animal_models()
        animal = (
            await session.execute(
                select(G2PRegisterAnimal).where(
                    animal_identified_by(G2PRegisterAnimal, vaccination.ear_tag_id),
                    G2PRegisterAnimal.link_internal_record_id == vaccination.link_internal_record_id,
                )
            )
        ).scalar()
        if not animal:
            return
        animal.vaccination_status = "UP_TO_DATE"
        await session.flush()
        _logger.info(
            "Vaccination %s: set animal %s vaccination_status to UP_TO_DATE",
            vaccination.internal_record_id, animal.ear_tag_id or animal.secondary_identifier,
        )

    def construct_search_text(self, payload: dict, extra: list[str] = None) -> str:
        _logger.info("Constructing search text for vaccination record")

        keys = [
            "functional_record_id",
            "ear_tag_id",
            "species",
            "vaccine_type",
            "batch_number",
            "administered_by",
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
        _logger.info("Constructing record name for vaccination record")

        keys = ["vaccine_type", "ear_tag_id"]
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
        """The same vaccine must not be recorded twice for one animal on the
        same day: same ear tag, same vaccine, same vaccination date. The Old
        System had no such check for vaccinations (only for health, vital
        and breeding events) — added here because a second identical row
        can only be a data-entry slip, and it would wrongly push the
        animal's next due date. Same two layers as the other event sections:
        this save's own rows, then register + intake drafts excluding this
        save's own rows.

        `search` / `exclude_submission_ids`: which tables to look in and which
        submission to ignore — see exists_in_tables in domain_validation_utils.
        Left unset (validate_domain_attributes) each row picks its own tables
        through tables_for(); post_intake_upsert passes the intake half with
        the submission itself excluded.
        """

        def key_of(record: dict):
            on = parse_date(record.get("vaccination_date"))
            if is_blank(record.get("ear_tag_id")) or is_blank(record.get("vaccine_type")) or on is None:
                return None
            return (
                str(record["ear_tag_id"]).strip(),
                str(record["vaccine_type"]).strip(),
                on,
            )

        repeated = first_repeated_key(records, key_of)
        if repeated:
            ear_tag_id, vaccine_type, on = repeated
            vaccine_label = await humanize_attribute_value(vaccine_type)
            validation_error(
                f"Vaccine '{vaccine_label}' for animal '{ear_tag_id}' on {on} "
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
            ear_tag_id, vaccine_type, on = key
            if await event_already_recorded(
                "Vaccination",
                {"ear_tag_id": ear_tag_id, "vaccine_type": vaccine_type, "vaccination_date": on},
                exclude_internal_record_ids=self_ids,
                exclude_submission_ids=exclude_submission_ids,
                search=search or tables_for(record),
            ):
                vaccine_label = await humanize_attribute_value(vaccine_type)
                validation_error(
                    f"Vaccine '{vaccine_label}' for animal '{ear_tag_id}' on {on} "
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
