from datetime import date
from typing import Optional

from .admin_area import G2PSchemaAdminArea
from openg2p_registry_core.schemas import (
    G2PRegisterBaseSchema,
    G2PGeoSchema,
    G2PRegisterHistorySchema,
    G2PGeoHistorySchema,
    G2PIntakeFormSchemaBase,
)
from ..models.enums import LivestockStateEnum, SourceSystemEnum, SyncStatusEnum


class G2PSchemaLivestock(G2PSchemaAdminArea):
    fayda_fin: Optional[str] = None
    farmer_reference_status: Optional[str] = None
    identity_verification_id: Optional[str] = None
    last_authentication_status: Optional[str] = None
    last_authenticated_on: Optional[str] = None
    authentication_expiry_date: Optional[str] = None


    farmer_uuid: Optional[str] = None
    farmer_id: Optional[str] = None
    fayda_fan_id: Optional[str] = None
    farmer_name: Optional[str] = None
    oan_id: Optional[str] = None
    secondary_identifier: Optional[str] = None
    registration_date: Optional[date] = None
    status: Optional[str] = None
    state: Optional[LivestockStateEnum] = None
    state_date: Optional[date] = None
    source_system: Optional[SourceSystemEnum] = None
    sync_status: Optional[SyncStatusEnum] = None
    total_animals: Optional[int] = None
    notes: Optional[str] = None
    surveyor_name: Optional[str] = None
    surveyor_mobile_number: Optional[str] = None
    supervisor_name: Optional[str] = None
    supervisor_mobile_number: Optional[str] = None


class G2PRegisterSchemaLivestock(G2PRegisterBaseSchema, G2PGeoSchema, G2PSchemaLivestock):
    """
    Schema for Livestock register.
    Inherits fields from G2PRegisterBaseSchema, G2PGeoSchema.
    Attributes inherited from G2PSchemaLivestock are specific to the Livestock domain.
    """


class G2PRegisterHistorySchemaLivestock(G2PRegisterHistorySchema, G2PGeoHistorySchema, G2PSchemaLivestock):
    """
    Schema for Livestock history.
    Inherits fields from G2PRegisterHistorySchema, G2PGeoHistorySchema.
    """


class G2PIntakeFormSchemaLivestock(G2PIntakeFormSchemaBase, G2PRegisterBaseSchema, G2PGeoSchema, G2PSchemaLivestock):
    """
    Schema for Livestock intake form.
    Inherits fields from G2PRegisterBaseSchema, G2PGeoSchema.
    Attributes inherited from G2PSchemaLivestock are specific to the Livestock domain and are
    included in the intake form schema for data collection.
    """
