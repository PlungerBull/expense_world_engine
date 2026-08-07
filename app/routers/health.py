from fastapi import APIRouter

from app import db
from app.schemas.health import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health():
    async with db.pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok"}
