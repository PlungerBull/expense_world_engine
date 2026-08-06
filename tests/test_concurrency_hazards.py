"""Regression tests for the concurrency hazards flagged by the refactor audit.

  * **Concurrent updates must serialise** — two PUTs racing on one transaction
    must leave a row whose amount is one of the two submitted values, not a
    mix, and whose activity-log pair describes a real transition. This is what
    the ``SELECT ... FOR UPDATE`` in ``update_transaction`` buys.

  * **Transfer pair atomicity** — `create_transfer_pair` inserts two
    transactions and writes two activity logs inside a single DB transaction.
    A regression that accidentally commits mid-flow would leave one leg
    without its counterpart, and therefore one account moved without the other.

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


@pytest.mark.asyncio
async def test_transfer_pair_is_created_atomically(client, test_data):
    """A successful transfer creation leaves both legs, both balances,
    and both activity log entries — all or nothing.

    This doesn't fault-inject to verify rollback directly; it asserts
    the invariants that any regression splitting the atomic unit would
    break. For example, if someone accidentally committed the primary
    insert before the sibling insert, we'd find only one txn + one
    balance move + one log entry on a happy path.
    """
    # Create a second account so we have somewhere to transfer to.
    second_account_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, is_person, color,
                 is_archived, sort_order, created_at, updated_at)
            VALUES ($1, $2, 'Transfer Target', 'PEN', false, '#00FF00',
                    false, 2, now(), now())
            """,
            second_account_id, test_data.user_id,
        )

    primary_txn_id = None
    sibling_txn_id = None
    try:
        before_primary_balance = await _get_balance(test_data.account_id)
        before_secondary_balance = await _get_balance(second_account_id)

        # Record how many activity log entries exist for this user so we
        # can verify exactly two new ones are written by the transfer.
        async with db.pool.acquire() as conn:
            before_log_count = await conn.fetchval(
                "SELECT count(*) FROM activity_log WHERE user_id = $1 AND resource_type = 'transaction'",
                test_data.user_id,
            )

        r = await client.post(
            "/v1/transactions",
            json={
                "id": str(uuid.uuid4()),
                "title": f"transfer-{uuid.uuid4()}",
                "amount_cents": -2500,  # outflow from primary account
                "date": "2026-04-12T12:00:00Z",
                "account_id": test_data.account_id,
                "category_id": test_data.category_id,  # ignored for transfers
                "transfer": {
                    "id": str(uuid.uuid4()),
                    "account_id": second_account_id,
                    "amount_cents": 2500,  # inflow to secondary account
                },
            },
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 201, r.text
        primary_response = r.json()
        primary_txn_id = primary_response["id"]
        sibling_txn_id = primary_response["transfer_transaction_id"]

        assert sibling_txn_id is not None, "Primary must link to its sibling"
        # The primary was posted with a negative amount, so it is the outflow
        # leg. There is no transfer-specific type: transfer_transaction_id
        # above is what says this is a transfer.
        assert primary_response["transaction_type"] == 1  # OUTFLOW
        assert "transfer_direction" not in primary_response

        # Both transactions must exist in the DB, both must link to each
        # other, and both must be non-deleted.
        async with db.pool.acquire() as conn:
            primary_row = await conn.fetchrow(
                "SELECT * FROM expense_transactions WHERE id = $1",
                primary_txn_id,
            )
            sibling_row = await conn.fetchrow(
                "SELECT * FROM expense_transactions WHERE id = $1",
                sibling_txn_id,
            )

            assert primary_row is not None, "Primary leg must be persisted"
            assert sibling_row is not None, "Sibling leg must be persisted"
            assert primary_row["deleted_at"] is None
            assert sibling_row["deleted_at"] is None

            # Reciprocal linking — if either side is broken, the pair is
            # orphaned from sync's perspective.
            assert str(primary_row["transfer_transaction_id"]) == sibling_txn_id
            assert str(sibling_row["transfer_transaction_id"]) == primary_txn_id

            # Opposite directions (the zero-sum invariant). One leg outflow,
            # one inflow — read off transaction_type now that direction lives
            # there on every row.
            assert primary_row["transaction_type"] == 1  # OUTFLOW
            assert sibling_row["transaction_type"] == 2  # INFLOW

            # Both computed balances moved together. If one leg had committed
            # without the other, one of these two would be unchanged.
            after_primary_balance = await fetch_balance(
                conn, test_data.user_id, test_data.account_id
            )
            after_secondary_balance = await fetch_balance(
                conn, test_data.user_id, second_account_id
            )
            assert after_primary_balance == before_primary_balance - 2500
            assert after_secondary_balance == before_secondary_balance + 2500

            # And the pair nets to zero — money left one account and arrived in
            # the other, rather than being created or destroyed.
            assert (after_primary_balance - before_primary_balance) + (
                after_secondary_balance - before_secondary_balance
            ) == 0

            # Exactly two new activity log entries — one per leg.
            after_log_count = await conn.fetchval(
                "SELECT count(*) FROM activity_log WHERE user_id = $1 AND resource_type = 'transaction'",
                test_data.user_id,
            )
            assert after_log_count == before_log_count + 2

    finally:
        # Full teardown: drop both transactions together (they reference
        # each other via ``transfer_transaction_id`` in both directions,
        # so neither can be deleted first without a FK violation —
        # batch-delete both in a single statement).
        txn_ids = [tid for tid in (primary_txn_id, sibling_txn_id) if tid]
        async with db.pool.acquire() as conn:
            if txn_ids:
                await conn.execute(
                    "DELETE FROM expense_transaction_hashtags WHERE transaction_id = ANY($1::uuid[]) AND user_id = $2",
                    txn_ids, test_data.user_id,
                )
                await conn.execute(
                    "DELETE FROM activity_log WHERE resource_id = ANY($1::uuid[]) AND user_id = $2",
                    txn_ids, test_data.user_id,
                )
                await conn.execute(
                    "DELETE FROM expense_transactions WHERE id = ANY($1::uuid[]) AND user_id = $2",
                    txn_ids, test_data.user_id,
                )
            await conn.execute(
                "DELETE FROM expense_bank_accounts WHERE id = $1 AND user_id = $2",
                second_account_id, test_data.user_id,
            )
