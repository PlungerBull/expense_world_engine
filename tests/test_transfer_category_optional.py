"""category_id is required for normal transactions but optional for transfers.

The transfer engine auto-assigns @Transfer/@Debt and discards any category_id
passed in the request, so callers should not be forced to send one. These tests
pin that contract:

  * POST /transactions with a transfer object and NO category_id -> 201, and the
    engine auto-assigns a category to both legs.
  * POST /transactions with no transfer and no category_id -> 422.
  * POST /transactions/batch (transfers disallowed) with an item missing
    category_id -> 422 with the clean "required" message.

Backward compatibility (still sending category_id on a transfer is accepted and
ignored) is already covered by tests/test_concurrency_hazards.py.
"""

import uuid

import pytest

from app import db


async def _make_second_account(user_id: str) -> str:
    account_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, is_person, color,
                 current_balance_cents, is_archived, sort_order,
                 created_at, updated_at)
            VALUES ($1, $2, 'Cat-Optional Target', 'PEN', false, '#00FF00',
                    50000, false, 2, now(), now())
            """,
            account_id, user_id,
        )
    return account_id


async def _soft_delete_account(account_id: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE expense_bank_accounts SET deleted_at = now() WHERE id = $1",
            account_id,
        )


@pytest.mark.asyncio
async def test_transfer_without_category_id_succeeds(client, test_data):
    """A transfer omitting category_id is accepted; the engine auto-assigns it."""
    second_account_id = await _make_second_account(test_data.user_id)
    try:
        r = await client.post(
            "/v1/transactions",
            json={
                "id": str(uuid.uuid4()),
                "title": f"transfer-no-cat-{uuid.uuid4()}",
                "amount_cents": -2500,
                "date": "2026-04-12T12:00:00Z",
                "account_id": test_data.account_id,
                # category_id deliberately omitted
                "transfer": {
                    "id": str(uuid.uuid4()),
                    "account_id": second_account_id,
                    "amount_cents": 2500,
                },
            },
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        # Posted with a negative amount, so this leg is the outflow. Transfers
        # are identified by the pairing FK, not by a type value.
        assert body["transaction_type"] == 1  # OUTFLOW
        assert body["transfer_transaction_id"] is not None
        # The engine assigned a category even though the caller sent none.
        assert body["category_id"] is not None
    finally:
        await _soft_delete_account(second_account_id)


@pytest.mark.asyncio
async def test_normal_transaction_without_category_id_rejected(client, test_data):
    """A non-transfer transaction still requires category_id."""
    r = await client.post(
        "/v1/transactions",
        json={
            "id": str(uuid.uuid4()),
            "title": f"no-cat-{uuid.uuid4()}",
            "amount_cents": -1000,
            "date": "2026-04-12T12:00:00Z",
            "account_id": test_data.account_id,
            # category_id deliberately omitted, no transfer object
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 422, r.text
    assert "category_id" in r.json()["error"]["fields"]


@pytest.mark.asyncio
async def test_batch_item_without_category_id_rejected(client, test_data):
    """Batch disallows transfers, so each item still requires category_id."""
    r = await client.post(
        "/v1/transactions/batch",
        json={
            "transactions": [
                {
                    "id": str(uuid.uuid4()),
                    "title": f"batch-no-cat-{uuid.uuid4()}",
                    "amount_cents": -1000,
                    "date": "2026-04-12T12:00:00Z",
                    "account_id": test_data.account_id,
                    # category_id deliberately omitted
                },
            ],
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 422, r.text
    # Batch errors are reported per-item: fields = {"items": [{"index", "fields"}]}.
    items = r.json()["error"]["fields"]["items"]
    assert items, r.text
    assert items[0]["fields"].get("category_id") == "Required for non-transfer transactions.", items
