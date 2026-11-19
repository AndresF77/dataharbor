"""One error envelope for every failure: {"error": {code, message, request_id, details}}."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

_STATUS_CODES = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_failed",
    503: "unavailable",
}


def error_body(
    request: Request, status: int, message: str, details: Any = None
) -> dict[str, Any]:
    return {
        "error": {
            "code": _STATUS_CODES.get(status, "error"),
            "message": message,
            "request_id": getattr(request.state, "request_id", None),
            "details": details,
        }
    }


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            error_body(request, exc.status_code, str(exc.detail)),
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Drop the raw input so rejected payloads are never echoed back.
        details = [
            {"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]}
            for err in exc.errors()
        ]
        return JSONResponse(
            error_body(request, 422, "Request validation failed", details),
            status_code=422,
        )
