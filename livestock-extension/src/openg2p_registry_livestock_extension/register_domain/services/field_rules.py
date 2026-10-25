"""Field checks shared by registered domain services; no core or worker patch.

Only supplied fields are checked. Required/form/record rules remain with their
native lifecycle. Names and dates normalize before domain calculations.
"""

import logging
import unicodedata
from dataclasses import dataclass, replace
from datetime import date

from ...farmer_reference.profile import (
    FarmerReferenceError,
    normalize_farmer_id,
    normalize_fin,
    normalize_uid,
)

PERSON_NAMES = frozenset(
    {
        "farmer_name",
        "first_name",
        "middle_name",
        "last_name",
        "da_name",
        "surveyor_name",
        "ai_technician_name",
        "veterinarian_name",
        "supervisor_name",
    }
    | {
        f"{part}_{lang}"
        for part in ("first_name", "middle_name", "last_name")
        for lang in ("amh", "other")
    }
)
DATE_FIELDS = frozenset(
    [
        "applied_on",
        "breeding_date",
        "date_of_birth",
        "date_onset",
        "date_resolution",
        "event_date",
        "expected_calving_date",
        "next_due_date",
        "pregnancy_confirmation_date",
        "registration_date",
        "retag_date",
        "state_date",
        "vaccination_date",
    ]
)
PAST_ONLY = frozenset({"date_of_birth", "registration_date"})
LABELS = {
    "farmer_id": "Farmer ID",
    "fayda_fin": "FIN (Fayda ID)",
    "fayda_fan_id": "Fayda ID (FAN)",
    "fayda_id": "Fayda ID (FAN)",
    "da_name": "DA Name",
    "surveyor_name": "DA Name",
    "ai_technician_name": "AI Technician Name",
    "veterinarian_name": "Veterinarian Name",
    "supervisor_name": "Supervisor Name",
    "date_of_birth": "Date of Birth",
}
MESSAGES = {
    "coss_field_name_format": "{label}: use 1-100 letters, spaces, hyphens or apostrophes. Remove digits and other symbols.",
    "coss_field_amharic_name_format": "{label}: use 1-100 Ethiopic letters, spaces, hyphens or apostrophes. Remove digits and other symbols.",
    "coss_field_farmer_id_format": "{label}: enter FR- followed by exactly 12 digits, preserving leading zeroes.",
    "coss_field_fin_format": "{label}: enter exactly 12 digits. Use the FAN field for a 16-digit FAN.",
    "coss_field_fan_format": "{label}: enter exactly 16 digits, optionally separated by spaces. Use the FIN field for a 12-digit FIN.",
    "coss_field_date_format": "{label}: enter a real date. API/import dates use Gregorian YYYY-MM-DD; correct the date before saving.",
    "coss_field_date_min": "{label}: the date is before 1900. Enter a date on or after 1900-01-01.",
    "coss_field_date_future": "{label}: the date is in the future. Enter today or an earlier date.",
    "coss_field_farmer_id_required": "{label}: enter the farmer's Farmer Registry ID (FR- followed by 12 digits).",
    "coss_field_fayda_one_of": "Fayda ID: enter the 12-digit FIN or the 16-digit FAN, or use Verify with Fayda.",
}


@dataclass(frozen=True)
class Issue:
    field: str
    key: str
    row: int | None = None


def explain(issue, messages=None):
    messages = messages or {}
    label = (
        messages.get(issue.field)
        or LABELS.get(issue.field)
        or issue.field.replace("_", " ").title()
    )
    if issue.row is not None:
        row_label = messages.get("coss_field_row_label", "{label} (row {row})")
        label = row_label.replace("{label}", label).replace("{row}", str(issue.row))
    template = messages.get(issue.key) or MESSAGES[issue.key]
    return template.replace("{label}", label)


def name_issue(value, field):
    if not isinstance(value, str):
        return Issue(field, "coss_field_name_format")
    cleaned = unicodedata.normalize("NFC", value.strip())
    amharic = field.endswith("_amh")
    key = "coss_field_amharic_name_format" if amharic else "coss_field_name_format"

    def letter(char):
        return unicodedata.category(char).startswith(("L", "M")) and (
            not amharic or 0x1200 <= ord(char) <= 0x137F
        )

    if (
        not 1 <= len(cleaned) <= 100
        or not any(char.isalpha() for char in cleaned)
        or any(not letter(char) and char not in " '-’" for char in cleaned)
    ):
        return Issue(field, key)
    return None


def clean_records(records):
    from .domain_validation_utils import parse_date

    problems = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        if record.get("edit_action") == "DELETE":
            continue
        for key, value in list(record.items()):
            field = key.rsplit(".", 1)[-1] if isinstance(key, str) else None
            if value is None or isinstance(value, str) and not value.strip():
                continue
            if field in PERSON_NAMES:
                problem = name_issue(value, field)
                if problem:
                    problems.append(problem)
                else:
                    record[key] = value.strip()
            elif field in {"farmer_id", "fayda_fin", "fayda_fan_id", "fayda_id"}:
                normalizer, error_key = {
                    "farmer_id": (normalize_farmer_id, "coss_field_farmer_id_format"),
                    "fayda_fin": (normalize_fin, "coss_field_fin_format"),
                    "fayda_fan_id": (normalize_uid, "coss_field_fan_format"),
                    "fayda_id": (normalize_uid, "coss_field_fan_format"),
                }[field]
                try:
                    record[key] = normalizer(value)
                except FarmerReferenceError:
                    problems.append(Issue(field, error_key))
            elif field in DATE_FIELDS:
                parsed = (
                    date.today()
                    if field == "registration_date" and value == "today"
                    else parse_date(value)
                )
                if parsed is None:
                    problems.append(Issue(field, "coss_field_date_format"))
                elif parsed < date(1900, 1, 1):
                    problems.append(Issue(field, "coss_field_date_min"))
                elif field in PAST_ONLY and parsed > date.today():
                    problems.append(Issue(field, "coss_field_date_future"))
                else:
                    record[key] = parsed.isoformat()
        problems.extend(identity_issues(record))
    return problems


def identity_issues(record):
    """A farmer is identified by its Farmer ID and one Fayda ID (FIN or FAN).

    Checked only on records that carry the identity fields (the intake's
    Farmer Identity section), so other sections and child rows are untouched.
    """
    def value(field):
        for key, item in record.items():
            if isinstance(key, str) and key.rsplit(".", 1)[-1] == field:
                return item.strip() if isinstance(item, str) else item
        return None

    fields = {key.rsplit(".", 1)[-1] for key in record if isinstance(key, str)}
    if not {"farmer_id", "fayda_fin", "fayda_fan_id"} <= fields:
        return []
    issues = []
    if not value("farmer_id"):
        issues.append(Issue("farmer_id", "coss_field_farmer_id_required"))
    if not value("fayda_fin") and not value("fayda_fan_id"):
        issues.append(Issue("fayda_fin", "coss_field_fayda_one_of"))
    return issues


async def _translations():
    """Use registry language metadata; English remains available in pure probes."""
    from openg2p_fastapi_common.context import dbengine, get_async_session_maker
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    try:
        if dbengine.get() is None:
            return {}
        async with get_async_session_maker()() as session:
            value = (
                await session.execute(
                    text(
                        "SELECT domain_translation FROM registry_languages WHERE is_default LIMIT 1"
                    )
                )
            ).scalar_one_or_none()
        if isinstance(value, str):
            import json

            value = json.loads(value)
        return value if isinstance(value, dict) else {}
    except (LookupError, RuntimeError, SQLAlchemyError, OSError, ValueError):
        logging.getLogger(__name__).warning(
            "Registry field translations unavailable; using explained English defaults"
        )
        return {}


async def validate_record_fields(records, *, table=False):
    problems = [
        replace(problem, row=index if table else None)
        for index, record in enumerate(records or [], 1)
        for problem in clean_records([record])
    ]
    if problems:
        from .domain_validation_utils import validation_error

        messages = await _translations()
        validation_error("; ".join(explain(problem, messages) for problem in problems))
