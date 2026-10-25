-- Livestock approvals by the record's place (geo_approval/): one stage, the
-- Woreda Approvers of the record's own woreda (Oct 2026 decision). The registry
-- computes them; the rule reads them with "var".
-- Fresh install: existing policies, stages and rules are never changed.
INSERT INTO "public"."approval_policy" ("id","policy_key","version","name","description","status","artifact_type","created_by","forbid_self_approval","forbid_repeat_approvers","created_at","updated_at") VALUES
('798fc939-f130-507b-9d68-a06b1274337e','registry.change_request.livestock',1,'Livestock change request','Woreda approval at the record''s approved place','active','registry.change_request','seed','TRUE','TRUE',NOW(),NOW()),
('b060749c-6cc6-58ab-824c-045d88a3c2a9','registry.change_request.livestock-farmer',1,'Livestock Farmer change request','Woreda approval at the record''s approved place','active','registry.change_request','seed','TRUE','TRUE',NOW(),NOW()),
('4e4b2e97-e623-5547-a973-b2b94dae888d','registry.intake_form.livestock',1,'Livestock intake','Woreda approval at the submitted place','active','registry.intake_form','seed','TRUE','TRUE',NOW(),NOW())
ON CONFLICT ("policy_key","version") DO NOTHING;
