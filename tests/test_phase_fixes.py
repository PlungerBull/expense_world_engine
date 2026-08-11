"""Regression tests for the audit-driven fixes shipped in this sprint.

Each test pins a single behaviour so a future refactor can't silently
regress the specific hazard the fix addressed.

  * Phase 1.7 — /activity?resource_id=<non-uuid> returns 422, not 500.
  * Phase 2.4 — category/hashtag names are trimmed, empties rejected,
    uniqueness is case-insensitive.
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
