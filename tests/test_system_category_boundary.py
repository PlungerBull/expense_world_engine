"""System categories are engine-assigned only (bug 6.7).

@Opening is excluded from every flow report, so a user filing an ordinary
row under it would move a balance while vanishing from monthly reports.
These tests pin the boundary: every public door into the ledger — create,
update, batch, and inbox promote — rejects a system ``category_id`` with
422 ``MSG_USER_CATEGORY``, while the engine's own path
(``create_opening_balance`` → ``create_transaction(allow_system_category=True)``)
keeps working. Mirrors ``test_reserved_category_names.py``, the 7.4 twin of
this boundary-vs-internal shape.
"""

import uuid

import pytest

from app import db
from app.helpers.validation import (
    MSG_ACTIVE_CATEGORY,
    MSG_USER_CATEGORY,
)

PAST_DATE = "2026-04-12T12:00:00Z"


def _idem() -> dict:
    return {"X-Idempotency-Key": str(uuid.uuid4())}


async def _make_account(user_id: str) -> str:
    account_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, is_person, color,
                 is_archived, sort_order, created_at, updated_at)
            VALUES ($1, $2, $3, 'PEN', false, '#00FF00',
                    false, 9, now(), now())
            """,
            account_id, user_id, f"SysCat-Test {uuid.uuid4().hex[:8]}",
        )
    return account_id


async def _soft_delete_account(account_id: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE expense_bank_accounts SET deleted_at = now() WHERE id = $1",
            account_id,
        )


async def _opening_category_id(client, test_data) -> str:
    """The real @Opening id, captured the way a client would see it —
    from an opening-balance POST on a throwaway account. Also the pin
    that the internal path still works (the one allow_system_category
    caller)."""
    account_id = await _make_account(test_data.user_id)
    r = await client.post(
        f"/v1/accounts/{account_id}/opening-balance",
        json={
            "transaction_id": str(uuid.uuid4()),
            "amount_cents": 10000,
            "date": PAST_DATE,
        },
        headers=_idem(),
    )
    assert r.status_code == 201, r.text
    await _soft_delete_account(account_id)
    return r.json()["category_id"]


def _txn_item(test_data, category_id: str, **overrides) -> dict:
    item = {
        "id": str(uuid.uuid4()),
        "title": f"syscat-{uuid.uuid4().hex[:8]}",
        "amount_cents": -1500,
        "date": PAST_DATE,
        "account_id": test_data.account_id,
        "category_id": category_id,
    }
    item.update(overrides)
    return item


@pytest.mark.asyncio
async def test_create_rejects_system_category(client, test_data):
    opening_id = await _opening_category_id(client, test_data)
    r = await client.post(
        "/v1/transactions",
        json=_txn_item(test_data, opening_id),
        headers=_idem(),
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["fields"]["category_id"] == MSG_USER_CATEGORY


@pytest.mark.asyncio
async def test_update_rejects_system_category(client, test_data):
    opening_id = await _opening_category_id(client, test_data)

    r = await client.post(
        "/v1/transactions",
        json=_txn_item(test_data, test_data.category_id),
        headers=_idem(),
    )
    assert r.status_code == 201, r.text
    txn = r.json()

    r = await client.put(
        f"/v1/transactions/{txn['id']}",
        json={"category_id": opening_id},
        headers=_idem(),
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["fields"]["category_id"] == MSG_USER_CATEGORY

    # The row is untouched — still on its user category.
    r = await client.get(f"/v1/transactions/{txn['id']}")
    assert r.json()["category_id"] == test_data.category_id

    await client.delete(f"/v1/transactions/{txn['id']}", headers=_idem())


@pytest.mark.asyncio
async def test_batch_rejects_system_category_all_or_nothing(client, test_data):
    opening_id = await _opening_category_id(client, test_data)
    good = _txn_item(test_data, test_data.category_id)
    bad = _txn_item(test_data, opening_id)

    r = await client.post(
        "/v1/transactions/batch",
        json={"transactions": [good, bad]},
        headers=_idem(),
    )
    assert r.status_code == 422, r.text
    items = r.json()["error"]["fields"]["items"]
    assert items == [
        {"index": 1, "fields": {"category_id": MSG_USER_CATEGORY}}
    ]

    # All-or-nothing: the good item did not land.
    r = await client.get(f"/v1/transactions/{good['id']}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_batch_distinguishes_deleted_from_system(client, test_data):
    """Pins the widened active_category_ids contract at the wire: a deleted
    category still reads as inactive, a system one as forbidden."""
    opening_id = await _opening_category_id(client, test_data)
    deleted_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO expense_categories
                (id, user_id, name, color, sort_order,
                 created_at, updated_at, deleted_at)
               VALUES ($1, $2, $3, '#123456', 0,
                 now(), now(), now())""",
            deleted_id, test_data.user_id, f"syscat-deleted {uuid.uuid4().hex[:8]}",
        )
    try:
        r = await client.post(
            "/v1/transactions/batch",
            json={
                "transactions": [
                    _txn_item(test_data, deleted_id),
                    _txn_item(test_data, opening_id),
                ]
            },
            headers=_idem(),
        )
        assert r.status_code == 422, r.text
        items = r.json()["error"]["fields"]["items"]
        assert items[0]["fields"]["category_id"] == MSG_ACTIVE_CATEGORY
        assert items[1]["fields"]["category_id"] == MSG_USER_CATEGORY
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM expense_categories WHERE id = $1", deleted_id
            )


@pytest.mark.asyncio
async def test_promote_rejects_system_category_and_ready_agrees(client, test_data):
    """The fourth door: promotion inserts the ledger row directly (not via
    create_transaction), so it carries its own guard — and ?ready=true must
    agree with it (a listed row must promote)."""
    opening_id = await _opening_category_id(client, test_data)
    draft = await client.post(
        "/v1/inbox",
        json={
            "id": str(uuid.uuid4()),
            "title": f"syscat-draft-{uuid.uuid4().hex[:8]}",
            "amount_cents": -900,
            "date": PAST_DATE,
            "account_id": test_data.account_id,
            "category_id": opening_id,
        },
        headers=_idem(),
    )
    assert draft.status_code == 201, draft.text
    inbox_id = draft.json()["id"]

    try:
        # Not listed as ready — the SQL predicate carries the is_system arm.
        r = await client.get("/v1/inbox", params={"ready": "true"})
        assert inbox_id not in {row["id"] for row in r.json()["items"]}

        # And promote refuses, on the same field with the same message.
        r = await client.post(
            f"/v1/inbox/{inbox_id}/promote",
            json={"id": str(uuid.uuid4())},
            headers=_idem(),
        )
        assert r.status_code == 422, r.text
        assert r.json()["error"]["fields"]["category_id"] == MSG_USER_CATEGORY
    finally:
        await client.delete(f"/v1/inbox/{inbox_id}", headers=_idem())


@pytest.mark.asyncio
async def test_opening_balance_path_still_works(client, test_data):
    """The internal caller pin: the engine may file under @Opening (and the
    seed carries the system category on the wire)."""
    account_id = await _make_account(test_data.user_id)
    try:
        r = await client.post(
            f"/v1/accounts/{account_id}/opening-balance",
            json={
                "transaction_id": str(uuid.uuid4()),
                "amount_cents": -2500,
                "date": PAST_DATE,
            },
            headers=_idem(),
        )
        assert r.status_code == 201, r.text
        cats = await client.get("/v1/categories")
        by_id = {c["id"]: c for c in cats.json()["items"]}
        assert by_id[r.json()["category_id"]]["is_system"] is True
    finally:
        await _soft_delete_account(account_id)
