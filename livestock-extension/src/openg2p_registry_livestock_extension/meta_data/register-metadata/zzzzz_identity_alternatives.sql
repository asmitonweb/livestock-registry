-- The intake asks for the Farmer ID and one Fayda ID (FIN12 or FAN16, or Verify
-- with Fayda); everything else about the farmer comes from the Farmer Registry.
-- "One of FIN or FAN" is checked by the field rules (Next and submit), not shown
-- as two required fields. Native approval and the approved Farmer dependency
-- still control live activation.
CREATE OR REPLACE FUNCTION pg_temp.oan_identity_alternatives(node jsonb)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE result jsonb;
BEGIN
 IF jsonb_typeof(node)='array' THEN
   SELECT COALESCE(jsonb_agg(pg_temp.oan_identity_alternatives(value) ORDER BY ord),'[]'::jsonb)
     INTO result FROM jsonb_array_elements(node) WITH ORDINALITY AS a(value,ord);
 ELSIF jsonb_typeof(node)='object' THEN
   SELECT COALESCE(jsonb_object_agg(key,pg_temp.oan_identity_alternatives(value)),'{}'::jsonb)
     INTO result FROM jsonb_each(node);
   IF node->>'widget-data-path' = '997676d3-7008-59f9-b23e-613ad79bbb08.farmer_id' THEN
     result := result || '{"widget-required":true,"widget-label":"Farmer ID","widget-data-placeholder":"FR- followed by 12 digits"}'::jsonb;
   ELSIF node->>'widget-data-path' = '997676d3-7008-59f9-b23e-613ad79bbb08.farmer_name' THEN
     result := (result - 'widget-data-validation') || '{"widget":"display","widget-type":"display","widget-readonly":true,"widget-required":false,"widget-label":"Farmer name (from the Farmer Registry)"}'::jsonb;
   ELSIF node->>'widget-data-path' = '997676d3-7008-59f9-b23e-613ad79bbb08.fayda_fan_id' THEN
     result := (result - 'widget-data-options') || '{"widget-required":false,"widget-label":"Fayda FAN (16 digits)","widget-data-placeholder":"Or enter the 16-digit FAN"}'::jsonb;
   ELSIF node->>'widget-data-path' = '997676d3-7008-59f9-b23e-613ad79bbb08.fayda_fin' THEN
     result := result || jsonb_build_object('widget-required',false,'widget-label','Fayda FIN (12 digits)','widget-data-placeholder','FIN, or use Verify with Fayda','widget-data-options','{"actions": [{"action": "disable", "condition": {"field": "997676d3-7008-59f9-b23e-613ad79bbb08.last_authentication_status", "operator": "equals", "value": "SUCCESS"}}]}'::jsonb);
   ELSIF node->>'widget-data-path' = 'f9c6a359-9563-5a43-b0fe-6c7e452037a3.fayda_fan_id' THEN
     result := result || '{"widget-required":false}'::jsonb;
   END IF;
   IF jsonb_typeof(node->'widgets')='array' AND (node->'widgets')::text LIKE '%997676d3-7008-59f9-b23e-613ad79bbb08.farmer_id%'
      AND NOT jsonb_path_exists(node->'widgets','lax $.** ? (@."widget-id" == "farmer_reference_status")') THEN
     result := jsonb_set(result,'{widgets}',result->'widgets' || jsonb_build_array('{"widget": "display", "widget-id": "farmer_reference_status", "widget-type": "display", "widget-label": "Farmer reference", "widget-readonly": true, "widget-required": false, "widget-data-path": "997676d3-7008-59f9-b23e-613ad79bbb08.farmer_reference_status"}'::jsonb));
   END IF;
   IF jsonb_typeof(node->'widgets')='array' AND (node->'widgets')::text LIKE '%f9c6a359-9563-5a43-b0fe-6c7e452037a3.farmer_id%'
      AND NOT jsonb_path_exists(node->'widgets','lax $.** ? (@."widget-data-path" == "f9c6a359-9563-5a43-b0fe-6c7e452037a3.fayda_fin")') THEN
     result := jsonb_set(result,'{widgets}',result->'widgets' || jsonb_build_array('{"widget": "display", "widget-id": "farmer_reference_fin", "widget-type": "display", "widget-label": "Fayda FIN", "widget-readonly": true, "widget-required": false, "widget-data-path": "f9c6a359-9563-5a43-b0fe-6c7e452037a3.fayda_fin"}'::jsonb));
   END IF;
 ELSE result:=node;
 END IF;
 RETURN result;
END $$;
UPDATE g2p_register_sections SET section_ui_schema=pg_temp.oan_identity_alternatives(section_ui_schema)
WHERE section_ui_schema IS NOT NULL;
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM g2p_register_sections,
   jsonb_path_query(section_ui_schema,'lax $.** ? (@."widget-data-path" == "997676d3-7008-59f9-b23e-613ad79bbb08.fayda_fan_id" || @."widget-data-path" == "997676d3-7008-59f9-b23e-613ad79bbb08.fayda_fin")') w
   WHERE w->>'widget-required'='true' OR jsonb_path_exists(w,'lax $."widget-data-options".actions[*] ? (@.action == "require")')) THEN
   RAISE EXCEPTION 'FIN and FAN are alternatives: neither may be marked required';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM g2p_register_sections,
   jsonb_path_query(section_ui_schema,'lax $.** ? (@."widget-data-path" == "997676d3-7008-59f9-b23e-613ad79bbb08.farmer_id")') w
   WHERE w->>'widget-required'='true') THEN
   RAISE EXCEPTION 'The intake must require the Farmer ID';
 END IF;
END $$;
