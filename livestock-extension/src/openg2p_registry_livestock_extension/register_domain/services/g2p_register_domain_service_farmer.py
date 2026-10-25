import logging
import re
from datetime import date

from openg2p_registry_core.services import G2PRegisterDomainService

from .admin_area_sync import sync_admin_area
from .audit_snapshot import AuditSnapshotMixin

from .domain_validation_utils import (
    ensure_livestock_intake_row,
    compose_farmer_name,
    is_blank,
    parse_date,
    resolve_today_default,
    sync_farmer_identity_to_livestock,
    validation_error,
)
from ...farmer_reference.profile import FarmerReferenceError, validate_identity_fields

_logger = logging.getLogger("g2p-register-domain-service")


# Farmer Registry IDs use FR- followed by exactly 12 digits.
_FARMER_ID_PATTERN = re.compile(r"^FR-[0-9]{12}$")
# Exactly 16 digits (the Fayda FAN carries no letter prefix), as enforced by
# _check_fayda_fan_id_format in
# g2p_livestock_registry/models/livestock_registry.py.
_FAYDA_FAN_ID_PATTERN = re.compile(r"^[0-9]{16}$")
# Ethiopian mobile numbers only, matching _check_mobile_numbers in gen1's
# g2p_crop_registry/model/crop_registry.py: +251 or a leading 0, followed by
# 7 or 9 (the only leading digits Ethiopian mobile numbers use) and 8 more
# digits, e.g. +251911223344 or 0911223344.
_MOBILE_NUMBER_PATTERN = re.compile(r"^(\+251[79]\d{8}|0[79]\d{8})$")


FARMER_REGISTER_ID = "f9c6a359-9563-5a43-b0fe-6c7e452037a3"


class G2PRegisterDomainServiceFarmer(AuditSnapshotMixin, G2PRegisterDomainService):

    async def validate_domain_attributes(self, records: list[dict]):
        from .field_rules import validate_record_fields
        await validate_record_fields(records)
        for record in records:
            try:
                validate_identity_fields(record)
            except FarmerReferenceError as error:
                validation_error(str(error))
            # Registration Date defaults to the literal "today" on the intake form;
            # resolve it before any check (see resolve_today_default).
            resolve_today_default(record, "registration_date")
            # Farmer ID is optional: the form does not mark it required, and
            # the Old System only checked its format when
            # one was given -- on its staging data most farmers had none.
            # Requiring it here while the form did not made a draft saved
            # with the field blank fail on reopen with "Farmer ID is
            # required": a new form omits an empty field from the payload
            # and slips past a key-present guard, a reopened one resends it
            # as null and does not. _validate_farmer_id still rejects a
            # malformed value.
            #
            # Fayda is guarded by "key present", not unconditionally: the
            # Farmer register also backs the (currently unused-by-intake)
            # Farmer Location section, whose payload never carries the key.
            # Drafts may be partial. The separately gated submission lifecycle
            # must verify the complete pair before onboarding or activation.
            self._fill_farmer_name(record)
            self._validate_farmer_id(record)
            self._validate_fayda_fan_id(record)
            self._validate_mobile_number(record, "mobile_number")
            self._validate_not_in_future(record, "date_of_birth")
            self._validate_not_in_future(record, "registration_date")

    def _fill_farmer_name(self, record: dict) -> None:
        """Compose farmer_name from First / Middle / Last Name when the payload
        carries name parts but no farmer_name of its own. The intake form has
        no farmer_name field, so without this the Farmer register row keeps
        farmer_name NULL, record_name falls back to the bare FR- id, and the
        Livestock record's approval-time mirror copies that NULL over the name
        it had at intake (see G2PRegisterDomainServiceLivestock
        ._sync_farmer_identity). When the payload carries name parts they are
        the source of truth (a name corrected on a reopened draft must not
        keep the old composed value); a payload with no name parts at all
        (API / migration sending farmer_name only) is left alone."""
        if not any(key in record for key in ("first_name", "middle_name", "last_name")):
            return
        composed = compose_farmer_name(
            record.get("first_name"), record.get("middle_name"), record.get("last_name")
        )
        if composed:
            record["farmer_name"] = composed
        elif is_blank(record.get("farmer_name")):
            record["farmer_name"] = None

    def _validate_farmer_id(self, record: dict) -> None:
        value = record.get("farmer_id")
        if value is None or str(value).strip() == "":
            return
        if not _FARMER_ID_PATTERN.match(str(value).strip().upper()):
            validation_error(
                "farmer_id must be FR- followed by exactly 12 digits, e.g. FR-123456789012"
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

    def construct_search_text(self, payload: dict, extra: list[str] = None) -> str:
        _logger.info("Constructing search text for farmer record")

        keys = [
            "functional_record_id",
            "farmer_id",
            "fayda_fan_id",
            "farmer_name",
            "first_name",
            "middle_name",
            "last_name",
            "mobile_number",
            "status",
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
        _logger.info("Constructing record name for farmer record")

        keys = ["farmer_name", "farmer_id"]
        record_name = []
        if extra:
            record_name.extend(str(item).strip() for item in extra if str(item).strip())
        record_name.extend(
            str(payload.get(key) or "").strip()
            for key in keys
            if str(payload.get(key) or "").strip()
        )

        return " ".join(record_name).strip()


    async def pre_approve(self, change_request, session):
        from ...farmer_reference.native import guard_farmer_approval
        await guard_farmer_approval(change_request, session)

    async def post_approve(self, change_request, session) -> None:
        from ...farmer_reference.native import record_native_decision
        await record_native_decision(session, change_request.change_request_id, True)
        # Stored Livestock mirrors require their own domain approval. Farmer approval
        # changes only this Farmer and releases already approved waiting intakes.

    async def post_ingest(self, register_id, register_row, session):
        raise FarmerReferenceError("FARMER_REFERENCE_CHANGE_REQUEST_REQUIRED")
