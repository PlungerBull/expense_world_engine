from fastapi import APIRouter

from app import db
from app.errors import AppError
from app.schemas.errors import ErrorResponse
from app.schemas.health import HealthResponse

router = APIRouter()


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={
        503: {
            "description": "Engine is running but the database is unreachable.",
            "model": ErrorResponse,
        }
    },
)
async def health():
    # A health probe reports unhealth; it never *is* the failure. Any probe
    # error — pool not yet created, connection refused, query error — is the
    # answer "unhealthy", so it maps to a deliberate 503, not a stray 500.
    try:
        async with db.pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception:
        raise AppError(503, "SERVICE_UNAVAILABLE", "Database is unreachable.")
    return {"status": "ok"}
