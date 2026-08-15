"""WP2 — currency is converted at read time, and nothing derived is stored.

`sql/021` dropped `expense_transactions.amount_home_cents`,
`expense_transactions.exchange_rate` and `expense_transaction_inbox.exchange_rate`.
These tests pin what that buys and what it costs:

  * The write path performs no rate lookup at all, so a transaction is
    recordable whether or not the FX table can price it. It used to `422`.
  * A row that cannot be priced makes its figure `null` **plus** a non-zero
    `unconverted_count` — never a partial total, and never a native amount
    wearing a home label.
  * `/dashboard` and `/reports/monthly` agree about the same month, because
    they share `compute_month_flow`.
  * Correcting a rate corrects every past report that used it.
  * `exchange_rate` on any write request is a `422`, not a silent drop.
  * A row's rate date resolves in the user's `display_timezone` — the same zone
    that buckets its month, so pricing and reporting never disagree.

Seeding rules this file obeys, because `exchange_rates` has no `user_id` and is
therefore shared across xdist workers:

  * **This file owns 2022** for rate rows, and 1992-06 for its unpriceable
    month. `test_exchange_rates_history` owns 1997, `test_home_currency_parity`
    owns 2001-2020, and `conftest` owns CURRENT_DATE.
  * The unconvertible assertions sit below 1997-01-14, the suite's earliest
    seeded rate. Anything seeded before that date breaks them.
  * Teardown deletes only this file's own dates — never `DELETE FROM
    exchange_rates`.

Ledger rows are scoped to a per-worker user and to categories this file creates,
so category-level figures are exact. Month totals are user-wide, which is safe
only because no other test writes a transaction dated 1992 or 2022 — the grep
that establishes that is `"19[0-9][0-9]-` / `"20[12][0-9]-` over `tests/`.
"""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app import db
from app.helpers.exchange_rate import clear_rate_cache


# 2022-05-10 is this file's working rate date; the priceable scenarios are dated
# to it. 3.58 is the rate docs/currency-model-decision.md uses in its worked
# example, kept so the numbers below read the same as the design record.
RATE_DATE = date(2022, 5, 10)
RATE = Decimal("3.58")

# 2022-03-31 / 2022-04-01 exist only for the timezone test, and their rates
# differ so that "which day did this price on" has a visible answer.
TZ_RATE_DATES = [date(2022, 3, 31), date(2022, 4, 1)]
TZ_RATES = {date(2022, 3, 31): Decimal("3.31"), date(2022, 4, 1): Decimal("3.41")}

SEEDED_DATES = [RATE_DATE] + TZ_RATE_DATES

# Below 1997-01-14, the earliest rate any test seeds. Carry-forward resolves the
# newest rate ON OR BEFORE a date, so nothing resolves here.
UNPRICEABLE = "1992-06-15T12:00:00Z"


class Fixtures:
    def __init__(self):
        self.usd_account_id = str(uuid.uuid4())
        self.usd_account_2_id = str(uuid.uuid4())
        self.pen_account_id = str(uuid.uuid4())
        self.category_id = str(uuid.uuid4())
        self.txn_ids: list[str] = []


@pytest.fixture
async def fx(test_data, db_pool):
    """Two USD accounts, a PEN account, a private category, and this file's rates."""
    data = Fixtures()

    async with db.pool.acquire() as conn:
        for rate_date in SEEDED_DATES:
            rate = TZ_RATES.get(rate_date, RATE)
            # DO UPDATE, not DO NOTHING: `test_correcting_a_rate_corrects_a_past
            # _report` mutates a rate in place, so a run whose teardown was
            # interrupted would leave the corrected value behind and silently
            # poison every later run. These dates belong to this file alone, so
            # overwriting them is safe.
            await conn.execute(
                """INSERT INTO exchange_rates
                    (base_currency, target_currency, rate, rate_date, created_at)
                   VALUES ('USD', 'PEN', $1, $2, now())
                   ON CONFLICT (base_currency, target_currency, rate_date)
                   DO UPDATE SET rate = EXCLUDED.rate""",
                rate, rate_date,
            )
        for account_id, name, currency in (
            (data.usd_account_id, "WP2-USD", "USD"),
            (data.usd_account_2_id, "WP2-USD-2", "USD"),
            (data.pen_account_id, "WP2-PEN", "PEN"),
        ):
            await conn.execute(
                """INSERT INTO expense_bank_accounts
                    (id, user_id, name, currency_code, is_person, color,
                     is_archived, sort_order, created_at, updated_at)
                   VALUES ($1, $2, $3, $4, false, '#00AAFF', false, 97,
                           now(), now())""",
                account_id, test_data.user_id, f"{name}-{account_id[:8]}", currency,
            )
        await conn.execute(
            """INSERT INTO expense_categories
                (id, user_id, name, color, sort_order,
                 created_at, updated_at)
               VALUES ($1, $2, $3, '#00AAFF', 97, now(), now())""",
            data.category_id, test_data.user_id, f"WP2 Cat {data.category_id[:8]}",
        )

    # get_rate caches negatives for an hour; a date probed before its row landed
    # would return a stale None and fail for the wrong reason.
    clear_rate_cache()

    yield data

    async with db.pool.acquire() as conn:
        if data.txn_ids:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = ANY($1::uuid[])",
                data.txn_ids,
            )
            await conn.execute(
                "DELETE FROM expense_transactions WHERE id = ANY($1::uuid[])",
                data.txn_ids,
            )
        await conn.execute(
            "DELETE FROM expense_bank_accounts WHERE id = ANY($1::uuid[])",
            [data.usd_account_id, data.usd_account_2_id, data.pen_account_id],
        )
        await conn.execute(
            "DELETE FROM expense_categories WHERE id = $1", data.category_id
        )
        # Only our own dates. Other workers share this table.
        await conn.execute(
            """DELETE FROM exchange_rates
               WHERE base_currency = 'USD' AND target_currency = 'PEN'
                 AND rate_date = ANY($1::date[])""",
            SEEDED_DATES,
        )
    clear_rate_cache()


async def _create(client, fx: Fixtures, **overrides) -> dict:
    """POST a transaction, register it for teardown, return the response body."""
    payload = {
        "id": str(uuid.uuid4()),
        "title": f"wp2-{uuid.uuid4()}",
        "amount_cents": -1000,
        "date": f"{RATE_DATE.isoformat()}T12:00:00Z",
        "account_id": fx.usd_account_id,
        "category_id": fx.category_id,
    }
    payload.update(overrides)
    r = await client.post(
        "/v1/transactions",
        json=payload,
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    fx.txn_ids.append(body["id"])
    return body


async def _report(client, year: int, month: int) -> dict:
    r = await client.get("/v1/reports/monthly", params={"year": year, "month": month})
    assert r.status_code == 200, r.text
    return r.json()


def _category(report: dict, category_id: str) -> dict:
    row = next((c for c in report["categories"] if c["id"] == category_id), None)
    assert row is not None, f"category {category_id} missing from report"
    return row


# ---------------------------------------------------------------------------
# The write path does no currency work
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_transaction_with_no_resolvable_rate_is_still_recordable(client, fx):
    """A USD transaction dated before every seeded rate must be created, not refused.

    This is the behaviour that inverted. The engine used to resolve a rate on
    the write path so it could store `amount_home_cents`, and refused with 422
    RATE_UNAVAILABLE when the FX table had nothing on or before the date — which
    meant a stale FX job could block recording a transaction that had already
    happened. Recording what happened is not a currency question.

    Two consequences worth having, both exercised here: cross-currency writes no
    longer fail when the rate table is behind, and transactions dated before the
    provider's floor (2024-03-02) become recordable at all.
    """
    body = await _create(client, fx, date=UNPRICEABLE, amount_cents=-2500)

    assert body["amount_cents"] == 2500  # stored positive; direction is in the type
    assert body["transaction_type"] == 1
    # Nothing derived came back, because nothing derived was stored.
    assert "amount_home_cents" not in body
    assert "exchange_rate" not in body


@pytest.mark.asyncio
async def test_unpriceable_rows_report_null_plus_a_count_never_a_partial_total(
    client, fx,
):
    """A month whose rows cannot be priced reports `null` and says how many.

    Both halves are load-bearing, and the second is the one that is easy to get
    wrong. A per-row NULL does not survive aggregation: `SUM` skips NULLs, and
    the inflow/outflow shape — `SUM(CASE WHEN x > 0 THEN x ELSE 0 END)` — scores
    a NULL row as ZERO, because `NULL > 0` is NULL rather than true. Without the
    count, a month where nothing could be converted reports 0 and is
    indistinguishable from a month where nothing happened.
    """
    await _create(client, fx, date=UNPRICEABLE, amount_cents=-2500)
    await _create(client, fx, date=UNPRICEABLE, amount_cents=-1500)

    report = await _report(client, 1992, 6)
    row = _category(report, fx.category_id)

    assert row["spent_home_cents"] is None
    assert row["unconverted_count"] == 2
    # The breakdown is not exempt: it is where the category total is summed from.
    assert len(row["hashtag_breakdown"]) == 1
    assert row["hashtag_breakdown"][0]["spent_home_cents"] is None
    assert row["hashtag_breakdown"][0]["unconverted_count"] == 2

    totals = report["totals"]
    assert totals["unconverted_count"] == 2
    assert totals["inflow_home_cents"] is None
    assert totals["outflow_home_cents"] is None
    assert totals["net_home_cents"] is None, (
        "a null outflow must not net to a number — that is the silent-zero shape"
    )


@pytest.mark.asyncio
async def test_a_priceable_row_converts_at_its_own_dates_rate(client, fx):
    """$25.00 on 2022-05-10 at 3.58 is S/89.50, and the row is not flagged."""
    await _create(client, fx, amount_cents=-2500)

    row = _category(await _report(client, 2022, 5), fx.category_id)
    assert row["spent_home_cents"] == -8950  # round(2500 * 3.58), outflow
    assert row["unconverted_count"] == 0


# ---------------------------------------------------------------------------
# The two read endpoints cannot disagree
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dashboard_and_monthly_report_agree_about_the_same_month(client, fx):
    """Same month, same numbers, from the two endpoints that both compute it.

    They share `compute_month_flow`, and this test exists because they once did
    not: each carried its own literal copy of the sign matrix (audit WP9.1), and
    the stated risk was exactly this — /dashboard and /reports/monthly
    disagreeing about the same month.
    """
    now = datetime.now(timezone.utc)
    await _create(
        client, fx,
        date=now.strftime("%Y-%m-%dT00:00:00+00:00"),
        account_id=fx.pen_account_id,
        amount_cents=-4200,
    )

    r = await client.get("/v1/dashboard")
    assert r.status_code == 200, r.text
    dashboard = r.json()
    report = await _report(client, now.year, now.month)

    assert dashboard["month"] == {"year": now.year, "month": now.month}
    assert dashboard["totals"] == report["totals"]
    assert _category(dashboard, fx.category_id) == _category(report, fx.category_id)


# ---------------------------------------------------------------------------
# The property the stored model could not have
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_correcting_a_rate_corrects_a_past_report(client, fx):
    """Fix the rate table once and every historical report using it corrects itself.

    Under the stored model a wrong rate was wrong forever: the conversion was
    frozen into the row at write time, and only a backfill could revisit it.
    Note the ledger is untouched here — the figure moves because the rate did.
    """
    await _create(client, fx, amount_cents=-2500)

    before = _category(await _report(client, 2022, 5), fx.category_id)
    assert before["spent_home_cents"] == -8950  # 2500 * 3.58

    async with db.pool.acquire() as conn:
        await conn.execute(
            """UPDATE exchange_rates SET rate = $1
               WHERE base_currency = 'USD' AND target_currency = 'PEN'
                 AND rate_date = $2""",
            Decimal("4.00"), RATE_DATE,
        )

    after = _category(await _report(client, 2022, 5), fx.category_id)
    assert after["spent_home_cents"] == -10000  # 2500 * 4.00, no write involved


# ---------------------------------------------------------------------------
# Which calendar day a row prices on
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_rate_date_resolves_in_the_users_display_timezone(client, fx, test_data):
    """A near-midnight transaction prices on the day it is REPORTED in.

    `expense_transactions.date` is `timestamptz`, so "the transaction's date" is
    not self-evident — a bare `::date` resolves in whatever timezone the database
    session happens to carry, which is machine-dependent. The rate date is
    therefore cast in the user's `display_timezone`, the same zone
    `compute_month_bounds` uses to bucket months, so a row is never counted in
    one month and priced in another.

    2022-03-31T23:00-05:00 is 2022-04-01T04:00Z. In America/Lima that is March
    31 (rate 3.31); in UTC it is April 1 (rate 3.41). One transaction, two
    answers, and the month follows the same boundary.
    """
    await _create(client, fx, date="2022-04-01T04:00:00Z", amount_cents=-1000)

    async def _set_timezone(zone: str) -> None:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE user_settings SET display_timezone = $1 WHERE user_id = $2",
                zone, test_data.user_id,
            )

    try:
        await _set_timezone("America/Lima")
        march = _category(await _report(client, 2022, 3), fx.category_id)
        assert march["spent_home_cents"] == -3310, "priced at the March 31 rate"

        april = await _report(client, 2022, 4)
        assert not [c for c in april["categories"]
                    if c["id"] == fx.category_id and c["spent_home_cents"]], (
            "the row belongs to March in America/Lima"
        )

        await _set_timezone("UTC")
        april = _category(await _report(client, 2022, 4), fx.category_id)
        assert april["spent_home_cents"] == -3410, "priced at the April 1 rate"
    finally:
        await _set_timezone("UTC")


# ---------------------------------------------------------------------------
# Fail closed on the field that no longer exists
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_exchange_rate_is_rejected_on_every_write_surface(client, fx, test_data):
    """`exchange_rate` 422s wherever a client used to send it.

    Silently dropping it would leave a caller believing the engine had honoured
    a rate it threw away — which is the failure mode `CLAUDE.md`'s fail-closed
    rule names directly: unknown input must 422 rather than be silently dropped.
    """
    existing = await _create(client, fx, amount_cents=-1000)
    inbox_id = str(uuid.uuid4())
    create_inbox = await client.post(
        "/v1/inbox",
        json={"id": inbox_id, "title": f"wp2-inbox-{uuid.uuid4()}"},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_inbox.status_code == 201, create_inbox.text

    base_txn = {
        "id": str(uuid.uuid4()),
        "title": f"wp2-rate-{uuid.uuid4()}",
        "amount_cents": -1000,
        "date": f"{RATE_DATE.isoformat()}T12:00:00Z",
        "account_id": fx.usd_account_id,
        "category_id": fx.category_id,
        "exchange_rate": 3.58,
    }
    surfaces = (
        ("POST /transactions", "post", "/v1/transactions", base_txn),
        ("PUT /transactions", "put", f"/v1/transactions/{existing['id']}",
         {"exchange_rate": 3.58}),
        ("POST /transactions/batch", "post", "/v1/transactions/batch",
         {"transactions": [base_txn]}),
        ("POST /inbox", "post", "/v1/inbox",
         {"id": str(uuid.uuid4()), "title": "x", "exchange_rate": 3.58}),
        ("PUT /inbox", "put", f"/v1/inbox/{inbox_id}", {"exchange_rate": 3.58}),
        ("POST /accounts/{id}/opening-balance", "post",
         f"/v1/accounts/{fx.usd_account_2_id}/opening-balance",
         {"transaction_id": str(uuid.uuid4()),
          "amount_cents": 1000,
          "date": f"{RATE_DATE.isoformat()}T12:00:00Z",
          "exchange_rate": 3.58}),
    )

    try:
        for label, method, url, payload in surfaces:
            r = await getattr(client, method)(
                url, json=payload,
                headers={"X-Idempotency-Key": str(uuid.uuid4())},
            )
            assert r.status_code == 422, (label, r.text)
            body = r.json()["error"]
            assert body["code"] == "VALIDATION_ERROR", (label, body)
            # The batch path nests it as `transactions.0.exchange_rate`, so the
            # key is matched by suffix rather than equality.
            fields = body.get("fields") or {}
            assert any(k.endswith("exchange_rate") for k in fields), (label, body)
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1", inbox_id
            )
            await conn.execute(
                "DELETE FROM expense_transaction_inbox WHERE id = $1", inbox_id
            )
