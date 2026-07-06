"""Integration tests for GET /v1/exchange-rates/history.

The history endpoint lists stored ``exchange_rates`` rows verbatim —
no fallback semantics, standard pagination envelope, ordered
``rate_date DESC, base ASC, target ASC``.

Tests seed rows on synthetic far-past dates (1997-01-xx) so assertions
are scoped to data this file controls; the dev DB's real rows (manual
2026 entries plus conftest's CURRENT_DATE seed) never collide with them.
Seeded rows are deleted in fixture teardown.

Run: .venv/bin/pytest tests/test_exchange_rates_history.py -v
"""
from datetime import date

import pytest

from app import db


# Far-past dates no real data can occupy (production data starts 2026).
DAY_NEW = "1997-01-15"
DAY_OLD = "1997-01-14"
DAY_EMPTY = "1996-12-25"

# (base, target, rate, rate_date) — PEN/USD only per the sql/015 currency lock.
# Two pairs on DAY_NEW exercise the base ASC interleave within a day.
SEED_ROWS = [
    ("PEN", "USD", 0.2849, DAY_NEW),
    ("USD", "PEN", 3.51, DAY_NEW),
    ("USD", "PEN", 3.50, DAY_OLD),
]


@pytest.fixture
async def history_rows(client):
    async with db.pool.acquire() as conn:
        for base, target, rate, rate_date in SEED_ROWS:
            await conn.execute(
                """INSERT INTO exchange_rates (base_currency, target_currency, rate, rate_date, created_at)
                   VALUES ($1, $2, $3, $4, now())
                   ON CONFLICT (base_currency, target_currency, rate_date) DO NOTHING""",
                base, target, rate, date.fromisoformat(rate_date),
            )
    yield SEED_ROWS
    async with db.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM exchange_rates WHERE rate_date IN ($1, $2)",
            date.fromisoformat(DAY_NEW), date.fromisoformat(DAY_OLD),
        )


@pytest.mark.asyncio
async def test_envelope_and_item_shape(client, history_rows):
    """Standard pagination envelope; items carry exactly base/target/rate_date/rate."""
    resp = await client.get("/v1/exchange-rates/history", params={"date": DAY_NEW})
    assert resp.status_code == 200
    body = resp.json()

    assert set(body.keys()) == {"items", "total", "limit", "offset"}
    assert body["limit"] == 50
    assert body["offset"] == 0

    for item in body["items"]:
        assert set(item.keys()) == {"base", "target", "rate_date", "rate"}
        assert isinstance(item["rate"], float), (
            "rate must serialize as a JSON number, matching the lookup endpoint"
        )


@pytest.mark.asyncio
async def test_exact_date_filter_and_within_day_ordering(client, history_rows):
    """The date filter returns only that day's rows, base ASC within the day."""
    resp = await client.get("/v1/exchange-rates/history", params={"date": DAY_NEW})
    body = resp.json()

    assert body["total"] == 2
    assert [(i["base"], i["target"]) for i in body["items"]] == [
        ("PEN", "USD"),
        ("USD", "PEN"),
    ]
    assert all(i["rate_date"] == DAY_NEW for i in body["items"])


@pytest.mark.asyncio
async def test_global_ordering_newest_first(client, history_rows):
    """Any page is sorted by (rate_date DESC, base ASC, target ASC).

    Asserted as a sortedness property of the returned page rather than
    against fixed rows — the dev DB holds real rows this test doesn't
    control, and a page of a correctly sorted list is itself sorted.
    """
    resp = await client.get("/v1/exchange-rates/history", params={"limit": 200})
    body = resp.json()
    items = body["items"]
    assert len(items) >= 3, "seeded rows should be present"

    for prev, cur in zip(items, items[1:]):
        assert (
            prev["rate_date"] > cur["rate_date"]
            or (
                prev["rate_date"] == cur["rate_date"]
                and (prev["base"], prev["target"]) < (cur["base"], cur["target"])
            )
        ), f"rows out of order: {prev} before {cur}"


@pytest.mark.asyncio
async def test_pagination_slices_filtered_set(client, history_rows):
    """limit/offset page through the filtered set; total stays the full count."""
    page1 = (
        await client.get(
            "/v1/exchange-rates/history",
            params={"date": DAY_NEW, "limit": 1, "offset": 0},
        )
    ).json()
    page2 = (
        await client.get(
            "/v1/exchange-rates/history",
            params={"date": DAY_NEW, "limit": 1, "offset": 1},
        )
    ).json()

    assert page1["total"] == 2 and page2["total"] == 2
    assert len(page1["items"]) == 1 and len(page2["items"]) == 1
    assert page1["items"][0]["base"] == "PEN"
    assert page2["items"][0]["base"] == "USD"


@pytest.mark.asyncio
async def test_empty_date_is_empty_page_not_error(client, history_rows):
    """A date with no rows returns items: [] / total: 0 — never 404."""
    resp = await client.get("/v1/exchange-rates/history", params={"date": DAY_EMPTY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0


@pytest.mark.asyncio
async def test_out_of_range_pagination_is_422(client):
    """FastAPI-level validation, surfaced in the house error envelope."""
    for params in ({"limit": 0}, {"limit": 201}, {"offset": -1}, {"date": "not-a-date"}):
        resp = await client.get("/v1/exchange-rates/history", params=params)
        assert resp.status_code == 422, f"{params} should fail validation"
        error = resp.json()["error"]
        assert error["code"] == "VALIDATION_ERROR"
        assert isinstance(error["fields"], dict), (
            "VALIDATION_ERROR.fields is always an object, never null"
        )
