import logging
import re
import uuid
from datetime import date, datetime, timezone

from openg2p_registry_core.models import (
    G2PFunctionalIdGenerationQueue,
    G2PRegisterChangeRequest,
)
from openg2p_registry_core.services import G2PRegisterDomainService
from sqlalchemy import and_, func, select

from .audit_snapshot import AuditSnapshotMixin

from .domain_validation_utils import (
    normalize_event_ear_tag,
    validate_known_code,
    animal_identified_by,
    _animal_models,
    as_int,
    ear_tag_exists,
    get_animal_gender,
    is_blank,
    parse_date,
    resolve_today_default,
    validate_species_matches,
    validation_error,
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

# Vital Event's own register_id and its sibling Animal's, from
# g2p_register_definitions.sql (both share master_register_id = the Livestock
# register). Used by post_approve, below, to auto-create offspring on a Birth
# event — mirrors LAND_REGISTER_ID/HOUSEHOLD_REGISTER_ID in the Farmer
# extension's own domain service.
VITAL_EVENT_REGISTER_ID = "76811bf6-07df-5ed4-9466-13b782f627fe"
ANIMAL_REGISTER_ID = "041a9f79-2142-548a-a15b-a4c76fc9f6f7"

# ET followed by exactly 10 digits — same format G2PRegisterDomainServiceAnimal
# enforces on a manually-entered ear tag (_EAR_TAG_PATTERN there).
_EAR_TAG_PATTERN = re.compile(r"^ET(\d{10})$")

# field -> human label used in the "Please provide the ... " message, mirroring
# the fields marked "widget-required" on the Vital Event Details form.
_REQUIRED_FIELDS = {
    "ear_tag_id": "livestock ear tag or secondary identifier",
    "species": "species",
    "event_type": "event type",
    "event_date": "event date",
}


class G2PRegisterDomainServiceVitalEvent(AuditSnapshotMixin, G2PRegisterDomainService):

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
            # Event Date defaults to the literal "today" on the intake form; resolve
            # it before any check (see resolve_today_default). Only shown once an
            # Event Type is picked, same as the required check right after it.
            resolve_today_default(record, "event_date")
            await self._validate_ear_tag_exists(record, batch_reference)
            await fill_species_and_age_from_animal(record)
            self._validate_required_fields(record)
            self._validate_disease_type(record)
            await validate_known_code(record, "disease_type", "LIVESTOCK_DISEASE", "Disease")
            await validate_species_matches(record)
            self._validate_not_in_future(record, "event_date")
            self._validate_not_in_future(record, "date_onset")
            self._validate_date_order(record, "date_onset", "date_resolution")
            self._validate_not_in_future(record, "date_resolution")
            self._validate_offspring_count(record)
            await self._validate_birth_female_only(record)
        # Run only once every record above has passed — so by this point
        # every ear_tag_id/event_type/disease_type used below is known
        # non-blank where required. Mirrors G2PRegisterDomainServiceAnimal's
        # own duplicate checks, which run the same way after its per-record
        # loop (see its validate_domain_attributes).
        await self._validate_no_duplicate_mortality(records)
        await self._validate_no_duplicate_disease(records)

    def _validate_required_fields(self, record: dict) -> None:
        for field, label in _REQUIRED_FIELDS.items():
            if is_blank(record.get(field)):
                validation_error(f"Please provide the {label} before saving the record.")

    async def _validate_birth_female_only(self, record: dict) -> None:
        """A BIRTH is logged against the dam -- only a Female animal can give
        birth. Same check as G2PRegisterDomainServiceBreeding._validate_female_only:
        the Ear Tag dropdown only offers Female animals once Event Type is
        BIRTH (livestock-dialog-overlay.js), but the tag is still text the
        server must check itself. MORTALITY / DISEASE apply to any animal."""
        if str(record.get("event_type") or "").upper() != "BIRTH":
            return
        ear_tag_id = record.get("ear_tag_id")
        if is_blank(ear_tag_id):
            return
        gender = await get_animal_gender(str(ear_tag_id).strip())
        if gender and str(gender).upper() != "FEMALE":
            validation_error(
                "A Birth event can only be logged against a Female animal "
                f"('{str(ear_tag_id).strip()}' is on file as {str(gender).title()})."
            )

    def _validate_disease_type(self, record: dict) -> None:
        # Only a DISEASE vital event carries a diagnosis — the form hides
        # disease_type for BIRTH/MORTALITY, so it must not be required there.
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

    def _validate_offspring_count(self, record: dict) -> None:
        is_birth = str(record.get("event_type") or "").upper() == "BIRTH"

        # Only births carry offspring; a non-birth event with a count is a
        # data-entry slip worth rejecting rather than silently storing.
        count = as_int(record.get("offspring_count"))
        if count is not None:
            if count < 0:
                validation_error("offspring_count must not be negative")
            if count > 0 and not is_birth:
                validation_error("offspring_count is only valid on a BIRTH event")

        if not is_birth:
            return

        # A Birth event without a count/sex can't generate the offspring
        # Animal profile(s) post_approve is about to create below — required
        # here, not left to silently produce zero animals.
        if count is None or count < 1:
            validation_error("Please provide the number of offspring before saving the record.")
        if is_blank(record.get("offspring_gender")):
            validation_error("Please provide the sex of the offspring before saving the record.")

    def _vital_event_models(self):
        """The Vital Event register + intake-form models, imported the same
        way post_approve/_next_ear_tag_number already do — see the comment
        on post_approve for why this must go through the
        "openg2p_registry_extensions" alias, not a relative "..models"
        import. Factored out here since the two duplicate-check helpers
        below need it as well.
        """
        import importlib

        models = importlib.import_module(
            "openg2p_registry_extensions.register_domain.models"
        )
        return models.G2PRegisterVitalEvent, models.G2PIntakeFormVitalEvent

    async def _validate_no_duplicate_mortality(
        self,
        records: list[dict],
        *,
        search: tuple[str, ...] | None = None,
        exclude_submission_ids: set[str] | None = None,
    ) -> None:
        """An animal can only die once — mirrors the Old System's "A
        Mortality event already exists for this animal" check
        (g2p_livestock_registry/models/livestock_event.py
        _check_duplicate_vital_event), which Gen2 had no equivalent of.
        Two layers, same as G2PRegisterDomainServiceAnimal's own duplicate
        checks: within this same save's own rows first (no DB round trip),
        then against everything else already saved anywhere — approved
        register or still-pending intake draft — excluding this save's own
        rows so re-saving an existing Mortality event's other fields isn't
        flagged against itself.

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

        seen_ear_tags: set[str] = set()
        for record in records:
            if str(record.get("event_type") or "").upper() != "MORTALITY":
                continue
            ear_tag_id = record.get("ear_tag_id")
            if is_blank(ear_tag_id):
                continue
            ear_tag_id = str(ear_tag_id).strip()

            if ear_tag_id in seen_ear_tags:
                validation_error("A Mortality event already exists for this animal.")
            seen_ear_tags.add(ear_tag_id)

            if await self._mortality_exists(
                ear_tag_id, self_ids, exclude_submission_ids, search or tables_for(record)
            ):
                validation_error("A Mortality event already exists for this animal.")

    async def _mortality_exists(
        self,
        ear_tag_id: str,
        exclude_internal_record_ids: set[str],
        exclude_submission_ids: set[str] | None = None,
        search: tuple[str, ...] = ("register", "intake"),
    ) -> bool:
        G2PRegisterVitalEvent, G2PIntakeFormVitalEvent = self._vital_event_models()

        def _condition(model):
            conditions = [model.ear_tag_id == ear_tag_id, model.event_type == "MORTALITY"]
            exclude_own_rows(model, conditions, exclude_internal_record_ids, exclude_submission_ids)
            return and_(*conditions)

        return await exists_in_tables(G2PRegisterVitalEvent, G2PIntakeFormVitalEvent, _condition, search)

    async def _validate_no_duplicate_disease(
        self,
        records: list[dict],
        *,
        search: tuple[str, ...] | None = None,
        exclude_submission_ids: set[str] | None = None,
    ) -> None:
        """Block re-logging the same disease case twice for the same
        animal — mirrors the Old System's duplicate check on (line_id,
        disease_type, effective date of onset), which Gen2 had no
        equivalent of. Effective onset = date_onset if given, else
        event_date — same fallback the Old System used, and safe here since
        _validate_required_fields already guarantees event_date is filled
        in for any record that reaches this point.

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

        seen: set[tuple] = set()
        for record in records:
            if str(record.get("event_type") or "").upper() != "DISEASE":
                continue
            ear_tag_id = record.get("ear_tag_id")
            disease_type = record.get("disease_type")
            if is_blank(ear_tag_id) or is_blank(disease_type):
                continue
            ear_tag_id = str(ear_tag_id).strip()
            disease_type = str(disease_type).strip().lower()
            onset = parse_date(record.get("date_onset")) or parse_date(record.get("event_date"))
            if onset is None:
                continue

            key = (ear_tag_id, disease_type, onset)
            if key in seen:
                validation_error(
                    "A Disease event with the same disease and date of onset already "
                    "exists for this animal."
                )
            seen.add(key)

            if await self._disease_case_exists(
                ear_tag_id, disease_type, onset, self_ids, exclude_submission_ids,
                search or tables_for(record),
            ):
                validation_error(
                    "A Disease event with the same disease and date of onset already "
                    "exists for this animal."
                )

    async def _disease_case_exists(
        self, ear_tag_id: str, disease_type: str, onset, exclude_internal_record_ids: set[str],
        exclude_submission_ids: set[str] | None = None,
        search: tuple[str, ...] = ("register", "intake"),
    ) -> bool:
        G2PRegisterVitalEvent, G2PIntakeFormVitalEvent = self._vital_event_models()

        def _condition(model):
            effective_onset = func.coalesce(model.date_onset, model.event_date)
            conditions = [
                model.ear_tag_id == ear_tag_id,
                model.event_type == "DISEASE",
                func.lower(model.disease_type) == disease_type,
                effective_onset == onset,
            ]
            exclude_own_rows(model, conditions, exclude_internal_record_ids, exclude_submission_ids)
            return and_(*conditions)

        return await exists_in_tables(G2PRegisterVitalEvent, G2PIntakeFormVitalEvent, _condition, search)

    async def post_approve(self, change_request: G2PRegisterChangeRequest, session) -> None:
        """BIRTH event -> auto-create the newborn Animal profile(s) under
        Livestock Details, with a freshly generated ear tag each — mirrors
        g2p.livestock.vital.event._create_offspring_profiles in the Odoo
        module (g2p_livestock_registry/models/livestock_event.py).

        Covers the CHANGE REQUEST path: a Birth event added/edited against a
        Livestock record that is already an approved register entry. See
        post_ingest, below, for the other way a Birth event reaches this
        table — a still-draft record's *first* approval.
        """
        if change_request.section_register_id != VITAL_EVENT_REGISTER_ID:
            return

        # Resolved through the "openg2p_registry_extensions" alias, not a
        # relative "..models" import — this method is reached via
        # G2PRegisterDomainFactory.get_domain_service, which imports THIS
        # service module by its real dotted name
        # ("openg2p_registry_livestock_extension...", see factory/
        # g2p_register_domain_factory.py); a relative import from here would
        # re-import models.py under that real name too, and SQLAlchemy
        # refuses the second declarative Table registration. Same reasoning
        # as domain_validation_utils._animal_models, just for VitalEvent.
        import importlib

        G2PRegisterVitalEvent = importlib.import_module(
            "openg2p_registry_extensions.register_domain.models"
        ).G2PRegisterVitalEvent

        vital_event = (
            await session.execute(
                select(G2PRegisterVitalEvent).where(
                    G2PRegisterVitalEvent.internal_record_id == change_request.internal_record_id
                )
            )
        ).scalar_one_or_none()

        if not vital_event:
            # change_request.internal_record_id is the PARENT Livestock
            # record's id here, not the Vital Event row's own id — for a
            # "new table row" change request (adding a Vital Event to a
            # Livestock record that's already an approved register entry),
            # the platform records no id for the specific child row that
            # was added (confirmed empirically against the identical
            # Health Event case — see
            # G2PRegisterDomainServiceHealthEvent.post_approve). Fall back
            # to the most recently created Vital Event under that parent —
            # the row this approval is almost certainly about. Without this
            # fallback, a Mortality/Disease/Birth event added via "Edit
            # Details" on an existing record silently never triggers offspring
            # creation or the Health Status sync below at all.
            vital_event = (
                await session.execute(
                    select(G2PRegisterVitalEvent)
                    .where(G2PRegisterVitalEvent.link_internal_record_id == change_request.internal_record_id)
                    .order_by(G2PRegisterVitalEvent.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

        if not vital_event:
            return
        await self._maybe_generate_offspring(vital_event, session)
        await self._sync_animal_health_status(vital_event, session)

    async def post_ingest(self, register_id: str, register_row, session) -> None:
        """Same offspring auto-creation as post_approve, above, but for a
        Birth event that reaches the register by a still-draft submission's
        FIRST approval (intake_form_register_ingest_worker converting the
        whole submission's rows from intake-form drafts into real register
        rows — see G2PRegisterDomainServiceFarmer.post_ingest for the same
        two-hooks-one-effect pattern). register_row here IS the just-inserted
        G2PRegisterVitalEvent — no lookup needed, unlike post_approve.
        """
        if register_id != VITAL_EVENT_REGISTER_ID:
            return
        await self._maybe_generate_offspring(register_row, session)
        await self._sync_animal_health_status(register_row, session)

    async def _sync_animal_health_status(self, vital_event, session) -> None:
        """MORTALITY -> the linked animal's Health Status becomes DECEASED;
        DISEASE -> SICK. Mirrors the Old System's create()-time side effect
        (g2p_livestock_registry/models/livestock_event.py: `rec.line_id.
        health_status = 'deceased'` / `'sick'`), which Gen2 had no
        equivalent of — recording one of these events left the animal's own
        Health Status field on Livestock Details completely untouched.

        Called from both post_approve and post_ingest, same as
        _maybe_generate_offspring above — but unlike offspring generation,
        this needs no "already synced" guard: (re)setting health_status to
        the same value on a repeat approval is harmless, not a duplicate
        side effect to prevent.
        """
        event_type = str(vital_event.event_type or "").upper()
        new_status = {"MORTALITY": "DECEASED", "DISEASE": "SICK"}.get(event_type)
        if new_status is None:
            return

        G2PRegisterAnimal, _ = _animal_models()
        animal = (
            await session.execute(
                select(G2PRegisterAnimal).where(
                    animal_identified_by(G2PRegisterAnimal, vital_event.ear_tag_id),
                    G2PRegisterAnimal.link_internal_record_id == vital_event.link_internal_record_id,
                )
            )
        ).scalar()
        if not animal:
            return
        animal.health_status = new_status
        await session.flush()
        _logger.info(
            "%s event %s: set animal %s health_status to %s",
            event_type, vital_event.internal_record_id, animal.ear_tag_id or animal.secondary_identifier, new_status,
        )

    async def post_intake_upsert(self, rows: list, session) -> None:
        """Reserve a BIRTH row's offspring ear tag(s) the moment its
        intake-form section is saved (staff clicking "Next"), instead of
        only once the whole submission is later approved/ingested
        (post_approve / post_ingest, above) — lets staff see the tag(s) the
        newborn(s) will get immediately, before they've even finished the
        rest of the form. _create_offspring_animals reuses these exact
        reserved tags at that later point rather than generating new ones,
        so what was shown here is guaranteed to be what gets created.

        Only reserves — never creates the Animal profile(s) themselves: at
        this point the dam herself may still be nothing more than an
        intake draft (not yet a live register row this offspring could link
        to), so actual creation stays deferred to post_approve/post_ingest.
        """
        await ensure_ear_tags_belong_to_submission(rows)
        # Intake-side duplicate checks: only here do the rows carry the
        # submission they belong to, which the search must leave out.
        records = intake_rows_as_records(rows)
        own = submission_ids_of(rows)
        await self._validate_no_duplicate_mortality(records, search=("intake",), exclude_submission_ids=own)
        await self._validate_no_duplicate_disease(records, search=("intake",), exclude_submission_ids=own)
        for row in rows:
            if str(getattr(row, "event_type", "") or "").upper() != "BIRTH":
                continue
            if not is_blank(getattr(row, "offspring_ear_tags", None)):
                continue  # already reserved — never re-reserve on a later edit
            count = as_int(getattr(row, "offspring_count", None)) or 0
            if count < 1:
                continue
            row.offspring_ear_tags = ", ".join(await self._reserve_ear_tags(session, count))

    async def _reserve_ear_tags(self, session, count: int) -> list[str]:
        G2PRegisterAnimal, G2PIntakeFormAnimal = _animal_models()
        next_tag_number = await self._next_ear_tag_number(session, G2PRegisterAnimal, G2PIntakeFormAnimal)
        tags = []
        for _ in range(count):
            if next_tag_number > 9999999999:
                validation_error(
                    "Cannot generate a new ear tag: the ET0000000000-ET9999999999 "
                    "sequence is exhausted."
                )
            tags.append(f"ET{next_tag_number:010d}")
            next_tag_number += 1
        return tags

    async def _maybe_generate_offspring(self, vital_event, session) -> None:
        if str(vital_event.event_type or "").upper() != "BIRTH":
            return
        if vital_event.offspring_generated:
            return  # already generated for this event — never double-create

        count = as_int(vital_event.offspring_count) or 0
        if count < 1:
            return

        await self._create_offspring_animals(vital_event, count, session)
        vital_event.offspring_generated = True

    async def _create_offspring_animals(self, vital_event, count: int, session) -> None:
        from .g2p_register_domain_service_animal import G2PRegisterDomainServiceAnimal

        G2PRegisterAnimal, G2PIntakeFormAnimal = _animal_models()

        # Breed isn't captured on the Vital Event itself (species is, via the
        # ear_tag_id autofill from Livestock Details) — copied from the dam's
        # own Animal row instead, same as the Odoo module's
        # _create_offspring_profiles. Best-effort: a dam with no breed on
        # file, or not found at all, just leaves the offspring's breed blank
        # for staff to fill in — this never blocks offspring creation.
        dam = (
            await session.execute(
                select(G2PRegisterAnimal).where(
                    animal_identified_by(G2PRegisterAnimal, vital_event.ear_tag_id),
                    G2PRegisterAnimal.link_internal_record_id == vital_event.link_internal_record_id,
                )
            )
        ).scalar()
        breed = dam.breed if dam else None

        # Reuse the tag(s) already reserved at intake-save time
        # (post_intake_upsert, above) when they're there and match this
        # count, so what staff were shown earlier is exactly what gets
        # created. Falls back to generating fresh ones otherwise — a draft
        # from before this feature existed, or any other mismatch.
        reserved = [
            tag.strip() for tag in (vital_event.offspring_ear_tags or "").split(",") if tag.strip()
        ]
        if len(reserved) == count:
            ear_tags = reserved
        else:
            ear_tags = await self._reserve_ear_tags(session, count)

        animal_service = G2PRegisterDomainServiceAnimal()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        actor = vital_event.last_approved_by or vital_event.created_by

        created_tags = []
        for ear_tag_id in ear_tags:
            internal_id = str(uuid.uuid4())

            payload = {
                "ear_tag_id": ear_tag_id,
                "species": vital_event.species,
                "breed": breed,
                # validate_domain_attributes requires the offspring sex on a
                # Birth event; a Birth approved before that rule existed may
                # still have none, and the animal's gender is not nullable.
                # FEMALE keeps what the ODK hooks used to set in that case.
                "gender": vital_event.offspring_gender or "FEMALE",
                "date_of_birth": vital_event.event_date,
                "registration_date": vital_event.event_date,
                "health_status": "HEALTHY",
                "vaccination_status": "NONE",
                # DRAFT, not CONFIRMED/DONE: a newborn profile is legitimately
                # incomplete (no breed found, weight not yet taken, ...) —
                # same as the Odoo module's 'state': 'draft' — pending staff
                # completing it under Livestock Details.
                "state": "DRAFT",
            }

            animal = G2PRegisterAnimal(
                internal_record_id=internal_id,
                link_internal_record_id=vital_event.link_internal_record_id,
                record_status="ACTIVE",
                created_by=actor,
                created_at=now,
                last_approved_by=actor,
                last_approved_at=now,
                **payload,
            )
            animal.record_name = animal_service.construct_record_name(payload)
            animal.search_text = animal_service.construct_search_text(payload)
            session.add(animal)

            # Insert the row directly, then enqueue functional-id generation —
            # the existing celery worker assigns the real AN-########## id
            # exactly as it would for any other new Animal record (mirrors
            # G2PRegisterDomainServiceFarmer._create_household_for_head).
            session.add(
                G2PFunctionalIdGenerationQueue(
                    register_id=ANIMAL_REGISTER_ID,
                    internal_record_id=internal_id,
                )
            )
            created_tags.append(ear_tag_id)

        await session.flush()
        from .domain_validation_utils import refresh_total_animals

        await refresh_total_animals(session, vital_event.link_internal_record_id)
        _logger.info(
            "Birth event %s: created %d offspring Animal row(s) under livestock %s: %s",
            vital_event.internal_record_id, count, vital_event.link_internal_record_id, created_tags,
        )

    async def _next_ear_tag_number(self, session, G2PRegisterAnimal, G2PIntakeFormAnimal) -> int:
        """The next unused ET+10-digit sequence number, one higher than the
        highest already in use anywhere — the approved register, a
        still-pending intake draft, or already reserved (but not yet
        materialized as an Animal row) on some other Vital Event's own
        offspring_ear_tags. That last source is what keeps two Birth events
        being drafted concurrently (post_intake_upsert, above) from ever
        reserving the same number twice. Mirrors _generate_next_ear_tag in
        the Odoo module, but computes the whole batch's starting point once
        rather than re-querying per offspring.
        """
        import importlib

        vital_event_models = importlib.import_module(
            "openg2p_registry_extensions.register_domain.models"
        )

        max_num = 0
        for model in (G2PRegisterAnimal, G2PIntakeFormAnimal):
            tags = (
                await session.execute(select(model.ear_tag_id).where(model.ear_tag_id.like("ET%")))
            ).scalars().all()
            for tag in tags:
                match = _EAR_TAG_PATTERN.match((tag or "").strip().upper())
                if match:
                    max_num = max(max_num, int(match.group(1)))

        for model in (
            vital_event_models.G2PRegisterVitalEvent,
            vital_event_models.G2PIntakeFormVitalEvent,
        ):
            reservations = (
                await session.execute(
                    select(model.offspring_ear_tags).where(model.offspring_ear_tags.is_not(None))
                )
            ).scalars().all()
            for reserved in reservations:
                for tag in (reserved or "").split(","):
                    match = _EAR_TAG_PATTERN.match(tag.strip().upper())
                    if match:
                        max_num = max(max_num, int(match.group(1)))

        return max_num + 1

    def construct_search_text(self, payload: dict, extra: list[str] = None) -> str:
        _logger.info("Constructing search text for vital event record")

        keys = [
            "functional_record_id",
            "ear_tag_id",
            "species",
            "event_type",
            "cause",
            "disease_type",
            "veterinarian_name",
            "reporting_officer",
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
        _logger.info("Constructing record name for vital event record")

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
