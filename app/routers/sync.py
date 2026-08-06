"""GET /v1/sync — delta sync endpoint.

Wire contract: see docs/engine-spec.md §Sync.
"""
from typing import Optional

from fastapi import APIRouter, Header, Query

from app import db
from app.deps import CurrentUser
from app.errors import validation_error
from app.helpers.account_balance import fetch_balances
from app.helpers.formatting import apply_debit_as_negative, apply_debit_as_negative_inbox
from app.helpers.sync import (
    WILDCARD_TOKEN,
    fetch_delta,
    get_checkpoint_since,
    rotate_checkpoint,
)
from app.schemas.accounts import account_from_row
from app.schemas.auth import settings_from_row
from app.schemas.categories import category_from_row
from app.schemas.hashtags import hashtag_from_row
from app.schemas.inbox import inbox_from_row
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


@router.get("")
async def sync(
    auth_user: CurrentUser,
    sync_token: str = Query(..., description="'*' for full fetch, or a token from a prior sync."),
    debit_as_negative: bool = Query(False),
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
            # Balances are computed from the ledger (sql/022), so this read must
            # happen INSIDE the snapshot — outside it, the account rows and the
            # transactions they summarise would come from two different points in
            # time and a delta could ship a balance that never existed. Scoped to
            # the accounts actually in the delta, but each still reports its FULL
            # balance: a delta of changed rows is not a delta of money.
            account_balances = await fetch_balances(
                conn, auth_user.id, [r["id"] for r in deltas["accounts"]]
            )
            new_token = await rotate_checkpoint(
                conn, auth_user.id, x_client_id, snapshot_at
            )

        # Reconciliations carry no home-currency values. They are scoped to one
        # account and therefore to one currency, so there is nothing to combine
        # and nothing to convert (docs/rework/WP2). A rate resolution used to
        # run here, deliberately outside the REPEATABLE READ snapshot.

        # Account home balances are intentionally null in sync responses;
        # clients that need them call /dashboard, which is the canonical place
        # for derived account-level values. The NATIVE balance is populated, from
        # the snapshot-consistent read above.
        #
        # ⚠️ Known gap, retired by docs/rework/WP4. Before sql/022 a ledger write
        # bumped the account row's updated_at (the balance UPDATE did it as a side
        # effect), which is what re-entered the account into the next delta with
        # its new balance. Nothing writes the account row on a transaction now, so
        # a balance can change without the account being re-delivered. The value
        # is never wrong when it IS delivered — it just stops being pushed. No
        # client is affected (sync_checkpoints holds zero rows) and WP4 deletes
        # this endpoint. See docs/client-breaking-changes.md.
        inbox_rows = [inbox_from_row(r) for r in deltas["inbox"]]
        # `_fetch_transactions_with_hashtags` adds an aggregated `hashtag_ids`
        # uuid[] column; `transaction_from_row` reads it off the row directly.
        transaction_rows = [transaction_from_row(r) for r in deltas["transactions"]]
        if debit_as_negative:
            inbox_rows = [apply_debit_as_negative_inbox(d) for d in inbox_rows]
            transaction_rows = [apply_debit_as_negative(d) for d in transaction_rows]

        return {
            "sync_token": new_token,
            "accounts": [
                account_from_row(r, account_balances[str(r["id"])])
                for r in deltas["accounts"]
            ],
            "categories": [category_from_row(r) for r in deltas["categories"]],
            "hashtags": [hashtag_from_row(r) for r in deltas["hashtags"]],
            "inbox": inbox_rows,
            "transactions": transaction_rows,
            "reconciliations": [
                reconciliation_from_row(r) for r in deltas["reconciliations"]
            ],
            "settings": settings_from_row(settings_row) if settings_row else None,
        }
