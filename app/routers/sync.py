"""GET /v1/sync — delta sync endpoint.

Wire contract: see plans/virtual-munching-lamport.md and engine-spec.md.
"""
from typing import Optional

from fastapi import APIRouter, Header, Query

from app import db
from app.deps import CurrentUser
from app.errors import validation_error
from app.helpers.sync import (
    WILDCARD_TOKEN,
    fetch_delta,
    get_checkpoint_since,
    rotate_checkpoint,
)
from app.routers.accounts import _account_from_row
from app.routers.auth import _settings_from_row
from app.routers.categories import _category_from_row
from app.routers.hashtags import _hashtag_from_row
from app.routers.inbox import _inbox_from_row
from app.schemas.reconciliations import reconciliation_from_row
from app.schemas.transactions import transaction_from_row

router = APIRouter(prefix="/sync", tags=["sync"])


def _is_valid_uuid(value: Optional[str]) -> bool:
    if not value:
        return False
    try:
        import uuid as _uuid
        _uuid.UUID(value)
        return True
    except (ValueError, TypeError):
        return False


def _transaction_with_hashtags(row) -> dict:
    """Serialize a transaction row including its embedded hashtag_ids array.

    The SQL query in `_fetch_transactions_with_hashtags` adds an aggregated
    `hashtag_ids` column (uuid[]); asyncpg returns it as a list of UUIDs.
    """
    data = transaction_from_row(row)
    data["hashtag_ids"] = [str(h) for h in row["hashtag_ids"]]
    return data


@router.get("")
async def sync(
    auth_user: CurrentUser,
    sync_token: str = Query(..., description="'*' for full fetch, or a token from a prior sync."),
    x_client_id: Optional[str] = Header(None, alias="X-Client-Id"),
):
    if not _is_valid_uuid(x_client_id):
        raise validation_error(
            "X-Client-Id header is required.",
            {"X-Client-Id": "Must be a UUID identifying this client install."},
        )

    async with db.pool.acquire() as conn:
        # REPEATABLE READ gives every read below the same MVCC snapshot, and
        # the checkpoint write at the end commits inside that snapshot — so a
        # concurrent mutation either lands entirely in this sync or entirely
        # in the next, never split across them.
        async with conn.transaction(isolation="repeatable_read"):
            since = await get_checkpoint_since(
                conn, auth_user.id, x_client_id, sync_token
            )
            snapshot_at, deltas, settings_row = await fetch_delta(
                conn, auth_user.id, since
            )
            new_token = await rotate_checkpoint(
                conn, auth_user.id, x_client_id, snapshot_at
            )

        # Serialization happens after the transaction commits — `*_from_row`
        # helpers are pure transforms and don't touch the DB. Account home
        # balances are intentionally null in sync responses; clients that need
        # them call /dashboard, which is the canonical place for derived values.
        return {
            "sync_token": new_token,
            "accounts": [_account_from_row(r) for r in deltas["accounts"]],
            "categories": [_category_from_row(r) for r in deltas["categories"]],
            "hashtags": [_hashtag_from_row(r) for r in deltas["hashtags"]],
            "inbox": [_inbox_from_row(r) for r in deltas["inbox"]],
            "transactions": [_transaction_with_hashtags(r) for r in deltas["transactions"]],
            "reconciliations": [reconciliation_from_row(r) for r in deltas["reconciliations"]],
            "settings": _settings_from_row(settings_row) if settings_row else None,
        }
