"""Delta-sync helpers.

The single entry point is `run_sync(conn, user_id, client_id, sync_token)` which
wraps every read and the checkpoint write in one REPEATABLE READ transaction so
that the snapshot is consistent across all synced tables.

Wire format and design rationale: see docs/engine-spec.md §Sync and docs/api-design-principles.md §3.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import asyncpg

from app.errors import validation_error

WILDCARD_TOKEN = "*"


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, TypeError):
        return False


async def _fetch_table(
    conn: asyncpg.Connection,
    table: str,
    user_id: str,
    since: Optional[datetime],
) -> list:
    """Generic per-table delta read.

    `since=None` means full fetch (wildcard) — also excludes soft-deleted rows
    so the first sync doesn't ship tombstones for rows the client has never seen.
    `since=<timestamp>` means delta — includes soft-deleted rows as tombstones.
    """
    if since is None:
        return await conn.fetch(
            f"SELECT * FROM {table} WHERE user_id = $1 AND deleted_at IS NULL",
            user_id,
        )
    return await conn.fetch(
        f"SELECT * FROM {table} WHERE user_id = $1 AND updated_at > $2",
        user_id,
        since,
    )


async def _fetch_transactions_with_hashtags(
    conn: asyncpg.Connection,
    user_id: str,
    since: Optional[datetime],
) -> list:
    """Fetch transactions with `hashtag_ids` aggregated from the junction table.

    Junction rows have `transaction_source = 1` for ledger transactions
    (the production convention; see api-design-principles.md). Soft-deleted
    junction rows are excluded from the array via FILTER WHERE deleted_at IS NULL.
    """
    where = (
        "t.user_id = $1 AND t.deleted_at IS NULL"
        if since is None
        else "t.user_id = $1 AND t.updated_at > $2"
    )
    params: list = [user_id]
    if since is not None:
        params.append(since)

    return await conn.fetch(
        f"""
        SELECT
            t.*,
            COALESCE(
                (
                    SELECT array_agg(th.hashtag_id ORDER BY th.hashtag_id)
                    FROM expense_transaction_hashtags th
                    WHERE th.transaction_id = t.id
                      AND th.transaction_source = 1
                      AND th.deleted_at IS NULL
                ),
                ARRAY[]::uuid[]
            ) AS hashtag_ids
        FROM expense_transactions t
        WHERE {where}
        """,
        *params,
    )


async def _fetch_user_settings(
    conn: asyncpg.Connection,
    user_id: str,
    since: Optional[datetime],
) -> Optional[asyncpg.Record]:
    """user_settings is a singleton — at most one row per user.

    Returns the row on wildcard (always), or on delta if `updated_at > since`.
    Returns None on delta when settings haven't changed since the checkpoint.
    """
    if since is None:
        return await conn.fetchrow(
            "SELECT * FROM user_settings WHERE user_id = $1",
            user_id,
        )
    return await conn.fetchrow(
        "SELECT * FROM user_settings WHERE user_id = $1 AND updated_at > $2",
        user_id,
        since,
    )


async def fetch_delta(
    conn: asyncpg.Connection,
    user_id: str,
    since: Optional[datetime],
) -> tuple[datetime, dict, Optional[asyncpg.Record]]:
    """Read every synced table at the same snapshot.

    Caller must have already opened a REPEATABLE READ transaction so that all
    queries below see the same MVCC snapshot. The returned `snapshot_at` is
    `now()` evaluated INSIDE that transaction — it becomes the next checkpoint
    boundary, and the next sync will use it for `WHERE updated_at > snapshot_at`.
    """
    snapshot_at = await conn.fetchval("SELECT now()")

    accounts = await _fetch_table(conn, "expense_bank_accounts", user_id, since)
    categories = await _fetch_table(conn, "expense_categories", user_id, since)
    hashtags = await _fetch_table(conn, "expense_hashtags", user_id, since)
    inbox = await _fetch_table(conn, "expense_transaction_inbox", user_id, since)
    reconciliations = await _fetch_table(conn, "expense_reconciliations", user_id, since)
    transactions = await _fetch_transactions_with_hashtags(conn, user_id, since)
    settings = await _fetch_user_settings(conn, user_id, since)

    return (
        snapshot_at,
        {
            "accounts": accounts,
            "categories": categories,
            "hashtags": hashtags,
            "inbox": inbox,
            "transactions": transactions,
            "reconciliations": reconciliations,
        },
        settings,
    )


async def get_checkpoint_since(
    conn: asyncpg.Connection,
    user_id: str,
    client_id: str,
    sync_token: str,
) -> Optional[datetime]:
    """Resolve a sync_token to its `last_sync_at` timestamp.

    Wildcard `*` returns None → caller treats it as a full fetch.
    A real token must match an existing checkpoint row for this `(user, client)`.
    Mismatches raise INVALID_SYNC_TOKEN — client must retry with `*`.
    """
    if sync_token == WILDCARD_TOKEN:
        return None

    if not _is_uuid(sync_token):
        raise validation_error(
            "Invalid sync_token.",
            {"sync_token": "Must be '*' or a UUID returned by a previous sync."},
        )

    row = await conn.fetchrow(
        """
        SELECT last_sync_at FROM sync_checkpoints
        WHERE user_id = $1 AND client_id = $2 AND last_sync_token = $3
        """,
        user_id,
        client_id,
        sync_token,
    )
    if row is None:
        raise validation_error(
            "sync_token is unknown for this client. Retry with sync_token=*.",
            {"sync_token": "Unknown token."},
        )
    return row["last_sync_at"]


async def rotate_checkpoint(
    conn: asyncpg.Connection,
    user_id: str,
    client_id: str,
    snapshot_at: datetime,
) -> str:
    """Issue a new opaque token and upsert the per-client checkpoint row.

    Old token is replaced atomically — once a sync completes, the previous
    token for this client is unusable. This is what makes leaked tokens harmless
    after the next legitimate sync.
    """
    new_token = str(uuid.uuid4())
    await conn.execute(
        """
        INSERT INTO sync_checkpoints
            (id, user_id, client_id, last_sync_token, last_sync_at, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, now(), now())
        ON CONFLICT (user_id, client_id)
        DO UPDATE SET
            last_sync_token = EXCLUDED.last_sync_token,
            last_sync_at    = EXCLUDED.last_sync_at,
            updated_at      = now()
        """,
        str(uuid.uuid4()),
        user_id,
        client_id,
        new_token,
        snapshot_at,
    )
    return new_token
