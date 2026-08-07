"""WP5 — schema slimming (sql/024; WP5 work package in git history).

Pins the behaviour the deletion program changed:

  * PUT /auth/settings fails closed: unknown fields (including the six
    deleted preference columns) 422 instead of being silently dropped.
  * display_timezone is validated on write, on both write paths
    (PUT /auth/settings and POST /auth/bootstrap) — it reaches
    `AT TIME ZONE` on every report read, so a bad value stored here
    would 500 the reports.
  * The settings response carries exactly the surviving fields; deleting
    a field means deleting it from the model, not omitting it (the
    null-over-omission rule is about values, not dead columns).
  * Report math is untouched by the is_archived drop.
  * The hashtag junction upsert works without its `version` column.
  * actor_type and email are gone from the wire.

Run: .venv/bin/pytest tests/test_wp5_schema_slimming.py -v
"""
import uuid

import pytest

from app import db


# ---------------------------------------------------------------------------
# Settings: fail closed + timezone validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_settings_field_returns_422(client):
    """A caller still sending a deleted preference gets told, not ignored."""
    r = await client.put(
        "/v1/auth/settings",
        json={"theme": 2},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 422, r.text
    body = r.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert "theme" in (body.get("fields") or {})


@pytest.mark.asyncio
async def test_invalid_timezone_returns_422(client):
    r = await client.put(
        "/v1/auth/settings",
        json={"display_timezone": "Not/AZone"},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 422, r.text
    body = r.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert "display_timezone" in (body.get("fields") or {})


@pytest.mark.asyncio
async def test_valid_timezone_updates_and_persists(client):
    r = await client.put(
        "/v1/auth/settings",
        json={"display_timezone": "America/Lima"},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 200, r.text
    assert r.json()["display_timezone"] == "America/Lima"

    me = await client.get("/v1/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["settings"]["display_timezone"] == "America/Lima"


@pytest.mark.asyncio
async def test_bootstrap_rejects_invalid_timezone(client):
    """The second write path — bootstrap's `timezone` — gets the same guard."""
    r = await client.post(
        "/v1/auth/bootstrap",
        json={"display_name": "tz-guard", "timezone": "Mars/OlympusMons"},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 422, r.text
    body = r.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert "timezone" in (body.get("fields") or {})


@pytest.mark.asyncio
async def test_settings_response_shape(client):
    """Exactly the surviving fields; version still present and bumping."""
    before = await client.get("/v1/auth/me")
    assert before.status_code == 200, before.text
    settings_before = before.json()["settings"]

    r = await client.put(
        "/v1/auth/settings",
        json={"display_timezone": "America/Lima"},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert set(body.keys()) == {
        "user_id", "main_currency", "display_timezone",
        "version", "created_at", "updated_at",
    }
    assert body["version"] == settings_before["version"] + 1

    # The user half of /auth/me lost `email` with the column.
    assert "email" not in before.json()["user"]


# ---------------------------------------------------------------------------
# Report math is untouched by the is_archived drop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_includes_every_category_with_spend(client, test_data):
    """compute_month_flow never filtered on is_archived, so dropping the
    column must not change a figure. A category with a -300 PEN expense
    reports exactly -300 (PEN is home, so it converts to itself)."""
    cat_id = str(uuid.uuid4())
    create_cat = await client.post(
        "/v1/categories",
        json={"id": cat_id, "name": f"wp5-report-{uuid.uuid4()}", "color": "#112233"},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_cat.status_code == 201, create_cat.text

    txn_id = str(uuid.uuid4())
    create_txn = await client.post(
        "/v1/transactions",
        json={
            "id": txn_id,
            "title": f"wp5-report-tx-{uuid.uuid4()}",
            "amount_cents": -300,
            "date": "2026-01-15T12:00:00Z",
            "account_id": test_data.account_id,
            "category_id": cat_id,
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_txn.status_code == 201, create_txn.text

    try:
        r = await client.get("/v1/reports/monthly", params={"year": 2026, "month": 1})
        assert r.status_code == 200, r.text
        cat_row = next(
            (c for c in r.json()["categories"] if c["id"] == cat_id), None,
        )
        assert cat_row is not None, (
            f"category {cat_id} disappeared from the monthly report"
        )
        assert cat_row["spent_home_cents"] == -300
        assert cat_row["unconverted_count"] == 0
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = ANY($1::uuid[]) AND user_id = $2",
                [txn_id, cat_id], test_data.user_id,
            )
            await conn.execute(
                "DELETE FROM expense_transactions WHERE id = $1 AND user_id = $2",
                txn_id, test_data.user_id,
            )
            await conn.execute(
                "DELETE FROM expense_categories WHERE id = $1 AND user_id = $2",
                cat_id, test_data.user_id,
            )


# ---------------------------------------------------------------------------
# Junction upsert without `version`
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hashtag_retag_round_trip_without_junction_version(client, test_data):
    """A → B → A,B: the second PUT re-activates A's soft-deleted junction
    row through the ON CONFLICT DO UPDATE path that used to bump `version`."""
    txn_id = str(uuid.uuid4())
    create_r = await client.post(
        "/v1/transactions",
        json={
            "id": txn_id,
            "title": f"wp5-retag-{uuid.uuid4()}",
            "amount_cents": -100,
            "date": "2026-04-12T12:00:00Z",
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
            "hashtag_ids": [test_data.hashtag_id],
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_r.status_code == 201, create_r.text
    assert create_r.json()["hashtag_ids"] == [test_data.hashtag_id]

    try:
        r = await client.put(
            f"/v1/transactions/{txn_id}",
            json={"hashtag_ids": [test_data.hashtag2_id]},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 200, r.text
        assert r.json()["hashtag_ids"] == [test_data.hashtag2_id]

        r = await client.put(
            f"/v1/transactions/{txn_id}",
            json={"hashtag_ids": [test_data.hashtag_id, test_data.hashtag2_id]},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 200, r.text
        assert sorted(r.json()["hashtag_ids"]) == sorted(
            [test_data.hashtag_id, test_data.hashtag2_id]
        )
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM expense_transaction_hashtags WHERE transaction_id = $1 AND user_id = $2",
                txn_id, test_data.user_id,
            )
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
                txn_id, test_data.user_id,
            )
            await conn.execute(
                "DELETE FROM expense_transactions WHERE id = $1 AND user_id = $2",
                txn_id, test_data.user_id,
            )


# ---------------------------------------------------------------------------
# Dropped wire fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dropped_fields_absent_from_responses(client, test_data):
    """actor_type off /activity, is_archived off categories/hashtags,
    parent_transaction_id off transactions."""
    activity = await client.get("/v1/activity?limit=1")
    assert activity.status_code == 200, activity.text
    for row in activity.json()["items"]:
        assert "actor_type" not in row

    cats = await client.get("/v1/categories?limit=1")
    assert cats.status_code == 200
    for row in cats.json()["items"]:
        assert "is_archived" not in row

    tags = await client.get("/v1/hashtags?limit=1")
    assert tags.status_code == 200
    for row in tags.json()["items"]:
        assert "is_archived" not in row

    txn = await client.get(f"/v1/transactions/{test_data.transaction_id}")
    assert txn.status_code == 200, txn.text
    assert "parent_transaction_id" not in txn.json()
