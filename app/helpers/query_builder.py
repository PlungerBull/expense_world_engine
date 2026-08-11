"""Shared helpers for building dynamic SQL queries.

Consolidates the dynamic UPDATE and soft-delete patterns that were
duplicated across every router.
"""

from typing import Awaitable, Callable, Optional

import asyncpg

from app.constants import ActivityAction
from app.errors import conflict, not_found
from app.helpers.activity_log import write_activity_log


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


def build_set_clause(
    fields: dict,
    *,
    start: int,
    extra: tuple[str, ...] = ("updated_at = now()", "version = version + 1"),
) -> tuple[str, list]:
    """Render a dynamic UPDATE's SET list.

    ``$start``-numbered assignments for ``fields``, then the ``extra``
    literal clauses. Returns ``(sql, params)`` where ``params`` are the
    field values in order. Keys are schema-validated column names, never
    raw user input.

    The WHERE clause stays at the call site — that is the part that
    genuinely differs (``id``+``user_id``+``deleted_at`` here;
    ``user_id``-keyed ``user_settings`` and ``id``-keyed ``users`` in
    ``helpers/auth.py``, neither of which has ``deleted_at``, and
    ``users`` has no ``version`` either).
    """
    clauses = [f"{key} = ${i}" for i, key in enumerate(fields, start=start)]
    clauses.extend(extra)
    return ", ".join(clauses), list(fields.values())


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
    set_sql, values = build_set_clause(fields, start=3)
    query = f"""
        UPDATE {table}
        SET {set_sql}
        WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
        RETURNING *
    """
    return await conn.fetchrow(query, resource_id, user_id, *values)


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


async def soft_delete_with_audit(
    conn: asyncpg.Connection,
    user_id: str,
    table: str,
    resource_type: str,
    row: asyncpg.Record,
    serialize: Callable[[asyncpg.Record], dict],
    *,
    refetch: Optional[Callable[[], Awaitable[asyncpg.Record]]] = None,
) -> dict:
    """The mechanical tail every soft delete shares: before-snapshot from
    ``row``, ``soft_delete``, after-snapshot, activity log, return after.

    ``row`` is the pre-fetched ACTIVE row (``fetch_owned_row_or_404``); the
    caller runs its guards and cascades as plain code *before* calling —
    hoisting them into hooks would just rebuild the deleted bodies as
    lambdas. ``serialize`` closes over any extra snapshot inputs (accounts
    close over the once-fetched balance pair). ``refetch`` replaces the
    ``RETURNING *`` row as the after-snapshot source when the serializer
    needs a computed column (reconciliations' ``difference_cents``).
    """
    return await _mutate_with_audit(
        conn, user_id, table, resource_type, row, serialize,
        mutate=soft_delete, action=ActivityAction.DELETED, refetch=refetch,
    )


async def restore_with_audit(
    conn: asyncpg.Connection,
    user_id: str,
    table: str,
    resource_type: str,
    row: asyncpg.Record,
    serialize: Callable[[asyncpg.Record], dict],
    *,
    refetch: Optional[Callable[[], Awaitable[asyncpg.Record]]] = None,
) -> dict:
    """Mirror of ``soft_delete_with_audit`` for restores: ``row`` is the
    pre-fetched SOFT-DELETED row (``deleted=True``), the mutate is
    ``restore``, the action is RESTORED. Same caller contract.
    """
    return await _mutate_with_audit(
        conn, user_id, table, resource_type, row, serialize,
        mutate=restore, action=ActivityAction.RESTORED, refetch=refetch,
    )


async def _mutate_with_audit(
    conn, user_id, table, resource_type, row, serialize, *, mutate, action, refetch
) -> dict:
    resource_id = str(row["id"])
    before = serialize(row)

    after_row = await mutate(conn, table, resource_id, user_id)
    if after_row is None:
        # The row changed state between the caller's fetch and this UPDATE
        # (a concurrent delete/restore won the race, so the mutate's
        # deleted_at predicate matched nothing). Fail closed with a 409
        # rather than serializing None into a 500 (bug 5.5).
        raise conflict(
            f"{resource_type} was changed by a concurrent request. "
            "Re-fetch and retry."
        )
    if refetch is not None:
        after_row = await refetch()
    after = serialize(after_row)

    await write_activity_log(
        conn, user_id, resource_type, resource_id, action,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after
