"""Ear Tag Replacement (Retagging) — SRS LR-03 / LR-14.

An animal already in the register whose ear tag is lost, damaged or being
upgraded gets a new tag through a change request on its Livestock record
(Livestock tab -> Ear Tag Replacement -> Edit Details). Nothing changes until
that change request is approved; then post_approve:

  1. moves the animal to the new tag (same animal row, same internal id), and
  2. moves every event line of that animal (health, vaccination, vital,
     breeding) to the new tag, so the overdue sweep, reminders, duplicate
     checks and outbreak alert — which all look animals up by ear tag —
     keep following it,

while the retagging row itself stays as the permanent old -> new link, with
reason, date, approving officer and justification (last_approved_by records
who approved the change request). A retired tag can never be issued again —
see ear_tag_is_retired, also enforced on the Animal section.
"""

import importlib
import logging
import re
from datetime import date

from openg2p_registry_core.errors import G2PRegistryErrorCodes, G2PRegistryException
from openg2p_registry_core.services import G2PRegisterDomainService
from sqlalchemy import exists, select, update

from .audit_snapshot import AuditSnapshotMixin, _get_change_payload_rows
from .domain_validation_utils import (
    normalize_ear_tag_text,
    animal_identified_by,
    _animal_models,
    ear_tag_exists,
    is_blank,
    parse_date,
    validation_error,
)

_logger = logging.getLogger("g2p-register-domain-service")

RETAGGING_REGISTER_ID = "b853f1db-6dd6-5b16-a0d8-25a8524142e6"

_EAR_TAG_PATTERN = re.compile(r"^ET\d{10}$")

# field -> human label used in the "Please provide the ... " message, mirroring
# the fields marked "widget-required" on the Ear Tag Replacement form.
_REQUIRED_FIELDS = {
    "ear_tag_id": "old ear tag",
    "new_ear_tag_id": "new ear tag",
    "reason": "reason for replacement",
    "retag_date": "date of retagging",
    "approving_officer": "approving officer",
    "justification": "justification notes",
}

# Every event line that names its animal by ear tag. Moved to the new tag on
# approval so the animal's history stays attached to it.
_EVENT_MODELS = ("G2PRegisterHealthEvent", "G2PRegisterVaccination", "G2PRegisterVitalEvent", "G2PRegisterBreeding")


def _reject(message: str) -> None:
    """Refuse the approval. Raised directly rather than through
    domain_validation_utils.validation_error: the ODK ingestion hooks
    (odk_ingest_hooks.py) currently turn that function -- and every
    validate_domain_attributes -- into a logged no-op in every process, so the
    save-time checks above can be silenced. post_approve is not wrapped, so the
    rules are enforced here again, at approval, where they always hold."""
    raise G2PRegistryException(
        code=G2PRegistryErrorCodes.REQUEST_VALIDATION_ERROR.value[1],
        message=message,
    )


def _models():
    # Through the "openg2p_registry_extensions" alias, never "..models" — see
    # domain_validation_utils._animal_models for why.
    return importlib.import_module("openg2p_registry_extensions.register_domain.models")


def _tag(value) -> str:
    return str(value or "").strip().upper()


async def ear_tag_is_retired(ear_tag_id: str) -> bool:
    """True if `ear_tag_id` was replaced by an applied retagging — a retired
    tag stays linked to its animal forever and must never be issued again."""
    if is_blank(ear_tag_id):
        return False

    from openg2p_fastapi_common.context import dbengine
    from sqlalchemy.ext.asyncio import async_sessionmaker

    G2PRegisterRetagging = _models().G2PRegisterRetagging
    session_maker = async_sessionmaker(dbengine.get(), expire_on_commit=False)
    async with session_maker() as session:
        return bool((await session.execute(select(exists().where(
            G2PRegisterRetagging.ear_tag_id == _tag(ear_tag_id),
            G2PRegisterRetagging.applied_on.is_not(None),
        )))).scalar())


class G2PRegisterDomainServiceRetagging(AuditSnapshotMixin, G2PRegisterDomainService):

    async def validate_domain_attributes(self, records: list[dict]):
        from .field_rules import validate_record_fields
        await validate_record_fields(records, table=True)
        applied_ids = await self._applied_record_ids(records)
        seen_old: set[str] = set()
        seen_new: set[str] = set()
        for record in records:
            # A row already applied is history: its old tag no longer names an
            # animal, so it must not be re-checked (the platform sends every
            # row of the section on each save).
            if str(record.get("internal_record_id") or "") in applied_ids:
                continue
            self._normalize(record)
            self._validate_required_fields(record)
            record["ear_tag_id"] = old_tag = normalize_ear_tag_text(record.get("ear_tag_id")) or ""
            record["new_ear_tag_id"] = new_tag = normalize_ear_tag_text(record.get("new_ear_tag_id")) or ""
            if not _EAR_TAG_PATTERN.match(new_tag):
                validation_error(
                    f"New ear tag '{new_tag}' must follow the national format: ET followed by 10 digits, "
                    "e.g. ET0000000123."
                )
            if old_tag == new_tag:
                validation_error("The new ear tag must be different from the old ear tag.")
            if old_tag in seen_old:
                validation_error(f"Ear tag '{old_tag}' is being replaced more than once.")
            if new_tag in seen_new:
                validation_error(f"New ear tag '{new_tag}' is used more than once.")
            seen_old.add(old_tag)
            seen_new.add(new_tag)
            retag_date = parse_date(record.get("retag_date"))
            if retag_date and retag_date > date.today():
                validation_error("Date of retagging must not be in the future.")

            animal = await self._find_animal(old_tag, record.get("link_internal_record_id"))
            if not animal:
                validation_error(
                    f"Old ear tag '{old_tag}' does not match any registered animal"
                    + (" of this livestock record." if record.get("link_internal_record_id") else ".")
                )
            if str(animal.health_status or "").upper() == "DECEASED":
                validation_error(f"Animal '{old_tag}' is deceased and cannot be retagged.")
            if animal.species:
                record["species"] = animal.species
            await self._validate_new_tag_free(new_tag)

    def _normalize(self, record: dict) -> None:
        for key in ("ear_tag_id", "new_ear_tag_id"):
            if not is_blank(record.get(key)):
                record[key] = _tag(record[key])

    def _validate_required_fields(self, record: dict) -> None:
        for field, label in _REQUIRED_FIELDS.items():
            if is_blank(record.get(field)):
                validation_error(f"Please provide the {label} before saving the record.")

    async def _validate_new_tag_free(self, new_tag: str) -> None:
        if await ear_tag_exists(new_tag):
            validation_error(f"New ear tag '{new_tag}' is already in use by another animal.")
        if await ear_tag_is_retired(new_tag):
            validation_error(f"Ear tag '{new_tag}' was retired by an earlier replacement and cannot be reused.")

    async def _applied_record_ids(self, records: list[dict]) -> set[str]:
        ids = [str(r["internal_record_id"]) for r in records if r.get("internal_record_id")]
        if not ids:
            return set()

        from openg2p_fastapi_common.context import dbengine
        from sqlalchemy.ext.asyncio import async_sessionmaker

        G2PRegisterRetagging = _models().G2PRegisterRetagging
        session_maker = async_sessionmaker(dbengine.get(), expire_on_commit=False)
        async with session_maker() as session:
            rows = await session.execute(select(G2PRegisterRetagging.internal_record_id).where(
                G2PRegisterRetagging.internal_record_id.in_(ids),
                G2PRegisterRetagging.applied_on.is_not(None),
            ))
            return {str(r) for r in rows.scalars().all()}

    async def _find_animal(self, old_tag: str, link_internal_record_id: str | None, session=None):
        """The registered, active animal carrying `old_tag` as its ear tag
        (not a secondary identifier — those animals have no tag to replace),
        under `link_internal_record_id` when known."""
        G2PRegisterAnimal, _ = _animal_models()
        query = select(G2PRegisterAnimal).where(
            G2PRegisterAnimal.ear_tag_id == old_tag,
            G2PRegisterAnimal.record_status == "ACTIVE",
        )
        if link_internal_record_id:
            query = query.where(G2PRegisterAnimal.link_internal_record_id == link_internal_record_id)
        if session is not None:
            return (await session.execute(query)).scalars().first()

        from openg2p_fastapi_common.context import dbengine
        from sqlalchemy.ext.asyncio import async_sessionmaker

        session_maker = async_sessionmaker(dbengine.get(), expire_on_commit=False)
        async with session_maker() as own_session:
            return (await own_session.execute(query)).scalars().first()

    async def post_approve(self, change_request, session) -> None:
        """Apply every retagging row this change request added. Runs in the
        approval's own transaction, after approve_table() inserted the rows:
        any check failing here raises, and the whole approval rolls back —
        so an approved retagging is always an applied one."""
        if change_request.section_register_id != RETAGGING_REGISTER_ID:
            return

        G2PRegisterRetagging = _models().G2PRegisterRetagging
        payload_rows = await _get_change_payload_rows(change_request.change_request_id, session)
        ids = [str(r["internal_record_id"]) for r in payload_rows if r.get("internal_record_id")]
        if not ids:
            return
        await session.flush()
        rows = (await session.execute(select(G2PRegisterRetagging).where(
            G2PRegisterRetagging.internal_record_id.in_(ids),
            G2PRegisterRetagging.applied_on.is_(None),
        ))).scalars().all()
        for row in rows:
            await self._apply(row, session)

    async def _apply(self, row, session) -> None:
        G2PRegisterAnimal, G2PIntakeFormAnimal = _animal_models()
        G2PRegisterRetagging = _models().G2PRegisterRetagging
        old_tag, new_tag = _tag(row.ear_tag_id), _tag(row.new_ear_tag_id)

        # Every rule re-checked here with _reject (see its docstring), against
        # the state at approval time: the animal or the new tag may have
        # changed since the change request was raised.
        for field, label in _REQUIRED_FIELDS.items():
            if is_blank(getattr(row, field, None)):
                _reject(f"Cannot apply retagging: the {label} is missing.")
        if not _EAR_TAG_PATTERN.match(new_tag):
            _reject(f"Cannot apply retagging: new ear tag '{new_tag}' must be ET followed by 10 digits.")
        if old_tag == new_tag:
            _reject("Cannot apply retagging: the new ear tag must be different from the old ear tag.")
        retag_date = parse_date(row.retag_date)
        if retag_date and retag_date > date.today():
            _reject("Cannot apply retagging: date of retagging is in the future.")
        animal = await self._find_animal(old_tag, row.link_internal_record_id, session)
        if not animal:
            _reject(f"Cannot apply retagging: no registered animal of this record carries ear tag '{old_tag}'.")
        if str(animal.health_status or "").upper() == "DECEASED":
            _reject(f"Cannot apply retagging: animal '{old_tag}' is deceased.")
        in_register = (await session.execute(select(exists().where(
            animal_identified_by(G2PRegisterAnimal, new_tag),
            G2PRegisterAnimal.internal_record_id != animal.internal_record_id,
        )))).scalar()
        in_intake = (await session.execute(select(exists().where(
            animal_identified_by(G2PIntakeFormAnimal, new_tag),
        )))).scalar()
        if in_register or in_intake:
            _reject(f"Cannot apply retagging: ear tag '{new_tag}' is already in use by another animal.")
        retired = (await session.execute(select(exists().where(
            G2PRegisterRetagging.ear_tag_id == new_tag,
            G2PRegisterRetagging.applied_on.is_not(None),
        )))).scalar()
        if retired:
            _reject(f"Cannot apply retagging: ear tag '{new_tag}' was retired by an earlier replacement.")

        animal.ear_tag_id = new_tag
        models = _models()
        for name in _EVENT_MODELS:
            model = getattr(models, name)
            await session.execute(
                update(model)
                .where(model.link_internal_record_id == row.link_internal_record_id, model.ear_tag_id == old_tag)
                .values(ear_tag_id=new_tag)
            )
        row.ear_tag_id, row.new_ear_tag_id = old_tag, new_tag
        row.applied_on = date.today()
        await session.flush()
        _logger.info(
            "Retagging %s applied: animal %s ear tag %s -> %s (reason %s)",
            row.internal_record_id, animal.internal_record_id, old_tag, new_tag, row.reason,
        )

    def construct_search_text(self, payload: dict, extra: list[str] = None) -> str:
        _logger.info("Constructing search text for retagging record")

        keys = [
            "functional_record_id",
            "ear_tag_id",
            "new_ear_tag_id",
            "species",
            "reason",
            "approving_officer",
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
        _logger.info("Constructing record name for retagging record")

        record_name = []
        if extra:
            record_name.extend(str(item).strip() for item in extra if str(item).strip())
        old_tag = str(payload.get("ear_tag_id") or "").strip()
        new_tag = str(payload.get("new_ear_tag_id") or "").strip()
        if old_tag or new_tag:
            record_name.append(f"RETAG {old_tag} -> {new_tag}".strip())

        return " ".join(record_name).strip()
