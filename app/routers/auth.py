"""HTTP handlers for /auth — thin adapters over helpers.auth."""

from typing import Optional

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from app import db
from app.deps import CurrentUser
from app.errors import not_found
from app.helpers import auth as auth_service
from app.helpers.idempotency import check_idempotency, store_idempotency
from app.schemas.auth import (
    BootstrapRequest,
    BootstrapResponse,
    SettingsUpdateRequest,
    UserSettingsResponse,
    settings_from_row,
    user_from_row,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/bootstrap", response_model=BootstrapResponse)
async def bootstrap(
    body: BootstrapRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached)

        async with conn.transaction():
            response = await auth_service.bootstrap(
                conn,
                auth_user.id,
                auth_user.email,
                body.display_name,
                body.timezone,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, response)
        return response


@router.get("/me", response_model=BootstrapResponse)
async def me(auth_user: CurrentUser):
    async with db.pool.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT * FROM users WHERE id = $1", auth_user.id
        )
        if user_row is None:
            raise not_found("user")

        settings_row = await conn.fetchrow(
            "SELECT * FROM user_settings WHERE user_id = $1", auth_user.id
        )
        if settings_row is None:
            raise not_found("user_settings")

    return {
        "user": user_from_row(user_row),
        "settings": settings_from_row(settings_row),
    }


@router.put("/settings", response_model=UserSettingsResponse)
async def update_settings(
    body: SettingsUpdateRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    fields = body.model_dump(exclude_none=True)

    async with db.pool.acquire() as conn:
        cached = await check_idempotency(conn, auth_user.id, x_idempotency_key)
        if cached is not None:
            return JSONResponse(content=cached)

        async with conn.transaction():
            response = await auth_service.update_settings(
                conn, auth_user.id, fields,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, response)
        return response
