"""Regression tests for the four ⚪ Low bugs (open-bugs.md, closed 2026-08-07).

  1. /health answers 503 with the standard error shape when the DB is
     unreachable (or the pool does not exist yet) — never a stray 500.
  2. The request-serving pool passes command_timeout to asyncpg.
  3. ?search= matches %, _ and \\ literally — a user searching "50%" is
     asking about the string "50%", not "50 followed by anything".
  4. compute_month_flow's hashtag aggregation filters transaction_source = 1
     like every other junction read. sql/027's CHECK means a source-2 row
     cannot exist today, so the test drops that constraint for its own scope
     (test DB only) to plant the rogue row the filter must ignore.
"""
from __future__ import annotations

import uuid

import pytest

from app import db
from app.config import settings


def _idem() -> dict[str, str]:
    return {"X-Idempotency-Key": str(uuid.uuid4())}


# ---------------------------------------------------------------------------
# 1. /health → 503 when the DB is unreachable
# ---------------------------------------------------------------------------


class _DeadPool:
    def acquire(self):
        raise ConnectionError("simulated: database is down")


@pytest.mark.parametrize("broken_pool", [_DeadPool(), None], ids=["acquire-raises", "pool-is-none"])
async def test_health_returns_503_when_db_unreachable(client, broken_pool):
    original = db.pool
    db.pool = broken_pool
    try:
        r = await client.get("/health")
    finally:
        db.pool = original

    assert r.status_code == 503, r.text
    body = r.json()
    assert body["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert body["error"]["message"] == "Database is unreachable."


async def test_health_ok_with_live_db(client):
    r = await client.get("/health")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# 2. command_timeout reaches asyncpg.create_pool
# ---------------------------------------------------------------------------


async def test_pool_command_timeout_wired(monkeypatch):
    captured: dict = {}

    async def fake_create_pool(dsn, **kwargs):
        captured.update(kwargs)
        return "sentinel-pool"

    original = db.pool
    monkeypatch.setattr(db.asyncpg, "create_pool", fake_create_pool)
    try:
        await db.connect()
        assert captured["command_timeout"] == settings.db_command_timeout
        assert settings.db_command_timeout > 0
    finally:
        db.pool = original


# ---------------------------------------------------------------------------
# 3. ?search= treats %, _ and \ as literals
# ---------------------------------------------------------------------------


async def _create_txn(client, test_data, title: str) -> str:
    txn_id = str(uuid.uuid4())
    r = await client.post(
        "/v1/transactions",
        json={
            "id": txn_id,
            "title": title,
            "amount_cents": -100,
            "date": "2026-05-10T12:00:00Z",
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
        },
        headers=_idem(),
    )
    assert r.status_code == 201, r.text
    return txn_id


async def _search_titles(client, term: str) -> set[str]:
    r = await client.get("/v1/transactions", params={"search": term})
    assert r.status_code == 200, r.text
    return {item["title"] for item in r.json()["items"]}


@pytest.mark.parametrize(
    "meta_title, decoy_title, term_suffix",
    [
        ("50% off", "50x off", "50%"),
        ("a_c", "abc", "a_c"),
        ("back\\slash", "backXslash", "back\\slash"),
    ],
    ids=["percent", "underscore", "backslash"],
)
async def test_search_matches_metacharacters_literally(
    client, test_data, meta_title, decoy_title, term_suffix
):
    # A unique tag isolates this test's rows from everything else in the
    # session; the decoy is what the unescaped pattern used to match.
    tag = f"lownits-{uuid.uuid4().hex[:8]}"
    ids = [
        await _create_txn(client, test_data, f"{tag}-{meta_title}"),
        await _create_txn(client, test_data, f"{tag}-{decoy_title}"),
    ]
    try:
        titles = await _search_titles(client, f"{tag}-{term_suffix}")
        assert titles == {f"{tag}-{meta_title}"}

        # The tag alone still finds both — escaping narrows, it doesn't break.
        assert await _search_titles(client, tag) == {
            f"{tag}-{meta_title}",
            f"{tag}-{decoy_title}",
        }
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = ANY($1::uuid[])", ids
            )
            await conn.execute(
                "DELETE FROM expense_transactions WHERE id = ANY($1::uuid[])", ids
            )


# ---------------------------------------------------------------------------
# 4. Hashtag aggregation ignores transaction_source ≠ 1
# ---------------------------------------------------------------------------


async def test_report_hashtags_ignore_non_ledger_junction_rows(client, test_data):
    txn_id = str(uuid.uuid4())
    r = await client.post(
        "/v1/transactions",
        json={
            "id": txn_id,
            "title": f"source-filter-{uuid.uuid4().hex[:8]}",
            "amount_cents": -700,
            "date": "2024-11-15T12:00:00Z",  # a month nothing else writes to
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
            "hashtag_ids": [test_data.hashtag_id],
        },
        headers=_idem(),
    )
    assert r.status_code == 201, r.text

    async with db.pool.acquire() as conn:
        # sql/027 CHECKs transaction_source = 1, so the rogue row must be
        # planted with the constraint out of the way. Test DB only; restored
        # in the finally below after the row is gone again.
        await conn.execute(
            "ALTER TABLE expense_transaction_hashtags "
            "DROP CONSTRAINT hashtags_transaction_source_valid"
        )
        await conn.execute(
            """INSERT INTO expense_transaction_hashtags
                (transaction_id, transaction_source, hashtag_id, user_id, created_at, updated_at)
               VALUES ($1, 2, $2, $3, now(), now())""",
            txn_id, test_data.hashtag2_id, test_data.user_id,
        )

    try:
        r = await client.get("/v1/reports/monthly", params={"year": 2024, "month": 11})
        assert r.status_code == 200, r.text
        groups = [
            set(b["hashtag_ids"])
            for cat in r.json()["categories"]
            for b in cat["hashtag_breakdown"]
        ]
        assert {test_data.hashtag_id} in groups
        assert all(test_data.hashtag2_id not in g for g in groups)
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM expense_transaction_hashtags "
                "WHERE transaction_id = $1 AND transaction_source = 2",
                txn_id,
            )
            await conn.execute(
                "ALTER TABLE expense_transaction_hashtags "
                "ADD CONSTRAINT hashtags_transaction_source_valid "
                "CHECK (transaction_source = 1)"
            )
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1", txn_id
            )
            await conn.execute(
                "DELETE FROM expense_transaction_hashtags WHERE transaction_id = $1", txn_id
            )
            await conn.execute(
                "DELETE FROM expense_transactions WHERE id = $1", txn_id
            )
