"""Regression test for idempotency-key replay.

When a client retries a write with the same ``X-Idempotency-Key``, the
engine must return the *exact* stored response from the original call —
same body, same HTTP status code. This is what makes it safe for clients
to retry network timeouts without risking duplicate balance moves.

The service layer refactor split business logic across helper modules
but kept the idempotency check/store in the router. This test guards
against future changes that might accidentally:

  * Skip the cached response check and re-run business logic (leading
    to duplicate inserts on retry).
  * Change the response shape in a way that makes cached responses
    diverge from fresh ones.
  * Strip or alter the HTTP status code on replay.

Run: .venv/bin/pytest tests/test_idempotency_replay.py -v
"""
import uuid

import pytest

from app import db


@pytest.mark.asyncio
async def test_create_transaction_replay_returns_identical_response(client, test_data):
    """POST /transactions twice with the same idempotency key.

    The second call must:
      * Return HTTP 201 (the same status code the first call returned).
      * Return a byte-for-byte identical JSON body.
      * NOT create a second transaction row in the DB.
      * NOT double-apply the balance delta.
    """
    idempotency_key = str(uuid.uuid4())
    payload = {
        "id": str(uuid.uuid4()),
        "title": f"idempotent-{uuid.uuid4()}",
        "amount_cents": -750,
        "date": "2026-04-12T10:00:00Z",
        "account_id": test_data.account_id,
        "category_id": test_data.category_id,
    }

    before_balance = await _get_balance(test_data.account_id)

    txn_id = None
    try:
        # First call — real execution.
        first = await client.post(
            "/v1/transactions",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )
        assert first.status_code == 201, first.text
        first_body = first.json()
        txn_id = first_body["id"]

        # Second call — same key. Must short-circuit to the cached response.
        second = await client.post(
            "/v1/transactions",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )
        assert second.status_code == 201, (
            f"Replay must preserve 201 status, got {second.status_code}"
        )

        # Byte-for-byte equality on the JSON body.
        second_body = second.json()
        assert second_body == first_body, (
            "Replayed response diverged from stored response"
        )

        # The DB must hold exactly one transaction with this id.
        async with db.pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT count(*) FROM expense_transactions WHERE id = $1 AND user_id = $2",
                txn_id, test_data.user_id,
            )
        assert count == 1, (
            f"Expected 1 transaction row after idempotent replay, found {count}"
        )

        # Balance must reflect exactly ONE application of the delta (750
        # subtracted, not 1500). This catches a regression where the
        # cached-response short-circuit is removed and the business logic
        # runs twice.
        after_balance = await _get_balance(test_data.account_id)
        assert after_balance == before_balance - 750, (
            f"Balance should have moved by -750 exactly once; "
            f"before={before_balance} after={after_balance}"
        )

    finally:
        async with db.pool.acquire() as conn:
            if txn_id:
                await conn.execute(
                    "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
                    txn_id, test_data.user_id,
                )
                await conn.execute(
                    "DELETE FROM expense_transactions WHERE id = $1 AND user_id = $2",
                    txn_id, test_data.user_id,
                )
            # Restore the account balance.
            await conn.execute(
                "UPDATE expense_bank_accounts SET current_balance_cents = $1 WHERE id = $2",
                before_balance, test_data.account_id,
            )
            # Purge the idempotency key so it doesn't pollute other tests.
            await conn.execute(
                "DELETE FROM idempotency_keys WHERE key = $1 AND user_id = $2",
                idempotency_key, test_data.user_id,
            )


async def _get_balance(account_id: str) -> int:
    async with db.pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT current_balance_cents FROM expense_bank_accounts WHERE id = $1",
            account_id,
        )
