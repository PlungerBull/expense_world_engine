"""Auth domain logic.

Service-layer functions for users and user_settings, called from
routers/auth.py. Routers stay thin (HTTP glue + idempotency) and delegate
business logic here.

See ``app/helpers/categories.py`` for the convention: these functions do NOT
open their own ``conn.transaction()`` — callers own transaction boundaries.
"""

from typing import Optional

import asyncpg

from app.constants import ActivityAction
from app.errors import not_found, validation_error
from app.helpers.activity_log import write_activity_log
from app.helpers.recalculate_home_currency import recalculate_home_currency
from app.schemas.auth import settings_from_row, user_from_row


async def bootstrap(
    conn: asyncpg.Connection,
    user_id: str,
    email: Optional[str],
    display_name: str,
    timezone: str,
) -> dict:
    """Upsert the user row and user_settings row for a freshly authenticated user.

    On first call for a user, INSERTs both ``users`` and ``user_settings`` and
    writes activity log entries for each creation. On subsequent calls, bumps
    ``last_login_at`` on the user row and fetches the existing settings row
    unchanged.

    Returns the canonical ``{"user": ..., "settings": ...}`` shape.
    """
    # Check if user exists
    existing = await conn.fetchrow(
        "SELECT id FROM users WHERE id = $1", user_id
    )

    if existing is None:
        # New user — insert
        user_row = await conn.fetchrow(
            """
            INSERT INTO users (id, email, display_name, last_login_at, created_at, updated_at)
            VALUES ($1, $2, $3, now(), now(), now())
            RETURNING *
            """,
            user_id,
            email,
            display_name,
        )
        await write_activity_log(
            conn, user_id, "user", user_id, ActivityAction.CREATED,
            after_snapshot=user_from_row(user_row),
        )
    else:
        # Existing user — update last_login_at
        user_row = await conn.fetchrow(
            """
            UPDATE users SET last_login_at = now(), updated_at = now()
            WHERE id = $1 RETURNING *
            """,
            user_id,
        )

    # Upsert user_settings
    settings_existing = await conn.fetchrow(
        "SELECT user_id FROM user_settings WHERE user_id = $1", user_id
    )

    if settings_existing is None:
        settings_row = await conn.fetchrow(
            """
            INSERT INTO user_settings (user_id, display_timezone, created_at, updated_at)
            VALUES ($1, $2, now(), now())
            RETURNING *
            """,
            user_id,
            timezone,
        )
        await write_activity_log(
            conn, user_id, "user_settings", user_id, ActivityAction.CREATED,
            after_snapshot=settings_from_row(settings_row),
        )
    else:
        settings_row = await conn.fetchrow(
            "SELECT * FROM user_settings WHERE user_id = $1", user_id
        )

    return {
        "user": user_from_row(user_row),
        "settings": settings_from_row(settings_row),
    }


async def update_settings(
    conn: asyncpg.Connection,
    user_id: str,
    fields: dict,
) -> dict:
    """Apply field updates to ``user_settings``, recalculating home currency when needed.

    Returns the unchanged settings if ``fields`` is empty (matches the prior
    router behaviour of treating empty-update as a fetch).

    If ``main_currency`` actually changes, triggers a full home-currency
    recalculation and records the summary on the activity log's after snapshot
    (a documented exception to the normal snapshot schema). The returned
    settings dict does NOT carry the recalculation summary — that augmentation
    lives only on the activity log.

    Raises:
        not_found: the user_settings row does not exist.
        validation_error: ``main_currency`` is not a known global currency code.
    """
    # Empty update — return current settings
    if not fields:
        row = await conn.fetchrow(
            "SELECT * FROM user_settings WHERE user_id = $1", user_id
        )
        if row is None:
            raise not_found("user_settings")
        return settings_from_row(row)

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

    # Before snapshot
    before_row = await conn.fetchrow(
        "SELECT * FROM user_settings WHERE user_id = $1", user_id
    )
    if before_row is None:
        raise not_found("user_settings")
    before = settings_from_row(before_row)

    # Build dynamic UPDATE — NOTE: user_settings uses WHERE user_id = $1
    # (single-param), not the standard WHERE id = $1 AND user_id = $2 pattern,
    # so the generic query_builder.dynamic_update helper cannot be used here.
    set_clauses = []
    params = [user_id]
    for i, (key, value) in enumerate(fields.items(), start=2):
        set_clauses.append(f"{key} = ${i}")
        params.append(value)
    set_clauses.append("version = version + 1")
    set_clauses.append("updated_at = now()")

    query = f"UPDATE user_settings SET {', '.join(set_clauses)} WHERE user_id = $1 RETURNING *"
    after_row = await conn.fetchrow(query, *params)
    after = settings_from_row(after_row)

    # Home-currency recalculation when main_currency actually changes
    old_currency = before_row["main_currency"]
    new_currency = after_row["main_currency"]
    recalc_summary = None

    if old_currency != new_currency:
        recalc_summary = await recalculate_home_currency(
            conn, user_id, new_currency,
        )

    await write_activity_log(
        conn, user_id, "user_settings", user_id, ActivityAction.UPDATED,
        before_snapshot=before,
        after_snapshot={**after, "recalculation": recalc_summary} if recalc_summary else after,
    )

    return after
