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


@pytest.mark.asyncio
async def test_replay_preserves_200_status_for_put(client, test_data):
    """The Sprint 3 refactor moved status code into the idempotency
    snapshot (the new ``response_status`` column on ``idempotency_keys``).
    The earlier test above proves replay returns 201 for create. This
    test proves the same envelope round-trip works for 200 responses on
    PUTs — guarding against a regression where the helper hardcodes 201
    or drops the status entirely.
    """
    # Create a fresh account so the PUT has a target.
    account_id = str(uuid.uuid4())
    create_r = await client.post(
        "/v1/accounts",
        json={
            "id": account_id,
            "name": f"replay-200-{uuid.uuid4()}",
            "currency_code": "PEN",
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_r.status_code == 201, create_r.text

    update_key = str(uuid.uuid4())
    new_color = f"#{uuid.uuid4().hex[:6]}"

    try:
        first = await client.put(
            f"/v1/accounts/{account_id}",
            json={"color": new_color},
            headers={"X-Idempotency-Key": update_key},
        )
        assert first.status_code == 200, first.text
        first_body = first.json()

        # Replay with the same key — must return 200 (NOT 201, NOT some
        # default), and the body must be byte-for-byte identical to the
        # first call. Confirms response_status round-trips through the
        # idempotency snapshot.
        second = await client.put(
            f"/v1/accounts/{account_id}",
            json={"color": new_color},
            headers={"X-Idempotency-Key": update_key},
        )
        assert second.status_code == 200, (
            f"Replay must preserve 200 status (no per-route drift to 201/default), "
            f"got {second.status_code}: {second.text}"
        )
        assert second.json() == first_body, (
            "Replayed PUT response diverged from first call's body"
        )

        # Snapshot in the DB carries the captured status.
        async with db.pool.acquire() as conn:
            stored_status = await conn.fetchval(
                """
                SELECT response_status FROM idempotency_keys
                WHERE key = $1 AND user_id = $2
                """,
                update_key, test_data.user_id,
            )
        assert stored_status == 200, (
            f"idempotency_keys.response_status should be 200, got {stored_status}"
        )

    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM idempotency_keys WHERE user_id = $1 AND key = ANY($2::text[])",
                test_data.user_id, [update_key],
            )
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
                account_id, test_data.user_id,
            )
            await conn.execute(
                "DELETE FROM expense_bank_accounts WHERE id = $1 AND user_id = $2",
                account_id, test_data.user_id,
            )
