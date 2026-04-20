from typing import Iterator, Optional

import asyncpg
from fastapi import APIRouter, Query

from app import db
from app.deps import CurrentUser
from app.errors import validation_error
from app.helpers.monthly_report import (
    compute_month_bounds,
    compute_month_flow,
    get_user_report_settings,
)
from app.schemas.reports import MonthlyReportRangeResponse, MonthlyReportResponse

router = APIRouter(prefix="/reports", tags=["reports"])

MAX_RANGE_MONTHS = 24


def _month_count(from_year: int, from_month: int, to_year: int, to_month: int) -> int:
    return (to_year - from_year) * 12 + (to_month - from_month) + 1


def _iter_months(
    from_year: int, from_month: int, to_year: int, to_month: int
) -> Iterator[tuple[int, int]]:
    year, month = from_year, from_month
    while (year, month) <= (to_year, to_month):
        yield year, month
        month += 1
        if month > 12:
            month = 1
            year += 1


async def _compute_one(
    conn: asyncpg.Connection,
    user_id: str,
    display_timezone: str,
    year: int,
    month: int,
) -> dict:
    resolved_year, resolved_month, start_utc, end_utc = compute_month_bounds(
        display_timezone, year, month
    )
    flow = await compute_month_flow(conn, user_id, start_utc, end_utc)
    return {
        "month": {"year": resolved_year, "month": resolved_month},
        "categories": flow["categories"],
        "totals": flow["totals"],
    }


@router.get("/monthly")
async def get_monthly_report(
    auth_user: CurrentUser,
    year: Optional[int] = Query(None, ge=1900, le=2100),
    month: Optional[int] = Query(None, ge=1, le=12),
    from_year: Optional[int] = Query(None, ge=1900, le=2100),
    from_month: Optional[int] = Query(None, ge=1, le=12),
    to_year: Optional[int] = Query(None, ge=1900, le=2100),
    to_month: Optional[int] = Query(None, ge=1, le=12),
    debit_as_negative: bool = Query(
        False,
        description=(
            "Accepted for API consistency with other read endpoints. Monthly "
            "report aggregates are already signed by construction (per-category "
            "spent_cents is positive for income and negative for expense; "
            "totals return split positive inflow/outflow). The flag is a no-op."
        ),
    ),
):
    # debit_as_negative is intentionally unused here — see parameter docstring.
    del debit_as_negative
    single_count = sum(v is not None for v in (year, month))
    range_count = sum(v is not None for v in (from_year, from_month, to_year, to_month))

    if single_count > 0 and range_count > 0:
        raise validation_error(
            "Pass either (year, month) or (from_year, from_month, to_year, to_month), not both.",
            {
                "year": "mutually exclusive with range form" if year is not None else None,
                "month": "mutually exclusive with range form" if month is not None else None,
            },
        )
    if single_count == 0 and range_count == 0:
        raise validation_error(
            "Missing query parameters. Use (year, month) for a single month or "
            "(from_year, from_month, to_year, to_month) for a range.",
            {
                "year": "required (single-month form)",
                "month": "required (single-month form)",
                "from_year": "required (range form)",
                "from_month": "required (range form)",
                "to_year": "required (range form)",
                "to_month": "required (range form)",
            },
        )
    if single_count == 1:
        raise validation_error(
            "Single-month form requires both year and month.",
            {
                "year": "required" if year is None else None,
                "month": "required" if month is None else None,
            },
        )
    if 0 < range_count < 4:
        missing = {
            "from_year": "required" if from_year is None else None,
            "from_month": "required" if from_month is None else None,
            "to_year": "required" if to_year is None else None,
            "to_month": "required" if to_month is None else None,
        }
        raise validation_error(
            "Range form requires all of from_year, from_month, to_year, to_month.",
            missing,
        )

    async with db.pool.acquire() as conn:
        settings = await get_user_report_settings(conn, auth_user.id)
        tz = settings["display_timezone"]

        if single_count == 2:
            payload = await _compute_one(conn, auth_user.id, tz, year, month)
            return MonthlyReportResponse(**payload).model_dump(mode="json")

        if (from_year, from_month) > (to_year, to_month):
            raise validation_error(
                "Range is inverted: from_* must be on or before to_*.",
                {"from_year": "after to_year", "to_year": "before from_year"},
            )

        count = _month_count(from_year, from_month, to_year, to_month)
        if count > MAX_RANGE_MONTHS:
            raise validation_error(
                f"Range too large: {count} months requested (max {MAX_RANGE_MONTHS}).",
                {"to_month": f"narrow the range to at most {MAX_RANGE_MONTHS} months"},
            )

        months: list[dict] = []
        for y, m in _iter_months(from_year, from_month, to_year, to_month):
            months.append(await _compute_one(conn, auth_user.id, tz, y, m))

        return MonthlyReportRangeResponse(months=months).model_dump(mode="json")
