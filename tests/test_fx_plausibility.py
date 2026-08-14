"""Pins for the provider-rate plausibility guard (split out of bug 1.7).

Since sql/021 ``exchange_rates`` is the only source of every home-currency
figure, so one bad provider row misprices every report touching that date rather
than one write — and because the daily job upserts ``ON CONFLICT DO NOTHING``,
the first rate accepted for a day is the one that stands. A bad morning value
cannot be corrected by the three later runs. So the guard has to fire before the
INSERT, which is what this file pins.

Owner decision 2026-08-13: refuse the rate, do not store-and-flag. A refused date
has no row, and both carry-forward implementations (``get_rate`` and
``home_rate_join``) resolve ``rate_date <= <date>``, so reads price at the last
good rate instead of at a wrong one.

Seeding rules this file obeys, because ``exchange_rates`` has no ``user_id`` and
its rows are global across xdist workers:

  * Seed only in 2015. ``test_home_currency_parity`` owns 2010-06,
    ``test_exchange_rates_history`` owns 1997, ``test_wp2_read_time_currency``
    owns 2022, ``test_sql027_checks`` owns 1999, ``conftest`` owns CURRENT_DATE.
  * Delete only our own dates in teardown — never ``DELETE FROM exchange_rates``.
  * The guard reads a baseline from a 7-day window before the date under test, so
    every case here keeps its own dates clear of the others'.

Run: .venv/bin/pytest tests/test_fx_plausibility.py -v
"""
from datetime import date
from decimal import Decimal

import pytest

from app import db
from app.jobs.fetch_exchange_rates import (
    MAX_BASELINE_GAP_DAYS,
    MAX_RATE_MOVE_FRACTION,
    _apply_rates,
    _upsert_rate,
)

# One decade-slot per concern so no case can read another's baseline: the
# guard only ever looks 7 days back, and these are months apart.
#
# Decimal, not float — `_upsert_rate` takes the Decimal the provider parse
# produces, and a float argument would not even reach the guard: the ratio
# against a Decimal baseline raises TypeError before any comparison happens.
# Constructing the rates the way the job does is also what keeps this file
# honest about the arithmetic it claims to pin.
BASELINE = Decimal("3.40")

CASES = {
    "spike": date(2015, 3, 10),
    "crash": date(2015, 4, 10),
    "within_band": date(2015, 5, 10),
    "boundary": date(2015, 6, 10),
    "exact_edge": date(2015, 10, 10),
    "no_baseline": date(2015, 7, 10),
    "stale_baseline": date(2015, 8, 10),
    "tally": date(2015, 9, 10),
}
# Every date this file may touch, for teardown: each case's day and the day
# before it (where the baseline is seeded), plus the stale case's far baseline.
TOUCHED_DATES = (
    list(CASES.values())
    + [day.replace(day=9) for day in CASES.values()]
    + [date(2015, 8, 1)]
)


async def _seed(conn, rate_date: date, rate: Decimal) -> None:
    """Insert a baseline row directly, bypassing the guard under test."""
    await conn.execute(
        """INSERT INTO exchange_rates (base_currency, target_currency, rate, rate_date, created_at)
           VALUES ('USD', 'PEN', $1, $2, now())
           ON CONFLICT (base_currency, target_currency, rate_date) DO NOTHING""",
        rate, rate_date,
    )


async def _row_exists(conn, rate_date: date) -> bool:
    return await conn.fetchval(
        """SELECT 1 FROM exchange_rates
           WHERE base_currency = 'USD' AND target_currency = 'PEN' AND rate_date = $1""",
        rate_date,
    ) is not None


@pytest.fixture
async def fx(test_data, db_pool):
    """Clean our dates before and after — a previous crashed run must not
    leave a baseline that changes another case's verdict."""
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


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

# (label, case key, the rate the provider "sends")
#
# The spike is the failure this guard exists for: a decimal point in the wrong
# place. At the real USD/PEN scale that is 34.0 for 3.40 — a 10x move that would
# otherwise price a $50 dinner at S/1,700 instead of S/170.
REFUSAL_CASES = [
    ("decimal-point slip", "spike", BASELINE * 10),
    ("collapse", "crash", BASELINE / 10),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label,case,bad_rate", REFUSAL_CASES, ids=[c[0] for c in REFUSAL_CASES]
)
async def test_implausible_rate_is_refused_before_insert(fx, label, case, bad_rate):
    """Refused in both directions, and refused *before* any INSERT.

    The "before the INSERT" half is the point: an after-the-fact check would
    have to undo a row that reports may already have read.
    """
    day = CASES[case]
    async with db.pool.acquire() as conn:
        await _seed(conn, day.replace(day=9), BASELINE)

        with pytest.raises(ValueError, match="implausible"):
            await _upsert_rate(conn, "PEN", day, bad_rate)

        assert not await _row_exists(conn, day), (
            f"{label}: the guard must refuse before any INSERT happens"
        )


@pytest.mark.asyncio
async def test_refusal_message_names_the_rate_carried_forward(fx):
    """The stderr line has to be actionable on its own — the job is unattended,
    so whoever reads it later needs the date, both rates, and what reads will do
    in the meantime, without going to the DB to reconstruct it."""
    day = CASES["spike"]
    baseline_day = day.replace(day=9)
    async with db.pool.acquire() as conn:
        await _seed(conn, baseline_day, BASELINE)
        with pytest.raises(ValueError) as excinfo:
            await _upsert_rate(conn, "PEN", day, BASELINE * 10)

    message = str(excinfo.value)
    for fragment in ("USD->PEN", str(day), str(baseline_day), "carry"):
        assert fragment in message, f"{fragment!r} missing from: {message}"


# ---------------------------------------------------------------------------
# Acceptances — the guard must not refuse legitimate data
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ordinary_movement_is_accepted(fx):
    """A real day-to-day move is well under 1%; 2% must sail through."""
    day = CASES["within_band"]
    async with db.pool.acquire() as conn:
        await _seed(conn, day.replace(day=9), BASELINE)
        assert await _upsert_rate(conn, "PEN", day, BASELINE * Decimal("1.02")) is True
        assert await _row_exists(conn, day)


@pytest.mark.asyncio
async def test_the_band_sits_where_the_constant_says(fx):
    """Just inside the band passes, just outside is refused.

    The half-point margin is what pins the band's *location* — that it is at 10%
    and not, say, 50% — which is the realistic regression: a fat-fingered
    constant.

    The exact edge is asserted separately, and only became assertable when the
    guard went all-Decimal (bug fx-store-float, 2026-08-13). In float the move
    constructed as ``BASELINE * 1.10`` computed as 10.000000000000019%, so ``>``
    and ``>=`` were indistinguishable at the nominal edge and this file said so.
    Decimal makes ``0.34 / 3.40`` exactly ``0.10``, so "a move of exactly the
    limit is accepted" is now a real property rather than an artefact of how the
    fixture happened to round.
    """
    day = CASES["boundary"]
    margin = MAX_RATE_MOVE_FRACTION / 20  # half a percentage point at 10%
    async with db.pool.acquire() as conn:
        await _seed(conn, day.replace(day=9), BASELINE)

        inside = BASELINE * (1 + MAX_RATE_MOVE_FRACTION - margin)
        assert await _upsert_rate(conn, "PEN", day, inside) is True

        # Same date, so this second call also proves the guard is what refuses —
        # not the unique constraint, which would raise a different error.
        outside = BASELINE * (1 + MAX_RATE_MOVE_FRACTION + margin)
        with pytest.raises(ValueError, match="implausible"):
            await _upsert_rate(conn, "PEN", day, outside)


@pytest.mark.asyncio
async def test_a_move_of_exactly_the_limit_is_accepted(fx):
    """``move > MAX_RATE_MOVE_FRACTION`` — the limit itself is inside the band."""
    day = CASES["exact_edge"]
    async with db.pool.acquire() as conn:
        await _seed(conn, day.replace(day=9), BASELINE)
        at_the_edge = BASELINE * (1 + MAX_RATE_MOVE_FRACTION)
        assert await _upsert_rate(conn, "PEN", day, at_the_edge) is True
        assert await _row_exists(conn, day)


@pytest.mark.asyncio
async def test_rate_with_no_baseline_is_accepted(fx):
    """Nothing to judge against — a first-ever rate must not be refused, or the
    table could never be seeded and a backfill could never start."""
    day = CASES["no_baseline"]
    async with db.pool.acquire() as conn:
        assert await _upsert_rate(conn, "PEN", day, BASELINE) is True
        assert await _row_exists(conn, day)


@pytest.mark.asyncio
async def test_baseline_older_than_the_window_is_not_used(fx):
    """Across a long gap a 10% move is ordinary, so there is no usable baseline.

    Without this, a backfill resuming after a gap would refuse real history.
    """
    day = CASES["stale_baseline"]
    stale_day = date(2015, 8, 1)
    assert (day - stale_day).days > MAX_BASELINE_GAP_DAYS, "fixture must be outside the window"

    async with db.pool.acquire() as conn:
        await _seed(conn, stale_day, BASELINE)
        # A 10x move — refused outright against a fresh baseline.
        assert await _upsert_rate(conn, "PEN", day, BASELINE * 10) is True


# ---------------------------------------------------------------------------
# The run-level contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_refused_rate_is_a_counted_failure_not_an_aborted_run(fx):
    """One bad target never blocks the rest, and the run reports it.

    ``run()`` returns 2 when ``failed`` is non-empty, so this is also what makes
    the job exit non-zero and the refusal reach stderr.
    """
    day = CASES["tally"]
    async with db.pool.acquire() as conn:
        await _seed(conn, day.replace(day=9), BASELINE)
        tally = await _apply_rates(
            conn, ["PEN"], {"PEN": BASELINE * 10}, day
        )

    assert tally["inserted"] == []
    assert len(tally["failed"]) == 1
    target, reason = tally["failed"][0]
    assert target == "PEN"
    assert "implausible" in reason
