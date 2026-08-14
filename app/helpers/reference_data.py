"""Shared machinery for the reference-data tables (accounts, categories,
hashtags): the name-uniqueness rule and, with it, everything a "named,
user-ordered collection row" has in common.

One rule, one rendering: a name is taken when an ACTIVE row of the same
table already holds it case-insensitively for this user — soft-deleted
rows release their name (sql/012 for categories/hashtags, sql/028 for
accounts, where the scope adds currency_code). The partial unique indexes
are the belt-and-suspenders under this check.
"""

from typing import Callable, Optional

import asyncpg

from app.constants import ActivityAction
from app.errors import conflict, not_found
from app.helpers.activity_log import write_activity_log
from app.helpers.query_builder import dynamic_update, fetch_owned_row_or_404
from app.helpers.validation import normalize_name, validate_color


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


async def update_named_resource(
    conn: asyncpg.Connection,
    user_id: str,
    *,
    table: str,
    resource_type: str,
    resource_id: str,
    fields: dict,
    serialize: Callable[[asyncpg.Record], dict],
    check_name: Optional[Callable[[asyncpg.Record, str], None]] = None,
) -> dict:
    """The whole of a named-resource PUT: empty-fields fetch-and-return,
    fetch-or-404, name normalization + uniqueness on rename, dynamic
    UPDATE, activity log.

    ``update_category`` and ``update_hashtag`` were byte-identical modulo
    the resource noun and one guard — that guard is ``check_name``, called
    with the before-row and the normalized new name so a resource can veto
    a rename (categories reject reserved names on non-system rows).
    ``update_account`` deliberately does NOT use this: currency
    immutability, currency-scoped uniqueness, and the balance-carrying
    serializer make it a different shape, not a noun swap.

    ``resource_type`` doubles as the activity-log type and the 404/409
    noun ("category" → ``category not found.`` / ``A category named …``).
    """
    if not fields:
        row = await fetch_owned_row_or_404(
            conn, table, resource_id, user_id, resource_type
        )
        return serialize(row)

    before_row = await fetch_owned_row_or_404(
        conn, table, resource_id, user_id, resource_type
    )
    before = serialize(before_row)

    # Every named resource that has a colour uses this path for its updates, so
    # the rule lives here once rather than in each caller's wrapper. Hashtags
    # route through here too but have no `color` field on their update schema
    # (and no column), so the guard can never fire for them — cost of the extra
    # `in` check is nil, and a colour column added to hashtags later inherits the
    # rule instead of quietly skipping it.
    if "color" in fields:
        validate_color(fields["color"])

    if "name" in fields:
        fields["name"] = normalize_name(fields["name"])
        if check_name is not None:
            check_name(before_row, fields["name"])
        if await name_taken(
            conn, table, user_id, fields["name"], exclude_id=resource_id
        ):
            raise conflict(
                f"A {resource_type} named '{fields['name']}' already exists."
            )

    after_row = await dynamic_update(conn, table, fields, resource_id, user_id)
    if after_row is None:
        raise not_found(resource_type)

    after = serialize(after_row)

    await write_activity_log(
        conn, user_id, resource_type, resource_id, ActivityAction.UPDATED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after
