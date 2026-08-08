"""Shared machinery for the reference-data tables (accounts, categories,
hashtags): the name-uniqueness rule and, with it, everything a "named,
user-ordered collection row" has in common.

One rule, one rendering: a name is taken when an ACTIVE row of the same
table already holds it case-insensitively for this user — soft-deleted
rows release their name (sql/012 for categories/hashtags, sql/028 for
accounts, where the scope adds currency_code). The partial unique indexes
are the belt-and-suspenders under this check.
"""

from typing import Optional

import asyncpg


async def name_taken(
    conn: asyncpg.Connection,
    table: str,
    user_id: str,
    name: str,
    *,
    exclude_id: Optional[str] = None,
    currency_code: Optional[str] = None,
) -> bool:
    """True if an active row already holds ``name``, case-insensitively.

    ``currency_code`` narrows the scope for accounts — their uniqueness key
    is (user, LOWER(name), currency). ``exclude_id`` skips the row being
    renamed or restored. Non-raising: create, rename, and restore each word
    their 409 differently, so the message stays at the call site.
    """
    conditions = ["user_id = $1", "LOWER(name) = LOWER($2)", "deleted_at IS NULL"]
    params: list = [user_id, name]
    if currency_code is not None:
        params.append(currency_code)
        conditions.append(f"currency_code = ${len(params)}")
    if exclude_id is not None:
        params.append(exclude_id)
        conditions.append(f"id != ${len(params)}")

    row = await conn.fetchrow(
        f"SELECT 1 FROM {table} WHERE {' AND '.join(conditions)}",
        *params,
    )
    return row is not None


async def next_sort_order(
    conn: asyncpg.Connection,
    table: str,
    user_id: str,
) -> int:
    """The append slot for a new row: MAX(sort_order) + 1 within the user's
    collection, 0 when it is empty (CLAUDE.md collection ordering).

    Deliberately spans soft-deleted rows — a deleted row keeps its slot and
    reclaims it on restore, so a new row must not land on it.
    """
    return await conn.fetchval(
        f"SELECT COALESCE(MAX(sort_order) + 1, 0) FROM {table} WHERE user_id = $1",
        user_id,
    )
