from datetime import datetime, timezone
from typing import Optional

import asyncpg
from fastapi import APIRouter

from app import db
from app.deps import CurrentUser
from app.errors import AppError
from app.helpers.exchange_rate import get_rate
from app.helpers.monthly_report import compute_month_bounds, compute_month_flow
from app.schemas.dashboard import DashboardResponse

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


async def _get_user_settings(conn: asyncpg.Connection, user_id: str) -> dict:
    row = await conn.fetchrow(
        "SELECT main_currency, display_timezone FROM user_settings WHERE user_id = $1",
        user_id,
    )
    if row is None:
        raise AppError(409, "SETTINGS_MISSING", "User settings not found. Call /auth/bootstrap first.")
    return {"main_currency": row["main_currency"], "display_timezone": row["display_timezone"]}


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

    result: list[dict] = []
    for row in rows:
        home_cents: Optional[int]
        rate_lookup = await get_rate(
            conn,
            from_currency=row["currency_code"],
            to_currency=main_currency,
            as_of=today,
        )
        if rate_lookup is None:
            home_cents = None
        else:
            home_cents = round(int(row["current_balance_cents"]) * rate_lookup[0])

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
        settings = await _get_user_settings(conn, auth_user.id)
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
