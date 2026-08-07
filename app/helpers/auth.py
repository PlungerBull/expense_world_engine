"""Auth domain logic.

Service-layer functions for users and user_settings, called from
routers/auth.py. Routers stay thin (HTTP glue + idempotency) and delegate
business logic here.

See ``app/helpers/categories.py`` for the convention: these functions do NOT
open their own ``conn.transaction()`` — callers own transaction boundaries.
"""

import asyncpg

from app.constants import ActivityAction
from app.errors import not_found, validation_error
from app.helpers.activity_log import write_activity_log
from app.helpers.validation import validate_timezone
from app.schemas.auth import settings_from_row, user_from_row


async def bootstrap(
    conn: asyncpg.Connection,
    user_id: str,
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
    # Validated unconditionally, not just on the insert branch: a returning
    # user sending a garbage zone should be told, not silently ignored.
    validate_timezone(timezone, "timezone")

    # Check if user exists
    existing = await conn.fetchrow(
        "SELECT id FROM users WHERE id = $1", user_id
    )

    if existing is None:
        # New user — insert
        user_row = await conn.fetchrow(
            """
            INSERT INTO users (id, display_name, last_login_at, created_at, updated_at)
            VALUES ($1, $2, now(), now(), now())
            RETURNING *
            """,
            user_id,
            display_name,
        )
        await write_activity_log(
            conn, user_id, "user", user_id, ActivityAction.CREATED,
            after_snapshot=user_from_row(user_row),
        )
    else:
        # Existing user — bump last_login_at.
        #
        # Activity log — deliberate exception: this UPDATE does NOT write
        # an activity_log entry. last_login_at is operational metadata
        # (every successful bootstrap call touches it), not a user action
        # worth auditing. Writing one per login would double-log every
        # session start across every device and crowd out signal in the
        # activity feed. If session-level audit becomes a requirement,
        # store it in a dedicated ``auth_sessions`` table instead of
        # inflating activity_log.
        user_row = await conn.fetchrow(
            """
            UPDATE users SET last_login_at = now(), updated_at = now()
            WHERE id = $1 RETURNING *
            """,
            user_id,
        )

    # Upsert user_settings
    settings_row = await conn.fetchrow(
        "SELECT * FROM user_settings WHERE user_id = $1", user_id
    )

    if settings_row is None:
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

    return {
        "user": user_from_row(user_row),
        "settings": settings_from_row(settings_row),
    }


async def update_settings(
    conn: asyncpg.Connection,
    user_id: str,
    fields: dict,
) -> dict:
    """Apply field updates to ``user_settings``.

    Returns the unchanged settings if ``fields`` is empty (matches the prior
    router behaviour of treating empty-update as a fetch).

    ``main_currency`` is not updatable: the home currency is locked to PEN
    (``sql/018``). It stays in ``SettingsUpdateRequest`` purely so a caller
    asking for a switch gets a loud 422 instead of a silently-ignored field.

    Raises:
        not_found: the user_settings row does not exist.
        validation_error: the request tried to change ``main_currency``.
    """
    # Empty update — return current settings
    if not fields:
        row = await conn.fetchrow(
            "SELECT * FROM user_settings WHERE user_id = $1", user_id
        )
        if row is None:
            raise not_found("user_settings")
        return settings_from_row(row)

    # Home currency is immutable. Reject rather than ignore: a caller that
    # silently "succeeds" here would cache a currency the engine never
    # adopted, and every amount it renders afterwards would be mislabelled.
    if "main_currency" in fields:
        raise validation_error(
            "Home currency cannot be changed.",
            {"main_currency": "The home currency is locked to PEN and is not updatable."},
        )

    # display_timezone reaches AT TIME ZONE on every report read; a bad value
    # stored here would 500 those reads, so it is rejected at the boundary.
    if "display_timezone" in fields:
        validate_timezone(fields["display_timezone"], "display_timezone")

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

    await write_activity_log(
        conn, user_id, "user_settings", user_id, ActivityAction.UPDATED,
        before_snapshot=before,
        after_snapshot=after,
    )

    return after


async def update_profile(
    conn: asyncpg.Connection,
    user_id: str,
    fields: dict,
) -> dict:
    """Apply identity-field updates to ``users`` (v1: display_name only).

    Empty ``fields`` raises 422. Unlike ``update_settings``, profile has a
    single mutable field in v1, so empty-body is a client bug, not a fetch.
    """
    if not fields:
        raise validation_error(
            "No fields to update.",
            {"display_name": "Pass at least one field to update."},
        )

    before_row = await conn.fetchrow(
        "SELECT * FROM users WHERE id = $1", user_id
    )
    if before_row is None:
        raise not_found("user")
    before = user_from_row(before_row)

    set_clauses = []
    params = [user_id]
    for i, (key, value) in enumerate(fields.items(), start=2):
        set_clauses.append(f"{key} = ${i}")
        params.append(value)
    set_clauses.append("updated_at = now()")

    query = f"UPDATE users SET {', '.join(set_clauses)} WHERE id = $1 RETURNING *"
    after_row = await conn.fetchrow(query, *params)
    after = user_from_row(after_row)

    await write_activity_log(
        conn, user_id, "user", user_id, ActivityAction.UPDATED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after
