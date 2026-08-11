"""insert_transaction_row — the one INSERT INTO expense_transactions.

Bloat-audit 2026-08-06 Duplicates §5: the column list existed at several sites
(create, batch, promote), each with its own UniqueViolation → 409 translation —
the same missed-site failure shape as the create_batch sign-matrix incident.
The helper is shared now, so one wire test pins the duplicate-id conflict
message for every path.
"""
import uuid

import pytest

from app import db


def _idem() -> dict:
    return {"X-Idempotency-Key": str(uuid.uuid4())}


@pytest.mark.asyncio
async def test_duplicate_id_conflict_names_the_transaction_id(client, test_data):
    txn_id = str(uuid.uuid4())
    inbox_id = str(uuid.uuid4())
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

        # The promote path speaks the identical sentence for the same collision.
        draft = await client.post(
            "/v1/inbox",
            json={
                "id": inbox_id,
                "title": "duplicate-id promote probe",
                "amount_cents": -500,
                "date": "2024-03-15T12:00:00Z",
                "account_id": test_data.account_id,
                "category_id": test_data.category_id,
            },
            headers=_idem(),
        )
        assert draft.status_code == 201, draft.text

        r = await client.post(
            f"/v1/inbox/{inbox_id}/promote",
            json={"id": txn_id},  # collides with the existing row
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
                "DELETE FROM activity_log WHERE resource_id = ANY($1::uuid[])",
                [txn_id, inbox_id],
            )
            await conn.execute(
                "DELETE FROM expense_transactions WHERE id = $1", txn_id
            )
            await conn.execute(
                "DELETE FROM expense_transaction_inbox WHERE id = $1", inbox_id
            )
