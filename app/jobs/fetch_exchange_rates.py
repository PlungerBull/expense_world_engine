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
"""
import asyncio
import json
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from typing import Optional

import asyncpg

from app.config import settings

CURRENCY_API_URL = (
    "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{version}/v1/currencies/usd.min.json"
)
HTTP_TIMEOUT_SECONDS = 30

# Wait-for-postgres budget, matched to deploy/local/backup.sh's `pg_isready`
# loop: 30 tries, 2s apart. Far beyond a normal local start.
DB_CONNECT_TRIES = 30
DB_CONNECT_WAIT_SECONDS = 2


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


async def _fetch_target_currencies(conn: asyncpg.Connection) -> list[str]:
    """Return every distinct non-USD currency currently referenced by the system."""
    rows = await conn.fetch(
        """
        SELECT DISTINCT currency_code AS code
        FROM expense_bank_accounts
        WHERE deleted_at IS NULL
          AND is_archived = false
          AND currency_code IS NOT NULL
          AND currency_code <> 'USD'
        UNION
        SELECT DISTINCT main_currency AS code
        FROM user_settings
        WHERE main_currency IS NOT NULL
          AND main_currency <> 'USD'
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


async def _upsert_rate(
    conn: asyncpg.Connection,
    target: str,
    rate_date: date,
    rate: float,
) -> bool:
    row = await conn.fetchrow(
        """
        INSERT INTO exchange_rates (base_currency, target_currency, rate_date, rate)
        VALUES ('USD', $1, $2, $3)
        ON CONFLICT (base_currency, target_currency, rate_date) DO NOTHING
        RETURNING id
        """,
        target,
        rate_date,
        rate,
    )
    return row is not None


async def run() -> int:
    pool = await _create_pool_waiting_for_db()
    if pool is None:
        print("[fetch_exchange_rates] failed to create DB pool", file=sys.stderr)
        return 1

    inserted = 0
    skipped = 0
    failed: list[str] = []

    try:
        async with pool.acquire() as conn:
            targets = await _fetch_target_currencies(conn)
            if not targets:
                print("[fetch_exchange_rates] no non-USD currencies in active use — nothing to do")
                return 0

            print(f"[fetch_exchange_rates] fetching USD -> {targets}")

            try:
                resp = _fetch_currency_api()
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

            for target in targets:
                if target not in rates:
                    print(
                        f"[fetch_exchange_rates] missing target {target} in response",
                        file=sys.stderr,
                    )
                    failed.append(target)
                    continue

                did_insert = await _upsert_rate(conn, target, rate_date, float(rates[target]))
                if did_insert:
                    inserted += 1
                    print(f"[fetch_exchange_rates] inserted USD->{target} {rate_date} = {rates[target]}")
                else:
                    skipped += 1
    finally:
        await pool.close()

    print(
        f"[fetch_exchange_rates] done: inserted={inserted} skipped={skipped} failed={len(failed)}"
    )
    if failed:
        print(f"[fetch_exchange_rates] failed targets: {', '.join(failed)}", file=sys.stderr)
        return 2
    return 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
