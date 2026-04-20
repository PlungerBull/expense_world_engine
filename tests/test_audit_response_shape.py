"""Regression tests for response-shape additions from Sprint 1.

Covers the wire-contract guarantees clients depend on:

  * Inbox responses include `amount_home_cents` and (for transfer rows)
    `transfer_amount_home_cents`, computed from the stored exchange_rate.
  * Reconciliation responses include `beginning_balance_home_cents` and
    `ending_balance_home_cents`, resolved through the same dedup-batched
    rate lookup the list endpoint uses.
  * `?debit_as_negative=true` flips the sign of `amount_cents` and
    `amount_home_cents` on /inbox and /sync (the two endpoints that
    grew the flag in Sprint 1.4 / 1.5).
  * The system_key column on expense_categories survives a display-name
    rename — a renamed @Transfer / @Debt is still found by the transfer
    pipeline, so subsequent transfers reuse the same row instead of
    lazily creating a duplicate. This was the bug Sprint 1.1 fixed.

Run: .venv/bin/pytest tests/test_audit_response_shape.py -v
"""
import uuid

import pytest

from app import db


CLIENT_ID = str(uuid.uuid4())
SYNC_HEADERS = {"X-Client-Id": CLIENT_ID}


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------


async def _cleanup_inbox(inbox_id: str, user_id: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
            inbox_id, user_id,
        )
        await conn.execute(
            "DELETE FROM expense_transaction_inbox WHERE id = $1 AND user_id = $2",
            inbox_id, user_id,
        )


async def _cleanup_recon(recon_id: str, user_id: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE expense_transactions SET reconciliation_id = NULL WHERE reconciliation_id = $1",
            recon_id,
        )
        await conn.execute(
            "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
            recon_id, user_id,
        )
        await conn.execute(
            "DELETE FROM expense_reconciliations WHERE id = $1 AND user_id = $2",
            recon_id, user_id,
        )


async def _hard_delete_txn(txn_id: str, user_id: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM expense_transaction_hashtags WHERE transaction_id = $1 AND user_id = $2",
            txn_id, user_id,
        )
        await conn.execute(
            "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
            txn_id, user_id,
        )
        await conn.execute(
            "DELETE FROM expense_transactions WHERE id = $1 AND user_id = $2",
            txn_id, user_id,
        )


async def _restore_balance(account_id: str, delta: int) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE expense_bank_accounts SET current_balance_cents = current_balance_cents + $1 WHERE id = $2",
            delta, account_id,
        )


# ---------------------------------------------------------------------------
# Inbox amount_home_cents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbox_response_includes_home_cents_computed_from_rate(
    client, test_data,
):
    """Inbox responses include amount_home_cents = round(amount_cents *
    exchange_rate). For test_data.account_id (PEN), the seeded USD->PEN
    rate is 3.75, so a 1000-cent expense at rate=3.75 produces a stored
    amount_cents=1000 and amount_home_cents=3750.
    """
    inbox_id = str(uuid.uuid4())
    create_r = await client.post(
        "/v1/inbox",
        json={
            "id": inbox_id,
            "title": f"home-cents-{uuid.uuid4()}",
            "amount_cents": -1000,  # signed; engine stores abs() and sets type
            "date": "2026-04-12T12:00:00Z",
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
            "exchange_rate": 3.75,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_r.status_code == 201, create_r.text

    try:
        body = create_r.json()
        assert body["amount_cents"] == 1000  # stored positive
        assert body["amount_home_cents"] == 3750, (
            f"Expected 1000 * 3.75 = 3750, got {body['amount_home_cents']}"
        )
        # transfer fields absent → home variant is null, not missing.
        assert "transfer_amount_home_cents" in body
        assert body["transfer_amount_home_cents"] is None

        # Same shape on the GET path.
        get_r = await client.get(f"/v1/inbox/{inbox_id}")
        assert get_r.status_code == 200
        get_body = get_r.json()
        assert get_body["amount_home_cents"] == 3750

    finally:
        await _cleanup_inbox(inbox_id, test_data.user_id)


# ---------------------------------------------------------------------------
# Reconciliation home_cents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconciliation_response_includes_home_cents(client, test_data):
    """POST /v1/reconciliations and GET /v1/reconciliations/{id} both
    include beginning/ending_balance_home_cents fields. For a same-
    currency account (PEN main currency), the home values equal the
    native values via the identity rate.
    """
    recon_id = str(uuid.uuid4())
    create_r = await client.post(
        "/v1/reconciliations",
        json={
            "id": recon_id,
            "account_id": test_data.account_id,
            "name": f"home-cents-recon-{uuid.uuid4()}",
            "beginning_balance_cents": 1000,
            "ending_balance_cents": 5000,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_r.status_code == 201, create_r.text

    try:
        body = create_r.json()
        assert "beginning_balance_home_cents" in body, (
            f"Missing beginning_balance_home_cents in response: {body}"
        )
        assert "ending_balance_home_cents" in body
        assert body["beginning_balance_cents"] == 1000
        assert body["ending_balance_cents"] == 5000

        # Detail endpoint surfaces the same fields.
        get_r = await client.get(f"/v1/reconciliations/{recon_id}")
        assert get_r.status_code == 200
        get_body = get_r.json()
        assert "beginning_balance_home_cents" in get_body
        assert "ending_balance_home_cents" in get_body

    finally:
        await _cleanup_recon(recon_id, test_data.user_id)


# ---------------------------------------------------------------------------
# debit_as_negative on /inbox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debit_as_negative_flips_inbox_expense_amounts(client, test_data):
    """For an EXPENSE inbox row, ?debit_as_negative=true returns
    amount_cents and amount_home_cents as negative. Default behavior
    keeps both positive.
    """
    inbox_id = str(uuid.uuid4())
    await client.post(
        "/v1/inbox",
        json={
            "id": inbox_id,
            "title": f"flag-test-{uuid.uuid4()}",
            "amount_cents": -500,
            "date": "2026-04-12T12:00:00Z",
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
            "exchange_rate": 1.0,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )

    try:
        # Default — positive.
        r_default = await client.get(f"/v1/inbox/{inbox_id}")
        assert r_default.status_code == 200
        assert r_default.json()["amount_cents"] == 500
        assert r_default.json()["amount_home_cents"] == 500

        # With flag — negative on the expense leg.
        r_flag = await client.get(
            f"/v1/inbox/{inbox_id}",
            params={"debit_as_negative": "true"},
        )
        assert r_flag.status_code == 200
        assert r_flag.json()["amount_cents"] == -500
        assert r_flag.json()["amount_home_cents"] == -500

    finally:
        await _cleanup_inbox(inbox_id, test_data.user_id)


# ---------------------------------------------------------------------------
# debit_as_negative on /sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debit_as_negative_flips_sync_transaction_amounts(client, test_data):
    """The sync delta flag negates expense and transfer-debit
    amounts on the transactions[] payload; default keeps everything
    positive.
    """
    txn_id = str(uuid.uuid4())
    create_r = await client.post(
        "/v1/transactions",
        json={
            "id": txn_id,
            "title": f"sync-flag-{uuid.uuid4()}",
            "amount_cents": -800,
            "date": "2026-04-12T12:00:00Z",
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_r.status_code == 201, create_r.text

    try:
        # Wildcard sync, default.
        r_default = await client.get(
            "/v1/sync",
            params={"sync_token": "*"},
            headers=SYNC_HEADERS,
        )
        assert r_default.status_code == 200, r_default.text
        txns_default = {t["id"]: t for t in r_default.json()["transactions"]}
        assert txn_id in txns_default
        assert txns_default[txn_id]["amount_cents"] == 800

        # With flag.
        r_flag = await client.get(
            "/v1/sync",
            params={"sync_token": "*", "debit_as_negative": "true"},
            headers={"X-Client-Id": str(uuid.uuid4())},  # fresh client → fresh checkpoint
        )
        assert r_flag.status_code == 200, r_flag.text
        txns_flag = {t["id"]: t for t in r_flag.json()["transactions"]}
        assert txn_id in txns_flag
        assert txns_flag[txn_id]["amount_cents"] == -800

    finally:
        await _hard_delete_txn(txn_id, test_data.user_id)
        await _restore_balance(test_data.account_id, 800)


# ---------------------------------------------------------------------------
# system_key rename safety — Sprint 1.1 bug fix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_category_survives_rename(client, test_data):
    """The transfer pipeline auto-seeds @Transfer (system_key='transfer')
    on first use. After a user renames the display name, a subsequent
    transfer must REUSE the same category row — looking up by system_key,
    not by name. Pre-Sprint-1.1 this lazily created a new @Transfer row
    every time, fragmenting category history.
    """
    # Need a second account to enable transfer.
    second_account_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, is_person, color,
                 current_balance_cents, is_archived, sort_order, created_at, updated_at)
            VALUES ($1, $2, 'rename-target', 'PEN', false, '#abcdef',
                    50000, false, 9, now(), now())
            """,
            second_account_id, test_data.user_id,
        )

    primary_a = sibling_a = primary_b = sibling_b = None
    transfer_category_id = None

    try:
        # First transfer — auto-seeds @Transfer.
        primary_a = str(uuid.uuid4())
        sibling_a = str(uuid.uuid4())
        r1 = await client.post(
            "/v1/transactions",
            json={
                "id": primary_a,
                "title": f"transfer-1-{uuid.uuid4()}",
                "amount_cents": -300,
                "date": "2026-04-12T12:00:00Z",
                "account_id": test_data.account_id,
                "category_id": test_data.category_id,
                "transfer": {
                    "id": sibling_a,
                    "account_id": second_account_id,
                    "amount_cents": 300,
                },
            },
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r1.status_code == 201, r1.text
        first_category_id = r1.json()["category_id"]

        # Look up the seeded @Transfer row by system_key.
        async with db.pool.acquire() as conn:
            transfer_row = await conn.fetchrow(
                """
                SELECT id, name FROM expense_categories
                WHERE user_id = $1 AND system_key = 'transfer' AND deleted_at IS NULL
                """,
                test_data.user_id,
            )
        assert transfer_row is not None, "Transfer should have seeded a system_key='transfer' row"
        transfer_category_id = str(transfer_row["id"])
        original_name = transfer_row["name"]
        assert first_category_id == transfer_category_id

        # Rename the display name.
        new_name = f"MyTransfersRenamed-{uuid.uuid4()}"
        rename_r = await client.put(
            f"/v1/categories/{transfer_category_id}",
            json={"name": new_name},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert rename_r.status_code == 200, rename_r.text
        assert rename_r.json()["name"] == new_name

        # Second transfer — engine must reuse the same row, NOT auto-create a new @Transfer.
        primary_b = str(uuid.uuid4())
        sibling_b = str(uuid.uuid4())
        r2 = await client.post(
            "/v1/transactions",
            json={
                "id": primary_b,
                "title": f"transfer-2-{uuid.uuid4()}",
                "amount_cents": -200,
                "date": "2026-04-12T12:00:00Z",
                "account_id": test_data.account_id,
                "category_id": test_data.category_id,
                "transfer": {
                    "id": sibling_b,
                    "account_id": second_account_id,
                    "amount_cents": 200,
                },
            },
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r2.status_code == 201, r2.text
        second_category_id = r2.json()["category_id"]

        assert second_category_id == transfer_category_id, (
            f"Renamed system category should be reused; "
            f"first transfer used {transfer_category_id}, second used {second_category_id}"
        )

        # Confirm there's still exactly ONE active row with system_key='transfer'.
        async with db.pool.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT count(*) FROM expense_categories
                WHERE user_id = $1 AND system_key = 'transfer' AND deleted_at IS NULL
                """,
                test_data.user_id,
            )
        assert count == 1, f"Expected 1 active transfer system row, found {count}"

    finally:
        # Restore the original display name so subsequent test runs are deterministic.
        if transfer_category_id:
            async with db.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE expense_categories SET name = $1 WHERE id = $2",
                    original_name, transfer_category_id,
                )
        # Delete the test transfer transactions.
        ids = [tid for tid in (primary_a, sibling_a, primary_b, sibling_b) if tid]
        if ids:
            async with db.pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM activity_log WHERE resource_id = ANY($1::uuid[]) AND user_id = $2",
                    ids, test_data.user_id,
                )
                await conn.execute(
                    "DELETE FROM expense_transactions WHERE id = ANY($1::uuid[]) AND user_id = $2",
                    ids, test_data.user_id,
                )
        # Restore primary account balance (we created two outflows of 300 and 200).
        await _restore_balance(test_data.account_id, 500)
        # Drop the second account.
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM expense_bank_accounts WHERE id = $1 AND user_id = $2",
                second_account_id, test_data.user_id,
            )
