import uuid

import asyncpg


async def ensure_system_category(conn: asyncpg.Connection, user_id: str, name: str) -> str:
    """Return the ID of a system category, creating it if it doesn't exist.

    Uses ON CONFLICT for race-safety when two concurrent requests both try
    to auto-create the same system category for the first time.
    """
    row = await conn.fetchrow(
        "SELECT id FROM expense_categories WHERE user_id = $1 AND name = $2 AND deleted_at IS NULL",
        user_id,
        name,
    )
    if row is not None:
        return str(row["id"])

    row = await conn.fetchrow(
        """
        INSERT INTO expense_categories (id, user_id, name, color, is_system, created_at, updated_at)
        VALUES ($1, $2, $3, '#6b7280', true, now(), now())
        ON CONFLICT (user_id, name) DO NOTHING
        RETURNING id
        """,
        str(uuid.uuid4()),
        user_id,
        name,
    )
    if row is not None:
        return str(row["id"])

    # Conflict path: another transaction created it concurrently.
    row = await conn.fetchrow(
        "SELECT id FROM expense_categories WHERE user_id = $1 AND name = $2 AND deleted_at IS NULL",
        user_id,
        name,
    )
    return str(row["id"])
