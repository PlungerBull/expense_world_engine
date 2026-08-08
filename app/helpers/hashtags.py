"""Hashtag domain logic.

Service-layer functions for expense_hashtags, called from routers/hashtags.py.
Routers stay thin (HTTP glue + idempotency) and delegate business logic here.

See ``app/helpers/idempotency.run_idempotent`` for the convention: these
functions do NOT open their own ``conn.transaction()`` — callers own
transaction boundaries.
"""

from typing import Optional
from uuid import UUID

import asyncpg

from app.constants import ActivityAction
from app.errors import conflict
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
from app.schemas.hashtags import hashtag_from_row


async def create_hashtag(
    conn: asyncpg.Connection,
    user_id: str,
    hashtag_id: UUID,
    name: str,
    sort_order: Optional[int],
) -> dict:
    """Validate uniqueness, insert, and log the creation.

    Raises:
        validation_error: name is empty after stripping.
        conflict: a non-deleted hashtag with the same name (case-insensitive)
            or id already exists.
    """
    name = normalize_name(name)
    if await name_taken(conn, "expense_hashtags", user_id, name):
        raise conflict(f"A hashtag named '{name}' already exists.")

    try:
        row = await conn.fetchrow(
            """
            INSERT INTO expense_hashtags
                (id, user_id, name, sort_order, created_at, updated_at)
            VALUES ($1, $2, $3, $4, now(), now())
            RETURNING *
            """,
            hashtag_id,
            user_id,
            name,
            # Omitted sort_order appends; an explicit value (including 0) is
            # respected verbatim (the old `or 0` collapsed explicit zeros too).
            sort_order
            if sort_order is not None
            else await next_sort_order(conn, "expense_hashtags", user_id),
        )
    except asyncpg.UniqueViolationError:
        raise conflict(f"A hashtag with id '{hashtag_id}' already exists.")

    response = hashtag_from_row(row)

    await write_activity_log(
        conn, user_id, "hashtag", str(row["id"]), ActivityAction.CREATED,
        after_snapshot=response,
    )
    return response


async def update_hashtag(
    conn: asyncpg.Connection,
    user_id: str,
    hashtag_id: str,
    fields: dict,
) -> dict:
    """Apply field updates, enforcing name uniqueness.

    Returns the unchanged hashtag if ``fields`` is empty (matches the
    prior router behaviour of treating empty-update as a fetch).

    Raises:
        not_found: no active hashtag with that id for this user.
        conflict: another non-deleted hashtag already uses the new name.
    """
    return await update_named_resource(
        conn, user_id,
        table="expense_hashtags",
        resource_type="hashtag",
        resource_id=hashtag_id,
        fields=fields,
        serialize=hashtag_from_row,
    )


async def delete_hashtag(
    conn: asyncpg.Connection,
    user_id: str,
    hashtag_id: str,
) -> dict:
    """Soft-delete a hashtag, cascading cleanup to junction rows.

    Cascade steps (atomically coupled — all inside the caller's transaction):
      1. Lookup the hashtag row (raises not_found if missing).
      2. Soft-delete every ``expense_transaction_hashtags`` junction row for
         this hashtag, capturing the affected transaction IDs.
      3. Bump ``updated_at`` + ``version`` on each parent transaction so
         readers see the hashtag_ids change.
      4. Soft-delete the hashtag row itself.
      5. Write the activity log with before/after snapshots.

    Raises:
        not_found: no active hashtag with that id for this user.
    """
    row = await fetch_owned_row_or_404(
        conn, "expense_hashtags", hashtag_id, user_id, "hashtag"
    )

    # Soft-delete all junction rows for this hashtag, capturing the
    # affected transaction IDs so we can bump their version + updated_at.
    # Without the parent bump, the row's version would miss the hashtag_ids change.
    #
    # Activity log — per-row entries for the junction table are
    # deliberately NOT written here (see helpers/transactions._sync_hashtags
    # for the rationale). Each affected parent transaction carries the new
    # hashtag_ids list via its version bump, and a single DELETED entry is
    # written for the hashtag itself below.
    affected = await conn.fetch(
        """
        UPDATE expense_transaction_hashtags
        SET deleted_at = now(), updated_at = now()
        WHERE hashtag_id = $1 AND user_id = $2 AND deleted_at IS NULL
        RETURNING transaction_id
        """,
        hashtag_id,
        user_id,
    )

    if affected:
        await conn.execute(
            """
            UPDATE expense_transactions
            SET updated_at = now(), version = version + 1
            WHERE id = ANY($1::uuid[]) AND user_id = $2
            """,
            list({r["transaction_id"] for r in affected}),
            user_id,
        )

    return await soft_delete_with_audit(
        conn, user_id, "expense_hashtags", "hashtag", row, hashtag_from_row
    )


async def restore_hashtag(
    conn: asyncpg.Connection,
    user_id: str,
    hashtag_id: str,
) -> dict:
    """Undo a soft-delete on a hashtag and log the restoration.

    Does NOT restore the junction rows cascaded-deleted at delete time —
    restoring them would silently re-tag transactions the user may no
    longer want labeled. The restored hashtag becomes an empty (zero
    transactions) label that can be re-applied manually.

    Checks for name collisions with active hashtags before clearing
    deleted_at.

    Raises:
        not_found: no soft-deleted hashtag with that id for this user.
        conflict: an active hashtag already uses the same name.
    """
    before_row = await fetch_owned_row_or_404(
        conn, "expense_hashtags", hashtag_id, user_id, "hashtag", deleted=True
    )

    if await name_taken(
        conn, "expense_hashtags", user_id, before_row["name"],
        exclude_id=hashtag_id,
    ):
        raise conflict(
            f"Cannot restore hashtag: an active hashtag named '{before_row['name']}' already exists."
        )

    return await restore_with_audit(
        conn, user_id, "expense_hashtags", "hashtag", before_row, hashtag_from_row
    )
