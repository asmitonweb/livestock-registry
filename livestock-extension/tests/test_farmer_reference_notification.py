"""Validate the native notification/template contract without trusting demographics."""
import importlib
import json
import os
from pathlib import Path
import sys
import unittest

PACKAGE=os.environ.get('REGISTRY_EXTENSION_MODULE','openg2p_registry_cropsown_extension')
sys.modules.setdefault('openg2p_registry_extensions',importlib.import_module(PACKAGE))
notification=importlib.import_module(PACKAGE+'.farmer_reference.notification')
from openg2p_registry_core.schemas import ChangePayload,ChangeRequestRequestPayload
from jinja2 import Environment


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.event={'event_id':'synthetic-event','event_type':notification.EVENT_TYPE,
            'farmer_id':'FR-000000000001','fayda_fan_id':'0000000000000001'}

    def test_uid16_with_leading_zero_is_preserved(self):
        self.assertEqual(notification.notification_identity(self.event),('FR-000000000001','0000000000000001'))

    def test_invalid_and_demographic_notifications_are_rejected(self):
        for event in ({**self.event,'first_name':'Untrusted'},{**self.event,'event_type':'unrelated'},
            {**self.event,'event_id':''},{**self.event,'event_id':'x'*257},{**self.event,'fayda_fan_id':'000000000001'},
            {**self.event,'fayda_fan_id':1234567890123456}):
            with self.subTest(event=event),self.assertRaises(ValueError): notification.notification_identity(event)

    def test_missing_required_identity_is_rejected(self):
        event=dict(self.event)
        del event['fayda_fan_id']
        with self.assertRaises(ValueError): notification.notification_identity(event)

    def test_update_requires_one_matching_internal_identity(self):
        def payload(rows): return ChangeRequestRequestPayload(internal_record_id='local',change_payload=rows)
        valid=ChangePayload(edit_action='UPDATE',internal_record_id='local',farmer_id=self.event['farmer_id'],fayda_fan_id=self.event['fayda_fan_id'])
        self.assertEqual(notification.notification_change_identity(payload([valid])),('FR-000000000001','0000000000000001'))
        for rows in ([],[valid,valid],[valid.model_copy(update={'edit_action':'ADD'})],
                     [valid.model_copy(update={'internal_record_id':'other'})]):
            with self.subTest(rows=rows),self.assertRaises(ValueError): notification.notification_change_identity(payload(rows))

    def test_enricher_returns_only_pair_and_template_never_emits_demographics(self):
        module=importlib.import_module(PACKAGE+'.ingestion_pipeline.enricher_services.farmer_reference')
        enriched=module.G2PFarmerReferenceNotificationEnricher().enrich(self.event,None)
        self.assertEqual(set(enriched),{'farmer_id','fayda_fan_id'})
        package=Path(importlib.import_module(PACKAGE).__file__).parent
        template=(package/'templates/farmer_reference_update.json.j2').read_text(encoding='utf-8')
        rendered=json.loads(Environment().from_string(template).render(expanded={**enriched,'first_name':'Untrusted','woreda_id':'untrusted'}))
        self.assertEqual(next(iter(rendered.values())),[enriched])

    def test_native_connector_routing_hint_is_bounded_and_never_becomes_profile(self):
        routed = {**self.event, 'register_mnemonic': 'Farmer'}
        self.assertEqual(notification.notification_fields(routed),
                         {'farmer_id': self.event['farmer_id'], 'fayda_fan_id': self.event['fayda_fan_id']})
        self.assertEqual(routed['register_mnemonic'], 'Farmer')
        for value in ('CropSown', '', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                notification.notification_fields({**routed, 'register_mnemonic': value})
        with self.assertRaises(ValueError):
            notification.notification_fields({**routed, 'first_name': 'Untrusted'})

    def test_native_ingest_response_template_preserves_ack_errors_and_encodes_timestamp(self):
        from datetime import datetime
        package=Path(importlib.import_module(PACKAGE).__file__).parent
        template=Environment().from_string((package/'templates/farmer_reference_ingest_response.json.j2').read_text())
        for status,payload,message in [('SUCCESS',{'correlation_id':'synthetic-correlation'},''),
                                       ('ERROR',None,'Invalid "identifier"\nretry')]:
            data={'response_header':{'request_id':'synthetic','response_status':status,
                'response_error_code':'TEST' if status=='ERROR' else '', 'response_error_message':message,
                'response_timestamp':datetime(2026,10,25,12,0)},'response_body':{'response_payload':payload}}
            rendered=json.loads(template.render(expanded=data))
            self.assertEqual(rendered['response_header']['response_status'],status)
            self.assertEqual(rendered['response_header']['response_error_message'],message)
            self.assertIsInstance(rendered['response_header']['response_timestamp'],str)
            self.assertEqual(rendered['response_body']['response_payload'],payload)


if __name__=='__main__':unittest.main()
