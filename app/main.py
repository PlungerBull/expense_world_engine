from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db
from app.routers import health


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

app.include_router(health.router)
