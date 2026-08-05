"""Regression tests for the audit-driven fixes shipped in this sprint.

Each test pins a single behaviour so a future refactor can't silently
regress the specific hazard the fix addressed.

  * Phase 1.7 — /activity?resource_id=<non-uuid> returns 422, not 500.
  * Phase 2.4 — category/hashtag names are trimmed, empties rejected,
    uniqueness is case-insensitive.
  * Phase 3.6 — activity_log rows carry actor_type and the GET /activity
    response exposes it.
  * Transfer edit guard — PUT on a transfer leg rejects date / exchange_rate
    in addition to the pre-existing amount_cents / account_id blocks, so
    the pair can't end up on two different days or with mismatched rates.
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
# Transfer edit guard — date / exchange_rate rejected on transfer legs
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_transfer_edit_guard_rejects_date_and_rate(client, test_data):
    """PUT on a transfer leg must reject `date` and `exchange_rate` with
    422. The PUT path mutates only the edited leg, so letting either
    through would desync the pair: different dates in the ledger, or
    mismatched historical rates producing a pair that no longer nets
    to zero in home currency."""
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
                "SELECT date, exchange_rate, amount_home_cents FROM expense_transactions WHERE id = $1",
                primary_id,
            )
            before_sibling = await conn.fetchrow(
                "SELECT date, exchange_rate, amount_home_cents FROM expense_transactions WHERE id = $1",
                sibling_id,
            )

        for field, payload in (
            ("date", {"date": "2026-04-20T12:00:00Z"}),
            ("exchange_rate", {"exchange_rate": 1.2345}),
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
                "SELECT date, exchange_rate, amount_home_cents FROM expense_transactions WHERE id = $1",
                primary_id,
            )
            after_sibling = await conn.fetchrow(
                "SELECT date, exchange_rate, amount_home_cents FROM expense_transactions WHERE id = $1",
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
# FX loud fallback — no silent 1.0 when a rate is missing
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_transaction_rate_unavailable_raises_422(client, test_data):
    """When the account's currency differs from main_currency and no
    exchange_rates row covers the transaction date, POST /v1/transactions
    must fail with 422 RATE_UNAVAILABLE — not silently fall back to a
    1.0 rate that corrupts amount_home_cents. Also asserts no row was
    written (fail-loud = no partial state)."""
    from app.helpers.exchange_rate import clear_rate_cache

    # Drop any negative-cache entries seeded by earlier tests.
    clear_rate_cache()

    usd_account_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, is_person, color,
                 current_balance_cents, is_archived, sort_order,
                 created_at, updated_at)
               VALUES ($1, $2, 'USD-No-Rate', 'USD', false, '#0000FF',
                       0, false, 99, now(), now())""",
            usd_account_id, test_data.user_id,
        )

    txn_id = str(uuid.uuid4())
    try:
        # 1900 — earlier than ANY row this suite seeds, so `rate_date <= $2`
        # returns nothing and the lookup raises.
        #
        # ⚠️ This was 2000-01-01 and was flaky: ~3 runs in 5 failed. Under
        # pytest-xdist the workers share one database, and
        # `test_exchange_rates_history` seeds rows dated 1997-01-14/15 that are
        # live for other workers until its teardown. A concurrent run of this
        # test then found a rate on or before 2000 and got a 200 instead of the
        # 422. `test_home_currency_parity` already documents the same hazard
        # (`:204`, `:233`, `:367`) — this test predated the rule.
        #
        # Any fixture date must be earlier than every other seeded date. Adding
        # a test that seeds something before 1900 breaks this one.
        r = await client.post(
            "/v1/transactions",
            json={
                "id": txn_id,
                "title": f"rate-unavailable-{uuid.uuid4()}",
                "amount_cents": -1000,
                "date": "1900-01-01T12:00:00Z",
                "account_id": usd_account_id,
                "category_id": test_data.category_id,
            },
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 422, r.text
        body = r.json()["error"]
        assert body["code"] == "RATE_UNAVAILABLE", body
        assert "exchange_rate" in (body.get("fields") or {}), body

        async with db.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM expense_transactions WHERE id = $1", txn_id,
            )
        assert row is None, "No ledger row should be written when rate lookup fails"

    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1", txn_id,
            )
            await conn.execute(
                "DELETE FROM expense_transactions WHERE id = $1", txn_id,
            )
            await conn.execute(
                "DELETE FROM expense_bank_accounts WHERE id = $1", usd_account_id,
            )
        clear_rate_cache()
