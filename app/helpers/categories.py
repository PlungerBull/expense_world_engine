"""Category domain logic.

Service-layer functions for expense_categories, called from routers/categories.py.
Routers stay thin (HTTP glue + idempotency) and delegate business logic here.

See ``app/helpers/idempotency.run_idempotent`` for the convention: these
functions do NOT open their own ``conn.transaction()`` — callers own
transaction boundaries.
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
from app.errors import conflict, forbidden, validation_error
from app.helpers.activity_log import write_activity_log
from app.helpers.query_builder import (
    fetch_owned_row_or_404,
    restore_with_audit,
    soft_delete_with_audit,
)
from app.helpers.reference_data import (
    name_taken,
    next_sort_order,
    update_named_resource,
)
from app.helpers.validation import normalize_name
from app.schemas.categories import category_from_row

# Reserved display names, folded to match the case-insensitive
# expense_categories_user_lower_name_active index (sql/012). Derived from the
# constant so a new system category is reserved automatically. Only *user*
# categories are checked against this set — system rows rename freely
# (including back to their own default), because lookup is by system_key.
RESERVED_CATEGORY_NAMES = {n.lower() for n in SYSTEM_CATEGORY_DEFAULT_NAMES.values()}


def _reject_reserved_name(name: str) -> None:
    """422 when a non-system category tries to claim a reserved name.

    Without this, the user's category squats the name and every later
    ``ensure_system_category`` seed hits the LOWER(name) unique index —
    which its ON CONFLICT arbiter does not cover — 500ing opening
    balances forever (bug 7.4).
    """
    if name.lower() in RESERVED_CATEGORY_NAMES:
        raise validation_error(
            "Category name validation failed.",
            {"name": f"'{name}' is reserved for system categories."},
        )


async def ensure_system_category(
    conn: asyncpg.Connection,
    user_id: str,
    key: SystemCategoryKey,
) -> str:
    """Return the ID of a system category, seeding it on first use.

    Lookup is by the immutable ``system_key`` column, not by display name,
    so the category row survives renames without the owning flow
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
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO expense_categories
                (id, user_id, name, is_system, system_key, created_at, updated_at)
            VALUES ($1, $2, $3, true, $4, now(), now())
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
    except asyncpg.UniqueViolationError:
        # The arbiter above covers only the system_key index. A violation
        # landing here is the LOWER(name) index: a *user* category is
        # squatting the reserved name. New squatters are rejected at the
        # category boundary (_reject_reserved_name), but rows created before
        # that check shipped can still exist — surface a clean 409 with the
        # remedy instead of a 500 (bug 7.4).
        raise conflict(
            f"A category named '{default_name}' already exists and is not the "
            f"system category. Rename it to enable this operation."
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
    if row is None:
        # DO NOTHING fired, yet the re-read found nothing: the concurrent
        # seeder's row was soft-deleted in between. Vanishingly rare; a clean
        # retryable 409 beats the TypeError this used to raise.
        raise conflict("System category seeding raced with a concurrent delete. Retry.")
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
        validation_error: name is empty after stripping, or is a reserved
            system-category name (created rows are never system rows).
        conflict: a non-deleted category with the same name (case-insensitive)
            or id already exists.
    """
    name = normalize_name(name)
    _reject_reserved_name(name)
    if await name_taken(conn, "expense_categories", user_id, name):
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
            # Omitted sort_order appends; an explicit value (including 0) is
            # respected verbatim (the old `or 0` collapsed explicit zeros too).
            sort_order
            if sort_order is not None
            else await next_sort_order(conn, "expense_categories", user_id),
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
    return await update_named_resource(
        conn, user_id,
        table="expense_categories",
        resource_type="category",
        resource_id=category_id,
        fields=fields,
        serialize=category_from_row,
        # System rows skip the reserved check: they may rename freely,
        # including back to their own default name — lookup is by
        # system_key, never by name.
        check_name=lambda row, name: (
            _reject_reserved_name(name) if row["system_key"] is None else None
        ),
    )


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
    row = await fetch_owned_row_or_404(
        conn, "expense_categories", category_id, user_id, "category"
    )

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

    return await soft_delete_with_audit(
        conn, user_id, "expense_categories", "category", row, category_from_row
    )


async def restore_category(
    conn: asyncpg.Connection,
    user_id: str,
    category_id: str,
) -> dict:
    """Undo a soft-delete on a category and log the restoration.

    Checks the stored name against both name rules before clearing deleted_at:
    it must not be reserved, and it must not collide with an active category
    (a user can delete a category and create a new one with the same name,
    which blocks restoration).

    Restore is the third write path into a category name, and it was the one
    bug 7.4 missed — create and update both reject reserved names, so a user
    category that held '@Opening' before that guard shipped could be
    soft-deleted and restored straight back into the squat. The difference
    here is that the name is not caller-supplied: it comes off the stored row,
    which is why the same rule needs stating a third time rather than being
    caught by a shared request validator.

    Raises:
        not_found: no soft-deleted category with that id for this user.
        validation_error: the stored name is reserved for system categories.
        conflict: an active category already uses the same name.
    """
    before_row = await fetch_owned_row_or_404(
        conn, "expense_categories", category_id, user_id, "category", deleted=True
    )

    # Same predicate update_category applies (see its check_name callback):
    # system rows own their reserved names and are exempt. That arm is
    # unreachable today — delete_category refuses to soft-delete a system row,
    # so no such row can be restored — but the exemption is written out rather
    # than assumed, because "the other path forbids it" is not a guarantee this
    # function makes for itself.
    if before_row["system_key"] is None:
        _reject_reserved_name(before_row["name"])

    # Ordering matters: reserved is a 422 about the name itself, collision is a
    # 409 about the world it is being restored into. A reserved name that also
    # collides must report the reserved failure, since that one cannot be
    # resolved by deleting the other row.
    if await name_taken(
        conn, "expense_categories", user_id, before_row["name"],
        exclude_id=category_id,
    ):
        raise conflict(
            f"Cannot restore category: an active category named '{before_row['name']}' already exists."
        )

    return await restore_with_audit(
        conn, user_id, "expense_categories", "category", before_row, category_from_row
    )
