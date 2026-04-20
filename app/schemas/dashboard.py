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


class DashboardHashtagBreakdown(BaseModel):
    hashtag_ids: list[str]
    spent_cents: int
    spent_home_cents: int


class DashboardCategory(BaseModel):
    id: str
    name: str
    spent_cents: int
    spent_home_cents: int
    hashtag_breakdown: list[DashboardHashtagBreakdown]


class DashboardTotals(BaseModel):
    inflow_cents: int
    inflow_home_cents: int
    outflow_cents: int
    outflow_home_cents: int
    net_cents: int
    net_home_cents: int


class DashboardResponse(BaseModel):
    month: DashboardMonth
    bank_accounts: list[DashboardAccount]
    people: list[DashboardAccount]
    categories: list[DashboardCategory]
    totals: DashboardTotals
