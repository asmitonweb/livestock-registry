"""What a Fayda (eSignet) verification puts on the farmer's form.

The verified person's claims are mapped to register fields by the provider's
``provider_config.claim_mapping`` (core's g2p_registrant_authentication_providers
row), or by CLAIM_MAPPING when the provider does not set one. A mapping entry is
the claim's name, or an object:

    {"claim": "name" | ["name#en", "name"],   # first one present wins
     "part": 0,                               # the n-th word of the claim
     "format": "date",                        # YYYY/MM/DD or YYYY-MM-DD -> YYYY-MM-DD
     "values": {"male": "MALE"}}              # claim value -> code, case-insensitive

A field the claims do not give is left out, so the staff can still enter it.
"""

import re
from typing import Any

# An Ethiopian name is the person's, the father's and the grandfather's name, in
# that order; eSignet gives it as one claim, in English and in Amharic.
CLAIM_MAPPING: dict[str, Any] = {
    "first_name": {"claim": ["name#en", "name#eng", "name"], "part": 0},
    "middle_name": {"claim": ["name#en", "name#eng", "name"], "part": 1},
    "last_name": {"claim": ["name#en", "name#eng", "name"], "part": 2},
    "first_name_amh": {"claim": ["name#am", "name#amh"], "part": 0},
    "middle_name_amh": {"claim": ["name#am", "name#amh"], "part": 1},
    "last_name_amh": {"claim": ["name#am", "name#amh"], "part": 2},
    "gender": {"claim": ["gender#en", "gender#eng", "gender"],
               "values": {"male": "MALE", "m": "MALE", "female": "FEMALE", "f": "FEMALE"}},
    "birth_date": {"claim": "birthdate", "format": "date"},
}

_DATE = re.compile(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})")


def _claim(claims: dict, names) -> Any:
    for name in [names] if isinstance(names, str) else names:
        value = claims.get(name)
        if isinstance(value, list):  # a claim in several languages, e.g. [{"language": "eng", "value": ...}]
            value = next((v.get("value") for v in value if isinstance(v, dict) and v.get("value")), None)
        if value not in (None, ""):
            return value
    return None


def _apply(value: Any, spec: dict) -> Any:
    if "part" in spec:
        words = str(value).split()
        part = int(spec["part"])
        value = words[part] if part < len(words) else None
    if value is None:
        return None
    if spec.get("format") == "date":
        match = _DATE.match(str(value))
        value = f"{match[1]}-{int(match[2]):02d}-{int(match[3]):02d}" if match else None
    if spec.get("values"):
        codes = {str(k).lower(): v for k, v in spec["values"].items()}
        value = codes.get(str(value).strip().lower())
    return value


def map_claims(claims: dict, mapping: dict | None = None) -> dict:
    """The register fields the claims give, as {field: value}."""
    values = {}
    for field, spec in (mapping or CLAIM_MAPPING).items():
        spec = {"claim": spec} if isinstance(spec, str) else dict(spec)
        raw = _claim(claims, spec.get("claim") or field)
        value = None if raw is None else _apply(raw, spec)
        if value not in (None, ""):
            values[field] = value
    return values


def same_value(entered: Any, verified: Any) -> bool:
    """A value on the form against the verified one: dates by day, text without case or edge spaces."""
    if entered is None or str(entered).strip() == "":
        return True  # left empty: nothing contradicts the verification
    left, right = str(entered).strip(), str(verified).strip()
    if _DATE.match(left) and _DATE.match(right):
        return left[:10] == right[:10]
    return left.casefold() == right.casefold()


# Core's failure reasons (g2p_registrant_authentication_service) and what the
# staff should do about each. The first matching phrase wins.
_FAILURES: tuple[tuple[str, str], ...] = (
    ("does not match registrant record",
     ("The person who signed in with Fayda is not the FIN on this form. Check the FIN, "
      "or ask the farmer to sign in with their own Fayda ID, then verify again")),
    ("does not have a foundational_id",
     "Enter the farmer's FIN before verifying"),
    ("Token subject is missing",
     ("Fayda did not return the person's FIN. Verify again; if it happens again, ask the "
      "administrator to check that the eSignet client requests the individual_id claim")),
    ("Missing id_token",
     "Fayda did not complete the sign-in. Verify again"),
    ("Provider not found",
     "The Fayda provider for this register was removed. Ask the administrator to set it up again"),
)
_GENERIC_FAILURE = ("Fayda could not verify the farmer. Verify again; if it fails again, "
                    "ask the administrator to check the eSignet connection")


def explain_failure(reason: str | None) -> str:
    timeout = re.search(r"Fayda \(eSignet\) did not answer within ([0-9]+(?:\.[0-9]+)?) seconds\.", reason or "")
    if timeout:
        return (f"Fayda (eSignet) did not answer within {timeout[1]} seconds. Verify again; "
                "if it keeps happening, ask the administrator to check eSignet or raise token_timeout_seconds.")
    for phrase, message in _FAILURES:
        if reason and phrase in reason:
            return message
    return _GENERIC_FAILURE
