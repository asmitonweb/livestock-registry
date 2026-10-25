"""Which approvers a record's place goes to, from standard data policies.

A level's approver holds that level's role (Woreda Approver, ...) and approves
for the places their registry data policies (published to Keycloak as
DP_<mnemonic> roles) allow on that level's Master Data id field, the same ids
that filter what they see. Only finite EQ/IN trees on those fields are read;
any other filter gives no approval scope, never a scope for every place.
A level never falls back to another: a place without its own approver is
refused with the reason, so the administrator assigns one.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

LEVELS = ("woreda", "zone", "region")
ROLES = {"woreda": "Woreda Approver", "zone": "Zone Approver", "region": "Region Approver"}
CONTEXT_FIELDS = tuple(f"geo_{level}_approver_ids" for level in LEVELS)


class GeoApprovalError(Exception):
    """Carries the message the submitter sees: what is wrong and how to fix it."""


@dataclass(frozen=True)
class Candidate:
    user_id: str
    # level -> the Master Data ids that level's policies allow
    scopes: Mapping[str, frozenset[str]] = field(default_factory=dict)


def _finite(expression, field_id, depth=0):
    """The ids an EQ/IN policy tree allows for field_id; None if it says nothing about it."""
    if depth > 10 or not isinstance(expression, dict):
        return None
    if expression.get("type") == "CONDITION":
        if expression.get("field_id") != field_id:
            return None
        operator = str(expression.get("operator", "")).lower()
        values = ([expression.get("value")] if operator == "eq"
                  else expression.get("values") if operator == "in" else None)
        if not isinstance(values, list) or not values:
            return None
        if any(not isinstance(value, str) or not value.strip() for value in values):
            return None
        return {value.strip() for value in values}
    if expression.get("type") == "GROUP" and expression.get("operator") in {"AND", "OR"}:
        children = expression.get("children")
        if not isinstance(children, list) or not children or len(children) > 100:
            return None
        sets = [_finite(child, field_id, depth + 1) for child in children]
        if expression["operator"] == "OR":
            # An OR branch that does not limit this field would allow every place.
            return None if any(item is None for item in sets) else set().union(*sets)
        known = [item for item in sets if item is not None]
        return set.intersection(*known) if known else None
    return None


def policy_scopes(policies) -> dict[str, frozenset[str]]:
    """level -> allowed ids, ALLOW minus DISALLOW, over one user's policies."""
    result = {}
    for level in LEVELS:
        allow, deny = set(), set()
        for policy in policies:
            values = _finite(policy.policy_filter_expression, f"{level}_id")
            if values is None:
                continue
            if policy.policy_type == "ALLOW":
                allow |= values
            elif policy.policy_type == "DISALLOW":
                deny |= values
        if allow - deny:
            result[level] = frozenset(allow - deny)
    return result


def record_place(*sources) -> dict[str, str]:
    """level -> id from the first source that carries any place id."""
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        place = {level: str(source[f"{level}_id"]).strip() for level in LEVELS
                 if source.get(f"{level}_id") and str(source[f"{level}_id"]).strip()}
        if place:
            return place
    return {}


def approver_context(place: Mapping[str, str], candidates, wanted, *, record_label="This record") -> dict:
    """geo_<level>_id and the geo_<level>_approver_ids that `wanted` names."""
    context = {}
    for level in LEVELS:
        if f"geo_{level}_approver_ids" not in wanted:
            continue
        value = place.get(level)
        if not value:
            raise GeoApprovalError(
                f"{record_label} has no {level.title()}, so its {ROLES[level]} cannot be chosen. "
                f"Select the {level} and submit again")
        ids = sorted({candidate.user_id for candidate in candidates
                      if value in candidate.scopes.get(level, ())})
        if not ids:
            raise GeoApprovalError(
                f"No {ROLES[level]} is assigned for {level.title()} {value}. Ask the administrator to give "
                f"a staff member the {ROLES[level]} role and a data policy for this {level}")
        context[f"geo_{level}_id"] = value
        context[f"geo_{level}_approver_ids"] = ids
    return context
