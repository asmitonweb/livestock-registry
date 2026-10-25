import logging

from openg2p_registry_core.services import G2PRegisterDomainService

from .audit_snapshot import AuditSnapshotMixin

from .domain_validation_utils import as_int, validation_error

_logger = logging.getLogger("g2p-register-domain-service")


class G2PRegisterDomainServiceImportBatch(AuditSnapshotMixin, G2PRegisterDomainService):

    async def validate_domain_attributes(self, records: list[dict]):
        from .field_rules import validate_record_fields
        await validate_record_fields(records, table=True)
        for record in records:
            self._validate_row_counts(record)

    def _validate_row_counts(self, record: dict) -> None:
        total = as_int(record.get("total_rows"))
        parts = [as_int(record.get(field)) for field in
                 ("success_count", "failure_count", "conflict_count")]
        for value in [total, *parts]:
            if value is not None and value < 0:
                validation_error("row counts must not be negative")
        if total is not None and all(part is not None for part in parts):
            if sum(parts) > total:
                validation_error(
                    "success_count + failure_count + conflict_count must not exceed total_rows"
                )

    def construct_search_text(self, payload: dict, extra: list[str] = None) -> str:
        _logger.info("Constructing search text for import batch record")

        keys = [
            "functional_record_id",
            "batch_reference",
            "source_system",
            "state",
            "import_filename",
            "processed_by",
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
        _logger.info("Constructing record name for import batch record")

        keys = ["batch_reference", "source_system"]
        record_name = []
        if extra:
            record_name.extend(str(item).strip() for item in extra if str(item).strip())
        record_name.extend(
            str(payload.get(key) or "").strip()
            for key in keys
            if str(payload.get(key) or "").strip()
        )

        return " ".join(record_name).strip()
