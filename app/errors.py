from typing import Optional

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


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
    fields = {}
    for err in exc.errors():
        loc = err.get("loc", ())
        # Skip the first element ("body", "query", etc.)
        field_name = ".".join(str(part) for part in loc[1:]) if len(loc) > 1 else str(loc[0]) if loc else "unknown"
        fields[field_name] = err.get("msg", "Invalid value")
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed.",
                "fields": fields if fields else None,
            }
        },
    )


def unauthorized(message: str = "Missing or invalid authentication token.") -> AppError:
    return AppError(401, "UNAUTHORIZED", message)


def not_found(resource: str = "resource") -> AppError:
    return AppError(404, "NOT_FOUND", f"{resource} not found.")


def validation_error(message: str, fields: Optional[dict] = None) -> AppError:
    return AppError(422, "VALIDATION_ERROR", message, fields)


def conflict(message: str) -> AppError:
    return AppError(409, "CONFLICT", message)
