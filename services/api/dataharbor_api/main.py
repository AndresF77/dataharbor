"""Application factory. Run locally with:

    uvicorn dataharbor_api.main:create_app --factory --reload
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from dataharbor_api import __version__, datasets, health, workspaces
from dataharbor_api.config import Settings, get_settings
from dataharbor_api.db import Database
from dataharbor_api.errors import install_error_handlers
from dataharbor_api.request_id import RequestIdMiddleware


def create_app(settings: Settings | None = None) -> FastAPI:
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

    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(workspaces.router)
    app.include_router(datasets.router)
    return app
