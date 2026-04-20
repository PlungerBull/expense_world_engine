import logging
from typing import Optional

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

_STARLETTE_CODE_MAP = {
    400: "BAD_REQUEST",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    429: "TOO_MANY_REQUESTS",
}


class AppError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        fields: Optional[dict] = None,
    ):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.fields = fields


async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "fields": exc.fields,
            }
        },
    )


async def validation_error_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    fields: dict = {}
    for err in exc.errors():
        loc = err.get("loc", ())
        # Skip the first element ("body", "query", etc.)
        field_name = ".".join(str(part) for part in loc[1:]) if len(loc) > 1 else str(loc[0]) if loc else "unknown"
        fields[field_name] = err.get("msg", "Invalid value")
    # Always emit an object (possibly empty) for VALIDATION_ERROR so clients
    # can uniformly iterate Object.keys without a null check. Non-validation
    # errors (UNAUTHORIZED, NOT_FOUND, etc.) still legitimately use `null`.
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed.",
                "fields": fields,
            }
        },
    )


def unauthorized(message: str = "Missing or invalid authentication token.") -> AppError:
    return AppError(401, "UNAUTHORIZED", message)


def not_found(resource: str = "resource") -> AppError:
    return AppError(404, "NOT_FOUND", f"{resource} not found.")


def validation_error(message: str, fields: Optional[dict] = None) -> AppError:
    # VALIDATION_ERROR.fields is always an object (empty when the caller
    # supplied nothing). Clients can uniformly iterate Object.keys without
    # a null check. Non-validation error factories below keep fields=None.
    return AppError(422, "VALIDATION_ERROR", message, fields if fields is not None else {})


def forbidden(message: str) -> AppError:
    return AppError(403, "FORBIDDEN", message)


def conflict(message: str) -> AppError:
    return AppError(409, "CONFLICT", message)


def settings_missing() -> AppError:
    # 422 rather than 409: the resource is not in a conflicting state,
    # it simply hasn't been provisioned yet. The dedicated SETTINGS_MISSING
    # code lets clients branch on "redirect user to bootstrap flow" without
    # parsing the message.
    return AppError(
        422,
        "SETTINGS_MISSING",
        "User settings not found. Call /auth/bootstrap first.",
        {"user_settings": "Must be provisioned via POST /v1/auth/bootstrap."},
    )


async def starlette_http_exception_handler(
    _request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    code = _STARLETTE_CODE_MAP.get(exc.status_code, "HTTP_ERROR")
    detail = exc.detail if isinstance(exc.detail, str) else "Request could not be processed."
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": code, "message": detail, "fields": None}},
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "Unhandled exception on %s %s", request.method, request.url.path
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "An unexpected error occurred.",
                "fields": None,
            }
        },
    )
