# ruff: noqa: E402
"""Domain initialization using the upstream Farmer 1.2 extension pattern."""
import asyncio
from .config import Settings
_config = Settings.get_config()
from openg2p_fastapi_common.app import Initializer as BaseInitializer
from openg2p_fastapi_common.context import dbengine
from openg2p_registry_core.app import Initializer as CoreInitializer
from .register_domain import models, services
from .farmer_reference.models import REFERENCE_MODELS
from .farmer_reference.runtime import install

MODEL_NAMES = ['G2PRegisterFarmer', 'G2PRegisterHistoryFarmer', 'G2PIntakeFormFarmer', 'G2PRegisterLivestock', 'G2PRegisterHistoryLivestock', 'G2PIntakeFormLivestock', 'G2PRegisterAnimal', 'G2PRegisterHistoryAnimal', 'G2PIntakeFormAnimal', 'G2PRegisterHealthEvent', 'G2PRegisterHistoryHealthEvent', 'G2PIntakeFormHealthEvent', 'G2PRegisterVaccination', 'G2PRegisterHistoryVaccination', 'G2PIntakeFormVaccination', 'G2PRegisterVitalEvent', 'G2PRegisterHistoryVitalEvent', 'G2PIntakeFormVitalEvent', 'G2PRegisterBreeding', 'G2PRegisterHistoryBreeding', 'G2PIntakeFormBreeding', 'G2PRegisterRetagging', 'G2PRegisterHistoryRetagging', 'G2PIntakeFormRetagging', 'G2PRegisterVaccineSchedule', 'G2PRegisterHistoryVaccineSchedule', 'G2PIntakeFormVaccineSchedule', 'G2PRegisterImportBatch', 'G2PRegisterHistoryImportBatch', 'G2PIntakeFormImportBatch', 'G2PRegisterAuditLog', 'G2PRegisterHistoryAuditLog', 'G2PIntakeFormAuditLog', 'G2PLivestockSpeciesConfig']
SERVICE_NAMES = ['G2PRegisterDomainServiceAnimal', 'G2PRegisterDomainServiceAuditLog', 'G2PRegisterDomainServiceBreeding', 'G2PRegisterDomainServiceFarmer', 'G2PRegisterDomainServiceHealthEvent', 'G2PRegisterDomainServiceImportBatch', 'G2PRegisterDomainServiceLivestock', 'G2PRegisterDomainServiceRetagging', 'G2PRegisterDomainServiceVaccination', 'G2PRegisterDomainServiceVaccineSchedule', 'G2PRegisterDomainServiceVitalEvent']


class Initializer(BaseInitializer):
    def initialize(self, **kwargs):
        super().initialize()
        CoreInitializer().initialize()
        for name in SERVICE_NAMES:
            getattr(services, name)()
        install()
        from .geo_approval.service import install as install_geo_approval
        install_geo_approval()
        from .register_domain.services.identity_verification import install_identity_verification
        install_identity_verification()

    async def fastapi_app_startup(self, app):
        await super().fastapi_app_startup(app)
        from importlib.util import find_spec
        if find_spec("openg2p_registry_staff_api"):
            from .controllers import OANIdentityVerificationController
            OANIdentityVerificationController().post_init()

    def migrate_database(self, args):
        async def migrate():
            # Native table creation followed by add-only mapped columns.
            # Existing types/constraints/data are never altered or dropped.
            for name in MODEL_NAMES:
                await getattr(models, name).create_migrate()
            for model in REFERENCE_MODELS:
                await model.create_migrate()
            from .schema_migration import add_missing_mapped_columns
            await add_missing_mapped_columns(dbengine.get(),
                [getattr(models,name) for name in MODEL_NAMES]+list(REFERENCE_MODELS))
        # OAN Farmer's migration-loop boundary: don't reuse connections from
        # the core migration's already-closed event loop.
        dbengine.get().sync_engine.dispose(close=False)
        asyncio.run(migrate())
