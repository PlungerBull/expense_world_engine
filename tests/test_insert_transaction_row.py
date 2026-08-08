"""insert_transaction_row — the one INSERT INTO expense_transactions.

Bloat-audit 2026-08-06 Duplicates §5: the column list existed at five sites
(create, batch, promote, both transfer legs), each with its own
UniqueViolation → 409 translation — the same missed-site failure shape as the
create_batch sign-matrix incident. The helper is shared now, so one wire test
pins the duplicate-id conflict message for every path; sibling linkage and
inbox_id propagation stay pinned by test_wp1_transfer_collapse.py and
test_inbox_transfers.py.
"""
import uuid

import pytest

from app import db


def _idem() -> dict:
    return {"X-Idempotency-Key": str(uuid.uuid4())}


@pytest.mark.asyncio
async def test_duplicate_id_conflict_names_the_transaction_id(client, test_data):
    txn_id = str(uuid.uuid4())
    other_account_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, is_person, color,
                 is_archived, sort_order, created_at, updated_at)
               VALUES ($1, $2, $3, 'PEN', false, '#123456', false, 9,
                 now(), now())""",
            other_account_id, test_data.user_id,
            f"insert-row-other-{other_account_id[:8]}",
        )
    body = {
        "id": txn_id,
        "title": "duplicate-id probe",
        "amount_cents": -1000,
        "date": "2024-03-15T12:00:00Z",
        "account_id": test_data.account_id,
        "category_id": test_data.category_id,
    }
    try:
        r = await client.post("/v1/transactions", json=body, headers=_idem())
        assert r.status_code == 201, r.text

        # Same id, fresh idempotency key → the UNIQUE violation surfaces as
        # the helper's single 409 wording, on every insert path alike.
        r = await client.post("/v1/transactions", json=body, headers=_idem())
        assert r.status_code == 409, r.text
        assert (
            r.json()["error"]["message"]
            == f"A transaction with id '{txn_id}' already exists."
        )

        # The transfer path speaks the identical sentence for a colliding leg.
        r = await client.post(
            "/v1/transactions",
            json={
                "id": str(uuid.uuid4()),
                "title": "duplicate-leg probe",
                "amount_cents": -500,
                "date": "2024-03-15T12:00:00Z",
                "account_id": test_data.account_id,
                "transfer": {
                    "id": txn_id,  # collides with the existing row
                    "account_id": other_account_id,
                    "amount_cents": 500,
                },
            },
            headers=_idem(),
        )
        assert r.status_code == 409, r.text
        assert (
            r.json()["error"]["message"]
            == f"A transaction with id '{txn_id}' already exists."
        )
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1", txn_id
            )
            await conn.execute(
                "DELETE FROM expense_transactions WHERE id = $1", txn_id
            )
            await conn.execute(
                "DELETE FROM expense_bank_accounts WHERE id = $1",
                other_account_id,
            )
