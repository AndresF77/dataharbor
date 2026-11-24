"""Application factory. Run locally with:

    uvicorn dataharbor_api.main:create_app --factory --reload
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from dataharbor_api import (
    __version__,
    contracts,
    datasets,
    health,
    landing,
    pipelines,
    workspaces,
)
from dataharbor_api.config import Settings, get_settings
from dataharbor_api.db import Database
from dataharbor_api.errors import install_error_handlers
from dataharbor_api.request_id import RequestIdMiddleware
from dataharbor_api.storage import MemoryObjectStore, ObjectStore, S3ObjectStore


def make_object_store(settings: Settings) -> ObjectStore:
    if settings.object_store == "memory":
        return MemoryObjectStore(settings.landing_bucket)
    return S3ObjectStore(
        settings.minio_endpoint,
        settings.minio_access_key,
        settings.minio_secret_key,
        settings.landing_bucket,
    )


def create_app(
    settings: Settings | None = None, object_store: ObjectStore | None = None
) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        app.state.database.dispose()

    app = FastAPI(
        title="DataHarbor API",
        version=__version__,
        lifespan=lifespan,
        openapi_url="/v1/openapi.json",
        docs_url="/v1/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.database = Database(settings.database_url)
    app.state.object_store = object_store or make_object_store(settings)

    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(workspaces.router)
    app.include_router(datasets.router)
    app.include_router(contracts.router)
    app.include_router(landing.router)
    app.include_router(pipelines.router)
    return app
