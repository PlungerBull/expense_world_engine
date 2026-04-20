"""Daily Frankfurter fetcher for the exchange_rates table.

Runs as a Render Cron Job (or manually via `python -m app.jobs.fetch_exchange_rates`).

Storage is canonical USD-based: one row per non-USD currency per day, stored as
`(base_currency='USD', target_currency=<X>, rate = units of X per 1 USD)`. Directional
math (invert) lives in `app.helpers.exchange_rate.get_rate`, so the fetcher only
needs to insert USD→X rows here.

The target list is derived from every non-USD currency currently referenced by an
active bank account or any user's main_currency. A single Frankfurter call
(`/latest?from=USD&to=<comma-separated>`) covers all of them. Upserts are idempotent
on the `(base_currency, target_currency, rate_date)` unique constraint — safe to
re-run the job at any time.
"""
import asyncio
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

import asyncpg

from app.config import settings

FRANKFURTER_URL = "https://api.frankfurter.app/latest"
HTTP_TIMEOUT_SECONDS = 30


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


def _fetch_frankfurter(targets: list[str]) -> dict:
    """Call Frankfurter /latest?from=USD&to=<targets>.

    Response shape: {"amount": 1.0, "base": "USD", "date": "2026-04-10", "rates": {"PEN": 3.75, ...}}
    """
    params = urllib.parse.urlencode({"from": "USD", "to": ",".join(targets)})
    url = f"{FRANKFURTER_URL}?{params}"
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read())


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
    pool = await asyncpg.create_pool(settings.supabase_db_url)
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
                resp = _fetch_frankfurter(targets)
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
