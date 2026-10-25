-- Intake-only Farmer Identity. Keep the registered Livestock detail section,
-- its Farmer Name and its stable section/mnemonic unchanged.
-- Run after popup/identity patches; repeated/full metadata replays are idempotent.
CREATE OR REPLACE FUNCTION pg_temp.oan_widgets(ui jsonb, ids text[])
RETURNS jsonb LANGUAGE sql AS $$
  SELECT COALESCE(jsonb_agg(w ORDER BY array_position(ids, w->>'widget-id')), '[]'::jsonb)
  FROM jsonb_path_query(ui, 'lax $.**') w
  WHERE jsonb_typeof(w) = 'object' AND w ? 'widget' AND w->>'widget-id' = ANY(ids)
$$;

INSERT INTO g2p_register_sections (register_id, section_id, section_register_id, is_core_section,
    section_mnemonic, section_description, documents_required, no_of_verifications_required, is_list,
    section_weightage, cr_auto_approve_for_bene_portal, cr_auto_approve_for_agent_portal,
    cr_auto_approve_for_staff_portal, cr_auto_approve_for_partner, section_ui_schema)
SELECT register_id, 'livestock_intake_identity_section_01', section_register_id, is_core_section,
    'ls_intake_identity', 'Farmer Identity', documents_required, no_of_verifications_required, is_list,
    section_weightage, cr_auto_approve_for_bene_portal, cr_auto_approve_for_agent_portal,
    cr_auto_approve_for_staff_portal, cr_auto_approve_for_partner,
    jsonb_build_object('section-id', 'livestock_intake_identity_section_01',
      'section-title', 'Farmer Identity', 'section-editable', true, 'section-required-for-submit', true,
      'panels', jsonb_build_array(jsonb_build_object(
        'panel-id', 'horizontal_panel_ls_intake_identity', 'panel-orientation', 'horizontal',
        'panels', jsonb_build_array(
          jsonb_build_object('panel-id', 'vertical_panel_ls_intake_identity_ids',
            'panel-orientation', 'vertical',
            'widgets', pg_temp.oan_widgets(section_ui_schema, ARRAY['farmer_id','fayda_fin','fayda_fan_id'])),
          jsonb_build_object('panel-id', 'vertical_panel_ls_intake_identity_verification',
            'panel-orientation', 'vertical',
            'widgets', pg_temp.oan_widgets(section_ui_schema, ARRAY['identity_verification','farmer_reference_status']))))))
FROM g2p_register_sections WHERE section_id = 'livestock_livestock_record_section_01'
ON CONFLICT (section_id) DO UPDATE SET
  section_mnemonic = EXCLUDED.section_mnemonic,
  section_description = EXCLUDED.section_description,
  section_ui_schema = EXCLUDED.section_ui_schema;

UPDATE g2p_intake_form_ui_tab_sections
SET section_id = 'livestock_intake_identity_section_01'
WHERE section_id = 'livestock_livestock_record_section_01';

UPDATE g2p_intake_form_ui_tab_sections ts SET section_order = ordered.position
FROM (
  SELECT x.tab_section_id, row_number() OVER (PARTITION BY x.tab_id
    ORDER BY x.section_order, x.section_id) AS position
  FROM g2p_intake_form_ui_tab_sections x
  WHERE EXISTS (SELECT 1 FROM g2p_intake_form_ui_tab_sections ident
    WHERE ident.tab_id = x.tab_id AND ident.section_id = 'livestock_intake_identity_section_01')
) ordered
WHERE ts.tab_section_id = ordered.tab_section_id AND ts.section_order <> ordered.position;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM g2p_register_sections
    WHERE section_id = 'livestock_intake_identity_section_01' AND section_mnemonic = 'ls_intake_identity') THEN
    RAISE EXCEPTION 'The Livestock intake must have its own Farmer Identity section';
  END IF;
  IF EXISTS (SELECT 1 FROM g2p_register_sections s,
      jsonb_path_query(s.section_ui_schema, 'lax $.** ? (exists (@."widget"))') w
    WHERE s.section_id = 'livestock_intake_identity_section_01'
      AND w->>'widget-id' NOT IN ('farmer_id','fayda_fin','fayda_fan_id',
                                  'identity_verification','farmer_reference_status')) THEN
    RAISE EXCEPTION 'Farmer Identity on an intake may hold only Farmer ID, FIN, FAN and Fayda verification';
  END IF;
  IF (SELECT count(DISTINCT w->>'widget-id') FROM g2p_register_sections s,
      jsonb_path_query(s.section_ui_schema, 'lax $.** ? (exists (@."widget"))') w
      WHERE s.section_id = 'livestock_intake_identity_section_01') <> 5 THEN
    RAISE EXCEPTION 'The intake identity section must contain all five identity/status widgets';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM g2p_intake_form_ui_tab_sections
      WHERE section_id = 'livestock_intake_identity_section_01')
    OR EXISTS (SELECT 1 FROM g2p_intake_form_ui_tab_sections
      WHERE section_id = 'livestock_livestock_record_section_01') THEN
    RAISE EXCEPTION 'Livestock intake bindings must use the intake-only Farmer Identity section';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM g2p_register_ui_tab_sections
      WHERE section_id = 'livestock_livestock_record_section_01') THEN
    RAISE EXCEPTION 'The registered Livestock detail section must stay bound';
  END IF;
END $$;
