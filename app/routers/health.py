from fastapi import APIRouter

from app import db

router = APIRouter()


@router.get("/health")
async def health():
    async with db.pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok"}
