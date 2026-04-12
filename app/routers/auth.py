from typing import Optional

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from app import db
from app.deps import CurrentUser
from app.errors import not_found, validation_error
from app.helpers.activity_log import write_activity_log
from app.helpers.idempotency import check_idempotency, store_idempotency
from app.schemas.auth import (
    BootstrapRequest,
    BootstrapResponse,
    SettingsUpdateRequest,
    UserResponse,
    UserSettingsResponse,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _user_from_row(row) -> dict:
    return UserResponse(
        id=str(row["id"]),
        email=row["email"],
        display_name=row["display_name"],
        last_login_at=row["last_login_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    ).model_dump(mode="json")


def _settings_from_row(row) -> dict:
    return UserSettingsResponse(
        user_id=str(row["user_id"]),
        theme=row["theme"],
        start_of_week=row["start_of_week"],
        main_currency=row["main_currency"],
        transaction_sort_preference=row["transaction_sort_preference"],
        display_timezone=row["display_timezone"],
        sidebar_show_bank_accounts=row["sidebar_show_bank_accounts"],
        sidebar_show_people=row["sidebar_show_people"],
        sidebar_show_categories=row["sidebar_show_categories"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
    ).model_dump(mode="json")


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
            # Check if user exists
            existing = await conn.fetchrow(
                "SELECT id FROM users WHERE id = $1", auth_user.id
            )

            if existing is None:
                # New user — insert
                user_row = await conn.fetchrow(
                    """
                    INSERT INTO users (id, email, display_name, last_login_at, created_at, updated_at)
                    VALUES ($1, $2, $3, now(), now(), now())
                    RETURNING *
                    """,
                    auth_user.id,
                    auth_user.email,
                    body.display_name,
                )
                await write_activity_log(
                    conn, auth_user.id, "user", auth_user.id, 1,
                    after_snapshot=_user_from_row(user_row),
                )
            else:
                # Existing user — update last_login_at
                user_row = await conn.fetchrow(
                    """
                    UPDATE users SET last_login_at = now(), updated_at = now()
                    WHERE id = $1 RETURNING *
                    """,
                    auth_user.id,
                )

            # Upsert user_settings
            settings_existing = await conn.fetchrow(
                "SELECT user_id FROM user_settings WHERE user_id = $1", auth_user.id
            )

            if settings_existing is None:
                settings_row = await conn.fetchrow(
                    """
                    INSERT INTO user_settings (user_id, display_timezone, created_at, updated_at)
                    VALUES ($1, $2, now(), now())
                    RETURNING *
                    """,
                    auth_user.id,
                    body.timezone,
                )
                await write_activity_log(
                    conn, auth_user.id, "user_settings", auth_user.id, 1,
                    after_snapshot=_settings_from_row(settings_row),
                )
            else:
                settings_row = await conn.fetchrow(
                    "SELECT * FROM user_settings WHERE user_id = $1", auth_user.id
                )

        response = {
            "user": _user_from_row(user_row),
            "settings": _settings_from_row(settings_row),
        }
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
        "user": _user_from_row(user_row),
        "settings": _settings_from_row(settings_row),
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

        # Empty update — return current settings
        if not fields:
            row = await conn.fetchrow(
                "SELECT * FROM user_settings WHERE user_id = $1", auth_user.id
            )
            if row is None:
                raise not_found("user_settings")
            return _settings_from_row(row)

        # Validate main_currency if provided
        if "main_currency" in fields:
            currency = await conn.fetchrow(
                "SELECT code FROM global_currencies WHERE code = $1",
                fields["main_currency"],
            )
            if currency is None:
                raise validation_error(
                    "Invalid currency code.",
                    {"main_currency": f"'{fields['main_currency']}' is not a valid currency code."},
                )

        async with conn.transaction():
            # Before snapshot
            before_row = await conn.fetchrow(
                "SELECT * FROM user_settings WHERE user_id = $1", auth_user.id
            )
            if before_row is None:
                raise not_found("user_settings")
            before = _settings_from_row(before_row)

            # Build dynamic UPDATE
            set_clauses = []
            params = [auth_user.id]
            for i, (key, value) in enumerate(fields.items(), start=2):
                set_clauses.append(f"{key} = ${i}")
                params.append(value)
            set_clauses.append("version = version + 1")
            set_clauses.append("updated_at = now()")

            query = f"UPDATE user_settings SET {', '.join(set_clauses)} WHERE user_id = $1 RETURNING *"
            after_row = await conn.fetchrow(query, *params)
            after = _settings_from_row(after_row)

            await write_activity_log(
                conn, auth_user.id, "user_settings", auth_user.id, 2,
                before_snapshot=before,
                after_snapshot=after,
            )

        await store_idempotency(conn, auth_user.id, x_idempotency_key, after)
        return after
