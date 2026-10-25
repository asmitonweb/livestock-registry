INSERT INTO "public"."approver_rule" ("id","stage_id","rule_type","rule_value","kind","required","created_at","updated_at")
SELECT v.id, s.id, 'expression', v.rule_value::json, 'approver', FALSE, NOW(), NOW()
FROM (VALUES
    ('4b587582-9f3a-59e9-a063-4f5426b5c533','registry.change_request.livestock',1,'{"logic": {"var": "geo_woreda_approver_ids"}}'),
    ('5a7acdf3-013b-576f-b7e0-a8b5027f95bc','registry.change_request.livestock-farmer',1,'{"logic": {"var": "geo_woreda_approver_ids"}}'),
    ('7cdbb339-5a89-5120-83df-e8f1f384f4dd','registry.intake_form.livestock',1,'{"logic": {"var": "geo_woreda_approver_ids"}}')
) AS v(id, policy_key, stage_order, rule_value)
INNER JOIN approval_policy p ON p.policy_key = v.policy_key AND p.version = 1
INNER JOIN approval_stage s ON s.policy_id = p.id AND s.stage_order = v.stage_order
WHERE NOT EXISTS (SELECT 1 FROM approver_rule r WHERE r.stage_id = s.id)
ON CONFLICT ("id") DO NOTHING;
