"""Daily exchange-rate fetcher for the exchange_rates table.

Runs as a scheduled job (or manually via `python -m app.jobs.fetch_exchange_rates`).

Provider: fawazahmed0/currency-api (keyless, CDN-hosted, ~200 currencies incl. PEN,
and date-versioned endpoints for historical backfill). Frankfurter, the original
provider, was dropped 2026-07-30: it serves ECB reference rates only, and the ECB
list does not include PEN — `to=PEN` can never resolve there.

Storage is canonical USD-based: one row per non-USD currency per day, stored as
`(base_currency='USD', target_currency=<X>, rate = units of X per 1 USD)`. Directional
math (invert) lives in `app.helpers.exchange_rate.get_rate`, so the fetcher only
needs to insert USD→X rows here.

The target list is derived from every non-USD currency currently referenced by an
active bank account or any user's main_currency. A single call (`@latest` returns all
currencies against USD) covers all of them. Upserts are idempotent on the
`(base_currency, target_currency, rate_date)` unique constraint — safe to re-run the
job at any time.

Provider rates are not trusted on sight: `_upsert_rate` refuses a non-positive
rate and one that moves more than `MAX_RATE_MOVE_FRACTION` from the previous
known rate, counting the target as a failure instead of storing it. A refused
date simply has no row, which reads carry forward across — see the constants for
why refusing is the safe arm. Note that the upsert is `ON CONFLICT DO NOTHING`
and this job fires every 6h, so the FIRST rate accepted for a day is the one that
stands: later runs cannot correct or overwrite it. That is what makes the guard
worth having at write time rather than as an after-the-fact report.
"""
import asyncio
import json
import sys
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Optional

import asyncpg

from app.config import settings
from app.constants import BASE_CURRENCY

CURRENCY_API_URL = (
    "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{version}/v1/currencies/usd.min.json"
)
HTTP_TIMEOUT_SECONDS = 30

# Wait-for-postgres budget, matched to deploy/local/backup.sh's `pg_isready`
# loop: 30 tries, 2s apart. Far beyond a normal local start.
DB_CONNECT_TRIES = 30
DB_CONNECT_WAIT_SECONDS = 2

# Plausibility band. A provider rate that moves further than this from the
# previous known rate is refused rather than stored — owner decision
# 2026-08-13, taking the "refuse" arm over "write it and flag it".
#
# Why refusing is the safe arm: a date with no row is not a hole. `get_rate`
# and `home_rate_join` both resolve `rate_date <= <date>` and carry the most
# recent earlier rate forward, so reads simply price at the last good rate —
# roughly right, and self-healing the moment a sane rate lands. Storing a bad
# one instead misprices every report touching that date until someone notices.
#
# Why 10%: real USD/PEN day-to-day movement is well under 1%, so this never
# touches legitimate data. What it catches is the failure that actually happens
# to currency feeds — a misplaced decimal point (33.373 for 3.3373), a zero, a
# garbage value. Since sql/021 this table is the only source of every
# home-currency figure, so one bad row misprices reports rather than one write.
MAX_RATE_MOVE_FRACTION = 0.10

# How far back a baseline may be drawn from. The band above asks "did this move
# more than 10% in about a day"; across a long gap that question has no answer —
# USD/PEN has legitimately ranged ~3.2-4.1 over recent years, so comparing
# across one would refuse real history mid-backfill. Beyond this window there is
# no usable baseline and the rate is accepted unchecked. The daily job is
# unaffected either way: it always has yesterday.
MAX_BASELINE_GAP_DAYS = 7


async def _create_pool_waiting_for_db() -> Optional[asyncpg.Pool]:
    """Create the pool, retrying while postgres is still coming up.

    Under the local profile this job runs from launchd with `RunAtLoad`, which
    starts it in parallel with Homebrew's postgres service — launchd has no
    notion of one agent depending on another, so at login the first connect can
    land before the socket exists. The engine survives that because `KeepAlive`
    makes launchd relaunch it until postgres answers; this job has no such
    safety net, so an unhandled ConnectionRefusedError meant exit 1 and no rate
    until the next 6-hourly fire — and every cross-currency write 422s in the
    meantime. Wait for postgres instead of racing it.

    Only connect-time transients are retried: refused/unreachable socket
    (OSError) and "the database system is starting up" (CannotConnectNowError,
    the window where postgres listens but is still in recovery). A real fault —
    missing database, bad credentials — still fails on the first try.
    """
    for attempt in range(1, DB_CONNECT_TRIES + 1):
        try:
            return await asyncpg.create_pool(settings.supabase_db_url)
        except (OSError, asyncpg.exceptions.CannotConnectNowError) as exc:
            if attempt == DB_CONNECT_TRIES:
                print(
                    f"[fetch_exchange_rates] postgres not up after "
                    f"{DB_CONNECT_TRIES * DB_CONNECT_WAIT_SECONDS}s: {exc}",
                    file=sys.stderr,
                )
                return None
            if attempt == 1:
                print("[fetch_exchange_rates] postgres not up yet — waiting for it")
            await asyncio.sleep(DB_CONNECT_WAIT_SECONDS)
    return None


@asynccontextmanager
async def _job_conn(prefix: str):
    """Pool lifecycle both jobs share: wait for postgres, acquire one
    connection, close the pool on the way out (including early returns
    inside the ``with``). Yields ``None`` when postgres never came up —
    the caller prints nothing more and exits 1; the wait itself already
    logged the failure with the job's own prefix.
    """
    pool = await _create_pool_waiting_for_db()
    if pool is None:
        print(f"[{prefix}] failed to create DB pool", file=sys.stderr)
        yield None
        return
    try:
        async with pool.acquire() as conn:
            yield conn
    finally:
        await pool.close()


async def _fetch_target_currencies(conn: asyncpg.Connection) -> list[str]:
    """Return every distinct non-USD currency currently referenced by the system."""
    rows = await conn.fetch(
        f"""
        SELECT DISTINCT currency_code AS code
        FROM expense_bank_accounts
        WHERE deleted_at IS NULL
          AND is_archived = false
          AND currency_code IS NOT NULL
          AND currency_code <> '{BASE_CURRENCY}'
        UNION
        SELECT DISTINCT main_currency AS code
        FROM user_settings
        WHERE main_currency IS NOT NULL
          AND main_currency <> '{BASE_CURRENCY}'
        """
    )
    return sorted({row["code"] for row in rows})


def _fetch_currency_api(version: str = "latest") -> dict:
    """Call currency-api for all USD rates; `version` is 'latest' or 'YYYY-MM-DD'.

    Raw shape: {"date": "2026-07-30", "usd": {"pen": 3.39, "eur": 0.87, ...}} (codes
    lowercase). Normalized here to the shape the caller consumes — {"date": ...,
    "rates": {"PEN": 3.39, ...}} — so the provider swap stays inside this function.
    """
    url = CURRENCY_API_URL.format(version=version)
    req = urllib.request.Request(url, headers={"User-Agent": "expense-world-engine/1.0"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        raw = json.loads(resp.read())
    return {
        "date": raw["date"],
        "rates": {code.upper(): rate for code, rate in raw.get("usd", {}).items()},
    }


async def _baseline_rate(
    conn: asyncpg.Connection,
    target: str,
    rate_date: date,
) -> Optional[tuple[float, date]]:
    """Most recent stored rate strictly before ``rate_date``, or None.

    Deliberately not ``helpers.exchange_rate.get_rate``, which answers a
    different question in all three respects that matter here: it resolves
    ``rate_date <= as_of`` (a row for the same day would answer itself, making
    a re-run compare a rate against itself), it caches for an hour (a baseline
    must read what is in the table *now*), and it carries forward without limit
    (see ``MAX_BASELINE_GAP_DAYS``).
    """
    row = await conn.fetchrow(
        f"""
        SELECT rate, rate_date FROM exchange_rates
        WHERE base_currency = '{BASE_CURRENCY}' AND target_currency = $1
          AND rate_date < $2
          AND rate_date >= $3
        ORDER BY rate_date DESC
        LIMIT 1
        """,
        target,
        rate_date,
        rate_date - timedelta(days=MAX_BASELINE_GAP_DAYS),
    )
    if row is None:
        return None
    return (float(row["rate"]), row["rate_date"])


async def _upsert_rate(
    conn: asyncpg.Connection,
    target: str,
    rate_date: date,
    rate: float,
) -> bool:
    """Insert one provider rate; True if a row was written.

    Raises ValueError rather than inserting when the rate is non-positive or
    implausible — since sql/021 this table is the only source of every
    home-currency figure, so one bad provider row misprices reports, not one
    write. Both guards live here so no caller (daily fetch, backfill) can skip
    them; the exchange_rates_rate_positive CHECK (sql/027) backstops the first
    and there is deliberately no SQL backstop for the second (plausibility is a
    judgment against neighbouring data, not a property of the row).

    Callers catch this and count the target into the run's failures — recording
    the rest of the day's rates is never blocked by one bad one, and a refused
    date is left with no row at all, which reads carry forward across.

    Ordering note: positivity is checked first, so a zero or negative rate never
    reaches the ratio below (which would divide by, or compare against, junk).
    """
    if rate <= 0:
        raise ValueError(f"non-positive rate for USD->{target} on {rate_date}: {rate}")

    # A first-ever rate, or one after a gap wider than the baseline window, has
    # nothing to be judged against and is accepted. Backfill note: it writes in
    # fetch-completion order, not date order, so which side of a neighbouring
    # pair gets checked is not deterministic — a bad rate landing first can push
    # its good neighbour into `failed` instead of itself. Both dates are named
    # in the run's stderr either way, which is what follow-up needs.
    baseline = await _baseline_rate(conn, target, rate_date)
    if baseline is not None:
        baseline_rate, baseline_date = baseline
        move = abs(rate - baseline_rate) / baseline_rate
        if move > MAX_RATE_MOVE_FRACTION:
            raise ValueError(
                f"implausible rate for USD->{target} on {rate_date}: {rate} moves "
                f"{move:.1%} from {baseline_rate} on {baseline_date} "
                f"(limit {MAX_RATE_MOVE_FRACTION:.0%}) — refused, "
                f"reads carry {baseline_date} forward"
            )
    row = await conn.fetchrow(
        f"""
        INSERT INTO exchange_rates (base_currency, target_currency, rate_date, rate)
        VALUES ('{BASE_CURRENCY}', $1, $2, $3)
        ON CONFLICT (base_currency, target_currency, rate_date) DO NOTHING
        RETURNING id
        """,
        target,
        rate_date,
        rate,
    )
    return row is not None


async def _apply_rates(
    conn: asyncpg.Connection,
    targets: list[str],
    rates: dict,
    rate_date: date,
) -> dict:
    """Upsert one date's rates for every target; returns the structured tally
    ``{"inserted": [target...], "skipped": [target...], "missing": [target...],
    "failed": [(target, reason)...]}``.

    The tally is lists, not counts, because the two callers label failures
    differently — the daily fetch reports bare targets, the backfill prefixes
    each with its date — and a helper that printed would flatten that
    (bloat-audit §18). ``skipped`` means already-present (``ON CONFLICT DO
    NOTHING`` hit); ``failed`` carries ``_upsert_rate``'s refusals — both the
    non-positive and the implausible kind — and never aborts the rest of the
    targets.
    """
    tally: dict = {"inserted": [], "skipped": [], "missing": [], "failed": []}
    for target in targets:
        if target not in rates:
            tally["missing"].append(target)
            continue
        try:
            did_insert = await _upsert_rate(conn, target, rate_date, float(rates[target]))
        except ValueError as exc:
            tally["failed"].append((target, str(exc)))
            continue
        tally["inserted" if did_insert else "skipped"].append(target)
    return tally


async def run() -> int:
    async with _job_conn("fetch_exchange_rates") as conn:
        if conn is None:
            return 1

        targets = await _fetch_target_currencies(conn)
        if not targets:
            print("[fetch_exchange_rates] no non-USD currencies in active use — nothing to do")
            return 0

        print(f"[fetch_exchange_rates] fetching USD -> {targets}")

        try:
            # to_thread, matching the backfill: _fetch_currency_api blocks on
            # urllib and has no business pinning the event loop even when, as
            # here, nothing else is scheduled on it.
            resp = await asyncio.to_thread(_fetch_currency_api)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            print(f"[fetch_exchange_rates] HTTP error: {exc}", file=sys.stderr)
            return 2
        except json.JSONDecodeError as exc:
            print(f"[fetch_exchange_rates] invalid JSON: {exc}", file=sys.stderr)
            return 2

        try:
            rate_date = datetime.strptime(resp["date"], "%Y-%m-%d").date()
            rates = resp["rates"]
        except (KeyError, ValueError) as exc:
            print(
                f"[fetch_exchange_rates] malformed response: {exc} (payload={resp})",
                file=sys.stderr,
            )
            return 2

        tally = await _apply_rates(conn, targets, rates, rate_date)

    for target in tally["missing"]:
        print(f"[fetch_exchange_rates] missing target {target} in response", file=sys.stderr)
    for _target, reason in tally["failed"]:
        print(f"[fetch_exchange_rates] {reason}", file=sys.stderr)
    for target in tally["inserted"]:
        print(f"[fetch_exchange_rates] inserted USD->{target} {rate_date} = {rates[target]}")

    failed = tally["missing"] + [target for target, _reason in tally["failed"]]
    print(
        f"[fetch_exchange_rates] done: inserted={len(tally['inserted'])} "
        f"skipped={len(tally['skipped'])} failed={len(failed)}"
    )
    if failed:
        print(f"[fetch_exchange_rates] failed targets: {', '.join(failed)}", file=sys.stderr)
        return 2
    return 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
