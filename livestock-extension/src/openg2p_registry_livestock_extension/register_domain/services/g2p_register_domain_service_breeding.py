import logging
from datetime import date, timedelta

from openg2p_registry_core.services import G2PRegisterDomainService
from sqlalchemy import and_

from .audit_snapshot import AuditSnapshotMixin

from .domain_validation_utils import (
    normalize_event_ear_tag,
    ear_tag_exists, get_animal_gender, is_blank, parse_date, validate_species_matches, validation_error,
    fill_species_and_age_from_animal,
    first_application_reference,
    ensure_ear_tags_belong_to_submission,
    exclude_own_rows,
    exists_in_tables,
    intake_rows_as_records,
    tables_for,
    submission_ids_of,
    live_rows,
)

_logger = logging.getLogger("g2p-register-domain-service")

# field -> human label used in the "Please provide the ... " message, mirroring
# the fields marked "widget-required" on the Breeding Details form.
_REQUIRED_FIELDS = {
    "ear_tag_id": "livestock ear tag or secondary identifier",
    "species": "species",
    "event_type": "event type",
}

# Same cycle window the Old System used (g2p_livestock_registry/models/
# livestock_event.py _check_duplicate_breeding_event): a Natural Breeding or
# AI event within 21 days either side of an existing one, for the same
# animal and same event_type, is treated as logging the same breeding cycle
# twice rather than a genuinely new attempt.
_CYCLE_WINDOW_DAYS = 21


class G2PRegisterDomainServiceBreeding(AuditSnapshotMixin, G2PRegisterDomainService):

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
            await self._validate_female_only(record)
            await validate_species_matches(record)
            self._validate_not_in_future(record, "breeding_date")
            self._validate_not_in_future(record, "pregnancy_confirmation_date")
            self._validate_date_order(record, "breeding_date", "expected_calving_date")
        # Run only once every record above has passed — mirrors
        # G2PRegisterDomainServiceVitalEvent's own duplicate checks, which
        # run the same way after its per-record loop.
        await self._validate_no_duplicate_breeding(records)

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

    async def _validate_female_only(self, record: dict) -> None:
        """Breeding is logged against the dam -- only a Female animal can be
        pregnant. The Ear Tag dropdown only lists Female animals of this
        submission (livestock-dialog-overlay.js), but the tag itself is still
        typed text the server must check on its own, same reasoning as every
        other ear-tag rule here (see _validate_ear_tag_exists)."""
        ear_tag_id = record.get("ear_tag_id")
        if is_blank(ear_tag_id):
            return
        gender = await get_animal_gender(str(ear_tag_id).strip())
        if gender and str(gender).upper() != "FEMALE":
            validation_error(
                "Breeding can only be logged against a Female animal "
                f"('{str(ear_tag_id).strip()}' is on file as {str(gender).title()})."
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

    def _breeding_models(self):
        """The Breeding register + intake-form models, imported the same way
        G2PRegisterDomainServiceVitalEvent._vital_event_models does — through
        the "openg2p_registry_extensions" alias, not a relative "..models"
        import, else SQLAlchemy refuses the second declarative Table
        registration. See that method's docstring for the full reasoning.
        """
        import importlib

        models = importlib.import_module(
            "openg2p_registry_extensions.register_domain.models"
        )
        return models.G2PRegisterBreeding, models.G2PIntakeFormBreeding

    async def _validate_no_duplicate_breeding(
        self,
        records: list[dict],
        *,
        search: tuple[str, ...] | None = None,
        exclude_submission_ids: set[str] | None = None,
    ) -> None:
        """Block logging the same breeding cycle twice — mirrors the Old
        System's "Another {AI/Natural Breeding} event already exists for
        this animal within the same breeding cycle" check
        (g2p_livestock_registry/models/livestock_event.py
        _check_duplicate_breeding_event), which Gen2 had no equivalent of.
        A conflict is a same ear_tag_id + same event_type (AI vs Natural
        Breeding are tracked as separate cycles) event whose breeding_date
        falls within _CYCLE_WINDOW_DAYS of this one. Two layers, same as
        the Mortality/Disease checks above: within this same save's own
        rows first (no DB round trip), then against everything else already
        saved anywhere — approved register or still-pending intake draft —
        excluding this save's own rows so re-saving an existing Breeding
        event's other fields isn't flagged against itself.

        `search` / `exclude_submission_ids`: which tables to look in and which
        submission to ignore — see exists_in_tables in domain_validation_utils.
        Left unset (validate_domain_attributes) each row picks its own tables
        through tables_for(); post_intake_upsert passes the intake half with
        the submission itself excluded.
        """
        self_ids = {
            str(record["internal_record_id"])
            for record in records
            if record.get("internal_record_id")
        }

        seen: list[tuple[str, str, date]] = []
        for record in records:
            ear_tag_id = record.get("ear_tag_id")
            event_type = record.get("event_type")
            breeding_date = parse_date(record.get("breeding_date"))
            if is_blank(ear_tag_id) or is_blank(event_type) or breeding_date is None:
                continue
            ear_tag_id = str(ear_tag_id).strip()
            event_type = str(event_type).strip().upper()

            for seen_ear_tag_id, seen_event_type, seen_date in seen:
                if (
                    seen_ear_tag_id == ear_tag_id
                    and seen_event_type == event_type
                    and abs((breeding_date - seen_date).days) <= _CYCLE_WINDOW_DAYS
                ):
                    self._raise_duplicate_breeding_error(event_type)
            seen.append((ear_tag_id, event_type, breeding_date))

            if await self._breeding_conflict_exists(
                ear_tag_id, event_type, breeding_date, self_ids, exclude_submission_ids,
                search or tables_for(record),
            ):
                self._raise_duplicate_breeding_error(event_type)

    def _raise_duplicate_breeding_error(self, event_type: str) -> None:
        label = "AI" if event_type == "AI" else "Natural Breeding"
        validation_error(
            f"Another {label} event already exists for this animal within the same "
            "breeding cycle."
        )

    async def _breeding_conflict_exists(
        self,
        ear_tag_id: str,
        event_type: str,
        breeding_date: date,
        exclude_internal_record_ids: set[str],
        exclude_submission_ids: set[str] | None = None,
        search: tuple[str, ...] = ("register", "intake"),
    ) -> bool:
        G2PRegisterBreeding, G2PIntakeFormBreeding = self._breeding_models()

        window_start = breeding_date - timedelta(days=_CYCLE_WINDOW_DAYS)
        window_end = breeding_date + timedelta(days=_CYCLE_WINDOW_DAYS)

        def _condition(model):
            conditions = [
                model.ear_tag_id == ear_tag_id,
                model.event_type == event_type,
                model.breeding_date >= window_start,
                model.breeding_date <= window_end,
            ]
            exclude_own_rows(model, conditions, exclude_internal_record_ids, exclude_submission_ids)
            return and_(*conditions)

        return await exists_in_tables(G2PRegisterBreeding, G2PIntakeFormBreeding, _condition, search)

    def construct_search_text(self, payload: dict, extra: list[str] = None) -> str:
        _logger.info("Constructing search text for breeding record")

        keys = [
            "functional_record_id",
            "ear_tag_id",
            "species",
            "event_type",
            "sire_or_semen_id",
            "ai_technician_name",
            "ai_technique",
            "semen_batch_number",
            "outcome",
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
        _logger.info("Constructing record name for breeding record")

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

    async def post_intake_upsert(self, rows: list, session) -> None:
        """Scoped ear-tag check (see ensure_ear_tags_belong_to_submission)
        and the intake-side duplicate check: only here do the rows carry the
        submission they belong to, which the search must leave out."""
        await ensure_ear_tags_belong_to_submission(rows)
        await self._validate_no_duplicate_breeding(
            intake_rows_as_records(rows), search=("intake",), exclude_submission_ids=submission_ids_of(rows)
        )
