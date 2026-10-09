import logging
import re
from datetime import date

from openg2p_registry_core.models import G2PRegisterChangeRequest
from openg2p_registry_core.services import G2PRegisterDomainService
from sqlalchemy import select

from .audit_snapshot import AuditSnapshotMixin

from .domain_validation_utils import (
    compose_farmer_name,
    is_blank,
    parse_date,
    sync_farmer_identity_to_livestock,
    validation_error,
)
from .embedded_files import persist_embedded_files

_logger = logging.getLogger("g2p-register-domain-service")

# LivestockStateEnum is NOT imported at module level here (unlike the rest of this
# file's plain `from ..models... import` style would suggest): this is the extension's
# only service module that needs something from ..models, and models/*.py each import
# back from ..services at module level (G2PRegisterDomainServiceLivestock included) —
# so a top-level `from ..models.enums import LivestockStateEnum` here deadlocks that
# cycle the moment this module is the first thing to touch the services package.
# _stage_order_to_state() below fetches it lazily instead, the same way
# _extension_models() fetches the models themselves further down this file.


def _stage_order_to_state():
    """stage_order (on the registry.intake_form.livestock AWE policy) -> the state
    that stage's approval advances a Livestock intake draft to. Mirrors gen1's
    Kebele -> Woreda -> Zone -> Region ladder (g2p_livestock_registry/models/
    livestock_registry.py _APPROVAL_LEVELS). The final stage (Region) is NOT
    listed here — it's handled separately in post_approval_stage below via
    event_type == "request_approved", so it always means VERIFIED regardless
    of which literal stage_order number the policy's last stage happens to be.
    """
    LivestockStateEnum = _extension_models().LivestockStateEnum
    return {
        1: LivestockStateEnum.KEBELE_APPROVED,
        2: LivestockStateEnum.WOREDA_APPROVED,
        3: LivestockStateEnum.ZONE_APPROVED,
    }


# FR- followed by exactly 10 digits, as enforced by _check_farmer_id_format in
# g2p_livestock_registry/models/livestock_registry.py.
_FARMER_ID_PATTERN = re.compile(r"^FR-\d{10}$")
# Exactly 16 digits (the Fayda FAN carries no letter prefix), as enforced by
# _check_fayda_fan_id_format in
# g2p_livestock_registry/models/livestock_registry.py.
_FAYDA_FAN_ID_PATTERN = re.compile(r"^\d{16}$")
# Ethiopian mobile numbers only, matching _check_mobile_numbers in gen1's
# g2p_crop_registry/model/crop_registry.py: +251 or a leading 0, followed by
# 7 or 9 (the only leading digits Ethiopian mobile numbers use) and 8 more
# digits, e.g. +251911223344 or 0911223344.
_MOBILE_NUMBER_PATTERN = re.compile(r"^(\+251[79]\d{8}|0[79]\d{8})$")

# Livestock's own register_id and Farmer's, from g2p_register_definitions.sql.
# Farmer and Livestock are SIBLING top-level registers (both master_register_id
# NULL) submitted together on one form, not parent/child — so there is no
# link_internal_record_id set by the platform itself the way a true child
# section gets one. post_approve/post_ingest below establish it.
LIVESTOCK_REGISTER_ID = "997676d3-7008-59f9-b23e-613ad79bbb08"
FARMER_REGISTER_ID = "f9c6a359-9563-5a43-b0fe-6c7e452037a3"


def _extension_models():
    """Import the extension's own models the same way the platform resolves
    the domain extension: through the "openg2p_registry_extensions" alias
    main.py points at this package's real module in sys.modules, not this
    package's own dotted name — see domain_validation_utils._animal_models's
    docstring for the full reasoning. Matters here specifically because
    G2PRegisterDomainFactory.get_domain_service (factory/g2p_register_domain_
    factory.py) resolves THIS service by importing
    "openg2p_registry_livestock_extension.register_domain.services" — the
    real name — so a plain "from ..models import ..." inside a method reached
    that way re-imports models.py under the real name too, and SQLAlchemy
    refuses the second declarative Table registration.
    """
    import importlib

    return importlib.import_module("openg2p_registry_extensions.register_domain.models")


class G2PRegisterDomainServiceLivestock(AuditSnapshotMixin, G2PRegisterDomainService):

    async def validate_domain_attributes(self, records: list[dict]):
        for record in records:
            # A record photo sent inline (ODK, or the intake 'file' widget) is
            # uploaded and replaced by its document_id before anything is saved.
            await persist_embedded_files(record, ("record_image_document_id",), purpose="livestock")
            # NOT required here, unlike the Farmer register's own copy of
            # these two fields: the "Farmer Details" section's farmer_id/
            # fayda_fan_id are a display-only mirror of the linked Farmer and
            # are never actually populated through this per-section save (see
            # g2p_intake_form_livestocks — 0/23 rows have ever had a
            # non-blank farmer_id here); they get copied over from the Farmer
            # register at finalize/approval time instead. Requiring them here
            # blocked every Livestock intake at this section regardless of
            # what the user typed.
            self._validate_farmer_id(record)
            self._validate_fayda_fan_id(record)
            self._validate_mobile_number(record, "surveyor_mobile_number")
            self._validate_mobile_number(record, "supervisor_mobile_number")
            self._validate_not_in_future(record, "registration_date")

    def _validate_farmer_id(self, record: dict) -> None:
        value = record.get("farmer_id")
        if value is None or str(value).strip() == "":
            return
        if not _FARMER_ID_PATTERN.match(str(value).strip().upper()):
            validation_error(
                "farmer_id must be FR- followed by exactly 10 digits, e.g. FR-1234567890"
            )

    def _validate_fayda_fan_id(self, record: dict) -> None:
        value = record.get("fayda_fan_id")
        if value is None or str(value).strip() == "":
            return
        if not _FAYDA_FAN_ID_PATTERN.match(str(value).strip()):
            validation_error(
                "fayda_fan_id must be exactly 16 digits, e.g. 1234567890123456"
            )

    def _validate_mobile_number(self, record: dict, field: str) -> None:
        value = record.get(field)
        if value is None or str(value).strip() == "":
            return
        if not _MOBILE_NUMBER_PATTERN.match(str(value).strip()):
            validation_error(
                f"{field} must be a valid Ethiopian mobile number, "
                "e.g. +251911223344 or 0911223344"
            )

    def _validate_not_in_future(self, record: dict, field: str) -> None:
        value = parse_date(record.get(field))
        if value is not None and value > date.today():
            validation_error(f"{field} must not be in the future")

    async def post_intake_upsert(self, rows: list, session) -> None:
        """Right after a Livestock-register section (Survey Personnel, Location)
        is saved: if the Farmer section was saved earlier, copy the farmer's
        identity onto these Livestock intake row(s) — the mirror of the Farmer
        service's hook, so the copy happens whichever section comes first. See
        sync_farmer_identity_to_livestock."""
        by_ref: dict[str, list] = {}
        for row in rows:
            ref = getattr(row, "application_reference", None)
            if ref:
                by_ref.setdefault(str(ref), []).append(row)
        for ref, group in by_ref.items():
            await sync_farmer_identity_to_livestock(session, ref, livestock_rows=group)

    async def post_approve(self, change_request: G2PRegisterChangeRequest, session) -> None:
        """Copy the linked Farmer's identity onto this Livestock record —
        see validate_domain_attributes' comment above: farmer_id/fayda_fan_id/
        farmer_name here are meant to be "a display-only mirror ... copied
        over from the Farmer register at finalize/approval time", but nothing
        had ever actually performed that copy (hence "0/23 rows have ever had
        a non-blank farmer_id here"). Covers editing an already-approved
        Livestock record; see post_ingest, below, for a still-draft record's
        first approval.
        """
        if change_request.section_register_id != LIVESTOCK_REGISTER_ID:
            return

        G2PRegisterLivestock = _extension_models().G2PRegisterLivestock

        livestock = (
            await session.execute(
                select(G2PRegisterLivestock).where(
                    G2PRegisterLivestock.internal_record_id == change_request.internal_record_id
                )
            )
        ).scalar_one_or_none()
        if not livestock:
            return
        await self._sync_farmer_identity(livestock, session)

    async def post_approval_stage(
        self, submission, stage_order: int | None, event_type: str, session
    ) -> None:
        """Advances the Livestock intake draft's own `state` through the
        Kebele -> Woreda -> Zone -> Region hierarchical approval ladder as
        each AWE stage completes — see LivestockStateEnum. Called by the
        core-patched g2p_awe_webhook_service.py (Fix 5, docker/staff-api/
        core-patches/apply_patches.py) once per approved stage:
        event_type="stage_completed" for the non-final stages (Kebele/
        Woreda/Zone), and once more with event_type="request_approved" for
        whichever stage is last (Region) — always VERIFIED regardless of
        that stage's literal stage_order number, so this stays correct even
        if the policy's stage count ever changes.

        Runs on the still-draft G2PIntakeFormLivestock row, not the live
        register row: the live register row for this submission does not
        exist yet at KEBELE/WOREDA/ZONE_APPROVED time (it's only created
        once the whole submission is approved and later ingested by the
        celery worker) — and by the time it IS created, ingestion copies
        this draft row's fields verbatim, `state` included, so setting it
        here is all that's needed for it to reach the live register too.
        """
        new_state = (
            _extension_models().LivestockStateEnum.VERIFIED
            if event_type == "request_approved"
            else _stage_order_to_state().get(stage_order)
        )
        if new_state is None:
            return

        G2PIntakeFormLivestock = _extension_models().G2PIntakeFormLivestock
        livestock_row = (
            await session.execute(
                select(G2PIntakeFormLivestock).where(
                    G2PIntakeFormLivestock.submission_id == submission.submission_id
                )
            )
        ).scalar_one_or_none()
        if not livestock_row:
            return
        livestock_row.state = new_state.value
        livestock_row.state_date = date.today()
        await session.flush()
        _logger.info(
            "Submission %s: state -> %s (stage_order=%s, event_type=%s)",
            submission.submission_id, new_state.value, stage_order, event_type,
        )

    async def post_ingest(self, register_id: str, register_row, session) -> None:
        """Same Farmer-identity sync as post_approve, above, for a Livestock
        record reaching the register via a still-draft submission's first
        approval. Safe to run here: the form's own section order (section_order
        10 for Farmer Details vs. 30+ for every Livestock section) guarantees
        the sibling Farmer row this submission carries is already inserted —
        and its history row already recorded — by the time this fires.
        """
        if register_id != LIVESTOCK_REGISTER_ID:
            return
        await self._sync_farmer_identity(register_row, session)

    async def _sync_farmer_identity(self, livestock, session) -> None:
        G2PRegisterFarmer = _extension_models().G2PRegisterFarmer

        farmer_internal_id = livestock.farmer_uuid or await self._find_sibling_farmer_internal_record_id(
            livestock, session
        )
        if not farmer_internal_id:
            return

        farmer = (
            await session.execute(
                select(G2PRegisterFarmer).where(
                    G2PRegisterFarmer.internal_record_id == farmer_internal_id
                )
            )
        ).scalar_one_or_none()
        if not farmer:
            return

        livestock.farmer_uuid = farmer_internal_id
        livestock.farmer_id = farmer.farmer_id
        livestock.fayda_fan_id = farmer.fayda_fan_id
        # Farmer rows registered before farmer_name was composed at intake
        # (see G2PRegisterDomainServiceFarmer._fill_farmer_name) still carry
        # NULL there; compose from the name parts rather than mirror the NULL.
        livestock.farmer_name = (
            farmer.farmer_name
            if not is_blank(farmer.farmer_name)
            else compose_farmer_name(farmer.first_name, farmer.middle_name, farmer.last_name)
        )
        # Mirrors the Fayda FAN into link_foundational_id — see G2PLivestock's
        # own module docstring and G2PFarmer's identical comment on this field.
        livestock.link_foundational_id = farmer.fayda_fan_id

    async def _find_sibling_farmer_internal_record_id(self, livestock, session) -> str | None:
        """This Livestock record's own submission_id, off its most recent
        history row, joined back to whichever Farmer register history row
        shares that same submission_id — i.e. the Farmer submitted alongside
        it on the same form. Farmer and Livestock have no other relationship
        the platform tracks (see LIVESTOCK_REGISTER_ID's comment above), so
        this is the only thing connecting them.
        """
        models = _extension_models()
        G2PRegisterHistoryFarmer = models.G2PRegisterHistoryFarmer
        G2PRegisterHistoryLivestock = models.G2PRegisterHistoryLivestock

        submission_id = (
            await session.execute(
                select(G2PRegisterHistoryLivestock.submission_id)
                .where(G2PRegisterHistoryLivestock.internal_record_id == livestock.internal_record_id)
                .where(G2PRegisterHistoryLivestock.submission_id.is_not(None))
                .order_by(G2PRegisterHistoryLivestock.created_at.asc())
                .limit(1)
            )
        ).scalar()
        if not submission_id:
            return None

        return (
            await session.execute(
                select(G2PRegisterHistoryFarmer.internal_record_id)
                .where(G2PRegisterHistoryFarmer.submission_id == submission_id)
                .order_by(G2PRegisterHistoryFarmer.created_at.asc())
                .limit(1)
            )
        ).scalar()

    def construct_search_text(self, payload: dict, extra: list[str] = None) -> str:
        _logger.info("Constructing search text for livestock record")

        keys = [
            "functional_record_id",
            "oan_id",
            "farmer_name",
            "farmer_id",
            "fayda_fan_id",
            "secondary_identifier",
            "status",
            "state",
            "source_system",
            "surveyor_name",
            "supervisor_name",
            "region",
            "zone",
            "woreda",
            "kebele",
            "country_code",
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
        _logger.info("Constructing record name for livestock record")

        # Farmer name only. Appending the OAN id here put the name and a long
        # identifier on one line in the register list, where the id is already
        # shown as its own column.
        keys = ["farmer_name"]
        record_name = []
        if extra:
            record_name.extend(str(item).strip() for item in extra if str(item).strip())
        record_name.extend(
            str(payload.get(key) or "").strip()
            for key in keys
            if str(payload.get(key) or "").strip()
        )

        return " ".join(record_name).strip()

    def compute_deduplication_score_for_register(
        self, change_request_id, register_id, incoming_data, session
    ):
        """Score each intake row once per (submission, register), not once per
        form section.

        Workaround for the platform's deduplication_intake_forms_vs_register
        worker: it loops over every section of the form whose register has
        purpose REGISTER and, for each, loads the whole intake row and asks
        this method to score it. The Livestock form has three such sections
        (Farmer Details, Survey Personnel, Location) all backed by this one
        register, so the same row was scored three times and the same match
        was written three times — showing one duplicate as three cards with a
        "03" badge in the intake dedup tab. The score itself is right; only
        the repeats are dropped here. The worker uses one session for the
        whole task, so noting "already scored" on session.info is scoped to
        exactly one worker run. Keyed on the row's own id as well, so a
        register section with several rows still scores each row.

        The proper fix is for the worker to visit each register once,
        regardless of how many sections point at it; drop this override once
        the platform does that.
        """
        if self._already_scored_in_this_run(
            "livestock_dedup_scored_vs_register", change_request_id, register_id, incoming_data, session
        ):
            return []
        return super().compute_deduplication_score_for_register(
            change_request_id, register_id, incoming_data, session
        )

    def compute_deduplication_score_for_change_request(
        self, change_request_id, register_id, incoming_data, other_change_requests, session
    ):
        """Same workaround as compute_deduplication_score_for_register, above,
        for the intake-vs-intake path: deduplication_intake_forms_vs_intake_
        forms_worker walks the same three sections and asked for the same row
        to be scored three times, so the "Intake Possible Duplicates" tab
        showed one other submission as three cards. Its own "best score per
        candidate" pass only runs within one call, not across the three. The
        change-request worker also calls this method, once per change
        request, so nothing is skipped on that path.
        """
        if self._already_scored_in_this_run(
            "livestock_dedup_scored_vs_intake", change_request_id, register_id, incoming_data, session
        ):
            return []
        return super().compute_deduplication_score_for_change_request(
            change_request_id, register_id, incoming_data, other_change_requests, session
        )

    @staticmethod
    def _already_scored_in_this_run(bucket, change_request_id, register_id, incoming_data, session) -> bool:
        key = (
            str(change_request_id),
            str(register_id),
            str((incoming_data or {}).get("internal_record_id")),
        )
        scored = session.info.setdefault(bucket, set())
        if key in scored:
            _logger.info(
                "Skipping repeat dedup scoring of the same row for submission "
                f"{change_request_id}, register {register_id} ({bucket})"
            )
            return True
        scored.add(key)
        return False
