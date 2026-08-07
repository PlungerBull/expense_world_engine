from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import db
from app.errors import (
    AppError,
    app_error_handler,
    starlette_http_exception_handler,
    unhandled_exception_handler,
    validation_error_handler,
)
from app.helpers.idempotency import capture_request_fingerprint
from app.routers import (
    accounts,
    activity,
    auth,
    categories,
    dashboard,
    exchange_rates,
    hashtags,
    health,
    inbox,
    pat,
    reconciliations,
    reports,
    transactions,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    yield
    await db.disconnect()


app = FastAPI(
    title="expense_world_engine",
    # Feature-complete engine (Steps 0-9.4). This version string is the one
    # clients see in /openapi.json, so it is the API contract's version, not
    # the deployment's — bump it when the wire format changes.
    version="1.0.0",
    lifespan=lifespan,
    # App-wide so no route can forget it: every request is fingerprinted
    # for idempotency-key reuse detection (sql/026). See
    # helpers/idempotency.py — same structural argument as the
    # transaction boundary living in run_idempotent.
    dependencies=[Depends(capture_request_fingerprint)],
)

def _openapi_without_fastapi_422_stub():
    """FastAPI's default 422 advertises HTTPValidationError/{detail: [...]},
    a shape this app never emits (errors are always {"error": {...}} — see
    errors.py). Every router overrides its 422 with ERROR_RESPONSES, so the
    stub components are unreferenced; this removes them from the document
    rather than publishing schemas nothing can ever receive.
    """
    if app.openapi_schema:
        return app.openapi_schema
    schema = _original_openapi()
    components = schema.get("components", {}).get("schemas", {})
    components.pop("HTTPValidationError", None)
    components.pop("ValidationError", None)
    app.openapi_schema = schema
    return schema


_original_openapi = app.openapi
app.openapi = _openapi_without_fastapi_422_stub

app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(StarletteHTTPException, starlette_http_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)

app.include_router(health.router)
app.include_router(auth.router, prefix="/v1")
app.include_router(accounts.router, prefix="/v1")
app.include_router(activity.router, prefix="/v1")
app.include_router(categories.router, prefix="/v1")
app.include_router(dashboard.router, prefix="/v1")
app.include_router(exchange_rates.router, prefix="/v1")
app.include_router(hashtags.router, prefix="/v1")
app.include_router(inbox.router, prefix="/v1")
app.include_router(pat.router, prefix="/v1")
app.include_router(reconciliations.router, prefix="/v1")
app.include_router(reports.router, prefix="/v1")
app.include_router(transactions.router, prefix="/v1")
