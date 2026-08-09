MAX_LIMIT = 200
DEFAULT_LIMIT = 50


def paginated_response(items: list, total: int, limit: int, offset: int) -> dict:
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


async def list_page(
    conn,
    *,
    from_sql: str,
    conditions: list,
    params: list,
    order_by: str,
    limit: int,
    offset: int,
    select: str = "*",
) -> tuple:
    """Count + fetch one page off a single predicate list. Returns (rows, total).

    The one place the ``LIMIT ${n+1} OFFSET ${n+2}`` placeholder arithmetic is
    rendered — before this helper, eight list endpoints each retyped it in two
    divergent idioms (bloat-audit §12). Callers build ``conditions``/``params``
    (appending a param before referencing ``${len(params)}``), and keep their
    own row mapping and envelope: this returns raw rows so it also serves the
    reconciliation detail route, whose nested transaction page is not a
    ``paginated_response``.

    ``from_sql``, ``select`` and ``order_by`` are interpolated, never bound —
    like ``query_builder``'s table names they must be literals at the call
    site, never user input. ``from_sql`` may carry an alias
    (``"expense_transactions t"``) matching alias-qualified conditions.

    ``conditions`` must be non-empty, and the tenant predicate
    (``user_id = $n``) is the caller's responsibility — a missing one is a
    security defect (CLAUDE.md). The one all-users table, ``exchange_rates``,
    is a deliberate non-adopter for exactly that reason.
    """
    if not conditions:
        raise ValueError("list_page requires at least one condition")
    where = " AND ".join(conditions)

    total = await conn.fetchval(
        f"SELECT count(*) FROM {from_sql} WHERE {where}", *params
    )

    rows = await conn.fetch(
        f"""
        SELECT {select} FROM {from_sql}
        WHERE {where}
        ORDER BY {order_by}
        LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """,
        *params,
        limit,
        offset,
    )
    return rows, total
