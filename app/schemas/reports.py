from pydantic import BaseModel

from app.schemas.dashboard import DashboardCategory, DashboardMonth, DashboardTotals


class MonthlyReportResponse(BaseModel):
    month: DashboardMonth
    categories: list[DashboardCategory]
    totals: DashboardTotals


class MonthlyReportRangeResponse(BaseModel):
    months: list[MonthlyReportResponse]
