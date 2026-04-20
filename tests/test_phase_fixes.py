"""Regression tests for the audit-driven fixes shipped in this sprint.

Each test pins a single behaviour so a future refactor can't silently
regress the specific hazard the fix addressed.

  * Phase 1.7 — /activity?resource_id=<non-uuid> returns 422, not 500.
  * Phase 2.4 — category/hashtag names are trimmed, empties rejected,
    uniqueness is case-insensitive.
  * Phase 2.5 — recalculate_home_currency emits an orphan_transfer_legs
    counter instead of silently skipping.
  * Phase 3.6 — activity_log rows carry actor_type and the GET /activity
    response exposes it.
"""
import uuid

import pytest

from app import db
from app.helpers.recalculate_home_currency import recalculate_home_currency


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
# Phase 2.5 — recalc surfaces orphan_transfer_legs in the result summary
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_recalc_reports_orphan_transfer_legs(client, test_data):
    """Stage an orphan transfer leg — a transfer whose sibling is
    soft-deleted, so the recalc helper's ``deleted_at IS NULL`` fetch
    only returns the surviving leg. The helper must count the orphan
    in its summary instead of silently skipping it."""
    async with db.pool.acquire() as conn:
        cat_row = await conn.fetchrow(
            "SELECT id FROM expense_categories WHERE user_id = $1 LIMIT 1",
            test_data.user_id,
        )
        cat_id = cat_row["id"]

        leg_a = uuid.uuid4()
        leg_b = uuid.uuid4()
        # Insert both legs, pointing at each other, then soft-delete leg_b
        # so leg_a is an orphan from the recalc fetch's perspective.
        await conn.execute(
            """
            INSERT INTO expense_transactions
                (id, user_id, title, amount_cents, amount_home_cents, transaction_type,
                 transfer_direction, date, account_id, category_id, exchange_rate,
                 cleared, created_at, updated_at)
            VALUES ($1, $2, 'orphan-leg-a', 100, 100, 3, 1, now(), $3, $4, 1.0,
                 false, now(), now())
            """,
            leg_a, test_data.user_id, test_data.account_id, cat_id,
        )
        await conn.execute(
            """
            INSERT INTO expense_transactions
                (id, user_id, title, amount_cents, amount_home_cents, transaction_type,
                 transfer_direction, date, account_id, category_id, exchange_rate,
                 cleared, transfer_transaction_id, created_at, updated_at)
            VALUES ($1, $2, 'orphan-leg-b', 100, 100, 3, 2, now(), $3, $4, 1.0,
                 false, $5, now(), now())
            """,
            leg_b, test_data.user_id, test_data.account_id, cat_id, leg_a,
        )
        await conn.execute(
            "UPDATE expense_transactions SET transfer_transaction_id = $1 WHERE id = $2",
            leg_b, leg_a,
        )
        # Soft-delete leg_b to simulate the orphan scenario.
        await conn.execute(
            "UPDATE expense_transactions SET deleted_at = now() WHERE id = $1",
            leg_b,
        )
        try:
            async with conn.transaction():
                result = await recalculate_home_currency(
                    conn, test_data.user_id, "PEN",
                )
            assert result["orphan_transfer_legs"] >= 1, result
        finally:
            await conn.execute(
                "DELETE FROM expense_transactions WHERE id = ANY($1::uuid[])",
                [leg_a, leg_b],
            )


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
