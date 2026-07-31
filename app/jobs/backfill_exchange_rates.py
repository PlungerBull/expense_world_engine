"""Historical backfill for the exchange_rates table.

Run manually (TODO.md "Backfill historical exchange rates"); the daily job
`app.jobs.fetch_exchange_rates` only ever inserts today's rate going forward,
so any date before the local deployment went live has no row at all.

    python -m app.jobs.backfill_exchange_rates --from 2024-03-02
    python -m app.jobs.backfill_exchange_rates --from 2025-01-01 --to 2025-12-31
    python -m app.jobs.backfill_exchange_rates --from 2025-01-01 --currencies PEN

How much history you actually need
----------------------------------
Less than it looks. `app.helpers.exchange_rate.get_rate` resolves a date with
`WHERE rate_date <= $1 ORDER BY rate_date DESC LIMIT 1` — it carries the most
recent earlier rate forward rather than demanding an exact match. So:

  * The HARD requirement is a single row on or before your earliest transaction
    date. That is the difference between a write succeeding and a write failing
    with 422 RATE_UNAVAILABLE.
  * Everything denser than that buys ACCURACY, not availability. A gap does not
    break anything; it just means the transactions inside it convert at the
    last rate before the gap.
  * Weekends and holidays therefore need no special handling. (This provider
    serves them anyway — it publishes every calendar day.)

Daily coverage across the range you actually hold data for is the right default:
one request per date, and it makes every transaction convert at its true
point-in-time rate, which is the whole reason `amount_home_cents` is cached per
row at write time (IAS 21.21 — spot rate at transaction date).

Provider floor
--------------
fawazahmed0/currency-api's dated endpoint starts at 2024-03-02. Earlier dates
404 (verified 2026-07-31, including the legacy `@1/<date>/` path), so this job
refuses them outright rather than silently leaving a hole. Data older than that
needs a different source.

Safe to re-run: dates already present are skipped before any HTTP call, and the
insert is the same `ON CONFLICT DO NOTHING` upsert the daily job uses. A run
that dies halfway costs nothing but the time already spent.
"""
import argparse
import asyncio
import json
import sys
import urllib.error
from datetime import date, timedelta
from typing import Optional

import asyncpg

from app.jobs.fetch_exchange_rates import (
    _create_pool_waiting_for_db,
    _fetch_currency_api,
    _fetch_target_currencies,
    _upsert_rate,
)

# Earliest date the provider's dated endpoint serves. See module docstring.
PROVIDER_FLOOR = date(2024, 3, 2)

# The CDN handles this comfortably; the cap is politeness, not a rate limit we
# have hit. Sequential would take ~4 minutes for a two-year range, this ~40s.
MAX_CONCURRENT_FETCHES = 6

# Per-date retries. A single dropped connection mid-run should not put a hole in
# the history and force a second pass.
FETCH_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 2

PROGRESS_EVERY = 50


async def _existing_dates(
    conn: asyncpg.Connection,
    targets: list[str],
    start: date,
    end: date,
) -> set[date]:
    """Dates in range that already hold a row for EVERY requested target.

    Partial dates are deliberately not counted as done: if PEN landed but a
    second currency did not, the date still needs another pass.
    """
    rows = await conn.fetch(
        """
        SELECT rate_date
        FROM exchange_rates
        WHERE base_currency = 'USD'
          AND target_currency = ANY($1::text[])
          AND rate_date BETWEEN $2 AND $3
        GROUP BY rate_date
        HAVING count(DISTINCT target_currency) = $4
        """,
        targets,
        start,
        end,
        len(targets),
    )
    return {row["rate_date"] for row in rows}


async def _fetch_one_date(day: date) -> dict:
    """Fetch one date's rates, retrying transient failures.

    `_fetch_currency_api` is blocking (urllib), so it runs in a worker thread —
    that is what lets the semaphore above overlap requests at all.
    """
    last_error = None
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            return await asyncio.to_thread(_fetch_currency_api, day.isoformat())
        except urllib.error.HTTPError as exc:
            # A 404 is the provider saying it has no data for this date. Retrying
            # cannot help, so fail it immediately rather than burning two more
            # attempts per missing date.
            if exc.code == 404:
                raise
            last_error = exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt < FETCH_ATTEMPTS:
            await asyncio.sleep(RETRY_WAIT_SECONDS)
    raise last_error


async def run(start: date, end: date, currencies: Optional[list[str]]) -> int:
    if start < PROVIDER_FLOOR:
        print(
            f"[backfill] refusing to start at {start}: the provider's dated endpoint "
            f"begins {PROVIDER_FLOOR}. Earlier history needs a different source.",
            file=sys.stderr,
        )
        return 2
    if start > end:
        print(f"[backfill] --from {start} is after --to {end}", file=sys.stderr)
        return 2

    pool = await _create_pool_waiting_for_db()
    if pool is None:
        print("[backfill] failed to create DB pool", file=sys.stderr)
        return 1

    inserted = 0
    skipped_existing = 0
    missing_target: list[str] = []
    gaps: list[str] = []
    failed: list[str] = []

    try:
        async with pool.acquire() as conn:
            targets = currencies or await _fetch_target_currencies(conn)
            if not targets:
                print("[backfill] no non-USD currencies in active use — nothing to do")
                return 0

            all_days = [
                start + timedelta(days=offset) for offset in range((end - start).days + 1)
            ]
            done = await _existing_dates(conn, targets, start, end)
            todo = [day for day in all_days if day not in done]
            skipped_existing = len(all_days) - len(todo)

            print(
                f"[backfill] USD -> {targets} | {start} .. {end} | "
                f"{len(all_days)} dates, {skipped_existing} already present, "
                f"{len(todo)} to fetch"
            )
            if not todo:
                return 0

            semaphore = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
            completed = 0

            async def fetch(day: date):
                """Always resolves to (day, payload|None, error|None).

                Errors are returned rather than raised so the date stays bound
                to its failure — raising out of the gather loses which date it
                was, which makes a report of 'HTTP 404' useless for follow-up.
                """
                async with semaphore:
                    try:
                        return day, await _fetch_one_date(day), None
                    except urllib.error.HTTPError as exc:
                        return day, None, f"HTTP {exc.code}"
                    except Exception as exc:  # noqa: BLE001 — reported, not swallowed
                        return day, None, f"{type(exc).__name__}: {exc}"

            # Fetches overlap, but writes happen here in completion order on the
            # single pooled connection — no concurrent use of one asyncpg
            # connection, which is not safe.
            for coro in asyncio.as_completed([fetch(day) for day in todo]):
                day, payload, error = await coro
                if error is not None:
                    # A 404 is the provider stating it has no data for that day,
                    # and re-running will never fix it. Kept separate from real
                    # failures so a permanent hole in their dataset doesn't make
                    # this job look broken forever.
                    (gaps if error == "HTTP 404" else failed).append(f"{day} ({error})")
                    continue

                rates = payload.get("rates", {})
                for target in targets:
                    if target not in rates:
                        missing_target.append(f"{day} {target}")
                        continue
                    if await _upsert_rate(conn, target, day, float(rates[target])):
                        inserted += 1

                completed += 1
                if completed % PROGRESS_EVERY == 0:
                    print(f"[backfill] {completed}/{len(todo)} dates fetched")
    finally:
        await pool.close()

    print(
        f"[backfill] done: inserted={inserted} already_present={skipped_existing} "
        f"provider_gaps={len(gaps)} missing_target={len(missing_target)} failed={len(failed)}"
    )
    if gaps:
        # Not an error: get_rate carries the previous day's rate forward, so a
        # transaction on a gap date still converts — just at the last published
        # rate before it. Printed in full so the dates are on the record.
        print(
            f"[backfill] provider has no data for {len(gaps)} date(s); "
            f"get_rate carries the previous rate forward for these: {gaps}"
        )
    if missing_target:
        print(f"[backfill] target absent from payload: {missing_target[:10]}", file=sys.stderr)
    if failed:
        print(f"[backfill] retryable failures — re-run to fill: {failed[:10]}", file=sys.stderr)
    return 2 if (failed or missing_target) else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--from", dest="start", required=True, help="first date, YYYY-MM-DD (inclusive)"
    )
    parser.add_argument(
        "--to", dest="end", default=None, help="last date, YYYY-MM-DD (inclusive; default today)"
    )
    parser.add_argument(
        "--currencies",
        default=None,
        help="comma-separated targets (default: every non-USD currency in active use)",
    )
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else date.today()
    currencies = (
        [c.strip().upper() for c in args.currencies.split(",") if c.strip()]
        if args.currencies
        else None
    )
    sys.exit(asyncio.run(run(start, end, currencies)))


if __name__ == "__main__":
    main()
