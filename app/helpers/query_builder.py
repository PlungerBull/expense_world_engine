"""Shared helpers for building dynamic SQL queries.

Consolidates the dynamic UPDATE and soft-delete patterns that were
duplicated across every router.
"""

from typing import Optional

import asyncpg

from app.errors import not_found


async def fetch_owned_row(
    conn: asyncpg.Connection,
    table: str,
    resource_id: str,
    user_id: str,
    *,
    deleted: bool = False,
    for_update: bool = False,
) -> Optional[asyncpg.Record]:
    """Fetch one row by id under the tenant-isolation predicate this module
    already owns for writes: ``id = $1 AND user_id = $2``.

    ``deleted=False`` (the default) resolves only active rows;
    ``deleted=True`` resolves only soft-deleted rows (the restore path).
    ``for_update=True`` takes a row lock for read-modify-write flows.
    Returns ``None`` when no row matches.

    ``table`` is always a literal at the call site, never user input.
    Deliberate non-adopters of this helper: ``helpers/pat.py`` (soft-delete
    column is ``revoked_at``), ``inbox.promote_inbox_item`` (extra status
    predicate), and ``reconciliations.fetch_reconciliation`` (computed
    ``difference_cents`` projection).
    """
    predicate = "IS NOT NULL" if deleted else "IS NULL"
    suffix = " FOR UPDATE" if for_update else ""
    return await conn.fetchrow(
        f"""
        SELECT * FROM {table}
        WHERE id = $1 AND user_id = $2 AND deleted_at {predicate}{suffix}
        """,
        resource_id,
        user_id,
    )


async def fetch_owned_row_or_404(
    conn: asyncpg.Connection,
    table: str,
    resource_id: str,
    user_id: str,
    resource: str,
    *,
    deleted: bool = False,
    for_update: bool = False,
) -> asyncpg.Record:
    """``fetch_owned_row`` that raises ``not_found(resource)`` on a miss.

    Callers that tolerate a miss (or raise something other than 404) use
    ``fetch_owned_row`` directly — the split keeps this return type
    non-Optional.
    """
    row = await fetch_owned_row(
        conn, table, resource_id, user_id, deleted=deleted, for_update=for_update
    )
    if row is None:
        raise not_found(resource)
    return row


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

    Also bumps ``updated_at`` and ``version`` (optimistic-concurrency counters).
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


async def restore(
    conn: asyncpg.Connection,
    table: str,
    resource_id: str,
    user_id: str,
) -> Optional[asyncpg.Record]:
    """Undo a soft-delete by clearing deleted_at, returning the updated row.

    Only matches rows that are currently soft-deleted — a restore on an
    already-active row returns ``None`` so callers can distinguish "not
    deleted" from "not found". Bumps ``updated_at`` and ``version``
    (optimistic-concurrency counters).
    """
    return await conn.fetchrow(
        f"""
        UPDATE {table}
        SET deleted_at = NULL, updated_at = now(), version = version + 1
        WHERE id = $1 AND user_id = $2 AND deleted_at IS NOT NULL
        RETURNING *
        """,
        resource_id,
        user_id,
    )
