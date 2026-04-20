"""HTTP handlers for /auth — thin adapters over helpers.auth."""

from typing import Optional

from fastapi import APIRouter, Header

from app import db
from app.deps import CurrentUser
from app.errors import not_found
from app.helpers import auth as auth_service
from app.helpers.idempotency import run_idempotent
from app.helpers.validation import extract_update_fields
from app.schemas.auth import (
    BootstrapRequest,
    BootstrapResponse,
    SettingsUpdateRequest,
    UserSettingsResponse,
    settings_from_row,
    user_from_row,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/bootstrap", response_model=BootstrapResponse, status_code=200)
async def bootstrap(
    body: BootstrapRequest,
    auth_user: CurrentUser,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    # 200, not 201: /bootstrap has upsert semantics. First call inserts the
    # user + settings rows; subsequent calls update last_login_at on the
    # existing rows. Unlike other POSTs (which are pure creates and return
    # 201), bootstrap may find the resource already present.
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: auth_service.bootstrap(
            conn,
            auth_user.id,
            auth_user.email,
            body.display_name,
            body.timezone,
        ),
    )


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
    fields = extract_update_fields(body)
    return await run_idempotent(
        auth_user.id,
        x_idempotency_key,
        status_code=200,
        work=lambda conn: auth_service.update_settings(
            conn, auth_user.id, fields,
        ),
    )
