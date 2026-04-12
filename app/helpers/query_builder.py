"""Shared helpers for building dynamic SQL queries.

Consolidates the dynamic UPDATE and soft-delete patterns that were
duplicated across every router.
"""

from typing import Optional

import asyncpg


async def dynamic_update(
    conn: asyncpg.Connection,
    table: str,
    fields: dict,
    resource_id: str,
    user_id: str,
) -> Optional[asyncpg.Record]:
    """Build and execute a dynamic UPDATE, returning the updated row.

    Always appends ``updated_at = now()`` and ``version = version + 1``.
    Only rows with ``deleted_at IS NULL`` are updated.

    Returns the ``RETURNING *`` row, or ``None`` if not found.
    """
    set_clauses = []
    params: list = [resource_id, user_id]
    for i, (key, value) in enumerate(fields.items(), start=3):
        set_clauses.append(f"{key} = ${i}")
        params.append(value)
    set_clauses.append("updated_at = now()")
    set_clauses.append("version = version + 1")

    query = f"""
        UPDATE {table}
        SET {', '.join(set_clauses)}
        WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
        RETURNING *
    """
    return await conn.fetchrow(query, *params)


async def soft_delete(
    conn: asyncpg.Connection,
    table: str,
    resource_id: str,
    user_id: str,
) -> Optional[asyncpg.Record]:
    """Soft-delete a resource by setting deleted_at, returning the updated row.

    Also bumps ``updated_at`` and ``version`` so delta sync picks up the change.
    """
    return await conn.fetchrow(
        f"""
        UPDATE {table}
        SET deleted_at = now(), updated_at = now(), version = version + 1
        WHERE id = $1 AND user_id = $2
        RETURNING *
        """,
        resource_id,
        user_id,
    )
