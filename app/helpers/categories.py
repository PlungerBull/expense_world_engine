"""Category domain logic.

Service-layer functions for expense_categories, called from routers/categories.py.
Routers stay thin (HTTP glue + idempotency) and delegate business logic here.

See ``app/helpers/balance.py`` for the convention: these functions do NOT
open their own ``conn.transaction()`` — callers own transaction boundaries.
"""

import uuid
from typing import Optional
from uuid import UUID

import asyncpg

from app.constants import (
    SYSTEM_CATEGORY_DEFAULT_NAMES,
    ActivityAction,
    SystemCategoryKey,
)
from app.errors import conflict, forbidden, not_found
from app.helpers.activity_log import write_activity_log
from app.helpers.query_builder import dynamic_update, restore, soft_delete
from app.schemas.categories import category_from_row


async def ensure_system_category(
    conn: asyncpg.Connection,
    user_id: str,
    key: SystemCategoryKey,
) -> str:
    """Return the ID of a system category, seeding it on first use.

    Lookup is by the immutable ``system_key`` column, not by display name,
    so the category row survives renames without the transfer pipeline
    fragmenting into duplicates.

    The ON CONFLICT clause makes concurrent first-time seeding race-safe:
    if two transactions both try to insert the same key, the loser hits
    the partial unique index and falls through to the re-read.
    """
    row = await conn.fetchrow(
        """
        SELECT id FROM expense_categories
        WHERE user_id = $1 AND system_key = $2 AND deleted_at IS NULL
        """,
        user_id,
        key.value,
    )
    if row is not None:
        return str(row["id"])

    default_name = SYSTEM_CATEGORY_DEFAULT_NAMES[key]
    row = await conn.fetchrow(
        """
        INSERT INTO expense_categories
            (id, user_id, name, color, is_system, system_key, created_at, updated_at)
        VALUES ($1, $2, $3, '#6b7280', true, $4, now(), now())
        ON CONFLICT (user_id, system_key)
            WHERE system_key IS NOT NULL AND deleted_at IS NULL
            DO NOTHING
        RETURNING id
        """,
        str(uuid.uuid4()),
        user_id,
        default_name,
        key.value,
    )
    if row is not None:
        return str(row["id"])

    # Conflict path: another transaction seeded it concurrently.
    row = await conn.fetchrow(
        """
        SELECT id FROM expense_categories
        WHERE user_id = $1 AND system_key = $2 AND deleted_at IS NULL
        """,
        user_id,
        key.value,
    )
    return str(row["id"])


async def create_category(
    conn: asyncpg.Connection,
    user_id: str,
    category_id: UUID,
    name: str,
    color: str,
    sort_order: Optional[int],
) -> dict:
    """Validate uniqueness, insert, and log the creation.

    Raises:
        conflict: a non-deleted category with the same name or id already exists.
    """
    existing = await conn.fetchrow(
        """
        SELECT id FROM expense_categories
        WHERE user_id = $1 AND name = $2 AND deleted_at IS NULL
        """,
        user_id,
        name,
    )
    if existing is not None:
        raise conflict(f"A category named '{name}' already exists.")

    try:
        row = await conn.fetchrow(
            """
            INSERT INTO expense_categories
                (id, user_id, name, color, sort_order, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, now(), now())
            RETURNING *
            """,
            category_id,
            user_id,
            name,
            color,
            sort_order or 0,
        )
    except asyncpg.UniqueViolationError:
        raise conflict(f"A category with id '{category_id}' already exists.")

    response = category_from_row(row)

    await write_activity_log(
        conn, user_id, "category", str(row["id"]), ActivityAction.CREATED,
        after_snapshot=response,
    )
    return response


async def update_category(
    conn: asyncpg.Connection,
    user_id: str,
    category_id: str,
    fields: dict,
) -> dict:
    """Apply field updates, enforcing system-category guards and name uniqueness.

    Returns the unchanged category if ``fields`` is empty (matches the
    prior router behaviour of treating empty-update as a fetch).

    Raises:
        not_found: no active category with that id for this user.
        conflict: another non-deleted category already uses the new name.
    """
    # Empty update — return current state unchanged
    if not fields:
        row = await conn.fetchrow(
            "SELECT * FROM expense_categories WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
            category_id,
            user_id,
        )
        if row is None:
            raise not_found("category")
        return category_from_row(row)

    before_row = await conn.fetchrow(
        "SELECT * FROM expense_categories WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
        category_id,
        user_id,
    )
    if before_row is None:
        raise not_found("category")

    before = category_from_row(before_row)

    # Name uniqueness check
    if "name" in fields:
        dup = await conn.fetchrow(
            """
            SELECT id FROM expense_categories
            WHERE user_id = $1 AND name = $2 AND id != $3 AND deleted_at IS NULL
            """,
            user_id,
            fields["name"],
            category_id,
        )
        if dup is not None:
            raise conflict(f"A category named '{fields['name']}' already exists.")

    after_row = await dynamic_update(conn, "expense_categories", fields, category_id, user_id)
    if after_row is None:
        raise not_found("category")

    after = category_from_row(after_row)

    await write_activity_log(
        conn, user_id, "category", category_id, ActivityAction.UPDATED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after


async def delete_category(
    conn: asyncpg.Connection,
    user_id: str,
    category_id: str,
) -> dict:
    """Soft-delete a category after enforcing guards on system categories and references.

    Raises:
        not_found: no active category with that id for this user.
        forbidden: attempting to delete a system category.
        conflict: category is still referenced by active transactions or inbox items.
    """
    row = await conn.fetchrow(
        "SELECT * FROM expense_categories WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
        category_id,
        user_id,
    )
    if row is None:
        raise not_found("category")

    # System categories cannot be deleted
    if row["is_system"]:
        raise forbidden(f"Cannot delete system category {row['name']}.")

    # Reference checks: active transactions and inbox items
    has_txns = await conn.fetchval(
        """
        SELECT 1 FROM expense_transactions
        WHERE category_id = $1 AND user_id = $2 AND deleted_at IS NULL
        LIMIT 1
        """,
        category_id,
        user_id,
    )
    if has_txns:
        raise conflict("Category is referenced by active transactions. Remove those references first.")

    has_inbox = await conn.fetchval(
        """
        SELECT 1 FROM expense_transaction_inbox
        WHERE category_id = $1 AND user_id = $2 AND deleted_at IS NULL
        LIMIT 1
        """,
        category_id,
        user_id,
    )
    if has_inbox:
        raise conflict("Category is referenced by active inbox items. Remove those references first.")

    before = category_from_row(row)

    after_row = await soft_delete(conn, "expense_categories", category_id, user_id)
    after = category_from_row(after_row)

    await write_activity_log(
        conn, user_id, "category", category_id, ActivityAction.DELETED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after


async def restore_category(
    conn: asyncpg.Connection,
    user_id: str,
    category_id: str,
) -> dict:
    """Undo a soft-delete on a category and log the restoration.

    Checks for name collisions with active categories before clearing
    deleted_at — a user can delete a category and create a new one with
    the same name, which would block restoration.

    Raises:
        not_found: no soft-deleted category with that id for this user.
        conflict: an active category already uses the same name.
    """
    before_row = await conn.fetchrow(
        "SELECT * FROM expense_categories WHERE id = $1 AND user_id = $2 AND deleted_at IS NOT NULL",
        category_id,
        user_id,
    )
    if before_row is None:
        raise not_found("category")

    dup = await conn.fetchrow(
        """
        SELECT id FROM expense_categories
        WHERE user_id = $1 AND name = $2 AND id != $3 AND deleted_at IS NULL
        """,
        user_id,
        before_row["name"],
        category_id,
    )
    if dup is not None:
        raise conflict(
            f"Cannot restore category: an active category named '{before_row['name']}' already exists."
        )

    before = category_from_row(before_row)

    after_row = await restore(conn, "expense_categories", category_id, user_id)
    after = category_from_row(after_row)

    await write_activity_log(
        conn, user_id, "category", category_id, ActivityAction.RESTORED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after
