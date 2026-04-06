from datetime import datetime
from typing import Optional

import asyncpg


async def lookup_exchange_rate(
    conn: asyncpg.Connection,
    account_id: str,
    date: datetime,
    user_id: str,
) -> float:
    """Look up the exchange rate for an account's currency on a given date.

    Returns 1.0 when the account currency matches the user's main_currency,
    or when no rate is found in the exchange_rates table.
    """
    # Get account currency
    account = await conn.fetchrow(
        "SELECT currency_code FROM expense_bank_accounts WHERE id = $1 AND user_id = $2",
        account_id,
        user_id,
    )
    if account is None:
        return 1.0

    # Get user's main currency
    settings = await conn.fetchrow(
        "SELECT main_currency FROM user_settings WHERE user_id = $1", user_id
    )
    if settings is None:
        return 1.0

    currency_code = account["currency_code"]
    main_currency = settings["main_currency"]

    if currency_code == main_currency:
        return 1.0

    # Look up rate: exact date match first, then fallback to most recent
    target_date = date.date() if isinstance(date, datetime) else date
    rate_row = await conn.fetchrow(
        """
        SELECT rate FROM exchange_rates
        WHERE base_currency = $1 AND target_currency = $2
          AND rate_date <= $3
        ORDER BY rate_date DESC
        LIMIT 1
        """,
        currency_code,
        main_currency,
        target_date,
    )
    if rate_row is None:
        return 1.0

    return float(rate_row["rate"])
