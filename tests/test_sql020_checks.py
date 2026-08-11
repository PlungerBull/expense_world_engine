"""Pins for sql/020's two storage invariants on ``expense_transactions``.

  * transactions_transaction_type_valid: ``transaction_type`` is 1 (outflow)
    or 2 (inflow), nothing else — direction, and only direction. sql/003
    declared the column ``NOT NULL`` but open to any smallint (the ledger
    half of closed bug 6.3).
  * transactions_amount_positive: ``amount_cents`` is stored positive — a
    database fact, not a habit. With direction in a typed column on every
    row, a negative stored amount would be a second, contradictory encoding
    of the same fact.

Salvaged from ``test_wp1_transfer_collapse.py`` ahead of the transfer
removal (2026-08-10) — the invariants outlive the feature whose collapse
introduced them.
"""
import uuid

import asyncpg
import pytest

from app import db


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_type", [0, 3])
async def test_ledger_rejects_a_transaction_type_outside_the_direction_enum(
    test_data, bad_type
):
    async with db.pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO expense_transactions
                    (id, user_id, title, amount_cents, transaction_type,
                     date, account_id, category_id,
                     created_at, updated_at)
                VALUES ($1, $2, 'bad-type', 1000, $3, now(), $4, $5, now(), now())
                """,
                str(uuid.uuid4()), test_data.user_id, bad_type,
                test_data.account_id, test_data.category_id,
            )


@pytest.mark.asyncio
async def test_ledger_rejects_a_negative_amount(test_data):
    async with db.pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO expense_transactions
                    (id, user_id, title, amount_cents, transaction_type,
                     date, account_id, category_id,
                     created_at, updated_at)
                VALUES ($1, $2, 'negative', -1000, 1, now(), $3, $4, now(), now())
                """,
                str(uuid.uuid4()), test_data.user_id,
                test_data.account_id, test_data.category_id,
            )
