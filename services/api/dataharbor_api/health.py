"""Liveness and readiness probes."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from dataharbor_api import __version__
from dataharbor_api.migrations import current_revision, head_revision

router = APIRouter(tags=["health"])


@router.get("/health")
def liveness() -> dict[str, str]:
    """The process is up. Does not touch the database."""
    return {"status": "ok", "version": __version__}


@router.get("/health/ready")
def readiness(request: Request) -> JSONResponse:
    """Ready to serve: database reachable and schema at the latest migration."""
    database = request.app.state.database
    expected = head_revision(request.app.state.settings.database_url)
    checks: dict[str, str] = {}
    try:
        with database.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            checks["database"] = "ok"
            current = current_revision(connection)
    except SQLAlchemyError:
        checks["database"] = "unreachable"
        current = None

    if checks["database"] == "ok":
        checks["migrations"] = "ok" if current == expected else "pending"

    ready = all(value == "ok" for value in checks.values())
    body = {
        "status": "ready" if ready else "not_ready",
        "checks": checks,
        "schema_revision": current,
        "expected_revision": expected,
    }
    return JSONResponse(body, status_code=200 if ready else 503)
