from datetime import datetime, timezone
from typing import Optional

import asyncpg
from fastapi import APIRouter

from app import db
from app.deps import CurrentUser
from app.helpers.exchange_rate import batch_get_rates
from app.helpers.monthly_report import (
    compute_month_bounds,
    compute_month_flow,
    get_user_report_settings,
)
from app.schemas.dashboard import DashboardResponse

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


async def _load_accounts(
    conn: asyncpg.Connection,
    user_id: str,
    main_currency: str,
    is_person: bool,
) -> list[dict]:
    if is_person:
        query = """
            SELECT id, name, currency_code, current_balance_cents
            FROM expense_bank_accounts
            WHERE user_id = $1
              AND deleted_at IS NULL
              AND is_person = true
            ORDER BY sort_order ASC, name ASC
        """
    else:
        query = """
            SELECT id, name, currency_code, current_balance_cents
            FROM expense_bank_accounts
            WHERE user_id = $1
              AND deleted_at IS NULL
              AND is_person = false
              AND is_archived = false
            ORDER BY sort_order ASC, name ASC
        """

    rows = await conn.fetch(query, user_id)
    today = datetime.now(timezone.utc).date()

    # Batch rate resolution — previously this loop fired one `get_rate` call
    # per account, producing an N+1 pattern on the hottest read in the app.
    # Collecting distinct currencies once and resolving in a single batch
    # call deduplicates lookups so the DB is hit once per currency, not once
    # per account.
    currencies = {row["currency_code"] for row in rows}
    rate_by_currency = (
        await batch_get_rates(conn, currencies, main_currency, today)
        if currencies
        else {}
    )

    result: list[dict] = []
    for row in rows:
        rate = rate_by_currency.get(row["currency_code"])
        home_cents: Optional[int] = (
            round(int(row["current_balance_cents"]) * rate)
            if rate is not None
            else None
        )

        result.append(
            {
                "id": str(row["id"]),
                "name": row["name"],
                "currency_code": row["currency_code"],
                "current_balance_cents": int(row["current_balance_cents"]),
                "current_balance_home_cents": home_cents,
            }
        )

    return result


@router.get("")
async def get_dashboard(auth_user: CurrentUser):
    async with db.pool.acquire() as conn:
        settings = await get_user_report_settings(conn, auth_user.id)
        year, month, start_utc, end_utc = compute_month_bounds(settings["display_timezone"])

        bank_accounts = await _load_accounts(
            conn, auth_user.id, settings["main_currency"], is_person=False
        )
        people = await _load_accounts(
            conn, auth_user.id, settings["main_currency"], is_person=True
        )
        flow = await compute_month_flow(conn, auth_user.id, start_utc, end_utc)

    return DashboardResponse(
        month={"year": year, "month": month},
        bank_accounts=bank_accounts,
        people=people,
        categories=flow["categories"],
        totals=flow["totals"],
    ).model_dump(mode="json")
