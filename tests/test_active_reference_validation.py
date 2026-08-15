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
            # inserted directly because the write path now rejects a
            # cross-tenant reference (7.1 fixed); this seeds the pre-fix shape
            # to prove the read/promote sides still hold on data already in
            # the table. Fully "ready" in every other respect.
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


@pytest.mark.asyncio
async def test_inbox_create_rejects_inactive_references(client, test_data):
    """POST /inbox refuses a well-formed id that is nonexistent, deleted,
    archived (accounts), or another tenant's — the write-side half of the
    reference rule (was open-bugs 7.1)."""
    deleted_account = str(uuid.uuid4())
    archived_account = str(uuid.uuid4())
    deleted_category = str(uuid.uuid4())
    user_b = str(uuid.uuid4())
    account_b = str(uuid.uuid4())
    category_b = str(uuid.uuid4())
    created_drafts: list[str] = []
    async with db.pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, is_person, color,
                 is_archived, sort_order, created_at, updated_at, deleted_at)
               VALUES ($1, $2, 'inbox-ref deleted', 'PEN', false, '#000000',
                 false, 0, now(), now(), now())""",
            deleted_account, test_data.user_id,
        )
        await conn.execute(
            """INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, is_person, color,
                 is_archived, sort_order, created_at, updated_at)
               VALUES ($1, $2, 'inbox-ref archived', 'PEN', false, '#000000',
                 true, 0, now(), now())""",
            archived_account, test_data.user_id,
        )
        await conn.execute(
            """INSERT INTO expense_categories
                (id, user_id, name, color, sort_order,
                 created_at, updated_at, deleted_at)
               VALUES ($1, $2, 'inbox-ref deleted cat', '#FF0000', 0,
                 now(), now(), now())""",
            deleted_category, test_data.user_id,
        )
        await conn.execute(
            """INSERT INTO users (id, display_name, created_at, updated_at)
               VALUES ($1, 'inbox-ref-user-b', now(), now())""",
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
        await conn.execute(
            """INSERT INTO expense_categories
                (id, user_id, name, color, sort_order,
                 created_at, updated_at)
               VALUES ($1, $2, 'B category', '#FF0000', 0,
                 now(), now())""",
            category_b, user_b,
        )
    try:
        bad_accounts = [
            str(uuid.uuid4()), deleted_account, archived_account, account_b
        ]
        for bad_id in bad_accounts:
            r = await client.post(
                "/v1/inbox",
                json={"id": str(uuid.uuid4()), "account_id": bad_id},
                headers=_idem(),
            )
            assert r.status_code == 422, r.text
            assert r.json()["error"]["fields"]["account_id"] == MSG_ACTIVE_ACCOUNT

        bad_categories = [str(uuid.uuid4()), deleted_category, category_b]
        for bad_id in bad_categories:
            r = await client.post(
                "/v1/inbox",
                json={"id": str(uuid.uuid4()), "category_id": bad_id},
                headers=_idem(),
            )
            assert r.status_code == 422, r.text
            assert r.json()["error"]["fields"]["category_id"] == MSG_ACTIVE_CATEGORY

        # Bad account + bad category: the account check raises first, same
        # short-circuit as the single transaction create.
        r = await client.post(
            "/v1/inbox",
            json={
                "id": str(uuid.uuid4()),
                "account_id": str(uuid.uuid4()),
                "category_id": str(uuid.uuid4()),
            },
            headers=_idem(),
        )
        assert r.status_code == 422, r.text
        body = r.json()["error"]
        assert body["message"] == "Account validation failed."
        assert body["fields"] == {"account_id": MSG_ACTIVE_ACCOUNT}

        # Sparse drafts stay accepted — the rule fires only on supplied ids.
        sparse_id = str(uuid.uuid4())
        r = await client.post(
            "/v1/inbox", json={"id": sparse_id}, headers=_idem()
        )
        assert r.status_code == 201, r.text
        created_drafts.append(sparse_id)

        valid_id = str(uuid.uuid4())
        r = await client.post(
            "/v1/inbox",
            json={
                "id": valid_id,
                "account_id": test_data.account_id,
                "category_id": test_data.category_id,
            },
            headers=_idem(),
        )
        assert r.status_code == 201, r.text
        created_drafts.append(valid_id)
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = ANY($1::uuid[])",
                created_drafts,
            )
            await conn.execute(
                "DELETE FROM expense_transaction_inbox WHERE id = ANY($1::uuid[])",
                created_drafts,
            )
            await conn.execute(
                "DELETE FROM expense_categories WHERE id = ANY($1::uuid[])",
                [deleted_category, category_b],
            )
            await conn.execute(
                "DELETE FROM expense_bank_accounts WHERE id = ANY($1::uuid[])",
                [deleted_account, archived_account, account_b],
            )
            await conn.execute("DELETE FROM users WHERE id = $1", user_b)


@pytest.mark.asyncio
async def test_inbox_update_rejects_inactive_references(client, test_data):
    """PUT /inbox/{id} applies the same reference rule to fields present in
    the update — and a rejected update leaves the draft untouched."""
    deleted_account = str(uuid.uuid4())
    inbox_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, is_person, color,
                 is_archived, sort_order, created_at, updated_at, deleted_at)
               VALUES ($1, $2, 'inbox-put deleted', 'PEN', false, '#000000',
                 false, 0, now(), now(), now())""",
            deleted_account, test_data.user_id,
        )
    try:
        r = await client.post(
            "/v1/inbox",
            json={"id": inbox_id, "account_id": test_data.account_id},
            headers=_idem(),
        )
        assert r.status_code == 201, r.text

        for bad_account in (str(uuid.uuid4()), deleted_account):
            r = await client.put(
                f"/v1/inbox/{inbox_id}",
                json={"account_id": bad_account},
                headers=_idem(),
            )
            assert r.status_code == 422, r.text
            assert r.json()["error"]["fields"]["account_id"] == MSG_ACTIVE_ACCOUNT

        r = await client.put(
            f"/v1/inbox/{inbox_id}",
            json={"category_id": str(uuid.uuid4())},
            headers=_idem(),
        )
        assert r.status_code == 422, r.text
        assert r.json()["error"]["fields"]["category_id"] == MSG_ACTIVE_CATEGORY

        # The rejected updates rolled back — the draft still holds its
        # original reference and no category.
        r = await client.get(f"/v1/inbox/{inbox_id}")
        assert r.status_code == 200, r.text
        assert r.json()["account_id"] == test_data.account_id
        assert r.json()["category_id"] is None

        r = await client.put(
            f"/v1/inbox/{inbox_id}",
            json={"category_id": test_data.category_id},
            headers=_idem(),
        )
        assert r.status_code == 200, r.text

        # A nonexistent inbox item is a 404 before any reference 422.
        r = await client.put(
            f"/v1/inbox/{uuid.uuid4()}",
            json={"account_id": str(uuid.uuid4())},
            headers=_idem(),
        )
        assert r.status_code == 404, r.text
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1", inbox_id
            )
            await conn.execute(
                "DELETE FROM expense_transaction_inbox WHERE id = $1", inbox_id
            )
            await conn.execute(
                "DELETE FROM expense_bank_accounts WHERE id = $1",
                deleted_account,
            )


@pytest.mark.asyncio
async def test_stale_draft_reference_is_promotes_problem(client, test_data):
    """A reference that dies after the draft was written stays promote's job:
    edits that don't touch it still succeed, and promote is where it 422s."""
    account_id = str(uuid.uuid4())
    inbox_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, is_person, color,
                 is_archived, sort_order, created_at, updated_at)
               VALUES ($1, $2, 'inbox-stale account', 'PEN', false, '#000000',
                 false, 0, now(), now())""",
            account_id, test_data.user_id,
        )
    try:
        r = await client.post(
            "/v1/inbox",
            json={
                "id": inbox_id,
                "title": "stale draft",
                "amount_cents": -1000,
                "date": "2024-03-15T12:00:00Z",
                "account_id": account_id,
                "category_id": test_data.category_id,
            },
            headers=_idem(),
        )
        assert r.status_code == 201, r.text

        async with db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE expense_bank_accounts SET deleted_at = now() WHERE id = $1",
                account_id,
            )

        # An edit that doesn't touch the reference does not re-validate it —
        # a stale draft stays editable.
        r = await client.put(
            f"/v1/inbox/{inbox_id}",
            json={"title": "still editable"},
            headers=_idem(),
        )
        assert r.status_code == 200, r.text

        # Re-sending the now-dead reference is refused.
        r = await client.put(
            f"/v1/inbox/{inbox_id}",
            json={"account_id": account_id},
            headers=_idem(),
        )
        assert r.status_code == 422, r.text
        assert r.json()["error"]["fields"]["account_id"] == MSG_ACTIVE_ACCOUNT

        # Promote remains the gate for what the row already holds.
        r = await client.post(
            f"/v1/inbox/{inbox_id}/promote",
            json={"id": str(uuid.uuid4())},
            headers=_idem(),
        )
        assert r.status_code == 422, r.text
        assert r.json()["error"]["fields"]["account_id"] == MSG_ACTIVE_ACCOUNT
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1", inbox_id
            )
            await conn.execute(
                "DELETE FROM expense_transaction_inbox WHERE id = $1", inbox_id
            )
            await conn.execute(
                "DELETE FROM expense_bank_accounts WHERE id = $1", account_id
            )
