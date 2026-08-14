"""Pins for bug fx-store-float (closed 2026-08-13): the stored rate is the
provider's decimal, digit for digit.

`exchange_rates.rate` is `numeric`, so whatever Python binds is what the column
keeps. Parsing provider JSON with the default `float` meant 3.37515314 was stored
as 3.3751531400000001070793587132357060909271240234375 — the float's true value,
spelled out. The error was ~1e-16 relative and mispriced nothing, and it was
parity-neutral besides (SQL and Python read the same stored row), which is why it
sat as a ⚪ Low. It is pinned rather than left alone because the rate table is the
sole input to every home-currency figure since sql/021, and rounding work — bug
1.7-round, which is what surfaced this — cannot reason about a column that does
not hold what was written to it.

The test that matters is the one on the text form. Comparing `Decimal` to
`Decimal` would pass under the old code too (`Decimal(3.3751531400000001...) ==
Decimal("3.37515314")` is False, but a round-trip through the same float is
not what the column stores), so the assertion is on `rate::text` — the digits
Postgres actually holds.

Seeding rules from `test_fx_plausibility`'s docstring apply: `exchange_rates` has
no `user_id` and its rows are global across xdist workers, so this file owns 2016
and deletes only its own dates.

Run: .venv/bin/pytest tests/test_fx_decimal_fidelity.py -v
"""
import json
from datetime import date
from decimal import Decimal

import pytest

from app import db
from app.jobs.fetch_exchange_rates import _apply_rates, _fetch_currency_api, _upsert_rate

# The real USD->PEN rate from 2026-08-13, the day this was found: 8 decimal
# places, and not representable in binary.
PROVIDER_RATE = "3.37515314"
FLOAT_EXPANSION = "3.3751531400000001070793587132357060909271240234375"

DAY_UPSERT = date(2016, 3, 10)
DAY_APPLY = date(2016, 4, 10)
TOUCHED_DATES = [DAY_UPSERT, DAY_APPLY]


@pytest.fixture
async def fx(test_data, db_pool):
    async with db.pool.acquire() as conn:
        await conn.execute(
            """DELETE FROM exchange_rates
               WHERE base_currency = 'USD' AND target_currency = 'PEN'
                 AND rate_date = ANY($1::date[])""",
            TOUCHED_DATES,
        )
    yield
    async with db.pool.acquire() as conn:
        await conn.execute(
            """DELETE FROM exchange_rates
               WHERE base_currency = 'USD' AND target_currency = 'PEN'
                 AND rate_date = ANY($1::date[])""",
            TOUCHED_DATES,
        )


async def _stored_text(conn, rate_date: date) -> str:
    return await conn.fetchval(
        """SELECT rate::text FROM exchange_rates
           WHERE base_currency = 'USD' AND target_currency = 'PEN' AND rate_date = $1""",
        rate_date,
    )


def test_provider_json_parses_to_decimal(monkeypatch):
    """The fix itself: `parse_float=Decimal` in `_fetch_currency_api`.

    Everything downstream is just the absence of a cast back, so this is the one
    place a regression could reintroduce the whole bug.
    """
    body = ('{"date": "2026-08-13", "usd": {"pen": %s, "jpy": 150}}' % PROVIDER_RATE).encode()

    class _Resp:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        "app.jobs.fetch_exchange_rates.urllib.request.urlopen",
        lambda req, timeout=None: _Resp(),
    )

    resp = _fetch_currency_api()
    rate = resp["rates"]["PEN"]
    assert isinstance(rate, Decimal), f"provider rate parsed as {type(rate).__name__}"
    assert str(rate) == PROVIDER_RATE
    # A whole-number rate is an int, not a float — `parse_float` never sees it.
    # `_apply_rates` normalizes it with `Decimal(...)`, exact either way.
    assert isinstance(resp["rates"]["JPY"], int)


@pytest.mark.asyncio
async def test_upsert_stores_the_providers_digits(fx):
    async with db.pool.acquire() as conn:
        assert await _upsert_rate(conn, "PEN", DAY_UPSERT, Decimal(PROVIDER_RATE)) is True
        stored = await _stored_text(conn, DAY_UPSERT)

    assert stored == PROVIDER_RATE, (
        f"stored {stored}, provider published {PROVIDER_RATE}"
    )
    assert stored != FLOAT_EXPANSION, "the float expansion is back in the column"


@pytest.mark.asyncio
async def test_full_parse_to_column_round_trip(fx):
    """End to end, the way the job runs it: raw provider bytes -> `_apply_rates`
    -> the column. The two halves of the fix (parse as Decimal, do not cast back)
    only produce a correct row together, and this is what fails if either is
    undone.
    """
    raw = json.loads(
        '{"date": "2026-08-13", "usd": {"pen": %s}}' % PROVIDER_RATE,
        parse_float=Decimal,
    )
    rates = {code.upper(): rate for code, rate in raw["usd"].items()}

    async with db.pool.acquire() as conn:
        tally = await _apply_rates(conn, ["PEN"], rates, DAY_APPLY)
        stored = await _stored_text(conn, DAY_APPLY)

    assert tally["inserted"] == ["PEN"], tally
    assert stored == PROVIDER_RATE
