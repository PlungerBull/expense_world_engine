"""Integration tests for Step 9.1 — home currency recalculation.

When PUT /auth/settings changes main_currency, every transaction's
amount_home_cents and exchange_rate must be rewritten. Transfer pairs
must net to zero in the new home currency.

Run: .venv/bin/pytest tests/test_home_currency_recalc.py -v
"""
import uuid

import pytest

from app import db


async def _set_currency(client, currency: str):
    return await client.put(
        "/v1/auth/settings",
        json={"main_currency": currency},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )


@pytest.mark.asyncio
async def test_regular_transaction_recalculates(client, test_data):
    """Changing main_currency rewrites amount_home_cents on a regular transaction."""
    try:
        # Verify current state (PEN home, PEN account → rate 1.0)
        async with db.pool.acquire() as conn:
            before = await conn.fetchrow(
                "SELECT exchange_rate, amount_home_cents FROM expense_transactions WHERE id = $1",
                test_data.transaction_id,
            )
        assert float(before["exchange_rate"]) == 1.0
        assert before["amount_home_cents"] == 5000

        # Change to USD — PEN→USD rate is 1/3.75 ≈ 0.2667
        r = await _set_currency(client, "USD")
        assert r.status_code == 200

        async with db.pool.acquire() as conn:
            after = await conn.fetchrow(
                "SELECT exchange_rate, amount_home_cents FROM expense_transactions WHERE id = $1",
                test_data.transaction_id,
            )
        # Rate should now be ~0.2667 (1/3.75), not 1.0
        assert float(after["exchange_rate"]) != 1.0
        # 5000 PEN cents * 0.2667 ≈ 1333 USD cents
        assert after["amount_home_cents"] == round(5000 * (1.0 / 3.75))

    finally:
        await _set_currency(client, "PEN")


@pytest.mark.asyncio
async def test_recalc_round_trip(client, test_data):
    """PEN→USD→PEN restores original amount_home_cents."""
    async with db.pool.acquire() as conn:
        original = await conn.fetchrow(
            "SELECT exchange_rate, amount_home_cents FROM expense_transactions WHERE id = $1",
            test_data.transaction_id,
        )

    try:
        await _set_currency(client, "USD")
        await _set_currency(client, "PEN")

        async with db.pool.acquire() as conn:
            restored = await conn.fetchrow(
                "SELECT exchange_rate, amount_home_cents FROM expense_transactions WHERE id = $1",
                test_data.transaction_id,
            )
        assert float(restored["exchange_rate"]) == 1.0
        assert restored["amount_home_cents"] == original["amount_home_cents"]
    finally:
        await _set_currency(client, "PEN")


@pytest.mark.asyncio
async def test_transfer_pair_nets_to_zero(client, test_data):
    """Cross-currency transfer pair nets to zero in new home currency."""
    usd_account_id = str(uuid.uuid4())
    tx_a_id = str(uuid.uuid4())
    tx_b_id = str(uuid.uuid4())

    async with db.pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, is_person, color, current_balance_cents,
                 is_archived, sort_order, created_at, updated_at)
               VALUES ($1, $2, 'USD Account', 'USD', false, '#0000FF', 0,
                 false, 2, now(), now())""",
            usd_account_id, test_data.user_id,
        )
        # Insert both legs first WITHOUT the FK link
        await conn.execute(
            """INSERT INTO expense_transactions
                (id, user_id, title, amount_cents, amount_home_cents, transaction_type,
                 transfer_direction, date, account_id, category_id, exchange_rate, cleared,
                 created_at, updated_at)
               VALUES ($1, $2, 'Transfer Out', 3750, 3750, 3,
                 1, now(), $3, $4, 1.0, false, now(), now())""",
            tx_a_id, test_data.user_id, test_data.account_id, test_data.category_id,
        )
        await conn.execute(
            """INSERT INTO expense_transactions
                (id, user_id, title, amount_cents, amount_home_cents, transaction_type,
                 transfer_direction, date, account_id, category_id, exchange_rate, cleared,
                 created_at, updated_at)
               VALUES ($1, $2, 'Transfer In', 1000, 3750, 3,
                 2, now(), $3, $4, 3.75, false, now(), now())""",
            tx_b_id, test_data.user_id, usd_account_id, test_data.category_id,
        )
        # Now link them
        await conn.execute(
            "UPDATE expense_transactions SET transfer_transaction_id = $1 WHERE id = $2",
            tx_b_id, tx_a_id,
        )
        await conn.execute(
            "UPDATE expense_transactions SET transfer_transaction_id = $1 WHERE id = $2",
            tx_a_id, tx_b_id,
        )

    try:
        r = await _set_currency(client, "USD")
        assert r.status_code == 200

        async with db.pool.acquire() as conn:
            a = await conn.fetchrow(
                "SELECT amount_home_cents FROM expense_transactions WHERE id = $1", tx_a_id
            )
            b = await conn.fetchrow(
                "SELECT amount_home_cents FROM expense_transactions WHERE id = $1", tx_b_id
            )

        # Both legs must have the same amount_home_cents (zero-sum)
        assert a["amount_home_cents"] == b["amount_home_cents"], (
            f"Transfer not zero-sum: {a['amount_home_cents']} vs {b['amount_home_cents']}"
        )
        # USD side is dominant → home = native = 1000
        assert b["amount_home_cents"] == 1000

    finally:
        await _set_currency(client, "PEN")
        async with db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE expense_transactions SET transfer_transaction_id = NULL WHERE id IN ($1, $2)",
                tx_a_id, tx_b_id,
            )
            await conn.execute(
                "DELETE FROM expense_transactions WHERE id IN ($1, $2)", tx_a_id, tx_b_id
            )
            await conn.execute(
                "DELETE FROM expense_bank_accounts WHERE id = $1", usd_account_id
            )


@pytest.mark.asyncio
async def test_no_change_no_recalculation(client, test_data):
    """Setting main_currency to the same value does not recalculate transactions."""
    # Ensure we're on PEN
    await _set_currency(client, "PEN")

    async with db.pool.acquire() as conn:
        before = await conn.fetchrow(
            "SELECT version FROM expense_transactions WHERE id = $1",
            test_data.transaction_id,
        )

    r = await _set_currency(client, "PEN")
    assert r.status_code == 200

    async with db.pool.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT version FROM expense_transactions WHERE id = $1",
            test_data.transaction_id,
        )

    assert after["version"] == before["version"], "Transaction version should not bump when currency unchanged"


@pytest.mark.asyncio
async def test_recalc_bumps_version_for_sync(client, test_data):
    """Recalculated transactions bump version+updated_at so /sync sees them."""
    async with db.pool.acquire() as conn:
        before = await conn.fetchrow(
            "SELECT version, updated_at FROM expense_transactions WHERE id = $1",
            test_data.transaction_id,
        )

    try:
        await _set_currency(client, "USD")

        async with db.pool.acquire() as conn:
            after = await conn.fetchrow(
                "SELECT version, updated_at FROM expense_transactions WHERE id = $1",
                test_data.transaction_id,
            )

        assert after["version"] > before["version"]
        assert after["updated_at"] > before["updated_at"]
    finally:
        await _set_currency(client, "PEN")


@pytest.mark.asyncio
async def test_activity_log_written(client, test_data):
    """One activity_log entry for the settings change (includes recalc summary)."""
    async with db.pool.acquire() as conn:
        count_before = await conn.fetchval(
            "SELECT count(*) FROM activity_log WHERE user_id = $1 AND resource_type = 'user_settings'",
            test_data.user_id,
        )

    try:
        await _set_currency(client, "USD")

        async with db.pool.acquire() as conn:
            count_after = await conn.fetchval(
                "SELECT count(*) FROM activity_log WHERE user_id = $1 AND resource_type = 'user_settings'",
                test_data.user_id,
            )

        assert count_after == count_before + 1
    finally:
        await _set_currency(client, "PEN")
