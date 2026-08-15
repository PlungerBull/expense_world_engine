"""Pins for sql/027 — one of the two CHECK constraints from bug 6.3.

  * exchange_rates_rate_positive: a non-positive rate would misprice every
    home-currency figure (the table is the sole source since sql/021). The
    fetch/backfill jobs also refuse it in _upsert_rate before the INSERT.

The other one, hashtags_transaction_source_valid, has moved to
`test_sql033_checks.py`: sql/027 pinned it to `= 1` because only the ledger
wrote junction rows, and sql/033 widened it to `IN (1, 2)` when the inbox
writer shipped — exactly the sequencing sql/027's header prescribed. The pin
belongs with the definition that is live.
"""
from datetime import date
from decimal import Decimal

import asyncpg
import pytest

from app import db
from app.jobs.fetch_exchange_rates import _upsert_rate


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_rate", [0, -3.4])
async def test_exchange_rate_must_be_positive(bad_rate):
    async with db.pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """INSERT INTO exchange_rates (base_currency, target_currency, rate, rate_date, created_at)
                   VALUES ('USD', 'PEN', $1, '1999-01-01', now())""",
                bad_rate,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_rate", [Decimal("0"), Decimal("-1")])
async def test_upsert_rate_refuses_non_positive_before_insert(bad_rate):
    """The job-side guard fires before SQL, so one bad provider value is a
    counted failure, not a CheckViolationError aborting the whole run."""
    async with db.pool.acquire() as conn:
        with pytest.raises(ValueError):
            await _upsert_rate(conn, "PEN", date(1999, 1, 2), bad_rate)
        row = await conn.fetchrow(
            "SELECT 1 FROM exchange_rates WHERE rate_date = '1999-01-02'"
        )
        assert row is None, "guard must reject before any INSERT happens"
