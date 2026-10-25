import logging

from openg2p_registry_core.services import G2PRegisterDomainService

from .audit_snapshot import AuditSnapshotMixin

from .domain_validation_utils import as_int, is_blank, validation_error

_logger = logging.getLogger("g2p-register-domain-service")

# field -> human label used in the "Please provide the ... " message, mirroring
# the fields marked "widget-required" on the Vaccine Schedule Details form.
_REQUIRED_FIELDS = {
    "vaccine_name": "vaccine name",
    "species": "species",
    "interval_days": "interval",
}


class G2PRegisterDomainServiceVaccineSchedule(AuditSnapshotMixin, G2PRegisterDomainService):

    async def validate_domain_attributes(self, records: list[dict]):
        from .field_rules import validate_record_fields
        await validate_record_fields(records, table=True)
        for record in records:
            self._validate_required_fields(record)
            self._validate_positive(record, "interval_days")

    def _validate_required_fields(self, record: dict) -> None:
        for field, label in _REQUIRED_FIELDS.items():
            if is_blank(record.get(field)):
                validation_error(f"Please provide the {label} before saving the record.")

    def _validate_positive(self, record: dict, field: str) -> None:
        value = as_int(record.get(field))
        if value is not None and value <= 0:
            validation_error(f"{field} must be greater than zero")

    def construct_search_text(self, payload: dict, extra: list[str] = None) -> str:
        _logger.info("Constructing search text for vaccine schedule record")

        keys = [
            "functional_record_id",
            "vaccine_name",
            "species",
            "notes",
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
        _logger.info("Constructing record name for vaccine schedule record")

        keys = ["vaccine_name", "species"]
        record_name = []
        if extra:
            record_name.extend(str(item).strip() for item in extra if str(item).strip())
        record_name.extend(
            str(payload.get(key) or "").strip()
            for key in keys
            if str(payload.get(key) or "").strip()
        )

        return " ".join(record_name).strip()
