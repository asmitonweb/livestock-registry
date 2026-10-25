-- Every Livestock and Farmer change request and every Livestock intake goes
-- through AWE to the Woreda Approvers of its woreda (geo_approval/). The Farmer identity section
-- keeps its own Farmer reference binding (zzz_farmer_reference_awe.sql).
INSERT INTO g2p_registry_awe_policy_configurations
(awe_policy_config_id,policy_scope,register_id,intake_form_id,section_id,policy_type,policy_key,context_field_names) VALUES
('c3fd39f4-7dbc-5358-8d84-edd8ba780125','REGISTER','997676d3-7008-59f9-b23e-613ad79bbb08',NULL,NULL,'registry.change_request','registry.change_request.livestock','["geo_woreda_id","geo_woreda_approver_ids"]'),
('18eedc19-cb83-50d1-bc34-c1b03ee51815','REGISTER','f9c6a359-9563-5a43-b0fe-6c7e452037a3',NULL,NULL,'registry.change_request','registry.change_request.livestock-farmer','["geo_woreda_id","geo_woreda_approver_ids"]'),
('506574e4-701d-5e8e-84db-545698ef007a','INTAKE_FORM','997676d3-7008-59f9-b23e-613ad79bbb08','e92e8be1-207f-518e-99c2-8bc21cc1f112',NULL,'registry.intake_form','registry.intake_form.livestock','["geo_woreda_id","geo_woreda_approver_ids"]')
ON CONFLICT (awe_policy_config_id) DO NOTHING;
