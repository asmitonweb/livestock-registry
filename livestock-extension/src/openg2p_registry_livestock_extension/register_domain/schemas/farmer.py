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


class G2PSchemaFarmer(G2PSchemaAdminArea):

    farmer_id: Optional[str] = None
    fayda_fan_id: Optional[str] = None
    fayda_fin: Optional[str] = None
    farmer_name: Optional[str] = None
    first_name: Optional[str] = None
    middle_name: Optional[str] = None
    last_name: Optional[str] = None
    first_name_amh: Optional[str] = None
    middle_name_amh: Optional[str] = None
    last_name_amh: Optional[str] = None
    first_name_other: Optional[str] = None
    middle_name_other: Optional[str] = None
    last_name_other: Optional[str] = None
    gender: Optional[str] = None
    date_of_birth: Optional[date] = None
    mobile_number: Optional[str] = None
    email: Optional[str] = None
    address_line_1: Optional[str] = None
    address_line_2: Optional[str] = None
    region_name: Optional[str] = None
    zone_name: Optional[str] = None
    woreda_name: Optional[str] = None
    kebele_name: Optional[str] = None
    registration_date: Optional[date] = None
    status: Optional[str] = None


class G2PRegisterSchemaFarmer(G2PRegisterBaseSchema, G2PGeoSchema, G2PSchemaFarmer):
    """
    Schema for Farmer register.
    Inherits fields from G2PRegisterBaseSchema, G2PGeoSchema.
    Attributes inherited from G2PSchemaFarmer are specific to the Farmer domain.
    """


class G2PRegisterHistorySchemaFarmer(G2PRegisterHistorySchema, G2PGeoHistorySchema, G2PSchemaFarmer):
    """
    Schema for Farmer history.
    Inherits fields from G2PRegisterHistorySchema, G2PGeoHistorySchema.
    """


class G2PIntakeFormSchemaFarmer(G2PIntakeFormSchemaBase, G2PRegisterBaseSchema, G2PGeoSchema, G2PSchemaFarmer):
    """
    Schema for Farmer intake form.
    Inherits fields from G2PRegisterBaseSchema, G2PGeoSchema.
    Attributes inherited from G2PSchemaFarmer are specific to the Farmer domain and are
    included in the intake form schema for data collection.
    """
