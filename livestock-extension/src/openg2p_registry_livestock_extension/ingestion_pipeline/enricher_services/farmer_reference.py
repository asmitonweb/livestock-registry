"""Validate notification identity only; native CR service fetches current source."""
from openg2p_registry_core.interfaces import G2PPayloadEnricherInterface
from ...farmer_reference.notification import notification_fields


class G2PFarmerReferenceNotificationEnricher(G2PPayloadEnricherInterface):
    def enrich(self, data, session):
        identity = notification_fields(data)
        # Never expand notification demographics into trusted local values.
        return identity
