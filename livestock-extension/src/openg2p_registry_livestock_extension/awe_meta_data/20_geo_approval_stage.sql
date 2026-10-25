INSERT INTO "public"."approval_stage" ("id","policy_id","stage_order","name","mode","mode_value","sla_hours","parallel_group","skip_if","on_empty","on_breach","escalation_rules_json","created_at","updated_at")
SELECT v.id, p.id, v.stage_order, v.name, 'any-n', 1, CAST(NULL AS integer), CAST(NULL AS integer),
       'null', 'block', CAST(NULL AS integer), 'null', NOW(), NOW()
FROM (VALUES
    ('b8ead924-b0e2-562d-9b56-906c5569dbc4','registry.change_request.livestock',1,'Woreda approval'),
    ('47374868-c8d7-52d4-a58e-718f50bce6d5','registry.change_request.livestock-farmer',1,'Woreda approval'),
    ('1bcf4451-0e90-59c1-a074-92d32afedc1e','registry.intake_form.livestock',1,'Woreda approval')
) AS v(id, policy_key, stage_order, name)
INNER JOIN approval_policy p ON p.policy_key = v.policy_key AND p.version = 1
WHERE NOT EXISTS (SELECT 1 FROM approval_stage s WHERE s.policy_id = p.id AND s.stage_order = v.stage_order)
ON CONFLICT ("id") DO NOTHING;
