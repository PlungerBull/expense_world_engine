from datetime import date as date_type, datetime
from typing import Optional

import asyncpg


async def get_rate(
    conn: asyncpg.Connection,
    from_currency: str,
    to_currency: str,
    as_of: date_type,
) -> Optional[tuple[float, date_type]]:
    """Return (rate, actual_rate_date) to convert `from_currency` → `to_currency` as of `as_of`.

    Exchange rates are stored canonically as USD-based rows:
      (base_currency='USD', target_currency=<X>, rate = units of X per 1 USD).

    Direction math:
      - from == to:               → (1.0, as_of)
      - from == 'USD':            → look up (USD, to), use rate as-is
      - to   == 'USD':            → look up (USD, from), invert (1 / rate)
      - cross (neither is USD):   → look up (USD, from) and (USD, to) on the same date,
                                     return (to_rate / from_rate, that_date)

    Returns None if any required rate row is missing. Callers decide the fallback.
    """
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()

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

    # Cross-rate: both legs must come from the same rate_date to stay internally consistent.
    row = await conn.fetchrow(
        """
        SELECT
            f.rate AS from_rate,
            t.rate AS to_rate,
            f.rate_date AS rate_date
        FROM exchange_rates f
        JOIN exchange_rates t
            ON t.base_currency = 'USD'
           AND t.target_currency = $2
           AND t.rate_date = f.rate_date
        WHERE f.base_currency = 'USD'
          AND f.target_currency = $1
          AND f.rate_date <= $3
        ORDER BY f.rate_date DESC
        LIMIT 1
        """,
        from_currency,
        to_currency,
        as_of,
    )
    if row is None or float(row["from_rate"]) == 0.0:
        return None
    return (float(row["to_rate"]) / float(row["from_rate"]), row["rate_date"])


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
