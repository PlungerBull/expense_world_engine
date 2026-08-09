"""The one read of ``user_settings`` every home-converting surface shares.

Lived in ``helpers/monthly_report.py`` until the bloat-audit §15
consolidation (2026-08-08): balances convert to home currency too, and a
balance module importing a *report* module for its settings read had the
layering backwards. Reports, dashboard, and balances now share this neutral
home.
"""

import asyncpg

from app.constants import HOME_CURRENCY
from app.errors import settings_missing


async def get_user_report_settings(
    conn: asyncpg.Connection,
    user_id: str,
) -> dict:
    """Load main_currency + display_timezone for a user, or 422 if they haven't bootstrapped.

    Also asserts that the user's ``main_currency`` is the currency
    ``helpers/home_currency.py`` interpolates into its SQL as a literal. That
    module cannot bind the value — its fragments are spliced into queries with
    differing ``$N`` numbering — and interpolation is only safe because sql/018
    locks ``main_currency`` to ``'PEN'``. The obligation to check is stated in
    that module's docstring; this is the chokepoint for it, because every query
    that converts reaches SQL through a caller of this function.

    Before WP2 the assertion lived in helpers/transfers.py, which was the only
    place holding both values at once. Conversion has moved to the read path, so
    the check moved with it.
    """
    row = await conn.fetchrow(
        "SELECT main_currency, display_timezone FROM user_settings WHERE user_id = $1",
        user_id,
    )
    if row is None:
        raise settings_missing()
    if row["main_currency"] != HOME_CURRENCY:
        raise RuntimeError(
            f"user_settings.main_currency is {row['main_currency']!r} but the "
            f"engine converts to {HOME_CURRENCY!r} (app.constants.HOME_CURRENCY). "
            "sql/018 is supposed to make this unreachable; if that CHECK was "
            "lifted, helpers/home_currency.py must be revisited at the same time."
        )
    return {"main_currency": row["main_currency"], "display_timezone": row["display_timezone"]}
