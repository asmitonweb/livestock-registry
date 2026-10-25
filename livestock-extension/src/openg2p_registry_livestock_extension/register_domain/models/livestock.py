"""LIVESTOCK — the root register and the hub of the model.

One record per farmer's livestock holding. Every livestock line (animals, health
events, vaccinations, vital events, breeding, vaccine schedule, import batches, audit
log) links straight back to this record, mirroring the `line_ids` / `*_event_ids`
one2many fields on `g2p.livestock.registry`.

The farmer is **identified, not owned**: the Farmer Registry is the system of record,
so this carries their identifiers and mirrors the Fayda FAN into the platform's
`link_foundational_id`. `oan_id` is the OAN identifier generated from the farmer code
and first name — see the id_generator service.
"""

from openg2p_registry_core.models.g2p_intake_form import G2PIntakeForm
from openg2p_registry_core.models import (
    G2PRegister, G2PRegisterHistory, G2PGeo, G2PGeoHistory
)
from sqlalchemy import Date, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .admin_area import G2PAdminArea
from ..services import G2PRegisterDomainServiceLivestock
from .enums import LivestockStateEnum, SourceSystemEnum, SyncStatusEnum


class G2PLivestock(G2PAdminArea):
    # Popup FIN evidence, distinct from the approved reference UID/FAN16.
    fayda_fin: Mapped[str] = mapped_column(String, nullable=True)
    farmer_reference_status: Mapped[str] = mapped_column(String, nullable=True)
    identity_verification_id: Mapped[str] = mapped_column(String, nullable=True)
    last_authentication_status: Mapped[str] = mapped_column(String, nullable=True)
    last_authenticated_on: Mapped[str] = mapped_column(String, nullable=True)
    authentication_expiry_date: Mapped[str] = mapped_column(String, nullable=True)


    # Farmer identity, carried not owned.
    farmer_uuid: Mapped[str] = mapped_column(String, nullable=True)
    farmer_id: Mapped[str] = mapped_column(String, nullable=True)
    fayda_fan_id: Mapped[str] = mapped_column(String, nullable=True)
    farmer_name: Mapped[str] = mapped_column(String, nullable=True)

    # Holding identity.
    oan_id: Mapped[str] = mapped_column(String, nullable=True)
    secondary_identifier: Mapped[str] = mapped_column(String, nullable=True)

    registration_date: Mapped[str] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=True)  # Attribute lookup (APPROVAL_STATUS)
    state: Mapped[LivestockStateEnum] = mapped_column(String, nullable=True)  # LivestockStateEnum
    state_date: Mapped[str] = mapped_column(Date, nullable=True)

    source_system: Mapped[SourceSystemEnum] = mapped_column(String, nullable=True)  # SourceSystemEnum
    sync_status: Mapped[SyncStatusEnum] = mapped_column(String, nullable=True)  # SyncStatusEnum

    total_animals: Mapped[int] = mapped_column(Integer, nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=True)

    # Survey personnel, as collected in the field.
    surveyor_name: Mapped[str] = mapped_column(String, nullable=True)
    surveyor_mobile_number: Mapped[str] = mapped_column(String, nullable=True)
    supervisor_name: Mapped[str] = mapped_column(String, nullable=True)
    supervisor_mobile_number: Mapped[str] = mapped_column(String, nullable=True)


# All Register classes should have the prefix G2PRegister
class G2PRegisterLivestock(G2PRegister, G2PGeo, G2PLivestock):
    __tablename__ = "g2p_register_livestocks"

    def get_search_text_fields(self) -> str:
        """Return livestock fields used to build search_text."""
        return G2PRegisterDomainServiceLivestock().construct_search_text(self.to_dict())

    def get_record_name_fields(self) -> str:
        """Return livestock record_name from domain service implementation."""
        return G2PRegisterDomainServiceLivestock().construct_record_name(self.to_dict())


# All Register History classes should have the prefix G2PRegisterHistory
class G2PRegisterHistoryLivestock(G2PRegisterHistory, G2PGeoHistory, G2PLivestock):
    __tablename__ = "g2p_register_history_livestocks"


# All Intake Form classes should have the prefix G2PIntakeForm
class G2PIntakeFormLivestock(G2PIntakeForm, G2PRegister, G2PGeo, G2PLivestock):
    __tablename__ = "g2p_intake_form_livestocks"

    def get_search_text_fields(self) -> str:
        """Return livestock fields used to build search_text."""
        return G2PRegisterDomainServiceLivestock().construct_search_text(self.to_dict())

    def get_record_name_fields(self) -> str:
        """Return livestock record_name from domain service implementation."""
        return G2PRegisterDomainServiceLivestock().construct_record_name(self.to_dict())
