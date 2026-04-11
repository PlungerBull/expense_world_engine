from typing import Optional

from pydantic import BaseModel


class DashboardMonth(BaseModel):
    year: int
    month: int


class DashboardAccount(BaseModel):
    id: str
    name: str
    currency_code: str
    current_balance_cents: int
    current_balance_home_cents: Optional[int] = None


class DashboardHashtagBreakdown(BaseModel):
    hashtag_combination: list[str]
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
