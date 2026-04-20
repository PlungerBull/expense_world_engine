"""Exchange rate lookups with an in-process TTL cache.

The cache is per-worker (no Redis). At 10K concurrent users this is
acceptable because rates change at most once per day, so a 1-hour TTL
means each worker hits the DB at most once per hour per distinct
(from, to, date) tuple. If you scale horizontally across N Render
dynos, each dyno maintains its own cache; worst case is N DB hits
per hour per rate instead of one.

Negative results (no rate available) are also cached — otherwise a
missing rate would be re-queried on every call. Eviction is lazy:
expired entries are deleted on next access.

Cache sizing: for realistic workloads (users typically have 1-3
currencies and care about recent dates), the cache stays well under
a few thousand entries (<1MB). No LRU cap is enforced today. If
the cache grows beyond ~10K entries in practice, an LRU cap should
be considered.
"""
from datetime import date as date_type, datetime
import time
from typing import Optional

import asyncpg


_RATE_CACHE_TTL_SECONDS = 3600  # 1 hour

_RateResult = Optional[tuple[float, date_type]]
_RATE_CACHE: dict[tuple[str, str, date_type], tuple[_RateResult, float]] = {}


def clear_rate_cache() -> None:
    """Empty the in-process rate cache. Test helper."""
    _RATE_CACHE.clear()


async def _fetch_rate_from_db(
    conn: asyncpg.Connection,
    from_currency: str,
    to_currency: str,
    as_of: date_type,
) -> _RateResult:
    """Execute the actual SQL rate lookup with no caching.

    Called by ``get_rate`` on cache miss. Input currencies are already
    upper-cased by the caller.

    Exchange rates are stored canonically as USD-based rows:
      (base_currency='USD', target_currency=<X>, rate = units of X per 1 USD).

    Direction math:
      - from == to:               → (1.0, as_of)
      - from == 'USD':            → look up (USD, to), use rate as-is
      - to   == 'USD':            → look up (USD, from), invert (1 / rate)
      - cross (neither is USD):   → unsupported under the Phase 1 PEN/USD-only
                                     policy (sql/015); returns None.

    Returns None if any required rate row is missing. Callers decide the fallback.
    """
    if from_currency == to_currency:
        return (1.0, as_of)

    if from_currency == "USD":
        row = await conn.fetchrow(
            """
            SELECT rate, rate_date FROM exchange_rates
            WHERE base_currency = 'USD' AND target_currency = $1
              AND rate_date <= $2
            ORDER BY rate_date DESC
            LIMIT 1
            """,
            to_currency,
            as_of,
        )
        if row is None:
            return None
        return (float(row["rate"]), row["rate_date"])

    if to_currency == "USD":
        row = await conn.fetchrow(
            """
            SELECT rate, rate_date FROM exchange_rates
            WHERE base_currency = 'USD' AND target_currency = $1
              AND rate_date <= $2
            ORDER BY rate_date DESC
            LIMIT 1
            """,
            from_currency,
            as_of,
        )
        if row is None or float(row["rate"]) == 0.0:
            return None
        return (1.0 / float(row["rate"]), row["rate_date"])

    # Cross-rate (neither side USD) is unsupported. Phase 1 only accepts PEN
    # and USD as currencies (sql/015 CHECK + the global_currencies FKs), so
    # this branch is unreachable for valid data. Return None explicitly so
    # the negative cache stores the result and callers fall back consistently
    # rather than leaning on the previous JOIN's silent-no-match behaviour.
    return None


async def get_rate(
    conn: asyncpg.Connection,
    from_currency: str,
    to_currency: str,
    as_of: date_type,
) -> _RateResult:
    """Return (rate, actual_rate_date) to convert `from_currency` → `to_currency` as of `as_of`.

    Cached: see module docstring. Callers do not need to think about
    the cache — it's transparent, per-worker, and self-evicting. Both
    hits and negative results (None) are cached for the same TTL.

    Returns None if any required rate row is missing. Callers decide
    the fallback.
    """
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()

    cache_key = (from_currency, to_currency, as_of)
    now = time.monotonic()

    cached = _RATE_CACHE.get(cache_key)
    if cached is not None:
        value, expires_at = cached
        if now < expires_at:
            return value
        # Expired — drop and fall through to the real lookup.
        del _RATE_CACHE[cache_key]

    result = await _fetch_rate_from_db(conn, from_currency, to_currency, as_of)
    _RATE_CACHE[cache_key] = (result, now + _RATE_CACHE_TTL_SECONDS)
    return result


async def batch_get_rates(
    conn: asyncpg.Connection,
    from_currencies: set[str],
    to_currency: str,
    as_of: date_type,
) -> dict[str, float]:
    """Resolve exchange rates for multiple source currencies to one target.

    Returns a ``{from_currency: rate}`` mapping. Currencies with no available
    rate are simply absent from the result (callers should treat them as None).

    This helper is what callers should use when they need rates for several
    accounts/rows at once — it deduplicates lookups so the DB is hit once per
    *distinct* currency rather than once per row. Callers that still loop and
    call ``get_rate`` row-by-row will hit an N+1 pattern.

    Implementation note: this currently calls ``get_rate`` once per distinct
    currency rather than issuing a single combined SQL query. That still
    eliminates the N+1 at the caller level (which was the hot path), and
    preserves ``get_rate``'s conversion paths (same-currency and USD-involving;
    cross-rate is intentionally unsupported under the PEN/USD-only policy).
    A true single-query version is possible but requires rewriting the SQL.
    """
    result: dict[str, float] = {}
    for currency in set(from_currencies):
        lookup = await get_rate(conn, currency, to_currency, as_of)
        if lookup is not None:
            result[currency] = lookup[0]
    return result


async def lookup_exchange_rate(
    conn: asyncpg.Connection,
    account_id: str,
    date: datetime,
    user_id: str,
) -> float:
    """Resolve the account's currency and the user's main currency, then look up the rate.

    Backwards-compatible wrapper around `get_rate`. Returns 1.0 when the account or
    user_settings can't be found, or when no rate is available — same fallback
    behaviour the rest of the engine relies on today.
    """
    account = await conn.fetchrow(
        "SELECT currency_code FROM expense_bank_accounts WHERE id = $1 AND user_id = $2",
        account_id,
        user_id,
    )
    if account is None:
        return 1.0

    settings = await conn.fetchrow(
        "SELECT main_currency FROM user_settings WHERE user_id = $1", user_id
    )
    if settings is None:
        return 1.0

    target_date = date.date() if isinstance(date, datetime) else date
    result = await get_rate(
        conn,
        from_currency=account["currency_code"],
        to_currency=settings["main_currency"],
        as_of=target_date,
    )
    if result is None:
        return 1.0
    return result[0]
