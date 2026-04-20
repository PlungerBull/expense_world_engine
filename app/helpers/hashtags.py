"""Hashtag domain logic.

Service-layer functions for expense_hashtags, called from routers/hashtags.py.
Routers stay thin (HTTP glue + idempotency) and delegate business logic here.

See ``app/helpers/balance.py`` for the convention: these functions do NOT
open their own ``conn.transaction()`` — callers own transaction boundaries.
"""

from typing import Optional
from uuid import UUID

import asyncpg

from app.constants import ActivityAction
from app.errors import conflict, not_found
from app.helpers.activity_log import write_activity_log
from app.helpers.query_builder import dynamic_update, restore, soft_delete
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
    existing = await conn.fetchrow(
        """
        SELECT id FROM expense_hashtags
        WHERE user_id = $1 AND LOWER(name) = LOWER($2) AND deleted_at IS NULL
        """,
        user_id,
        name,
    )
    if existing is not None:
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
            sort_order or 0,
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
    # Empty update — return current state unchanged
    if not fields:
        row = await conn.fetchrow(
            "SELECT * FROM expense_hashtags WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
            hashtag_id,
            user_id,
        )
        if row is None:
            raise not_found("hashtag")
        return hashtag_from_row(row)

    before_row = await conn.fetchrow(
        "SELECT * FROM expense_hashtags WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
        hashtag_id,
        user_id,
    )
    if before_row is None:
        raise not_found("hashtag")

    before = hashtag_from_row(before_row)

    # Name normalization + case-insensitive uniqueness
    if "name" in fields:
        fields["name"] = normalize_name(fields["name"])
        dup = await conn.fetchrow(
            """
            SELECT id FROM expense_hashtags
            WHERE user_id = $1 AND LOWER(name) = LOWER($2)
              AND id != $3 AND deleted_at IS NULL
            """,
            user_id,
            fields["name"],
            hashtag_id,
        )
        if dup is not None:
            raise conflict(f"A hashtag named '{fields['name']}' already exists.")

    after_row = await dynamic_update(conn, "expense_hashtags", fields, hashtag_id, user_id)
    if after_row is None:
        raise not_found("hashtag")

    after = hashtag_from_row(after_row)

    await write_activity_log(
        conn, user_id, "hashtag", hashtag_id, ActivityAction.UPDATED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after


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
         delta sync picks up the hashtag_ids change.
      4. Soft-delete the hashtag row itself.
      5. Write the activity log with before/after snapshots.

    Raises:
        not_found: no active hashtag with that id for this user.
    """
    row = await conn.fetchrow(
        "SELECT * FROM expense_hashtags WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
        hashtag_id,
        user_id,
    )
    if row is None:
        raise not_found("hashtag")

    before = hashtag_from_row(row)

    # Soft-delete all junction rows for this hashtag, capturing the
    # affected transaction IDs so we can bump their version + updated_at.
    # Without the parent bump, /sync would miss the hashtag_ids change.
    #
    # Activity log — per-row entries for the junction table are
    # deliberately NOT written here (see helpers/transactions._sync_hashtags
    # for the rationale). Each affected parent transaction's next delta
    # sync carries the new hashtag_ids list via its version bump, and a
    # single DELETED entry is written for the hashtag itself below.
    affected = await conn.fetch(
        """
        UPDATE expense_transaction_hashtags
        SET deleted_at = now(), updated_at = now(), version = version + 1
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

    after_row = await soft_delete(conn, "expense_hashtags", hashtag_id, user_id)
    after = hashtag_from_row(after_row)

    await write_activity_log(
        conn, user_id, "hashtag", hashtag_id, ActivityAction.DELETED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after


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
    before_row = await conn.fetchrow(
        "SELECT * FROM expense_hashtags WHERE id = $1 AND user_id = $2 AND deleted_at IS NOT NULL",
        hashtag_id,
        user_id,
    )
    if before_row is None:
        raise not_found("hashtag")

    dup = await conn.fetchrow(
        """
        SELECT id FROM expense_hashtags
        WHERE user_id = $1 AND LOWER(name) = LOWER($2)
          AND id != $3 AND deleted_at IS NULL
        """,
        user_id,
        before_row["name"],
        hashtag_id,
    )
    if dup is not None:
        raise conflict(
            f"Cannot restore hashtag: an active hashtag named '{before_row['name']}' already exists."
        )

    before = hashtag_from_row(before_row)

    after_row = await restore(conn, "expense_hashtags", hashtag_id, user_id)
    after = hashtag_from_row(after_row)

    await write_activity_log(
        conn, user_id, "hashtag", hashtag_id, ActivityAction.RESTORED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after
