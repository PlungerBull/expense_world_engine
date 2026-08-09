from datetime import date as date_type
from typing import Optional

import asyncpg
from fastapi import APIRouter, Query

from app import db
from app.deps import CurrentUser
from app.errors import ERROR_RESPONSES
from app.helpers.account_balance import fetch_balances, fetch_home_balances
from app.helpers.exchange_rate import rate_lookup_date
from app.helpers.monthly_report import compute_month_bounds, compute_month_flow
from app.helpers.settings import get_user_report_settings
from app.schemas.dashboard import DashboardResponse, dashboard_account_from_row

router = APIRouter(prefix="/dashboard", tags=["dashboard"], responses=ERROR_RESPONSES)


async def _load_accounts(
    conn: asyncpg.Connection,
    user_id: str,
    main_currency: str,
    today: date_type,
    is_person: bool,
    archived: bool = False,
) -> list[dict]:
    """Fetch one of three dashboard account slices.

    Balances are read for exactly this slice's accounts, in one query, after the
    rows are known. Two queries per panel, and the balance one is driven by
    ``(user_id, account_id)`` rather than reading the whole ledger.

    Every balance in the returned list is in its OWN account's currency and they
    are never added together — an account holds one immutable currency, and the
    only combined figure on this endpoint is ``current_balance_home_cents``,
    which converts each account to the home currency first.

    Slice selection:
      * ``is_person=True``                  → people (no archive filter; the
                                              People API has no archive concept yet).
      * ``is_person=False, archived=False`` → active bank accounts (the default
                                              `bank_accounts` panel).
      * ``is_person=False, archived=True``  → archived bank accounts (the
                                              `archived_accounts` panel surfaced
                                              by `?include_archived=true`).
    """
    if is_person:
        query = """
            SELECT id, name, currency_code
            FROM expense_bank_accounts
            WHERE user_id = $1
              AND deleted_at IS NULL
              AND is_person = true
            ORDER BY sort_order ASC, name ASC
        """
    else:
        archive_clause = "is_archived = true" if archived else "is_archived = false"
        query = f"""
            SELECT id, name, currency_code
            FROM expense_bank_accounts
            WHERE user_id = $1
              AND deleted_at IS NULL
              AND is_person = false
              AND {archive_clause}
            ORDER BY sort_order ASC, name ASC
        """

    rows = await conn.fetch(query, user_id)
    balances = await fetch_balances(conn, user_id, [r["id"] for r in rows])
    # main_currency/today stay caller-supplied: get_dashboard reads settings
    # and the clock once for all three slices — no per-slice midnight drift,
    # no tripled settings read.
    homes = await fetch_home_balances(
        conn,
        main_currency=main_currency,
        today=today,
        balances=balances,
        currency_by_id={str(r["id"]): r["currency_code"] for r in rows},
    )

    return [
        dashboard_account_from_row(row, balances[str(row["id"])], homes[str(row["id"])])
        for row in rows
    ]


# The `archived_categories` and `archived_hashtags` panels were here — two
# lifetime aggregators over `is_archived` rows. They are gone (sql/021 era, WP2).
#
# Not because converting them was hard, but because archiving a category was
# never a distinct feature: soft delete already hides a row from pickers while
# leaving past transactions that reference it fully intact, which is what
# archiving was for. These panels were `expense_categories.is_archived` and
# `expense_hashtags.is_archived`'s last readers, and sql/024 then removed
# both columns.
#
# `archived_accounts` stays, and the asymmetry is deliberate: an archived
# ACCOUNT still holds real money; an archived category holds only history.


@router.get("", response_model=DashboardResponse)
async def get_dashboard(
    auth_user: CurrentUser,
    include_archived: bool = Query(
        False,
        description=(
            "When true, the response includes the `archived_accounts` panel. "
            "When false (default), that field is returned as null."
        ),
    ),
):
    # No debit_as_negative here (removed 2026-08-08, bloat-audit §16): dashboard
    # aggregates are already signed by construction, and FastAPI ignores unknown
    # query params, so a caller still sending it is silently unaffected.
    async with db.pool.acquire() as conn:
        settings = await get_user_report_settings(conn, auth_user.id)
        year, month, start_utc, end_utc = compute_month_bounds(settings["display_timezone"])
        # One clock read for all three account slices — no per-slice drift.
        today = rate_lookup_date(settings["display_timezone"])

        bank_accounts = await _load_accounts(
            conn, auth_user.id, settings["main_currency"], today, is_person=False
        )
        people = await _load_accounts(
            conn, auth_user.id, settings["main_currency"], today, is_person=True
        )
        flow = await compute_month_flow(
            conn, auth_user.id, start_utc, end_utc, settings["display_timezone"]
        )

        archived_accounts: Optional[list[dict]] = None
        if include_archived:
            archived_accounts = await _load_accounts(
                conn, auth_user.id, settings["main_currency"], today,
                is_person=False, archived=True,
            )

    return DashboardResponse(
        month={"year": year, "month": month},
        bank_accounts=bank_accounts,
        people=people,
        categories=flow["categories"],
        totals=flow["totals"],
        archived_accounts=archived_accounts,
    ).model_dump(mode="json")
