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
from app.helpers.account_balance import fetch_balance


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
            # Purge the idempotency key so it doesn't pollute other tests.
            await conn.execute(
                "DELETE FROM idempotency_keys WHERE key = $1 AND user_id = $2",
                idempotency_key, test_data.user_id,
            )


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


@pytest.mark.asyncio
async def test_replay_works_forever_no_expiry(client, test_data):
    """Idempotency keys are permanent (sql/026, bug 4.1).

    Under the old 24h TTL, an expired key was invisible to the claim but
    still blocked the re-store (`ON CONFLICT DO NOTHING`), so the key went
    permanently dead and every later retry re-ran the write. Keys no
    longer expire: a replay arbitrarily long after the original write must
    return the stored response and execute nothing. Simulated by
    backdating created_at far past the old TTL — with no expiry filter,
    the row's age must be irrelevant.
    """
    idempotency_key = str(uuid.uuid4())
    payload = {
        "id": str(uuid.uuid4()),
        "title": f"permanent-{uuid.uuid4()}",
        "amount_cents": -1250,
        "date": "2026-05-01T10:00:00Z",
        "account_id": test_data.account_id,
        "category_id": test_data.category_id,
    }
    txn_id = None
    try:
        first = await client.post(
            "/v1/transactions",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )
        assert first.status_code == 201, first.text
        first_body = first.json()
        txn_id = first_body["id"]

        # Age the key row 30 days — well past the deleted 24h TTL.
        async with db.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE idempotency_keys
                SET created_at = created_at - interval '30 days'
                WHERE key = $1 AND user_id = $2
                """,
                idempotency_key, test_data.user_id,
            )

        second = await client.post(
            "/v1/transactions",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )
        assert second.status_code == 201, (
            f"A 30-day-old key must still replay, got {second.status_code}: {second.text}"
        )
        assert second.json() == first_body

        async with db.pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT count(*) FROM expense_transactions WHERE id = $1 AND user_id = $2",
                txn_id, test_data.user_id,
            )
        assert count == 1, f"Aged-key replay must not re-run the write, found {count} rows"
    finally:
        await _cleanup_transaction(txn_id, idempotency_key, test_data.user_id)


@pytest.mark.asyncio
async def test_key_reuse_with_different_body_is_409(client, test_data):
    """Reusing a key for a DIFFERENT request must be loud (sql/026).

    Before the request fingerprint, a reused key silently returned the
    unrelated stored response and the new write vanished — and with
    permanent keys that client bug would be swallowed forever. Now the
    stored sha256(method, path, query, body) mismatches and the engine
    answers 409 in the standard error shape, leaving the original
    snapshot intact and executing nothing.
    """
    idempotency_key = str(uuid.uuid4())
    payload = {
        "id": str(uuid.uuid4()),
        "title": f"fingerprint-{uuid.uuid4()}",
        "amount_cents": -500,
        "date": "2026-05-02T10:00:00Z",
        "account_id": test_data.account_id,
        "category_id": test_data.category_id,
    }
    txn_id = None
    try:
        first = await client.post(
            "/v1/transactions",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )
        assert first.status_code == 201, first.text
        first_body = first.json()
        txn_id = first_body["id"]

        # Same key, different request: a new id and amount.
        different = {**payload, "id": str(uuid.uuid4()), "amount_cents": -200000}
        second = await client.post(
            "/v1/transactions",
            json=different,
            headers={"X-Idempotency-Key": idempotency_key},
        )
        assert second.status_code == 409, (
            f"Key reuse with a different body must 409, "
            f"got {second.status_code}: {second.text}"
        )
        err = second.json()["error"]
        assert err["code"] == "CONFLICT"
        assert err["message"]

        async with db.pool.acquire() as conn:
            # The mismatched request executed nothing.
            rogue = await conn.fetchval(
                "SELECT count(*) FROM expense_transactions WHERE id = $1",
                different["id"],
            )
            # And the original snapshot survived untouched.
            stored_status = await conn.fetchval(
                "SELECT response_status FROM idempotency_keys WHERE key = $1 AND user_id = $2",
                idempotency_key, test_data.user_id,
            )
        assert rogue == 0, "409'd request must not have written a row"
        assert stored_status == 201

        # A correct replay (original body) still works after the 409.
        third = await client.post(
            "/v1/transactions",
            json=payload,
            headers={"X-Idempotency-Key": idempotency_key},
        )
        assert third.status_code == 201
        assert third.json() == first_body
    finally:
        await _cleanup_transaction(txn_id, idempotency_key, test_data.user_id)


async def _cleanup_transaction(txn_id, idempotency_key, user_id):
    async with db.pool.acquire() as conn:
        if txn_id:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
                txn_id, user_id,
            )
            await conn.execute(
                "DELETE FROM expense_transactions WHERE id = $1 AND user_id = $2",
                txn_id, user_id,
            )
        await conn.execute(
            "DELETE FROM idempotency_keys WHERE key = $1 AND user_id = $2",
            idempotency_key, user_id,
        )
