-- The user may declare FIN12 or FAN16. A signed source resolves a missing Farmer ID.
-- Native approval and the approved Farmer dependency still control live activation.
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
     result := result || '{"widget-required":false}'::jsonb;
   ELSIF node->>'widget-data-path' = '997676d3-7008-59f9-b23e-613ad79bbb08.fayda_fan_id' THEN
     result := result || jsonb_build_object('widget-required',false,'widget-data-options','{"actions": [{"action": "require", "condition": {"field": "997676d3-7008-59f9-b23e-613ad79bbb08.fayda_fin", "operator": "empty"}}]}'::jsonb);
   ELSIF node->>'widget-data-path' = '997676d3-7008-59f9-b23e-613ad79bbb08.fayda_fin' THEN
     result := result || jsonb_build_object('widget-required',false,'widget-data-options','{"actions": [{"action": "disable", "condition": {"field": "997676d3-7008-59f9-b23e-613ad79bbb08.last_authentication_status", "operator": "equals", "value": "SUCCESS"}}, {"action": "require", "condition": {"field": "997676d3-7008-59f9-b23e-613ad79bbb08.fayda_fan_id", "operator": "empty"}}]}'::jsonb);
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
   jsonb_path_query(section_ui_schema,'lax $.** ? (@."widget-data-path" == "997676d3-7008-59f9-b23e-613ad79bbb08.fayda_fan_id" || @."widget-data-path" == "997676d3-7008-59f9-b23e-613ad79bbb08.farmer_id")') w
   WHERE w->>'widget-required'='true') THEN
   RAISE EXCEPTION 'FIN-only intake must not require FAN or an unresolved Farmer ID';
 END IF;
END $$;
