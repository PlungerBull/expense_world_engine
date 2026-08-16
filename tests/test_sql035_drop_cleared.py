"""Pins for sql/035 — `cleared` is gone from the ledger, and the inbox never had it.

The column shipped in sql/003 as the standard bank-ledger pre-step to
reconciliation ("seen on the statement"), and the reconciliation feature was
then built without ever reading it: nothing but the request body wrote it and
nothing but a list filter read it. `schema-reference.md` claimed it "drives
reconciliation", which is the kind of documented-but-false wiring that makes a
client build a workflow on a flag the engine ignores — the TUI's cleared field
was exactly that.

Three things must stay true, and they can rot independently:

  * the **column** is gone (the schema pin — a future migration cannot quietly
    re-add it as a stored derivable);
  * the **write surfaces** fail closed on it (422 on the unknown field, never a
    silent drop) — this is what tells a client still sending it to stop;
  * the **read surfaces** never emit it, on any transaction representation.

The inbox section is the one that answers the question this started as: a draft
has neither `cleared` nor `reconciliation_id`, and never did. It is not a ledger
row, so neither concept applies.

Run: .venv/bin/pytest tests/test_sql035_drop_cleared.py -v
"""
import uuid

import pytest

from app import db


def _idem():
    return {"X-Idempotency-Key": str(uuid.uuid4())}


def _txn_body(test_data, **extra):
    return {
        "id": str(uuid.uuid4()),
        "title": "sql035",
        "amount_cents": -1500,
        "date": "2026-08-10T12:00:00Z",
        "account_id": test_data.account_id,
        "category_id": test_data.category_id,
        **extra,
    }


# ---------------------------------------------------------------------------
# The column
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_column_is_gone():
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT table_name FROM information_schema.columns
               WHERE column_name = 'cleared'"""
        )
    assert rows == [], f"`cleared` is back on {[r['table_name'] for r in rows]}"


# ---------------------------------------------------------------------------
# Write surfaces fail closed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_rejects_cleared(client, test_data):
    r = await client.post(
        "/v1/transactions", json=_txn_body(test_data, cleared=True), headers=_idem()
    )
    assert r.status_code == 422, r.text
    body = r.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert "cleared" in (body.get("fields") or {}), body


@pytest.mark.asyncio
async def test_batch_rejects_cleared(client, test_data):
    """The batch item is the shape a per-model `extra="forbid"` copy would have
    missed — items are nested request models, where Pydantic config does not
    propagate. They inherit StrictModel, so the pin holds."""
    r = await client.post(
        "/v1/transactions/batch",
        json={"transactions": [_txn_body(test_data, cleared=False)]},
        headers=_idem(),
    )
    assert r.status_code == 422, r.text
    body = r.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert any("cleared" in key for key in (body.get("fields") or {})), body


@pytest.mark.asyncio
async def test_update_rejects_cleared(client, test_data):
    r = await client.put(
        f"/v1/transactions/{test_data.transaction_id}",
        json={"cleared": True},
        headers=_idem(),
    )
    assert r.status_code == 422, r.text
    body = r.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert "cleared" in (body.get("fields") or {}), body


# ---------------------------------------------------------------------------
# Read surfaces never emit it
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_transaction_representation_carries_cleared(client, test_data):
    created = await client.post(
        "/v1/transactions", json=_txn_body(test_data), headers=_idem()
    )
    assert created.status_code == 201, created.text
    txn_id = created.json()["id"]

    one = await client.get(f"/v1/transactions/{txn_id}")
    listing = await client.get("/v1/transactions?limit=5")
    updated = await client.put(
        f"/v1/transactions/{txn_id}", json={"title": "sql035 renamed"}, headers=_idem()
    )

    assert "cleared" not in created.json(), created.text
    assert "cleared" not in one.json(), one.text
    assert "cleared" not in updated.json(), updated.text
    for row in listing.json()["items"]:
        assert "cleared" not in row, row


# ---------------------------------------------------------------------------
# The inbox has neither concept
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inbox_has_neither_cleared_nor_reconciliation(client, test_data):
    """A draft is not a ledger row. `cleared` never existed on the inbox table
    and `reconciliation_id` never did either — this pins the absence on the wire
    in both directions, write and read."""
    inbox_id = str(uuid.uuid4())
    created = await client.post(
        "/v1/inbox",
        json={"id": inbox_id, "title": "sql035 draft", "amount_cents": -900},
        headers=_idem(),
    )
    assert created.status_code == 201, created.text
    assert "cleared" not in created.json(), created.text
    assert "reconciliation_id" not in created.json(), created.text

    for field in ({"cleared": True}, {"reconciliation_id": str(uuid.uuid4())}):
        r = await client.put(f"/v1/inbox/{inbox_id}", json=field, headers=_idem())
        assert r.status_code == 422, (field, r.text)
        assert next(iter(field)) in (r.json()["error"].get("fields") or {}), r.text

    await client.delete(f"/v1/inbox/{inbox_id}", headers=_idem())
