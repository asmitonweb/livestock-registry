-- Optional verification or editable unverified FIN fallback. UID/FAN16 remains distinct.
CREATE OR REPLACE FUNCTION pg_temp.oan_popup_identity(node jsonb)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE result jsonb;
BEGIN
  IF jsonb_typeof(node)='array' THEN
    SELECT COALESCE(jsonb_agg(pg_temp.oan_popup_identity(value) ORDER BY ord),'[]'::jsonb)
      INTO result FROM jsonb_array_elements(node) WITH ORDINALITY AS a(value,ord);
  ELSIF jsonb_typeof(node)='object' THEN
    SELECT COALESCE(jsonb_object_agg(key,pg_temp.oan_popup_identity(value)),'{}'::jsonb)
      INTO result FROM jsonb_each(node);
    IF node->>'widget-data-path' = '997676d3-7008-59f9-b23e-613ad79bbb08.foundational_id' THEN
      result := result || '{"widget":"display","widget-type":"display","widget-readonly":true,"widget-required":false}'::jsonb;
    END IF;
    IF jsonb_typeof(node->'widgets')='array'
       AND (node->'widgets')::text LIKE '%997676d3-7008-59f9-b23e-613ad79bbb08.farmer_id%'
       AND NOT jsonb_path_exists(node->'widgets','lax $.** ? (@."widget-id" == "fayda_fin")') THEN
      result := jsonb_set(result,'{widgets}',result->'widgets' || jsonb_build_array('{"widget": "text", "widget-id": "fayda_fin", "widget-type": "input", "widget-label": "Fayda FIN (12 digits, optional)", "widget-readonly": false, "widget-required": false, "widget-data-path": "997676d3-7008-59f9-b23e-613ad79bbb08.fayda_fin", "widget-data-placeholder": "Enter FIN only for manual fallback; Verify with Fayda fills it automatically", "widget-data-validation": {"pattern": "^[0-9]{12}$", "patternMessage": "Fayda FIN: enter exactly 12 digits, or use the separate 16-digit FAN field. Upload ID evidence for approval."}, "widget-data-options": {"action": "disable", "condition": {"field": "997676d3-7008-59f9-b23e-613ad79bbb08.last_authentication_status", "operator": "equals", "value": "SUCCESS"}}}'::jsonb));
    END IF;
    IF jsonb_typeof(node->'widgets')='array'
       AND (node->'widgets')::text LIKE '%997676d3-7008-59f9-b23e-613ad79bbb08.farmer_id%'
       AND NOT jsonb_path_exists(node->'widgets','lax $.** ? (@."widget" == "oan-identity-verification")') THEN
      result := jsonb_set(result,'{widgets}',result->'widgets' || jsonb_build_array('{"widget": "oan-identity-verification", "widget-id": "identity_verification", "widget-type": "input", "widget-label": "Fayda verification", "widget-readonly": false, "widget-required": false, "widget-data-path": {"foundationalId": "997676d3-7008-59f9-b23e-613ad79bbb08.fayda_fin", "verificationId": "997676d3-7008-59f9-b23e-613ad79bbb08.identity_verification_id", "status": "997676d3-7008-59f9-b23e-613ad79bbb08.last_authentication_status", "verifiedOn": "997676d3-7008-59f9-b23e-613ad79bbb08.last_authenticated_on", "expiresOn": "997676d3-7008-59f9-b23e-613ad79bbb08.authentication_expiry_date"}, "widget-data-options": {"registerId": "997676d3-7008-59f9-b23e-613ad79bbb08", "fieldPrefix": "997676d3-7008-59f9-b23e-613ad79bbb08", "foundationalIdPath": "997676d3-7008-59f9-b23e-613ad79bbb08.fayda_fin", "internalRecordIdPath": "997676d3-7008-59f9-b23e-613ad79bbb08.internal_record_id"}}'::jsonb));
    END IF;
  ELSE result:=node;
  END IF;
  RETURN result;
END $$;
UPDATE g2p_register_sections SET section_ui_schema=pg_temp.oan_popup_identity(section_ui_schema)
WHERE section_ui_schema IS NOT NULL;
-- Native flag enables the Verify action; it does not require verification at finalize.
UPDATE g2p_register_definitions SET requires_registrant_authentication=true
WHERE register_id='997676d3-7008-59f9-b23e-613ad79bbb08';
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM g2p_register_sections WHERE
   jsonb_path_exists(section_ui_schema,'lax $.** ? (@."widget" == "oan-identity-verification")')) THEN
   RAISE EXCEPTION 'Popup-only Fayda widget is missing from domain identity metadata';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM g2p_register_sections,
   jsonb_path_query(section_ui_schema,'lax $.** ? (@."widget-id" == "fayda_fin")') w
   WHERE w->>'widget-data-path' = '997676d3-7008-59f9-b23e-613ad79bbb08.fayda_fin'
     AND w->>'widget' = 'text' AND w->>'widget-readonly' = 'false'
     AND w->>'widget-required' = 'false'
     AND (w->'widget-data-options'->'condition'->>'value' = 'SUCCESS' OR jsonb_path_exists(w, 'lax $."widget-data-options".actions[*] ? (@.action == "disable" && @.condition.value == "SUCCESS")'))) THEN
   RAISE EXCEPTION 'Optional manual FIN fallback with the SUCCESS lock is missing';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM g2p_register_definitions WHERE register_id='997676d3-7008-59f9-b23e-613ad79bbb08' AND requires_registrant_authentication) THEN
   RAISE EXCEPTION 'Fayda Verify must be enabled; this flag is not a finalize verification gate';
 END IF;
END $$;
