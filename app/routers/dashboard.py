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
    """Fetch one of four dashboard account slices.

    Balances are read for exactly this slice's accounts, in one query, after the
    rows are known. Two queries per panel, and the balance one is driven by
    ``(user_id, account_id)`` rather than reading the whole ledger.

    Every balance in the returned list is in its OWN account's currency and they
    are never added together — an account holds one immutable currency, and the
    only combined figure on this endpoint is ``current_balance_home_cents``,
    which converts each account to the home currency first.

    The two flags are a plain 2x2 — ``is_person`` picks the collection,
    ``archived`` picks the state, and every combination is a real panel:

      * ``is_person=False, archived=False`` → active bank accounts (`bank_accounts`)
      * ``is_person=True,  archived=False`` → active people (`people`)
      * ``is_person=False, archived=True``  → archived bank accounts
      * ``is_person=True,  archived=True``  → archived people (`archived_people`)

    The last two are surfaced only by ``?include_archived=true``. They stay two
    panels rather than one merged list because people and bank accounts are
    separate collections on every other surface; merging them only here would
    file an archived person among archived cards.

    ⚠️ The person slice ignored ``archived`` entirely until 2026-08-14, which
    left a real split: ``GET /accounts?include_people=true`` filtered archived
    rows out while this panel did not, so an archived person would have
    vanished from one surface and persisted on the other. Never observed —
    no person could exist before ``POST /people`` shipped in the same change.

    Note what is NOT filtered here: a zero balance. A settled person reports
    ``0`` and stays in the list — ``0`` is a balance, not a missing value, and
    it is the only thing distinguishing a cleared debt from an unrecorded one.
    Collapsing settled rows is a client display choice (owner decision,
    2026-08-13).
    """
    query = """
        SELECT id, name, currency_code
        FROM expense_bank_accounts
        WHERE user_id = $1
          AND deleted_at IS NULL
          AND is_person = $2
          AND is_archived = $3
        ORDER BY sort_order ASC, name ASC
    """

    rows = await conn.fetch(query, user_id, is_person, archived)
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
            "When true, the response includes the `archived_accounts` and "
            "`archived_people` panels. When false (default), both fields are "
            "returned as null."
        ),
    ),
):
    # No debit_as_negative here (removed 2026-08-08, bloat-audit §16): dashboard
    # aggregates are already signed by construction, and FastAPI ignores unknown
    # query params, so a caller still sending it is silently unaffected.
    async with db.pool.acquire() as conn:
        settings = await get_user_report_settings(conn, auth_user.id)
        year, month, start_utc, end_utc = compute_month_bounds(settings["display_timezone"])
        # One clock read for every account slice — no per-slice drift.
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

        # Both archived panels are gated on the one flag and stay `None`
        # otherwise — null-over-omission, so the keys are always on the wire.
        archived_accounts: Optional[list[dict]] = None
        archived_people: Optional[list[dict]] = None
        if include_archived:
            archived_accounts = await _load_accounts(
                conn, auth_user.id, settings["main_currency"], today,
                is_person=False, archived=True,
            )
            archived_people = await _load_accounts(
                conn, auth_user.id, settings["main_currency"], today,
                is_person=True, archived=True,
            )

    return DashboardResponse(
        month={"year": year, "month": month},
        bank_accounts=bank_accounts,
        people=people,
        categories=flow["categories"],
        totals=flow["totals"],
        archived_accounts=archived_accounts,
        archived_people=archived_people,
    ).model_dump(mode="json")
