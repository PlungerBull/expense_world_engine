"""Regression tests for the audit-driven fixes shipped in this sprint.

Each test pins a single behaviour so a future refactor can't silently
regress the specific hazard the fix addressed.

  * Phase 1.7 — /activity?resource_id=<non-uuid> returns 422, not 500.
  * Phase 2.4 — category/hashtag names are trimmed, empties rejected,
    uniqueness is case-insensitive.
  * Phase 3.6 — activity_log rows carry actor_type and the GET /activity
    response exposes it.
  * Transfer edit guard — PUT on a transfer leg rejects date in addition to
    the pre-existing amount_cents / account_id blocks, so the pair can't end
    up on two different days.
"""
import uuid

import pytest

from app import db


# ---------------------------------------------------------------------------
# Phase 1.7 — non-UUID resource_id returns 422
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_activity_resource_id_non_uuid_returns_422(client):
    r = await client.get("/v1/activity?resource_id=not-a-uuid")
    assert r.status_code == 422, r.text
    body = r.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert "resource_id" in (body.get("fields") or {})


# ---------------------------------------------------------------------------
# Phase 2.4 — category name normalization
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_category_name_empty_after_trim_rejected(client):
    r = await client.post(
        "/v1/categories",
        json={"id": str(uuid.uuid4()), "name": "   ", "color": "#112233"},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 422, r.text
    assert "name" in (r.json()["error"].get("fields") or {})


@pytest.mark.asyncio
async def test_category_name_trimmed_on_create(client, test_data):
    name = f"  spaced-{uuid.uuid4()}  "
    cat_id = str(uuid.uuid4())
    r = await client.post(
        "/v1/categories",
        json={"id": cat_id, "name": name, "color": "#112233"},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    try:
        assert r.status_code == 201, r.text
        assert r.json()["name"] == name.strip()
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute("DELETE FROM activity_log WHERE resource_id = $1", cat_id)
            await conn.execute("DELETE FROM expense_categories WHERE id = $1", cat_id)


@pytest.mark.asyncio
async def test_category_name_case_insensitive_uniqueness(client, test_data):
    base = f"Dupe-{uuid.uuid4()}"
    first_id = str(uuid.uuid4())
    first = await client.post(
        "/v1/categories",
        json={"id": first_id, "name": base, "color": "#112233"},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert first.status_code == 201
    try:
        second = await client.post(
            "/v1/categories",
            json={"id": str(uuid.uuid4()), "name": base.lower(), "color": "#445566"},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert second.status_code == 409, second.text
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute("DELETE FROM activity_log WHERE resource_id = $1", first_id)
            await conn.execute("DELETE FROM expense_categories WHERE id = $1", first_id)


@pytest.mark.asyncio
async def test_hashtag_name_case_insensitive_uniqueness(client, test_data):
    base = f"Tag-{uuid.uuid4()}"
    first_id = str(uuid.uuid4())
    first = await client.post(
        "/v1/hashtags",
        json={"id": first_id, "name": base},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert first.status_code == 201
    try:
        second = await client.post(
            "/v1/hashtags",
            json={"id": str(uuid.uuid4()), "name": base.upper()},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert second.status_code == 409, second.text
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute("DELETE FROM activity_log WHERE resource_id = $1", first_id)
            await conn.execute("DELETE FROM expense_hashtags WHERE id = $1", first_id)


# ---------------------------------------------------------------------------
# Phase 3.6 — activity_log actor_type surfaces in response
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_activity_response_includes_actor_type(client, test_data):
    """Any mutation should produce an activity_log row whose actor_type
    is 'user' and reaches the caller through the response."""
    cat_id = str(uuid.uuid4())
    r = await client.post(
        "/v1/categories",
        json={"id": cat_id, "name": f"actor-{uuid.uuid4()}", "color": "#aabbcc"},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 201
    try:
        activity = await client.get(f"/v1/activity?resource_id={cat_id}")
        assert activity.status_code == 200, activity.text
        items = activity.json()["items"]
        assert items, "expected at least one activity row for the new category"
        assert all("actor_type" in row for row in items)
        assert items[0]["actor_type"] == "user"
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute("DELETE FROM activity_log WHERE resource_id = $1", cat_id)
            await conn.execute("DELETE FROM expense_categories WHERE id = $1", cat_id)


# ---------------------------------------------------------------------------
# Transfer edit guard — date rejected on transfer legs
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_transfer_edit_guard_rejects_date(client, test_data):
    """PUT on a transfer leg must reject `date` with 422.

    The PUT path mutates only the edited leg, so letting a date change through
    would desync the pair — the two legs are required to share a date, and
    splitting them across a month boundary would make @Transfer report a spread
    that was never paid.

    `exchange_rate` used to be asserted here alongside `date`. It is gone: since
    sql/021 there is no such column and no such request field, and rejecting it
    is now the request schema's job (`extra="forbid"`), not the transfer guard's.
    tests/test_wp2_read_time_currency.py covers that on every affected endpoint.
    """
    second_account_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, is_person, color,
                 current_balance_cents, is_archived, sort_order,
                 created_at, updated_at)
            VALUES ($1, $2, 'Guard-Transfer-Target', 'PEN', false, '#123456',
                    0, false, 9, now(), now())
            """,
            second_account_id, test_data.user_id,
        )

    primary_id = sibling_id = None
    created_ids: list[str] = []
    try:
        create_r = await client.post(
            "/v1/transactions",
            json={
                "id": str(uuid.uuid4()),
                "title": f"guard-transfer-{uuid.uuid4()}",
                "amount_cents": -1500,
                "date": "2026-04-10T12:00:00Z",
                "account_id": test_data.account_id,
                "category_id": test_data.category_id,
                "transfer": {
                    "id": str(uuid.uuid4()),
                    "account_id": second_account_id,
                    "amount_cents": 1500,
                },
            },
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert create_r.status_code == 201, create_r.text
        primary_id = create_r.json()["id"]
        sibling_id = create_r.json()["transfer_transaction_id"]
        created_ids = [primary_id, sibling_id]

        async with db.pool.acquire() as conn:
            before_primary = await conn.fetchrow(
                "SELECT date, amount_cents, account_id FROM expense_transactions WHERE id = $1",
                primary_id,
            )
            before_sibling = await conn.fetchrow(
                "SELECT date, amount_cents, account_id FROM expense_transactions WHERE id = $1",
                sibling_id,
            )

        for field, payload in (
            ("date", {"date": "2026-04-20T12:00:00Z"}),
        ):
            r = await client.put(
                f"/v1/transactions/{primary_id}",
                json=payload,
                headers={"X-Idempotency-Key": str(uuid.uuid4())},
            )
            assert r.status_code == 422, (field, r.text)
            body = r.json()["error"]
            assert body["code"] == "VALIDATION_ERROR"
            assert field in (body.get("fields") or {}), (field, body)

        async with db.pool.acquire() as conn:
            after_primary = await conn.fetchrow(
                "SELECT date, amount_cents, account_id FROM expense_transactions WHERE id = $1",
                primary_id,
            )
            after_sibling = await conn.fetchrow(
                "SELECT date, amount_cents, account_id FROM expense_transactions WHERE id = $1",
                sibling_id,
            )
        assert dict(after_primary) == dict(before_primary), "primary leg was mutated by a rejected PUT"
        assert dict(after_sibling) == dict(before_sibling), "sibling leg was mutated by a rejected PUT"

    finally:
        if created_ids:
            async with db.pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM activity_log WHERE resource_id = ANY($1::uuid[])",
                    created_ids,
                )
                await conn.execute(
                    "DELETE FROM expense_transactions WHERE id = ANY($1::uuid[])",
                    created_ids,
                )
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM expense_bank_accounts WHERE id = $1", second_account_id,
            )


# ---------------------------------------------------------------------------
# `test_create_transaction_rate_unavailable_raises_422` was here.
#
# It asserted that a USD transaction dated before any seeded rate returned 422
# RATE_UNAVAILABLE and wrote no row — correct while the engine had to resolve a
# rate in order to store `amount_home_cents`. sql/021 removed the stored
# conversion, so no write resolves a rate and no write can fail this way. The
# behaviour it pinned is now inverted: that same request must SUCCEED, because
# recording what happened must never be blocked by a rate lookup.
#
# Its replacement is in tests/test_wp2_read_time_currency.py, which asserts the
# write succeeds and the missing rate surfaces at READ time as a null figure
# plus a non-zero `unconverted_count`.
#
# Its fixture date, 1900-01-01, used to be the suite's floor — it had to be
# earlier than every other seeded rate. That constraint dies with it. The floor
# that still matters is 1997-01-14 (test_exchange_rates_history), which is what
# test_home_currency_parity's 1990 unconvertible assertion sits below.
# ---------------------------------------------------------------------------
