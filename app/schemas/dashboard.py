from typing import Optional

from pydantic import BaseModel, Field


class DashboardMonth(BaseModel):
    year: int
    month: int


class DashboardAccount(BaseModel):
    id: str
    name: str
    currency_code: str
    current_balance_cents: int
    current_balance_home_cents: Optional[int] = Field(
        None,
        description=(
            "Account balance converted to the user's home currency. null only "
            "when no exchange rate is available from the account's currency to "
            "the home currency for today's date. Same-currency accounts are "
            "always populated (identity rate). Cross-currency accounts whose "
            "pair is missing from exchange_rates return null; clients should "
            "display the native balance as a fallback."
        ),
    )


def dashboard_account_from_row(
    row,
    balance_cents: int,
    balance_home_cents: Optional[int] = None,
) -> dict:
    """Serialize an account row for a dashboard panel.

    Exists so that exactly two functions in the engine can emit
    ``current_balance_cents`` -- this one and ``schemas.accounts.account_from_row``
    -- and neither is callable without being handed a balance. The dashboard
    previously built the field in a dict literal, which meant the required
    positional on ``account_from_row`` guarded only half the surface.

    Same contract as ``account_from_row``: ``balance_cents`` is required because
    sql/022 dropped the stored column and there is nothing on ``row`` to fall
    back to.
    """
    return DashboardAccount(
        id=str(row["id"]),
        name=row["name"],
        currency_code=row["currency_code"],
        current_balance_cents=balance_cents,
        current_balance_home_cents=balance_home_cents,
    ).model_dump(mode="json")


# Every aggregate below is home-currency and nullable, and every one is paired
# with `unconverted_count`. Both halves are load-bearing:
#
#   * Home-currency ONLY. There is no native counterpart, because grouping by
#     category has no currency partition — a category holding $15 and S/25 would
#     report 4000, a number in no currency at all.
#   * NULLABLE, with a count. A row whose date has no resolvable rate has no
#     home value, and SUM does not propagate that: it skips NULLs, and the
#     inflow/outflow shape (`CASE WHEN x > 0 ... ELSE 0`) scores a NULL row as
#     ZERO. A month where nothing could be converted would report 0 and look
#     like a month where nothing happened. `unconverted_count` is the only thing
#     that tells them apart, so a non-zero count makes the figure null rather
#     than a partial total.


class DashboardHashtagBreakdown(BaseModel):
    hashtag_ids: list[str]
    spent_home_cents: Optional[int] = None
    unconverted_count: int = 0


class DashboardCategory(BaseModel):
    id: str
    name: str
    spent_home_cents: Optional[int] = None
    unconverted_count: int = 0
    hashtag_breakdown: list[DashboardHashtagBreakdown]


class DashboardTotals(BaseModel):
    inflow_home_cents: Optional[int] = None
    outflow_home_cents: Optional[int] = None
    net_home_cents: Optional[int] = None
    unconverted_count: int = 0


class DashboardResponse(BaseModel):
    month: DashboardMonth
    bank_accounts: list[DashboardAccount]
    people: list[DashboardAccount]
    categories: list[DashboardCategory]
    totals: DashboardTotals
    # `archived_categories` and `archived_hashtags` used to sit here. Archiving a
    # category was never a distinct feature — soft delete already hides a row
    # from pickers while leaving its past transactions intact — and these panels
    # were the last readers of `is_archived` on those two tables (docs/rework/WP2,
    # then WP5). An archived ACCOUNT is different: it still holds real money,
    # which is why this one panel survives.
    archived_accounts: Optional[list[DashboardAccount]] = Field(
        None,
        description=(
            "Populated only when `?include_archived=true`; null otherwise. "
            "Same shape as `bank_accounts` — `current_balance_cents` is the "
            "lifetime balance (no new transactions can land on archived rows)."
        ),
    )
