"""FastAPI application entry-point."""

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .controllers import (
    connectors_router,
    health_router,
    metadata_router,
    runs_router,
    webhook_router,
)
from .database import get_engine
from . import db_migrations, ingest_log
from . import metrics as connector_metrics
from .models import Base
from .observability import instrument_app

# Ensure transports and auth strategies self-register on import
from . import transports as _transports  # noqa: F401
from .auth import strategies as _auth_strategies  # noqa: F401


@asynccontextmanager
async def _lifespan(app: FastAPI):
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await db_migrations.apply(conn)
    yield


def create_app() -> FastAPI:
    settings = get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    ingest_log.configure(
        settings.ingest_log_file,
        service="connector-api",
        max_bytes=settings.ingest_log_max_bytes,
        backups=settings.ingest_log_backups,
    )

    app = FastAPI(
        title=settings.openapi_title,
        version=settings.openapi_version,
        lifespan=_lifespan,
    )

    if settings.cors_origins:
        origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(health_router)
    app.include_router(connectors_router)
    app.include_router(metadata_router)
    app.include_router(webhook_router)
    app.include_router(runs_router)

    if settings.metrics_enabled and connector_metrics.enabled():
        try:
            from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
        except Exception:
            pass
        else:
            @app.get(settings.metrics_path, include_in_schema=False)
            def _metrics() -> Response:
                payload = generate_latest(connector_metrics.registry())
                return Response(content=payload, media_type=CONTENT_TYPE_LATEST)

    instrument_app(app)

    return app


app = create_app()


def serve():
    settings = get_settings()
    uvicorn.run(
        "openg2p_connector_service.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
    )


if __name__ == "__main__":
    serve()
