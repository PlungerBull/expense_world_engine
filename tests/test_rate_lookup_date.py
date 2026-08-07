"""rate_lookup_date: "today" for rate lookups is the user's timezone, not UTC.

Bloat audit 2026-08-06, Correctness §7 (owner decision: display_timezone).
Four sites used `datetime.now(timezone.utc).date()` while reports resolved
"today" in display_timezone via compute_month_bounds — so a balance and a
report viewed between local midnight and UTC midnight could use rates from
different days. One helper now owns the definition.

No freezegun / datetime monkeypatching in this suite (test_rate_cache.py
explains why), so these tests are deterministic without mocking the clock.
"""
import uuid
from datetime import datetime, timezone

import pytest

from app import db
from app.helpers.exchange_rate import rate_lookup_date


def test_result_depends_on_the_timezone():
    """Etc/GMT-14 (UTC+14) and Etc/GMT+12 (UTC-12) are 26 hours apart, so
    their calendar dates can never coincide — this fails if anyone reverts
    the helper to a single UTC clock read."""
    assert rate_lookup_date("Etc/GMT-14") != rate_lookup_date("Etc/GMT+12")


def test_junk_timezone_falls_back_to_utc():
    # Sample UTC before and after: if midnight lands between the samples the
    # result must still equal one of them.
    before = datetime.now(timezone.utc).date()
    result = rate_lookup_date("Not/AZone")
    after = datetime.now(timezone.utc).date()
    assert result in (before, after)


@pytest.mark.asyncio
async def test_reads_survive_a_junk_stored_timezone(client, test_data):
    """display_timezone is validated on write (helpers/auth.py), but older
    rows may hold junk; simulate one via direct SQL and confirm every
    current-date rate consumer still answers instead of 500ing."""
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_settings SET display_timezone = 'Not/AZone' WHERE user_id = $1",
            test_data.user_id,
        )
    try:
        # /dashboard and /reports/monthly also pin the resolve_timezone
        # fallback on the SQL AT TIME ZONE binding in compute_month_flow —
        # this test is what surfaced that a junk zone 500'd them.
        for path in (
            "/v1/accounts",
            "/v1/dashboard",
            "/v1/reports/monthly?year=2026&month=8",
            "/v1/exchange-rates?target=PEN",
        ):
            r = await client.get(path)
            assert r.status_code == 200, f"{path}: {r.status_code} {r.text}"
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE user_settings SET display_timezone = 'UTC' WHERE user_id = $1",
                test_data.user_id,
            )
