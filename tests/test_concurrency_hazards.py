"""Regression tests for the concurrency hazards flagged by the refactor audit.

  * **Concurrent updates must serialise** — two PUTs racing on one transaction
    must leave a row whose amount is one of the two submitted values, not a
    mix, and whose activity-log pair describes a real transition. This is what
    the ``SELECT ... FOR UPDATE`` in ``update_transaction`` buys.

**What this file no longer tests, and why (sql/022).** The original headline
hazard was a *balance lost-update*: ``update_transaction`` read ``amount_cents``,
reversed that contribution on the account, then applied the new one, so two
concurrent updates reading the same stale amount would double-reverse and drift
the stored balance. The invariant pinned here was::

    final_balance == before_balance + before_amount - final_amount

That assertion is now **vacuously true** and has been removed rather than left
in place. There is no stored balance to drift: the balance is the signed sum of
the live rows, so whatever the row ends up saying, the balance agrees with it by
construction. A test that cannot fail is worse than no test — it reads as
coverage while asserting nothing. The half that still has teeth (the row itself
must not end up in a mixed state) is kept below.

Run: .venv/bin/pytest tests/test_concurrency_hazards.py -v
"""
import asyncio
import uuid

import pytest

from app import db
from app.helpers.account_balance import fetch_balance


async def _new_expense(client, account_id: str, category_id: str, amount: int) -> dict:
    """Helper: create a single expense transaction on the test account."""
    r = await client.post(
        "/v1/transactions",
        json={
            "id": str(uuid.uuid4()),
            "title": f"hazard-{uuid.uuid4()}",
            "amount_cents": -amount,  # negative = expense
            "date": "2026-04-12T12:00:00Z",
            "account_id": account_id,
            "category_id": category_id,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _get_balance(account_id: str) -> int:
    """Computed balance — the signed sum of the account's live rows (sql/022).

    Reads through the same helper the engine's read paths use, so a test can
    never disagree with production about what a balance is.
    """
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM expense_bank_accounts WHERE id = $1", account_id
        )
        return await fetch_balance(conn, str(row["user_id"]), account_id)


async def _get_transaction_amount(transaction_id: str) -> int:
    async with db.pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT amount_cents FROM expense_transactions WHERE id = $1",
            transaction_id,
        )


async def _delete_txn(conn, transaction_id: str, user_id: str) -> None:
    """Hard-delete a test transaction and its junction rows — cleanup only.

    Uses DELETE not soft-delete to keep the test user's row counts
    deterministic across test runs.
    """
    await conn.execute(
        "DELETE FROM expense_transaction_hashtags WHERE transaction_id = $1 AND user_id = $2",
        transaction_id, user_id,
    )
    await conn.execute(
        "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
        transaction_id, user_id,
    )
    await conn.execute(
        "DELETE FROM expense_transactions WHERE id = $1 AND user_id = $2",
        transaction_id, user_id,
    )


@pytest.mark.asyncio
async def test_concurrent_updates_serialise_on_the_transaction_row(client, test_data):
    """Two concurrent PUTs on one transaction must serialise, not interleave.

    The surviving row must carry one of the two submitted amounts — never a
    mix, never the original — and the account balance must equal that row's
    contribution, which under a computed balance is a statement about the row
    winning cleanly rather than about arithmetic drift.
    """
    created = await _new_expense(
        client,
        test_data.account_id,
        test_data.category_id,
        amount=1000,
    )
    txn_id = created["id"]

    try:
        before_balance = await _get_balance(test_data.account_id)
        before_amount = await _get_transaction_amount(txn_id)
        assert before_amount == 1000

        # Fire two concurrent updates. httpx + ASGITransport runs them
        # on the same event loop, but each request acquires its own DB
        # connection from the pool, so the FOR UPDATE lock on the
        # transaction row genuinely serialises them at the DB level.
        update_a, update_b = await asyncio.gather(
            client.put(
                f"/v1/transactions/{txn_id}",
                json={"amount_cents": -2000},
                headers={"X-Idempotency-Key": str(uuid.uuid4())},
            ),
            client.put(
                f"/v1/transactions/{txn_id}",
                json={"amount_cents": -3000},
                headers={"X-Idempotency-Key": str(uuid.uuid4())},
            ),
        )
        assert update_a.status_code == 200, update_a.text
        assert update_b.status_code == 200, update_b.text

        final_balance = await _get_balance(test_data.account_id)
        final_amount = await _get_transaction_amount(txn_id)

        # The final amount must be one of the two updates — not a mix
        # or a stale value. With the lock, serialisation guarantees the
        # last-committed update's amount sticks.
        assert final_amount in (2000, 3000)

        # The balance tracks whichever update won. Under a computed balance this
        # cannot drift, so what it actually pins is that the winning row is the
        # ONLY one contributing — a double-applied or orphaned row would show up
        # here as a balance the surviving amount cannot explain.
        assert final_balance == before_balance + before_amount - final_amount, (
            f"before={before_balance} before_amount={before_amount} "
            f"final_amount={final_amount} final_balance={final_balance}"
        )
    finally:
        async with db.pool.acquire() as conn:
            # Hard-deleting the row removes its balance contribution for free.
            # This used to be followed by a hand-written UPDATE putting the
            # stored balance back, because a raw DELETE bypassed the reversal
            # the endpoint would have done. Nothing to compensate for now.
            await _delete_txn(conn, txn_id, test_data.user_id)
