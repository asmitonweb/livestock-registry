"""ODK Ingestion Hooks for OpenG2P Livestock Registry.

Intercepts ingestion requests, ensures database connections, transforms ODK Central payloads,
wraps payloads into OpenG2P standard envelopes, and ensures complete storage into
both Intake Forms and primary Livestock Registry tables without datetime serialization errors.
"""
import json
import logging
import os
import sys
import uuid
from contextvars import ContextVar
import threading
_odk_local = threading.local()
from datetime import date, datetime
from jinja2 import Template

# Alias openg2p_registry_extensions so Celery worker / platform packages can import extension settings
try:
    import openg2p_registry_livestock_extension
    sys.modules["openg2p_registry_extensions"] = openg2p_registry_livestock_extension
    import openg2p_registry_livestock_extension.config
    sys.modules["openg2p_registry_extensions.config"] = openg2p_registry_livestock_extension.config
    import openg2p_registry_livestock_extension.register_domain
    sys.modules["openg2p_registry_extensions.register_domain"] = openg2p_registry_livestock_extension.register_domain
except Exception:
    pass

try:
    from openg2p_fastapi_common.models import BaseORMModel
    from sqlalchemy import Column, String, Boolean
    import openg2p_registry_core.models as core_models

    class G2PAttributeValueSpeciesConfig(BaseORMModel):
        __tablename__ = "g2p_attribute_value_species_configs"
        __table_args__ = {"extend_existing": True}
        value_id = Column(String, primary_key=True)
        requires_ear_tag = Column(Boolean, nullable=True)
        is_flock_species = Column(Boolean, nullable=True)

    core_models.G2PAttributeValueSpeciesConfig = G2PAttributeValueSpeciesConfig
    sys.modules["openg2p_registry_core.models"].G2PAttributeValueSpeciesConfig = G2PAttributeValueSpeciesConfig
except Exception:
    pass

try:
    from openg2p_registry_core.helpers.template_helper import TemplateHelper
    if TemplateHelper.get_component() is None:
        TemplateHelper()
except Exception:
    pass

_logger = logging.getLogger("openg2p.odk_ingest_hooks")
_active_session: ContextVar = ContextVar("odk_active_session", default=None)
_registry_engine = None


def _make_json_serializable(obj):
    """Recursively convert dates/datetimes to ISO strings for JSON serialization."""
    if isinstance(obj, dict):
        return {k: _make_json_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_make_json_serializable(item) for item in obj]
    elif isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj


def _patch_request_response_helper():
    """Patch RequestResponseHelper and JSONResponse in partner API to format errors as jsonable structures."""
    try:
        import starlette.responses
        import json
        from fastapi.encoders import jsonable_encoder

        orig_render = starlette.responses.JSONResponse.render

        def safe_render(self, content):
            try:
                return orig_render(self, content)
            except TypeError:
                return json.dumps(
                    jsonable_encoder(content),
                    ensure_ascii=False,
                    allow_nan=False,
                    indent=None,
                    separators=(",", ":"),
                ).encode("utf-8")

        starlette.responses.JSONResponse.render = safe_render

        try:
            import openg2p_registry_partner_api.ingestion.helpers.request_response_helper as rrh
            rrh.JSONResponse = starlette.responses.JSONResponse

            orig_construct = rrh.RequestResponseHelper._construct_data_model_response
            def patched_construct(self, response_template_store_id, response):
                _logger.debug("ODK Hook: Ingestion response constructing: %s", response)
                if hasattr(response, "model_dump"):
                    try:
                        content = response.model_dump(mode="json")
                    except Exception:
                        content = jsonable_encoder(response.model_dump())
                else:
                    content = jsonable_encoder(response)
                return rrh.JSONResponse(content=content)

            rrh.RequestResponseHelper._construct_data_model_response = patched_construct
        except Exception as err_rrh:
            _logger.warning("ODK Hook: Could not patch RequestResponseHelper method: %s", err_rrh)

        _logger.info("ODK Hook: Successfully patched JSONResponse & RequestResponseHelper")
    except Exception as e:
        _logger.warning("ODK Hook: Could not patch RequestResponseHelper: %s", e)


def _patch_ingest_controller():
    """Patch G2PIngestController to log exact ingestion failures and return clean JSON error responses."""
    try:
        import functools
        from openg2p_registry_partner_api.ingestion.controllers.g2p_ingest_controller import G2PIngestController
        from starlette.responses import JSONResponse
        from fastapi.encoders import jsonable_encoder

        orig_controller_ingest = G2PIngestController.ingest_data

        @functools.wraps(orig_controller_ingest)
        async def patched_controller_ingest(self, *args, **kwargs):
            try:
                return await orig_controller_ingest(self, *args, **kwargs)
            except Exception as ex:
                _logger.exception("ODK Hook: Exception in G2PIngestController.ingest_data:")
                from .odk_ingest_events import log_request_failed
                log_request_failed(ex)
                return JSONResponse(
                    content=jsonable_encoder({
                        "response_header": {
                            "request_id": "",
                            "response_status": "ERROR",
                            "response_error_code": "INGESTION_ERROR",
                            "response_error_message": str(ex),
                            "response_timestamp": datetime.now().isoformat()
                        },
                        "response_body": None
                    }),
                    status_code=500
                )

        G2PIngestController.ingest_data = patched_controller_ingest
        _logger.info("ODK Hook: Successfully patched G2PIngestController")
    except Exception as e:
        _logger.warning("ODK Hook: Could not patch G2PIngestController: %s", e)


def _get_registry_engine():
    """Construct or reuse an AsyncEngine connected to the primary livestock database."""
    global _registry_engine
    if _registry_engine is not None:
        return _registry_engine

    from sqlalchemy.ext.asyncio import create_async_engine
    hostname = (
        os.environ.get("REGISTRY_CELERY_WORKERS_DB_HOSTNAME")
        or os.environ.get("REGISTRY_PARTNER_API_DB_HOSTNAME")
        or os.environ.get("REGISTRY_STAFF_PORTAL_API_DB_HOSTNAME")
        or os.environ.get("COMMON_DB_HOSTNAME")
        or os.environ.get("REGISTRY_CORE_DB_HOSTNAME")
        or "postgres"
    )
    port = (
        os.environ.get("REGISTRY_CELERY_WORKERS_DB_PORT")
        or os.environ.get("REGISTRY_PARTNER_API_DB_PORT")
        or os.environ.get("REGISTRY_STAFF_PORTAL_API_DB_PORT")
        or os.environ.get("COMMON_DB_PORT")
        or os.environ.get("REGISTRY_CORE_DB_PORT")
        or "5432"
    )
    dbname = (
        os.environ.get("REGISTRY_CELERY_WORKERS_DB_DBNAME")
        or os.environ.get("REGISTRY_PARTNER_API_DB_DBNAME")
        or os.environ.get("REGISTRY_STAFF_PORTAL_API_DB_DBNAME")
        or os.environ.get("COMMON_DB_DBNAME")
        or os.environ.get("REGISTRY_CORE_DB_DBNAME")
        or "livestock"
    )
    username = (
        os.environ.get("REGISTRY_CELERY_WORKERS_DB_USERNAME")
        or os.environ.get("REGISTRY_PARTNER_API_DB_USERNAME")
        or os.environ.get("REGISTRY_STAFF_PORTAL_API_DB_USERNAME")
        or os.environ.get("COMMON_DB_USERNAME")
        or os.environ.get("REGISTRY_CORE_DB_USERNAME")
        or "livestock_user"
    )
    password = (
        os.environ.get("REGISTRY_CELERY_WORKERS_DB_PASSWORD")
        or os.environ.get("REGISTRY_PARTNER_API_DB_PASSWORD")
        or os.environ.get("REGISTRY_STAFF_PORTAL_API_DB_PASSWORD")
        or os.environ.get("COMMON_DB_PASSWORD")
        or os.environ.get("REGISTRY_CORE_DB_PASSWORD")
        or "livestock_pass"
    )
    dsn = f"postgresql+asyncpg://{username}:{password}@{hostname}:{port}/{dbname}"
    # NullPool, like the master-data engine below: celery tasks drive this engine
    # from more than one event loop in the same process, and a pooled asyncpg
    # connection left mid-operation by a failed task made the next File Import
    # fail with "another operation is in progress".
    from sqlalchemy.pool import NullPool
    _registry_engine = create_async_engine(dsn, poolclass=NullPool)
    return _registry_engine


_master_data_engine = None


def _get_master_data_engine():
    """Construct or reuse an AsyncEngine connected to the master_data database."""
    global _master_data_engine
    if _master_data_engine is not None:
        return _master_data_engine

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    md_hostname = (
        os.environ.get("REGISTRY_STAFF_PORTAL_API_MASTER_DATA_DB_HOSTNAME")
        or os.environ.get("REGISTRY_CELERY_WORKERS_MASTER_DATA_DB_HOSTNAME")
        or os.environ.get("REGISTRY_PARTNER_API_MASTER_DATA_DB_HOSTNAME")
        or os.environ.get("REGISTRY_CELERY_BEAT_MASTER_DATA_DB_HOSTNAME")
        or os.environ.get("REGISTRY_CORE_MASTER_DATA_DB_HOSTNAME")
        or os.environ.get("MASTER_DATA_DB_HOSTNAME")
        or "postgres"
    )
    md_port = (
        os.environ.get("REGISTRY_STAFF_PORTAL_API_MASTER_DATA_DB_PORT")
        or os.environ.get("REGISTRY_CELERY_WORKERS_MASTER_DATA_DB_PORT")
        or os.environ.get("REGISTRY_PARTNER_API_MASTER_DATA_DB_PORT")
        or os.environ.get("REGISTRY_CELERY_BEAT_MASTER_DATA_DB_PORT")
        or os.environ.get("REGISTRY_CORE_MASTER_DATA_DB_PORT")
        or os.environ.get("MASTER_DATA_DB_PORT")
        or "5432"
    )
    md_dbname = (
        os.environ.get("REGISTRY_STAFF_PORTAL_API_MASTER_DATA_DB_DBNAME")
        or os.environ.get("REGISTRY_CELERY_WORKERS_MASTER_DATA_DB_DBNAME")
        or os.environ.get("REGISTRY_PARTNER_API_MASTER_DATA_DB_DBNAME")
        or os.environ.get("REGISTRY_CELERY_BEAT_MASTER_DATA_DB_DBNAME")
        or os.environ.get("REGISTRY_CORE_MASTER_DATA_DB_DBNAME")
        or os.environ.get("MASTER_DATA_DB_DBNAME")
        or "master_data"
    )
    md_user = (
        os.environ.get("REGISTRY_STAFF_PORTAL_API_MASTER_DATA_DB_USERNAME")
        or os.environ.get("REGISTRY_CELERY_WORKERS_MASTER_DATA_DB_USERNAME")
        or os.environ.get("REGISTRY_PARTNER_API_MASTER_DATA_DB_USERNAME")
        or os.environ.get("REGISTRY_CELERY_BEAT_MASTER_DATA_DB_USERNAME")
        or os.environ.get("REGISTRY_CORE_MASTER_DATA_DB_USERNAME")
        or os.environ.get("MASTER_DATA_DB_USER")
        or "master_data_user"
    )
    md_pass = (
        os.environ.get("REGISTRY_STAFF_PORTAL_API_MASTER_DATA_DB_PASSWORD")
        or os.environ.get("REGISTRY_CELERY_WORKERS_MASTER_DATA_DB_PASSWORD")
        or os.environ.get("REGISTRY_PARTNER_API_MASTER_DATA_DB_PASSWORD")
        or os.environ.get("REGISTRY_CELERY_BEAT_MASTER_DATA_DB_PASSWORD")
        or os.environ.get("REGISTRY_CORE_MASTER_DATA_DB_PASSWORD")
        or os.environ.get("MASTER_DATA_DB_PASSWORD")
        or "master_data_pass"
    )
    md_dsn = f"postgresql+asyncpg://{md_user}:{md_pass}@{md_hostname}:{md_port}/{md_dbname}"
    _master_data_engine = create_async_engine(md_dsn, poolclass=NullPool)
    return _master_data_engine


_GEO_LABEL_CACHE = {
    "central_ethiopia": "Central Ethiopia",
    "amhara": "Amhara",
    "dire": "Dire Dawa",
    "dire_dawa": "Dire Dawa",
    "oromia": "Oromia",
    "benishangul": "Benishangul-Gumuz",
    "ethiopia_somali": "Somali",
    "somali": "Somali",
    "tigray": "Tigray",
    "afar": "Afar",
    "sidama": "Sidama",
    "south_ethiopian": "South Ethiopian",
    "south_west_ethiopia": "South West Ethiopia",
    "gambela": "Gambela",
    "harari": "Harari",
    "addis_ababa": "Addis Ababa",
    "region-ET01": "Tigray",
    "region-ET02": "Afar",
    "region-ET03": "Amhara",
    "region-ET04": "Oromia",
    "region-ET05": "Somali",
    "region-ET06": "Benishangul-Gumuz",
    "region-ET07": "Central Ethiopia",
    "region-ET08": "South Ethiopian",
    "region-ET11": "South West Ethiopia",
    "region-ET12": "Gambela",
    "region-ET13": "Harari",
    "region-ET14": "Addis Ababa",
    "region-ET15": "Dire Dawa",
    "region-ET16": "Sidama",
    "ET01": "Tigray",
    "ET02": "Afar",
    "ET03": "Amhara",
    "ET04": "Oromia",
    "ET05": "Somali",
    "ET06": "Benishangul-Gumuz",
    "ET07": "Central Ethiopia",
    "ET08": "South Ethiopian",
    "ET11": "South West Ethiopia",
    "ET12": "Gambela",
    "ET13": "Harari",
    "ET14": "Addis Ababa",
    "ET15": "Dire Dawa",
    "ET16": "Sidama",
}


async def resolve_geo_label(code: str | None, level_prefix: str = "") -> str | None:
    """Resolve an administrative code or mnemonic to a human-readable display label."""
    if not code:
        return None
    code_str = str(code).strip()
    if not code_str:
        return None
    if code_str in _GEO_LABEL_CACHE:
        return _GEO_LABEL_CACHE[code_str]

    try:
        engine = _get_master_data_engine()
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from sqlalchemy import text

        session_maker = async_sessionmaker(engine, expire_on_commit=False)
        async with session_maker() as session:
            candidates = [
                code_str,
                f"{level_prefix}-{code_str}" if level_prefix else code_str,
                f"zone-{code_str}",
                f"woreda-{code_str}",
                f"kebele-{code_str}",
                f"region-{code_str}",
            ]
            q = text(
                "SELECT level_value_mnemonic FROM g2p_geo_level_values "
                "WHERE level_value_id = :c1 OR level_value_id = :c2 OR level_value_id = :c3 "
                "OR level_value_id = :c4 OR level_value_id = :c5 OR level_value_id = :c6 "
                "OR level_value_id ILIKE :c_like "
                "LIMIT 1"
            )
            res = await session.execute(
                q,
                {
                    "c1": candidates[0],
                    "c2": candidates[1],
                    "c3": candidates[2],
                    "c4": candidates[3],
                    "c5": candidates[4],
                    "c6": candidates[5],
                    "c_like": f"%{code_str}",
                },
            )
            row = res.fetchone()
            if row and row[0]:
                _GEO_LABEL_CACHE[code_str] = row[0]
                return row[0]
    except Exception as e:
        _logger.warning("ODK Hook: Geo resolution error for %s: %s", code_str, e)

    cleaned = code_str.replace("_", " ").title() if "_" in code_str else code_str
    _GEO_LABEL_CACHE[code_str] = cleaned
    return cleaned


def _calc_age_str(dob_val) -> str | None:
    """Calculate age string ('X years, Y months') from date of birth."""
    if not dob_val:
        return None
    try:
        from datetime import date, datetime

        if isinstance(dob_val, str):
            dob = datetime.fromisoformat(dob_val.replace("Z", "")).date()
        elif isinstance(dob_val, date):
            dob = dob_val
        else:
            return None
        today = date.today()
        years = today.year - dob.year
        months = today.month - dob.month
        if today.day < dob.day:
            months -= 1
        if months < 0:
            years -= 1
            months += 12
        if years < 0:
            return None
        return f"{years} years, {months} months"
    except Exception:
        return None


_SPECIES_CANONICAL_MAP = {
    "cattle": "LIVESTOCK_SPECIES_CATTLE",
    "cow": "LIVESTOCK_SPECIES_CATTLE",
    "bovine": "LIVESTOCK_SPECIES_CATTLE",
    "sheep": "LIVESTOCK_SPECIES_SHEEP",
    "ovine": "LIVESTOCK_SPECIES_SHEEP",
    "goat": "LIVESTOCK_SPECIES_GOAT",
    "caprine": "LIVESTOCK_SPECIES_GOAT",
    "camel": "LIVESTOCK_SPECIES_CAMEL",
    "donkey": "LIVESTOCK_SPECIES_DONKEY",
    "horse": "LIVESTOCK_SPECIES_HORSE",
    "mule": "LIVESTOCK_SPECIES_MULE",
    "poultry": "LIVESTOCK_SPECIES_POULTRY",
    "chicken": "LIVESTOCK_SPECIES_POULTRY",
    "pig": "LIVESTOCK_SPECIES_PIG",
    "swine": "LIVESTOCK_SPECIES_PIG",
    "beehive": "LIVESTOCK_SPECIES_BEEHIVE",
    "bee": "LIVESTOCK_SPECIES_BEEHIVE",
}

_VALID_SPECIES_SET = {
    "LIVESTOCK_SPECIES_CATTLE",
    "LIVESTOCK_SPECIES_SHEEP",
    "LIVESTOCK_SPECIES_GOAT",
    "LIVESTOCK_SPECIES_CAMEL",
    "LIVESTOCK_SPECIES_DONKEY",
    "LIVESTOCK_SPECIES_HORSE",
    "LIVESTOCK_SPECIES_MULE",
    "LIVESTOCK_SPECIES_POULTRY",
    "LIVESTOCK_SPECIES_PIG",
    "LIVESTOCK_SPECIES_BEEHIVE",
}


def normalize_species(val: str | None) -> str:
    """Normalize a raw species string into canonical OpenG2P attribute value_id."""
    if not val:
        return "LIVESTOCK_SPECIES_CATTLE"
    s = str(val).strip()
    if s.upper() in _VALID_SPECIES_SET:
        return s.upper()
    s_low = s.lower()
    if s_low in _SPECIES_CANONICAL_MAP:
        return _SPECIES_CANONICAL_MAP[s_low]
    if s_low.startswith("livestock_species_"):
        cand = s_low.upper()
        if cand in _VALID_SPECIES_SET:
            return cand
        cand_sub = cand.replace("LIVESTOCK_SPECIES_", "").lower()
        if cand_sub in _SPECIES_CANONICAL_MAP:
            return _SPECIES_CANONICAL_MAP[cand_sub]
    for k, v in _SPECIES_CANONICAL_MAP.items():
        if k in s_low:
            return v
    return "LIVESTOCK_SPECIES_CATTLE"


def normalize_breed(breed_val: str | None, species_val: str | None = None) -> str | None:
    """Normalize a raw breed string into canonical OpenG2P attribute value_id based on species."""
    if not breed_val:
        return None
    b = str(breed_val).strip()
    b_upper = b.upper()
    b_lower = b.lower()
    sp = normalize_species(species_val) if species_val else None

    if b_upper.startswith("LIVESTOCK_BREED_"):
        return b_upper

    if sp == "LIVESTOCK_SPECIES_GOAT":
        if "begait" in b_lower:
            return "LIVESTOCK_BREED_BEGAIT_GOAT"
        if "abergelle" in b_lower:
            return "LIVESTOCK_BREED_ABERGELLE"
        if "afar" in b_lower:
            return "LIVESTOCK_BREED_AFAR_GOAT"
        if "boer" in b_lower:
            return "LIVESTOCK_BREED_BOER_CROSS"
        if "central" in b_lower or "highland" in b_lower:
            return "LIVESTOCK_BREED_CENTRAL_HIGHLAND"
        if "somali" in b_lower or "long" in b_lower:
            return "LIVESTOCK_BREED_LONG_EARED_SOMALI"
        if "woito" in b_lower or "guji" in b_lower:
            return "LIVESTOCK_BREED_WOITO_GUJI"
        return "LIVESTOCK_BREED_LOCAL_GOAT"

    elif sp == "LIVESTOCK_SPECIES_CATTLE":
        if "begait" in b_lower:
            return "LIVESTOCK_BREED_BEGAIT"
        if "boran" in b_lower:
            return "LIVESTOCK_BREED_BORAN"
        if "fogera" in b_lower:
            return "LIVESTOCK_BREED_FOGERA"
        if "horro" in b_lower:
            return "LIVESTOCK_BREED_HORRO"
        if "sheko" in b_lower:
            return "LIVESTOCK_BREED_SHEKO"
        if "arsi" in b_lower:
            return "LIVESTOCK_BREED_ARSI"
        if "barka" in b_lower:
            return "LIVESTOCK_BREED_BARKA"
        if "holstein" in b_lower:
            return "LIVESTOCK_BREED_HOLSTEIN_CROSS"
        if "jersey" in b_lower:
            return "LIVESTOCK_BREED_JERSEY_CROSS"
        return "LIVESTOCK_BREED_LOCAL_ZEBU"

    elif sp == "LIVESTOCK_SPECIES_SHEEP":
        if "menz" in b_lower:
            return "LIVESTOCK_BREED_MENZ"
        if "afar" in b_lower:
            return "LIVESTOCK_BREED_AFAR_SHEEP"
        if "blackhead" in b_lower or "somali" in b_lower:
            return "LIVESTOCK_BREED_BLACKHEAD_SOMALI"
        if "bonga" in b_lower:
            return "LIVESTOCK_BREED_BONGA"
        if "horro" in b_lower:
            return "LIVESTOCK_BREED_HORRO_SHEEP"
        if "washera" in b_lower:
            return "LIVESTOCK_BREED_WASHERA"
        if "dorper" in b_lower:
            return "LIVESTOCK_BREED_DORPER_CROSS"
        return "LIVESTOCK_BREED_LOCAL_SHEEP"

    elif sp == "LIVESTOCK_SPECIES_CAMEL":
        if "afar" in b_lower:
            return "LIVESTOCK_BREED_AFAR_CAMEL"
        if "somali" in b_lower:
            return "LIVESTOCK_BREED_SOMALI_CAMEL"
        return "LIVESTOCK_BREED_LOCAL_CAMEL"

    elif sp == "LIVESTOCK_SPECIES_DONKEY":
        if "abyssinian" in b_lower:
            return "LIVESTOCK_BREED_ABYSSINIAN_DONKEY"
        if "sinnar" in b_lower:
            return "LIVESTOCK_BREED_SINNAR"
        return "LIVESTOCK_BREED_LOCAL_DONKEY"

    elif sp == "LIVESTOCK_SPECIES_HORSE":
        if "abyssinian" in b_lower:
            return "LIVESTOCK_BREED_ABYSSINIAN_HORSE"
        if "oromo" in b_lower:
            return "LIVESTOCK_BREED_OROMO_HORSE"
        return "LIVESTOCK_BREED_LOCAL_HORSE"

    elif sp == "LIVESTOCK_SPECIES_MULE":
        return "LIVESTOCK_BREED_LOCAL_MULE"

    elif sp == "LIVESTOCK_SPECIES_PIG":
        if "landrace" in b_lower:
            return "LIVESTOCK_BREED_LANDRACE"
        if "white" in b_lower or "large" in b_lower:
            return "LIVESTOCK_BREED_LARGE_WHITE"
        return "LIVESTOCK_BREED_LOCAL_PIG"

    elif sp == "LIVESTOCK_SPECIES_POULTRY":
        if "bovans" in b_lower:
            return "LIVESTOCK_BREED_BOVANS_BROWN"
        if "isa" in b_lower:
            return "LIVESTOCK_BREED_ISA_BROWN"
        if "koekoek" in b_lower:
            return "LIVESTOCK_BREED_KOEKOEK"
        if "sasso" in b_lower:
            return "LIVESTOCK_BREED_SASSO"
        if "leghorn" in b_lower or "white" in b_lower:
            return "LIVESTOCK_BREED_WHITE_LEGHORN_CROSS"
        return "LIVESTOCK_BREED_LOCAL_CHICKEN"

    elif sp == "LIVESTOCK_SPECIES_BEEHIVE":
        return "LIVESTOCK_BREED_APIS_MELLIFERA"

    if "begait" in b_lower:
        return "LIVESTOCK_BREED_BEGAIT"
    return b


def normalize_vaccine(vaccine_val: str | None, species_val: str | None = None) -> str:
    """Normalize a raw vaccine string into canonical OpenG2P attribute value_id based on species."""
    if not vaccine_val:
        return "ROUTINE"
    v = str(vaccine_val).strip()
    if v.upper().startswith("VACCINE_TYPE_"):
        return v.upper()
    v_low = v.lower()
    sp = normalize_species(species_val) if species_val else None

    if "bovine_pasteurellosis" in v_low:
        return "VACCINE_TYPE_PASTEURELLOSIS_CATTLE"
    if "caprine_pasteurellosis" in v_low:
        return "VACCINE_TYPE_PASTEURELLOSIS_GOAT"
    if "ovine_pasteurellosis" in v_low:
        return "VACCINE_TYPE_PASTEURELLOSIS_SHEEP"
    if "camel_pasteurellosis" in v_low:
        return "VACCINE_TYPE_PASTEURELLOSIS_CAMEL"
    if "pasteurellosis" in v_low:
        if sp == "LIVESTOCK_SPECIES_GOAT":
            return "VACCINE_TYPE_PASTEURELLOSIS_GOAT"
        if sp == "LIVESTOCK_SPECIES_SHEEP":
            return "VACCINE_TYPE_PASTEURELLOSIS_SHEEP"
        if sp == "LIVESTOCK_SPECIES_CAMEL":
            return "VACCINE_TYPE_PASTEURELLOSIS_CAMEL"
        return "VACCINE_TYPE_PASTEURELLOSIS_CATTLE"

    if "anthrax" in v_low:
        if sp == "LIVESTOCK_SPECIES_GOAT":
            return "VACCINE_TYPE_ANTHRAX_GOAT"
        if sp == "LIVESTOCK_SPECIES_SHEEP":
            return "VACCINE_TYPE_ANTHRAX_SHEEP"
        if sp == "LIVESTOCK_SPECIES_CAMEL":
            return "VACCINE_TYPE_ANTHRAX_CAMEL"
        return "VACCINE_TYPE_ANTHRAX_CATTLE"

    if "blackleg" in v_low:
        return "VACCINE_TYPE_BLACKLEG"
    if "cbpp" in v_low:
        return "VACCINE_TYPE_CBPP"
    if "ccpp" in v_low:
        return "VACCINE_TYPE_CCPP"
    if "fmd" in v_low:
        return "VACCINE_TYPE_FMD_CATTLE"
    if "lsd" in v_low or "lumpy" in v_low:
        return "VACCINE_TYPE_LSD"
    if "brucellosis" in v_low:
        return "VACCINE_TYPE_BRUCELLOSIS_S19"
    if "rabies" in v_low:
        if sp == "LIVESTOCK_SPECIES_DONKEY":
            return "VACCINE_TYPE_RABIES_DONKEY"
        return "VACCINE_TYPE_RABIES_CATTLE"
    if "ppr" in v_low:
        if sp == "LIVESTOCK_SPECIES_SHEEP":
            return "VACCINE_TYPE_PPR_SHEEP"
        return "VACCINE_TYPE_PPR_GOAT"
    if "sgp" in v_low or "pox" in v_low:
        if sp == "LIVESTOCK_SPECIES_SHEEP":
            return "VACCINE_TYPE_SGP_SHEEP"
        if sp == "LIVESTOCK_SPECIES_CAMEL":
            return "VACCINE_TYPE_CAMEL_POX_VACCINE"
        if sp == "LIVESTOCK_SPECIES_POULTRY":
            return "VACCINE_TYPE_FOWL_POX"
        return "VACCINE_TYPE_SGP_GOAT"
    if "enterotoxaemia" in v_low:
        return "VACCINE_TYPE_ENTEROTOXAEMIA"
    if "newcastle" in v_low:
        return "VACCINE_TYPE_NEWCASTLE"
    if "gumboro" in v_low:
        return "VACCINE_TYPE_GUMBORO"
    if "fowl_typhoid" in v_low:
        return "VACCINE_TYPE_FOWL_TYPHOID"
    if "marek" in v_low:
        return "VACCINE_TYPE_MAREK"
    if "ahs" in v_low:
        if sp == "LIVESTOCK_SPECIES_DONKEY":
            return "VACCINE_TYPE_AHS_DONKEY"
        return "VACCINE_TYPE_AHS"
    if "tetanus" in v_low:
        return "VACCINE_TYPE_TETANUS_HORSE"
    if v_low in ("routine", ""):
        return "ROUTINE"
    return v.upper()



def _alias_extension_submodules():
    """Alias all openg2p_registry_livestock_extension submodules as openg2p_registry_extensions."""
    target = "openg2p_registry_livestock_extension"
    alias = "openg2p_registry_extensions"
    try:
        import openg2p_registry_livestock_extension
        sys.modules[alias] = openg2p_registry_livestock_extension
    except Exception:
        pass

    try:
        import openg2p_registry_livestock_extension.register_domain.services
        import openg2p_registry_livestock_extension.register_domain.models
        import openg2p_registry_livestock_extension.register_domain.factory
        import openg2p_registry_livestock_extension.config
    except Exception:
        pass

    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith(target):
            alias_name = alias + mod_name[len(target):]
            sys.modules[alias_name] = sys.modules[mod_name]
        elif mod_name.startswith(alias):
            target_name = target + mod_name[len(alias):]
            sys.modules[target_name] = sys.modules[mod_name]


def _ensure_domain_factory_initialized():
    """Ensure template helper, document service, and extension alias are initialized cleanly."""
    _alias_extension_submodules()
    try:
        from openg2p_registry_core.helpers.template_helper import TemplateHelper
        if TemplateHelper.get_component() is None:
            TemplateHelper()
    except Exception:
        pass

    try:
        from openg2p_registry_core.services import G2PDocumentService
        if G2PDocumentService.get_component() is None:
            G2PDocumentService()
    except Exception:
        pass

    try:
        from openg2p_registry_core.services.g2p_awe_integration_service import G2PAweIntegrationService
        if G2PAweIntegrationService.get_component() is None:
            G2PAweIntegrationService()
    except Exception:
        pass

    # ingest_data resolves the data model's response template through this
    # service; the celery worker's Initializer never creates it, so File Import
    # rows failed with "'NoneType' object has no attribute
    # 'resolve_template_store_id'" right after being saved.
    try:
        from openg2p_registry_core.services.g2p_template_service import G2PTemplateService
        if G2PTemplateService.get_component() is None:
            G2PTemplateService()
    except Exception:
        pass



def _patch_celery_transformation_worker():
    """Patch Session.add and _transform_enriched_data_json for Celery transformation worker."""
    try:
        from sqlalchemy.orm import Session
        if not hasattr(Session, "_ls_orig_add"):
            orig_add = Session.add
            Session._ls_orig_add = orig_add

            def patched_add(self, instance, _warn=True):
                try:
                    from openg2p_registry_core.models.ingestion_pipeline import IncomingEnrichedTransformedData
                    if isinstance(instance, IncomingEnrichedTransformedData) and instance.ingest_id:
                        existing = self.get(IncomingEnrichedTransformedData, instance.ingest_id)
                        if existing and existing is not instance:
                            existing.enriched_data_json = instance.enriched_data_json
                            existing.transformed_data_json = instance.transformed_data_json
                            existing.enriched_data_xml = instance.enriched_data_xml
                            existing.transformed_data_xml = instance.transformed_data_xml
                            return existing
                except Exception:
                    pass
                return orig_add(self, instance, _warn=_warn)

            Session.add = patched_add
            _logger.info("ODK Hook: Successfully patched Session.add for Celery transformation worker")
    except Exception as e:
        _logger.debug("ODK Hook: Could not patch Session.add: %s", e)

    try:
        import importlib
        importlib.import_module("openg2p_registry_celery_worker.tasks.ingest_data_transformation_worker")
        tw = sys.modules.get("openg2p_registry_celery_worker.tasks.ingest_data_transformation_worker")
        if tw and not hasattr(tw, "_ls_orig_transform_json"):
            orig_transform_json = tw._transform_enriched_data_json
            tw._ls_orig_transform_json = orig_transform_json

            def patched_transform_json(incoming_classified_data, enriched_data_json, session):
                from .odk_ingest_events import log_received, log_transform_failed, log_transformed

                ingest_id = getattr(incoming_classified_data, "ingest_id", None)
                payload = enriched_data_json
                if isinstance(payload, dict):
                    if "body" in payload and isinstance(payload["body"], dict):
                        payload = payload["body"].get("message", {}).get("payload", payload)
                    elif "message" in payload and isinstance(payload["message"], dict):
                        payload = payload["message"].get("payload", payload)
                log_received(ingest_id, payload)
                tmpl = _load_transform_template()
                if tmpl:
                    try:
                        rendered = tmpl.render(expanded=payload)
                        transformed = json.loads(rendered)
                        _logger.info("ODK Hook: Successfully transformed enriched data using local ls_odk_transform.j2")
                        log_transformed(ingest_id, transformed)
                        return transformed
                    except Exception as err_tmpl:
                        _logger.error("ODK Hook: Error rendering local transformation template: %s", err_tmpl)
                        log_transform_failed(
                            ingest_id, err_tmpl,
                            "falling back to the platform's transform; fix ls_odk_transform.j2 for this payload",
                        )
                return orig_transform_json(incoming_classified_data, enriched_data_json, session)

            tw._transform_enriched_data_json = patched_transform_json
            _logger.info("ODK Hook: Successfully patched _transform_enriched_data_json")
    except Exception as e:
        _logger.debug("ODK Hook: Could not patch _transform_enriched_data_json: %s", e)


def _patch_celery_ingest_worker():
    """Patch ingest_data_worker to auto-approve and promote intake form records to live registers."""
    try:
        import importlib
        # Ensure openg2p_registry_extensions alias is active before loading worker
        import openg2p_registry_livestock_extension
        sys.modules["openg2p_registry_extensions"] = openg2p_registry_livestock_extension
        import openg2p_registry_livestock_extension.config
        sys.modules["openg2p_registry_extensions.config"] = openg2p_registry_livestock_extension.config

        # 1. Patch application_reference generation to avoid unique constraint collisions
        try:
            import openg2p_registry_core.helpers.application_reference_generator as arg_mod
            if not hasattr(arg_mod, "_ls_orig_gar"):
                _orig_gar = arg_mod.generate_application_reference
                arg_mod._ls_orig_gar = _orig_gar

                def _safe_gar(now=None):
                    base = _orig_gar(now)
                    return f"{base}-{uuid.uuid4().hex[:4].upper()}"

                arg_mod.generate_application_reference = _safe_gar
                _logger.info("ODK Hook: Successfully patched generate_application_reference")

            import openg2p_registry_core.models.g2p_intake_form as form_mod
            if hasattr(form_mod, "_default_application_reference"):
                form_mod._default_application_reference = arg_mod.generate_application_reference
        except Exception as err_gar:
            _logger.warning("ODK Hook: Could not patch application_reference generator: %s", err_gar)

        # 2. Patch G2PIntakeFormDataService._insert_live_register_row to sanitize enums and enrich livestock/child records
        try:
            from openg2p_registry_core.services.intake_form_data_service import G2PIntakeFormDataService
            if not hasattr(G2PIntakeFormDataService, "_ls_orig_insert_live"):
                _orig_ilrr = G2PIntakeFormDataService._insert_live_register_row
                G2PIntakeFormDataService._ls_orig_insert_live = _orig_ilrr

                async def _safe_insert_live_register_row(
                    self,
                    submission,
                    section,
                    register_definition,
                    schema_class,
                    register_class,
                    intake_row,
                    session,
                ):
                    # Sanitize attributes on intake_row to conform to livestock register schema enums
                    if hasattr(intake_row, "state") and getattr(intake_row, "state", None) not in ("DRAFT", "CONFIRMED", "DONE"):
                        intake_row.state = "CONFIRMED"
                    if hasattr(intake_row, "event_type"):
                        et = str(getattr(intake_row, "event_type", "") or "").upper()
                        if "AI" in et or "INSEMINATION" in et:
                            intake_row.event_type = "AI"
                        elif "NATURAL" in et:
                            intake_row.event_type = "NATURAL"
                    if hasattr(intake_row, "location"):
                        loc = str(getattr(intake_row, "location", "") or "").upper()
                        if "HOME" in loc:
                            intake_row.location = "HOME"
                        elif "VET" in loc:
                            intake_row.location = "VETERINARY"
                        elif "MARKET" in loc:
                            intake_row.location = "MARKET"
                        elif "FIELD" in loc:
                            intake_row.location = "FIELD"
                        elif "QUARANTINE" in loc:
                            intake_row.location = "QUARANTINE_CENTER"
                        elif loc and loc not in ("HOME", "VETERINARY", "MARKET", "FIELD", "QUARANTINE_CENTER", "OTHER"):
                            intake_row.location = "OTHER"

                    # If this is Livestock or Farmer register, ensure geo labels are populated
                    is_livestock = getattr(register_class, "__tablename__", "") == "g2p_register_livestocks"
                    is_farmer = getattr(register_class, "__tablename__", "") == "g2p_register_farmers"
                    if is_livestock or is_farmer:
                        try:
                            # 1. Resolve Geo Labels
                            for geo_field in ("region", "zone", "woreda", "kebele"):
                                cur_val = getattr(intake_row, geo_field, None)
                                if cur_val:
                                    lbl = await resolve_geo_label(cur_val, geo_field)
                                    if lbl:
                                        setattr(intake_row, geo_field, lbl)
                        except Exception as ex_geo_res:
                            _logger.warning("ODK Hook: Error resolving geo labels for %s: %s", getattr(register_class, "__tablename__", ""), ex_geo_res)

                    if is_livestock:
                        try:

                            # 2. Synchronize Farmer details
                            from sqlalchemy import select, func
                            from openg2p_registry_extensions.register_domain.models import (
                                G2PIntakeFormFarmer, G2PRegisterFarmer, G2PIntakeFormAnimal
                            )
                            f_row = (await session.execute(
                                select(G2PIntakeFormFarmer).where(G2PIntakeFormFarmer.submission_id == submission.submission_id)
                            )).scalar_one_or_none()
                            if not f_row and hasattr(submission, "application_reference") and submission.application_reference:
                                f_row = (await session.execute(
                                    select(G2PIntakeFormFarmer).where(G2PIntakeFormFarmer.application_reference == submission.application_reference)
                                )).scalar_one_or_none()

                            farmer_name = None
                            farmer_id = None
                            fayda_fan_id = None
                            if f_row:
                                farmer_name = f_row.farmer_name or f"{f_row.first_name or ''} {f_row.middle_name or ''} {f_row.last_name or ''}".strip() or None
                                farmer_id = f_row.farmer_id
                                fayda_fan_id = f_row.fayda_fan_id

                            if farmer_name:
                                intake_row.farmer_name = farmer_name
                                intake_row.record_name = farmer_name
                            elif getattr(intake_row, "farmer_name", None):
                                intake_row.record_name = intake_row.farmer_name

                            if farmer_id:
                                intake_row.farmer_id = farmer_id
                            if fayda_fan_id:
                                intake_row.fayda_fan_id = fayda_fan_id

                            # 3. Total Animals
                            if not getattr(intake_row, "total_animals", None):
                                anim_count = (await session.execute(
                                    select(func.count()).select_from(G2PIntakeFormAnimal).where(
                                        G2PIntakeFormAnimal.submission_id == submission.submission_id
                                    )
                                )).scalar() or 0
                                intake_row.total_animals = anim_count
                        except Exception as ex_ls_sync:
                            _logger.warning("ODK Hook: Error prepping intake_row for livestock live insert: %s", ex_ls_sync)

                    # Ensure child records link to livestock internal_record_id
                    is_child_table = getattr(register_class, "__tablename__", "") in (
                        "g2p_register_animals", "g2p_register_health_events",
                        "g2p_register_vaccinations", "g2p_register_vital_events",
                        "g2p_register_breedings"
                    )
                    if is_child_table and not getattr(intake_row, "link_internal_record_id", None):
                        try:
                            from sqlalchemy import select
                            from openg2p_registry_extensions.register_domain.models import G2PIntakeFormLivestock, G2PRegisterLivestock
                            ls_parent_id = (await session.execute(
                                select(G2PIntakeFormLivestock.internal_record_id).where(
                                    G2PIntakeFormLivestock.submission_id == submission.submission_id
                                )
                            )).scalar()
                            if not ls_parent_id and hasattr(submission, "application_reference") and submission.application_reference:
                                ls_parent_id = (await session.execute(
                                    select(G2PRegisterLivestock.internal_record_id).where(
                                        G2PRegisterLivestock.link_internal_record_id == submission.application_reference
                                    )
                                )).scalar()
                            if ls_parent_id:
                                intake_row.link_internal_record_id = ls_parent_id
                        except Exception as ex_link:
                            _logger.debug("ODK Hook: Link internal_record_id lookup error: %s", ex_link)

                    # Normalize species, breed, and vaccine_type on intake_row if present
                    if hasattr(intake_row, "species") and getattr(intake_row, "species", None):
                        intake_row.species = normalize_species(intake_row.species)
                    if hasattr(intake_row, "breed") and getattr(intake_row, "breed", None):
                        intake_row.breed = normalize_breed(intake_row.breed, getattr(intake_row, "species", None))
                    if hasattr(intake_row, "vaccine_type") and getattr(intake_row, "vaccine_type", None):
                        intake_row.vaccine_type = normalize_vaccine(intake_row.vaccine_type, getattr(intake_row, "species", None))

                    # Call original live register row insert
                    live_row = await _orig_ilrr(
                        self,
                        submission,
                        section,
                        register_definition,
                        schema_class,
                        register_class,
                        intake_row,
                        session,
                    )

                    # Post-insert propagation for livestock and farmer
                    if live_row:
                        try:
                            if is_livestock:
                                for attr in ("record_name", "farmer_name", "farmer_id", "fayda_fan_id", "total_animals", "region", "zone", "woreda", "kebele"):
                                    ival = getattr(intake_row, attr, None)
                                    if ival is not None:
                                        setattr(live_row, attr, ival)
                                if not live_row.record_name and live_row.farmer_name:
                                    live_row.record_name = live_row.farmer_name
                                from openg2p_registry_extensions.register_domain.services import G2PRegisterDomainServiceLivestock
                                live_row.search_text = G2PRegisterDomainServiceLivestock().construct_search_text(live_row.to_dict())
                            elif is_farmer:
                                for attr in ("region", "zone", "woreda", "kebele"):
                                    ival = getattr(intake_row, attr, None)
                                    if ival is not None:
                                        setattr(live_row, attr, ival)
                                from openg2p_registry_extensions.register_domain.services import G2PRegisterDomainServiceFarmer
                                live_row.search_text = G2PRegisterDomainServiceFarmer().construct_search_text(live_row.to_dict())

                            for attr in ("species", "breed", "vaccine_type"):
                                ival = getattr(intake_row, attr, None)
                                if ival is not None and hasattr(live_row, attr):
                                    setattr(live_row, attr, ival)
                        except Exception as ex_post:
                            _logger.warning("ODK Hook: Error updating live_row: %s", ex_post)

                    # Post-insert offspring generation for BIRTH events
                    if getattr(register_class, "__tablename__", "") == "g2p_register_vital_events" and live_row:
                        try:
                            if str(getattr(live_row, "event_type", "") or "").upper() == "BIRTH":
                                from openg2p_registry_extensions.register_domain.services.g2p_register_domain_service_vital_event import (
                                    G2PRegisterDomainServiceVitalEvent
                                )
                                await G2PRegisterDomainServiceVitalEvent()._maybe_generate_offspring(live_row, session)
                        except Exception as ex_birth:
                            _logger.warning("ODK Hook: Error generating offspring for birth event: %s", ex_birth)

                    return live_row

                G2PIntakeFormDataService._insert_live_register_row = _safe_insert_live_register_row
                _logger.info("ODK Hook: Successfully wrapped G2PIntakeFormDataService._insert_live_register_row")
        except Exception as err_ilrr:
            _logger.warning("ODK Hook: Could not wrap _insert_live_register_row: %s", err_ilrr)

        importlib.import_module("openg2p_registry_celery_worker.tasks.ingest_data_worker")
        worker_mod = sys.modules.get("openg2p_registry_celery_worker.tasks.ingest_data_worker")

        # 3. Patch _prepare_submission_for_ingestion to avoid duplicate ValueError on retries
        if worker_mod and hasattr(worker_mod, "_prepare_submission_for_ingestion") and not hasattr(worker_mod, "_ls_orig_psfi"):
            orig_psfi = worker_mod._prepare_submission_for_ingestion
            worker_mod._ls_orig_psfi = orig_psfi

            async def patched_prepare_submission_for_ingestion(incoming_classified_data, session):
                try:
                    from openg2p_registry_core.models.g2p_intake_form import G2PIntakeFormSubmission
                    sub_id = incoming_classified_data.intake_form_submission_id
                    if sub_id:
                        existing = await session.get(G2PIntakeFormSubmission, sub_id)
                        if existing and existing.draft_status == "FINAL":
                            _logger.info("ODK Hook: Ingestion %s already linked to finalized submission %s", incoming_classified_data.ingest_id, sub_id)
                            return sub_id, True
                except Exception as ex_prep:
                    _logger.debug("ODK Hook: prep check exception: %s", ex_prep)
                return await orig_psfi(incoming_classified_data, session)

            worker_mod._prepare_submission_for_ingestion = patched_prepare_submission_for_ingestion

        if worker_mod and hasattr(worker_mod, "_validate_transformed_data_json") and not hasattr(worker_mod, "_ls_orig_vtdj"):
            orig_vtdj = worker_mod._validate_transformed_data_json
            worker_mod._ls_orig_vtdj = orig_vtdj

            def safe_validate_transformed_data_json(
                incoming_classified_data,
                incoming_enriched_transformed_data,
                ordered_sections,
            ):
                tdj = incoming_enriched_transformed_data.transformed_data_json
                if isinstance(tdj, dict):
                    allowed = {s.section_mnemonic for s in ordered_sections}
                    filtered = {k: v for k, v in tdj.items() if k in allowed}
                    incoming_enriched_transformed_data.transformed_data_json = filtered
                return orig_vtdj(
                    incoming_classified_data,
                    incoming_enriched_transformed_data,
                    ordered_sections,
                )

            worker_mod._validate_transformed_data_json = safe_validate_transformed_data_json

        if worker_mod and hasattr(worker_mod, "_save_sections_async") and not hasattr(worker_mod, "_ls_orig_save_sections_async"):
            orig_save_sections_async = worker_mod._save_sections_async
            worker_mod._ls_orig_save_sections_async = orig_save_sections_async

            async def patched_save_sections_async(
                submission_id: str,
                incoming_classified_data,
                ordered_sections: list,
                transformed_data: dict,
                session,
            ) -> None:
                # session.bind is this worker module's own pooled engine, tied to
                # its event loop. It is swapped in only for this save and restored
                # in the finally below: left in place, the next File Import task
                # in the same process drove it from another loop, failing with
                # "another operation is in progress" and leaving transactions
                # open that held row locks and stalled the whole pipeline.
                prev_dbengine = None
                swapped_dbengine = False
                try:
                    from openg2p_fastapi_common.context import dbengine
                    if hasattr(session, "bind") and session.bind:
                        prev_dbengine = dbengine.get()
                        dbengine.set(session.bind)
                        swapped_dbengine = True
                    else:
                        _ensure_dbengine_initialized()
                    _ensure_domain_factory_initialized()
                except Exception as ex_init:
                    _logger.warning("ODK Hook: Error during save initialization: %s", ex_init)

                # Pre-process transformed_data before saving
                f_name = None
                f_id = None
                f_fan = None
                animal_count = 0
                try:
                    # 1. Geo resolution on all location sections
                    for sec in ("ls_farmer_identity", "ls_farmer_location", "ls_livestock_location", "ls_livestock_record"):
                        rows = transformed_data.get(sec) or []
                        if isinstance(rows, list):
                            for r in rows:
                                if isinstance(r, dict):
                                    for g_fld in ("region", "zone", "woreda", "kebele"):
                                        if r.get(g_fld):
                                            r[g_fld] = await resolve_geo_label(r[g_fld], g_fld)

                    # 2. Synchronize farmer details & animal count to livestock sections
                    f_id_rows = transformed_data.get("ls_farmer_identity") or []
                    if f_id_rows and isinstance(f_id_rows[0], dict):
                        f0 = f_id_rows[0]
                        f_name = f0.get("farmer_name") or f"{f0.get('first_name', '')} {f0.get('middle_name', '')} {f0.get('last_name', '')}".strip() or None
                        f_id = f0.get("farmer_id")
                        f_fan = f0.get("fayda_fan_id")

                    animals = transformed_data.get("ls_animal_details") or []
                    animal_count = len(animals) if isinstance(animals, list) else 0

                    for ls_sec in ("ls_livestock_record", "ls_livestock_location"):
                        ls_rows = transformed_data.get(ls_sec) or []
                        if isinstance(ls_rows, list):
                            for r in ls_rows:
                                if isinstance(r, dict):
                                    if f_name:
                                        r["farmer_name"] = f_name
                                        r["record_name"] = f_name
                                    if f_id:
                                        r["farmer_id"] = f_id
                                    if f_fan:
                                        r["fayda_fan_id"] = f_fan
                                    r["total_animals"] = animal_count

                    # 3. Normalize species & breed for animals and build animal map
                    animal_map = {}
                    if isinstance(animals, list):
                        for a in animals:
                            if isinstance(a, dict):
                                sp = normalize_species(a.get("species") or a.get("species_id"))
                                a["species"] = sp
                                if a.get("breed"):
                                    a["breed"] = normalize_breed(a.get("breed"), sp)
                                if a.get("ear_tag_id"):
                                    animal_map[str(a["ear_tag_id"]).strip()] = a

                    for ev_sec in ("ls_health_event_details", "ls_vaccination_details", "ls_vital_event_details", "ls_breeding_details"):
                        ev_rows = transformed_data.get(ev_sec) or []
                        if isinstance(ev_rows, list):
                            for ev in ev_rows:
                                if isinstance(ev, dict):
                                    tag = str(ev.get("ear_tag_id") or "").strip()
                                    sp = None
                                    if tag in animal_map:
                                        a = animal_map[tag]
                                        if not ev.get("species"):
                                            ev["species"] = a.get("species")
                                        if not ev.get("age"):
                                            dob = a.get("date_of_birth")
                                            if dob:
                                                ev["age"] = _calc_age_str(dob)
                                            elif a.get("age"):
                                                ev["age"] = str(a.get("age"))
                                    if ev.get("species"):
                                        ev["species"] = normalize_species(ev["species"])
                                        sp = ev["species"]
                                    if ev_sec == "ls_vaccination_details" and ev.get("vaccine_type"):
                                        ev["vaccine_type"] = normalize_vaccine(ev["vaccine_type"], sp)
                except Exception as ex_enrich:
                    _logger.warning("ODK Hook: Error enriching transformed_data: %s", ex_enrich)

                t_session = _active_session.set(session)
                try:
                    res = await orig_save_sections_async(
                        submission_id,
                        incoming_classified_data,
                        ordered_sections,
                        transformed_data,
                        session,
                    )

                    # Post-save safety check on G2PIntakeFormLivestock and G2PIntakeFormFarmer (Option B: Left in PENDING state)
                    try:
                        from sqlalchemy import select
                        from openg2p_registry_extensions.register_domain.models import G2PIntakeFormLivestock, G2PIntakeFormFarmer
                        ls_intake_rows = (await session.execute(
                            select(G2PIntakeFormLivestock).where(G2PIntakeFormLivestock.submission_id == submission_id)
                        )).scalars().all()
                        for ls_row in ls_intake_rows:
                            if f_name:
                                ls_row.farmer_name = f_name
                                ls_row.record_name = f_name
                            if f_id:
                                ls_row.farmer_id = f_id
                            if f_fan:
                                ls_row.fayda_fan_id = f_fan
                            ls_row.total_animals = animal_count
                            for geo_field in ("region", "zone", "woreda", "kebele"):
                                cur_val = getattr(ls_row, geo_field, None)
                                if cur_val:
                                    lbl = await resolve_geo_label(cur_val, geo_field)
                                    if lbl:
                                        setattr(ls_row, geo_field, lbl)
                            ls_row.status = "FINAL"

                        farmer_intake_rows = (await session.execute(
                            select(G2PIntakeFormFarmer).where(G2PIntakeFormFarmer.submission_id == submission_id)
                        )).scalars().all()
                        for f_row in farmer_intake_rows:
                            for geo_field in ("region", "zone", "woreda", "kebele"):
                                cur_val = getattr(f_row, geo_field, None)
                                if cur_val:
                                    lbl = await resolve_geo_label(cur_val, geo_field)
                                    if lbl:
                                        setattr(f_row, geo_field, lbl)
                            f_row.status = "FINAL"

                        await session.flush()
                        _logger.info("ODK Hook: Saved intake records for submission %s in PENDING state (Option B)", submission_id)
                    except Exception as ex_post_save:
                        _logger.debug("ODK Hook: Post-save intake update check: %s", ex_post_save)

                    return res
                finally:
                    _active_session.reset(t_session)
                    if swapped_dbengine and prev_dbengine is not None:
                        try:
                            from openg2p_fastapi_common.context import dbengine
                            dbengine.set(prev_dbengine)
                        except Exception:
                            pass

            worker_mod._save_sections_async = patched_save_sections_async
            _logger.info("ODK Hook: Successfully patched ingest_data_worker._save_sections_async (Option B)")



        if worker_mod and hasattr(worker_mod, "_process_ingestion_async") and not hasattr(worker_mod, "_ls_orig_process_ingestion_async"):
            orig_process_ingestion_async = worker_mod._process_ingestion_async
            worker_mod._ls_orig_process_ingestion_async = orig_process_ingestion_async

            async def resilient_process_ingestion_async(ingest_id: str) -> None:
                from sqlalchemy.ext.asyncio import async_sessionmaker as _log_sessionmaker
                from .odk_ingest_events import log_ingest_outcome

                def _log_session_maker():
                    eng = getattr(worker_mod, "_async_engine", None) or _get_registry_engine()
                    return _log_sessionmaker(bind=eng, expire_on_commit=False)

                _odk_local.is_odk = True
                try:
                    try:
                        await orig_process_ingestion_async(ingest_id)
                        await log_ingest_outcome(ingest_id, _log_session_maker())
                    except Exception as ex:
                        from openg2p_registry_core.models import IncomingClassifiedData, ProcessStatusEnum
                        from sqlalchemy.ext.asyncio import async_sessionmaker

                        err_msg = str(getattr(ex, "message", None) or ex)
                        _logger.warning("ODK Hook: Ingestion of record %s rejected by validation/deduplication: %s", ingest_id, err_msg)

                        try:
                            eng = getattr(worker_mod, "_async_engine", None) or _get_registry_engine()
                            sm = async_sessionmaker(bind=eng, expire_on_commit=False)
                            async with sm() as fail_session:
                                icd = await fail_session.get(IncomingClassifiedData, ingest_id)
                                if icd:
                                    icd.ingestion_status = ProcessStatusEnum.FAILED.value
                                    icd.ingestion_latest_error_code = err_msg
                                    icd.ingestion_date_time = datetime.now()
                                    icd.ingestion_number_of_attempts += 1
                                    sub_id = icd.intake_form_submission_id
                                    if sub_id:
                                        try:
                                            await worker_mod._delete_submission_async(sub_id, fail_session)
                                            icd.intake_form_submission_id = None
                                        except Exception:
                                            pass
                                    fail_session.add(icd)
                                    await fail_session.commit()
                        except Exception as ex_mark:
                            _logger.error("ODK Hook: Could not mark ingestion failure: %s", ex_mark)

                        await log_ingest_outcome(ingest_id, _log_session_maker(), error=ex)

                        # Return cleanly without re-raising to Celery.
                        # This prevents task retries, avoids queue blockage, and stops worker crashes.
                        return
                finally:
                    _odk_local.is_odk = False

            worker_mod._process_ingestion_async = resilient_process_ingestion_async
            _logger.info("ODK Hook: Successfully patched ingest_data_worker._process_ingestion_async for resilience")
    except Exception as e:
        _logger.warning("ODK Hook: Could not patch ingest_data_worker: %s", e)


def _patch_vital_event_offspring_gender():
    """Ensure newborn animal creation from BIRTH vital event never fails with null gender."""
    try:
        from openg2p_registry_livestock_extension.register_domain.services.g2p_register_domain_service_vital_event import (
            G2PRegisterDomainServiceVitalEvent,
        )

        orig_create = G2PRegisterDomainServiceVitalEvent._create_offspring_animals

        async def patched_create_offspring_animals(self, vital_event, count: int, session):
            if not getattr(vital_event, "offspring_gender", None):
                vital_event.offspring_gender = "FEMALE"
            return await orig_create(self, vital_event, count, session)

        G2PRegisterDomainServiceVitalEvent._create_offspring_animals = patched_create_offspring_animals
        _logger.info("ODK Hook: Successfully patched G2PRegisterDomainServiceVitalEvent._create_offspring_animals")
    except Exception as e:
        _logger.warning("ODK Hook: Could not patch G2PRegisterDomainServiceVitalEvent: %s", e)


def _ensure_dbengine_initialized():
    """Ensure dbengine and master-data engines point to the container PostgreSQL instance."""
    _ensure_domain_factory_initialized()
    _patch_celery_transformation_worker()
    _patch_vital_event_offspring_gender()
    try:
        from openg2p_fastapi_common.context import dbengine
        import openg2p_registry_core.engine as core_engine

        # Wrap dbengine.get and dbengine.set so it NEVER falls back to localhost:5432
        _orig_dbengine_get = dbengine.get
        _orig_dbengine_set = dbengine.set

        def _safe_dbengine_get():
            val = _orig_dbengine_get()
            if val is None or getattr(getattr(val, "url", None), "host", None) in ("localhost", "127.0.0.1", None):
                val = _get_registry_engine()
                _orig_dbengine_set(val)
            return val

        def _safe_dbengine_set(value):
            if value is not None and getattr(getattr(value, "url", None), "host", None) in ("localhost", "127.0.0.1", None):
                value = _get_registry_engine()
            _orig_dbengine_set(value)

        dbengine.get = _safe_dbengine_get
        dbengine.set = _safe_dbengine_set

        try:
            from openg2p_fastapi_common.app import Initializer as BaseInitializer
            def _patched_init_db(self):
                e = _get_registry_engine()
                dbengine.set(e)
            BaseInitializer.init_db = _patched_init_db
        except Exception:
            pass

        try:
            import openg2p_fastapi_common.app as fc_app
            if hasattr(fc_app, "_config") and hasattr(fc_app._config, "db_hostname"):
                # From the same env-derived settings as _get_registry_engine();
                # a hard-coded "postgres" host only resolves in local compose.
                _reg_url = _get_registry_engine().url
                fc_app._config.db_hostname = _reg_url.host
                fc_app._config.db_port = _reg_url.port
                fc_app._config.db_dbname = _reg_url.database
                fc_app._config.db_username = _reg_url.username
                fc_app._config.db_password = _reg_url.password
                fc_app._config.db_datasource = _reg_url.render_as_string(hide_password=False)
        except Exception:
            pass

        if core_engine._engines is None:
            core_engine._engines = {}

        _orig_get_engine = getattr(core_engine, "get_engine", None)
        if _orig_get_engine:
            def _safe_get_engine(name="db_engine_master_data"):
                if name == "db_engine_master_data":
                    return _get_master_data_engine()
                res = _orig_get_engine(name)
                if res is None or getattr(getattr(res, "url", None), "host", None) in ("localhost", "127.0.0.1", None):
                    res = _get_registry_engine()
                return res
            core_engine.get_engine = _safe_get_engine

        try:
            from openg2p_registry_core.config import Settings as CoreSettings
            _core_cfg = CoreSettings.get_config(strict=False)
            # Same source as _get_master_data_engine() (the service's own
            # *_MASTER_DATA_DB_* env), not a host that exists only locally.
            _md_url = _get_master_data_engine().url
            _core_cfg.master_data_db_hostname = _md_url.host
            _core_cfg.master_data_db_port = _md_url.port
            _core_cfg.master_data_db_dbname = _md_url.database
            _core_cfg.master_data_db_username = _md_url.username
            _core_cfg.master_data_db_password = _md_url.password
        except Exception:
            pass

        _logger.info(
            "ODK Hook: dbengine and master_data wired to %s / %s",
            _get_registry_engine().url.host, _get_master_data_engine().url.host,
        )
    except Exception as e:
        _logger.warning("ODK Hook: Could not ensure dbengine initialized: %s", e)



def _load_transform_template():
    """Load the Jinja2 transformation template for livestock ODK submissions."""
    dir_path = os.path.dirname(os.path.abspath(__file__))
    tmpl_path = os.path.join(dir_path, "templates", "ls_odk_transform.j2")
    if os.path.exists(tmpl_path):
        with open(tmpl_path, "r", encoding="utf-8") as f:
            return Template(f.read())
    return None


# File Import columns that describe the row's animal (the keys ls_odk_transform.j2
# reads from each entry of the `livestock` list).
_FILE_IMPORT_ANIMAL_COLUMNS = (
    "ear_tag_id", "secondary_identifier", "species", "breed", "colour", "weight",
    "quantity", "gender", "date_of_birth", "age", "health_status",
    "vaccination_status", "registration_date",
)


def _is_blank_cell(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _patch_ingest_service():
    """Patch G2PIngestService to transform and process ODK submissions smoothly."""
    try:
        from openg2p_registry_core.services.g2p_ingest_service import G2PIngestService
        if hasattr(G2PIngestService, "_ls_orig_ingest_data"):
            return

        orig_ingest_data = G2PIngestService.ingest_data
        G2PIngestService._ls_orig_ingest_data = orig_ingest_data

        async def patched_ingest_data(self, data_model_mnemonic: str, data: dict, *, register_id=None, intake_form_id=None):
            _ensure_dbengine_initialized()
            _patch_request_response_helper()

            target_data_model = "MY_DATA_MODEL"
            livestock_register_id = "997676d3-7008-59f9-b23e-613ad79bbb08"
            livestock_intake_form_id = "e92e8be1-207f-518e-99c2-8bc21cc1f112"

            # File Import (celery import_file_process_worker) sends each row as
            # {"headers": {...}, "body": <row>} with the import's own data model,
            # register and intake form. It is not an ODK submission: rewriting it
            # onto MY_DATA_MODEL (which is only the connector's label, not a
            # registry data model) failed every row with DATA_MODEL_NOT_FOUND.
            is_file_import = (
                isinstance(data, dict)
                and isinstance(data.get("headers"), dict)
                and "body" in data
                and bool(data_model_mnemonic and register_id and intake_form_id)
            )
            if is_file_import:
                # The worker's {"headers", "body"} shape does not match the data
                # model's key paths ($.body.header.sender_id, $.body.signature,
                # $.body['header','message']), so re-wrap it. The row goes where
                # the model's semantic pattern reads the business payload
                # ($.body.message.search_response[0].data.reg_records[0]); the
                # transformation step renders it with ls_odk_transform.j2, whose
                # flat keys (farmer_id, fayda_fan_id, owner_id, region, ...) are
                # the import file's columns.
                headers = data["headers"]
                row = data["body"]
                # A CSV / Excel row is flat, but the template reads animals from a
                # `livestock` list. Animal columns on the row (ear_tag_id,
                # species, breed, ...) become that row's one animal.
                if isinstance(row, dict) and not row.get("livestock"):
                    animal = {
                        k: row[k] for k in _FILE_IMPORT_ANIMAL_COLUMNS
                        if not _is_blank_cell(row.get(k))
                    }
                    if animal.get("ear_tag_id") or animal.get("species"):
                        row = dict(row, livestock=[animal])
                data = {
                    "body": {
                        "header": {
                            "sender_id": headers.get("sender_id") or "Staff Portal",
                            "message_id": headers.get("message_id") or uuid.uuid4().hex,
                        },
                        "signature": headers.get("signature") or "file-import",
                        "message": {
                            "search_response": [{"data": {"reg_records": [row]}}],
                        },
                    }
                }

            if isinstance(data, dict) and not is_file_import:
                has_envelope = (
                    ("header" in data and "message" in data) or
                    (isinstance(data.get("body"), dict) and "message" in data.get("body", {}))
                )
                if not has_envelope:
                    instance_id = (
                        data.get("instanceID")
                        or (data.get("meta") or {}).get("instanceID")
                        or (data.get("__system") or {}).get("submissionId")
                        or str(uuid.uuid4())
                    )
                    if isinstance(instance_id, str) and instance_id.startswith("uuid:"):
                        instance_id = instance_id[5:]

                    data = {
                        "body": {
                            "header": {
                                "sender_id": "Livestock",
                                "message_id": instance_id,
                                "signature": "dummy_sig"
                            },
                            "message": {
                                "payload": data
                            }
                        }
                    }

                data_model_mnemonic = target_data_model
                if not register_id:
                    register_id = livestock_register_id
                if not intake_form_id:
                    intake_form_id = livestock_intake_form_id

            data = _make_json_serializable(data)

            res = await orig_ingest_data(
                self,
                data_model_mnemonic,
                data,
                register_id=register_id,
                intake_form_id=intake_form_id,
            )
            return _make_json_serializable(res)

        G2PIngestService.ingest_data = patched_ingest_data
        _logger.info("ODK Hook: Successfully patched G2PIngestService")
    except Exception as e:
        _logger.error("ODK Hook: Error patching G2PIngestService: %s", e)


def _patch_domain_services_for_odk():
    try:
        from openg2p_registry_livestock_extension.register_domain.services.g2p_register_domain_service_health_event import G2PRegisterDomainServiceHealthEvent
        from openg2p_registry_livestock_extension.register_domain.services.g2p_register_domain_service_vaccination import G2PRegisterDomainServiceVaccination
        from openg2p_registry_livestock_extension.register_domain.services.g2p_register_domain_service_breeding import G2PRegisterDomainServiceBreeding
        from openg2p_registry_livestock_extension.register_domain.services.g2p_register_domain_service_vital_event import G2PRegisterDomainServiceVitalEvent

        services = [
            G2PRegisterDomainServiceHealthEvent,
            G2PRegisterDomainServiceVaccination,
            G2PRegisterDomainServiceBreeding,
            G2PRegisterDomainServiceVitalEvent
        ]

        for service in services:
            if hasattr(service, "_validate_ear_tag_exists") and not hasattr(service, "_ls_orig_validate_ear_tag_exists"):
                orig_validate = service._validate_ear_tag_exists
                service._ls_orig_validate_ear_tag_exists = orig_validate

                async def patched_validate_ear_tag_exists(self, record: dict, application_reference: str | None = None, _orig=orig_validate):
                    is_odk_flag = getattr(_odk_local, "is_odk", False)
                    print(f"ODK Hook: patched_validate_ear_tag_exists called for {record.get('ear_tag_id')}, is_odk={is_odk_flag}", flush=True)
                    if is_odk_flag:
                        # Bypass validation for ODK where animal and event are in the same submission payload
                        print("ODK Hook: Bypassing ear_tag_exists validation", flush=True)
                        return
                    return await _orig(self, record, application_reference)

                service._validate_ear_tag_exists = patched_validate_ear_tag_exists

        print("ODK Hook: Successfully patched domain services for ear tag validation bypass", flush=True)
    except Exception as e:
        print(f"ODK Hook: Error patching domain services: {e}", flush=True)


def install_odk_hooks():
    funcs = [
        _patch_request_response_helper,
        _patch_ingest_controller,
        _ensure_domain_factory_initialized,
        _patch_celery_transformation_worker,
        _patch_celery_ingest_worker,
        _ensure_dbengine_initialized,
        _patch_ingest_service,
        _patch_domain_services_for_odk,
    ]
    for func in funcs:
        print(f"ODK Hook: Running {func.__name__}", flush=True)
        try:
            func()
        except Exception as e:
            _logger.error("ODK Hook: Error in %s: %s", func.__name__, e, exc_info=True)


# Initialize hooks on import
install_odk_hooks()
