"""Active-reference validation is single-sourced (bloat-audit Duplicates §4).

``validation.active_account_row`` / ``active_category_row`` (+ the vectorised
``active_*_ids``) are now the one rendering of "must reference an active
[non-archived] account / active category"; the raising ``validate_active_*``
helpers wrap them and every collect-all-errors flow shares the
``MSG_ACTIVE_ACCOUNT`` / ``MSG_ACTIVE_CATEGORY`` strings. These tests pin the
helpers' semantics, the message constants at the wire, and the one SQL
rendering that stays separate by design — ``?ready=true`` — which gained the
``user_id`` arm its Python twin always had (a cross-tenant draft used to show
as promotable while promote 422ed).
"""
import uuid

import pytest

from app import db
from app.helpers.validation import (
    MSG_ACTIVE_ACCOUNT,
    MSG_ACTIVE_CATEGORY,
    active_account_ids,
    active_account_row,
    active_category_ids,
)


def _idem() -> dict:
    return {"X-Idempotency-Key": str(uuid.uuid4())}


@pytest.mark.asyncio
async def test_vectorised_helpers_exclude_deleted_archived_and_foreign(test_data):
    """active_*_ids returns exactly the subset that passes the reference rule."""
    deleted_id, archived_id = str(uuid.uuid4()), str(uuid.uuid4())
    foreign_user = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        try:
            await conn.execute(
                """INSERT INTO expense_bank_accounts
                    (id, user_id, name, currency_code, is_person, color,
                     is_archived, sort_order, created_at, updated_at, deleted_at)
                   VALUES ($1, $2, 'active-ref deleted', 'PEN', false, '#000000',
                     false, 0, now(), now(), now())""",
                deleted_id, test_data.user_id,
            )
            await conn.execute(
                """INSERT INTO expense_bank_accounts
                    (id, user_id, name, currency_code, is_person, color,
                     is_archived, sort_order, created_at, updated_at)
                   VALUES ($1, $2, 'active-ref archived', 'PEN', false, '#000000',
                     true, 0, now(), now())""",
                archived_id, test_data.user_id,
            )

            result = await active_account_ids(
                conn,
                [test_data.account_id, deleted_id, archived_id, str(uuid.uuid4())],
                test_data.user_id,
            )
            assert result == {test_data.account_id}

            # The seeded account is invisible to a foreign tenant.
            assert (
                await active_account_ids(conn, [test_data.account_id], foreign_user)
                == set()
            )
            # Empty input short-circuits to an empty set.
            assert await active_account_ids(conn, [], test_data.user_id) == set()

            # Row helper agrees with the set helper.
            assert (
                await active_account_row(conn, archived_id, test_data.user_id)
                is None
            )
            # Categories: mapping id → is_system (membership = active).
            assert await active_category_ids(
                conn, [test_data.category_id], test_data.user_id
            ) == {test_data.category_id: False}
        finally:
            await conn.execute(
                "DELETE FROM expense_bank_accounts WHERE id = ANY($1::uuid[])",
                [deleted_id, archived_id],
            )


@pytest.mark.asyncio
async def test_message_constants_reach_the_wire(client, test_data):
    """The shared strings are what endpoints emit — batch and single-create sites."""
    # Batch: unknown account + unknown category on one item.
    r = await client.post(
        "/v1/transactions/batch",
        json={
            "transactions": [
                {
                    "id": str(uuid.uuid4()),
                    "title": "active-ref batch",
                    "amount_cents": -1000,
                    "date": "2024-03-15T12:00:00Z",
                    "account_id": str(uuid.uuid4()),
                    "category_id": str(uuid.uuid4()),
                }
            ]
        },
        headers=_idem(),
    )
    assert r.status_code == 422, r.text
    fields = r.json()["error"]["fields"]["items"][0]["fields"]
    assert fields["account_id"] == MSG_ACTIVE_ACCOUNT
    assert fields["category_id"] == MSG_ACTIVE_CATEGORY

    # Single create: the raising wrappers, one reference at a time. It reaches
    # the same two constants by a different route from the batch accumulator
    # above, so each is separately capable of drifting off them.
    deleted_account_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, is_person, color,
                 is_archived, sort_order, created_at, updated_at, deleted_at)
               VALUES ($1, $2, 'active-ref create deleted', 'PEN', false,
                 '#000000', false, 0, now(), now(), now())""",
            deleted_account_id, test_data.user_id,
        )
    try:
        r = await client.post(
            "/v1/transactions",
            json={
                "id": str(uuid.uuid4()),
                "title": "active-ref create",
                "amount_cents": -1000,
                "date": "2024-03-15T12:00:00Z",
                "account_id": deleted_account_id,
                "category_id": str(uuid.uuid4()),
            },
            headers=_idem(),
        )
        assert r.status_code == 422, r.text
        assert r.json()["error"]["fields"]["account_id"] == MSG_ACTIVE_ACCOUNT

        # Same path, active account, unknown category — the account check
        # raises first, so the category constant needs its own probe.
        r = await client.post(
            "/v1/transactions",
            json={
                "id": str(uuid.uuid4()),
                "title": "active-ref create category",
                "amount_cents": -1000,
                "date": "2024-03-15T12:00:00Z",
                "account_id": test_data.account_id,
                "category_id": str(uuid.uuid4()),
            },
            headers=_idem(),
        )
        assert r.status_code == 422, r.text
        assert r.json()["error"]["fields"]["category_id"] == MSG_ACTIVE_CATEGORY
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM expense_bank_accounts WHERE id = $1",
                deleted_account_id,
            )


@pytest.mark.asyncio
async def test_ready_filter_is_tenant_scoped(client, test_data):
    """A draft referencing another tenant's account must not show as ready —
    promote 422s it, and since 2026-08-08 the ?ready=true SQL agrees."""
    user_b = str(uuid.uuid4())
    account_b = str(uuid.uuid4())
    inbox_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        try:
            await conn.execute(
                """INSERT INTO users (id, display_name, created_at, updated_at)
                   VALUES ($1, 'active-ref-user-b', now(), now())""",
                user_b,
            )
            await conn.execute(
                """INSERT INTO expense_bank_accounts
                    (id, user_id, name, currency_code, is_person, color,
                     is_archived, sort_order, created_at, updated_at)
                   VALUES ($1, $2, 'B account', 'PEN', false, '#000000',
                     false, 0, now(), now())""",
                account_b, user_b,
            )
            # A draft for the TEST user that references user B's account —
            # inserted directly because the write path stores it unvalidated
            # (open-bugs 7.1). Fully "ready" in every other respect.
            await conn.execute(
                """INSERT INTO expense_transaction_inbox
                    (id, user_id, title, amount_cents, transaction_type, date,
                     account_id, category_id, status, created_at, updated_at)
                   VALUES ($1, $2, 'cross-tenant draft', 1000, 2,
                     '2024-03-15T12:00:00Z', $3, $4, 1, now(), now())""",
                inbox_id, test_data.user_id, account_b, test_data.category_id,
            )

            r = await client.get("/v1/inbox?ready=true&limit=200")
            assert r.status_code == 200, r.text
            assert inbox_id not in [row["id"] for row in r.json()["items"]]

            # The paired Python implementation still rejects it identically.
            r = await client.post(
                f"/v1/inbox/{inbox_id}/promote",
                json={"id": str(uuid.uuid4())},
                headers=_idem(),
            )
            assert r.status_code == 422, r.text
            assert (
                r.json()["error"]["fields"]["account_id"] == MSG_ACTIVE_ACCOUNT
            )
        finally:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1", inbox_id
            )
            await conn.execute(
                "DELETE FROM expense_transaction_inbox WHERE id = $1", inbox_id
            )
            await conn.execute(
                "DELETE FROM expense_bank_accounts WHERE id = $1", account_b
            )
            await conn.execute("DELETE FROM users WHERE id = $1", user_b)
