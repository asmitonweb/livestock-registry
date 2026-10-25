"""Approver routing by place: policy scopes, level fallback, explained refusals."""
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

_spec = importlib.util.spec_from_file_location("geo_scopes", Path(__file__).parents[1] / "src" /
    "openg2p_registry_livestock_extension" / "geo_approval" / "scopes.py")
scopes = sys.modules["geo_scopes"] = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scopes)

WANTED = {"geo_woreda_approver_ids", "geo_zone_approver_ids"}
PLACE = {"region": "region-ET04", "zone": "zone-ET0407", "woreda": "woreda-ET040701"}


def condition(field, *values):
    if len(values) == 1:
        return {"type": "CONDITION", "field_id": field, "operator": "eq", "value": values[0]}
    return {"type": "CONDITION", "field_id": field, "operator": "in", "values": list(values)}


def policy(expression, policy_type="ALLOW"):
    return SimpleNamespace(policy_filter_expression=expression, policy_type=policy_type)


def candidate(user, **scope):
    return scopes.Candidate(user, {level: frozenset(values) for level, values in scope.items()})


class PolicyScopeTests(unittest.TestCase):
    def test_level_follows_the_policy_field(self):
        self.assertEqual(scopes.policy_scopes([policy(condition("woreda_id", "w1", "w2")),
                                               policy(condition("zone_id", "z1"))]),
                         {"woreda": {"w1", "w2"}, "zone": {"z1"}})

    def test_disallow_removes_places(self):
        self.assertEqual(scopes.policy_scopes([policy(condition("woreda_id", "w1", "w2")),
                                               policy(condition("woreda_id", "w2"), "DISALLOW")]),
                         {"woreda": {"w1"}})

    def test_or_with_an_unlimited_branch_gives_no_scope(self):
        tree = {"type": "GROUP", "operator": "OR",
                "children": [condition("woreda_id", "w1"), condition("gender", "F")]}
        self.assertEqual(scopes.policy_scopes([policy(tree)]), {})

    def test_and_narrows_by_the_field_it_limits(self):
        tree = {"type": "GROUP", "operator": "AND",
                "children": [condition("woreda_id", "w1", "w2"), condition("woreda_id", "w2", "w3"),
                             condition("gender", "F")]}
        self.assertEqual(scopes.policy_scopes([policy(tree)]), {"woreda": {"w2"}})

    def test_unknown_filters_give_no_scope(self):
        for tree in (None, {"type": "CONDITION", "field_id": "woreda_id", "operator": "like", "value": "w%"},
                     condition("woreda_id", " "), {"type": "GROUP", "operator": "NOT", "children": []}):
            self.assertEqual(scopes.policy_scopes([policy(tree)]), {})


class RecordPlaceTests(unittest.TestCase):
    def test_first_source_with_a_place_wins(self):
        self.assertEqual(scopes.record_place(None, {"name": "x"}, {"woreda_id": " w1 ", "zone_id": ""},
                                             {"woreda_id": "w9"}), {"woreda": "w1"})

    def test_no_place(self):
        self.assertEqual(scopes.record_place({}, None), {})


class ApproverContextTests(unittest.TestCase):
    def test_woreda_stage_gets_the_woredas_approvers_only(self):
        context = scopes.approver_context(PLACE, (candidate("w.officer", woreda={PLACE["woreda"]}),
                                                  candidate("z.officer", zone={PLACE["zone"]}),
                                                  candidate("other", woreda={"woreda-X"})), {"geo_woreda_approver_ids"})
        self.assertEqual(context, {"geo_woreda_id": PLACE["woreda"], "geo_woreda_approver_ids": ["w.officer"]})

    def test_each_wanted_level_is_resolved(self):
        context = scopes.approver_context(PLACE, (candidate("w", woreda={PLACE["woreda"]}),
                                                  candidate("z", zone={PLACE["zone"]})), WANTED)
        self.assertEqual(context["geo_zone_approver_ids"], ["z"])

    def test_no_fallback_to_a_higher_level(self):
        with self.assertRaises(scopes.GeoApprovalError) as raised:
            scopes.approver_context(PLACE, (candidate("z", zone={PLACE["zone"]}),), {"geo_woreda_approver_ids"})
        message = str(raised.exception)
        self.assertIn("No Woreda Approver is assigned for Woreda woreda-ET040701", message)
        self.assertIn("Ask the administrator to give a staff member the Woreda Approver role", message)

    def test_missing_level_is_explained(self):
        with self.assertRaises(scopes.GeoApprovalError) as raised:
            scopes.approver_context({"zone": "z1"}, (), {"geo_woreda_approver_ids"}, record_label="This submission")
        self.assertIn("This submission has no Woreda, so its Woreda Approver cannot be chosen", str(raised.exception))
        self.assertIn("Select the woreda", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
