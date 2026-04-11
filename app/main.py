from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app import db
from app.errors import AppError, app_error_handler, validation_error_handler
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
    reconciliations,
    transactions,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    yield
    await db.disconnect()


app = FastAPI(
    title="expense_world_engine",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)

app.include_router(health.router)
app.include_router(auth.router, prefix="/v1")
app.include_router(accounts.router, prefix="/v1")
app.include_router(activity.router, prefix="/v1")
app.include_router(categories.router, prefix="/v1")
app.include_router(dashboard.router, prefix="/v1")
app.include_router(exchange_rates.router, prefix="/v1")
app.include_router(hashtags.router, prefix="/v1")
app.include_router(inbox.router, prefix="/v1")
app.include_router(reconciliations.router, prefix="/v1")
app.include_router(transactions.router, prefix="/v1")
