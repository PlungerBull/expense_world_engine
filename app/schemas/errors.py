"""OpenAPI mirror of the error envelope.

Documentation only — the live error path (``app/errors.py``) builds its
``JSONResponse`` directly and never serializes through these models. They
exist so ``openapi.json`` documents the shape the app actually emits instead
of FastAPI's default ``HTTPValidationError`` (which the app never emits; the
override in ``main.py`` strips it).
"""

from typing import Optional

from pydantic import BaseModel


class ErrorBody(BaseModel):
    code: str
    message: str
    # An object mapping field name -> message on validation errors (possibly
    # empty), null on errors that are not about specific fields.
    fields: Optional[dict[str, str]] = None


class ErrorResponse(BaseModel):
    error: ErrorBody
