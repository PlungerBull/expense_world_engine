from datetime import datetime, timezone
from typing import Optional

import asyncpg
from fastapi import APIRouter, Query

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
    archived: bool = False,
) -> list[dict]:
    """Fetch one of three dashboard account slices.

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
            SELECT id, name, currency_code, current_balance_cents
            FROM expense_bank_accounts
            WHERE user_id = $1
              AND deleted_at IS NULL
              AND is_person = true
            ORDER BY sort_order ASC, name ASC
        """
    else:
        archive_clause = "is_archived = true" if archived else "is_archived = false"
        query = f"""
            SELECT id, name, currency_code, current_balance_cents
            FROM expense_bank_accounts
            WHERE user_id = $1
              AND deleted_at IS NULL
              AND is_person = false
              AND {archive_clause}
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


# Signed-amount expressions reused by the lifetime aggregators below.
# Mirrors the convention in helpers/monthly_report.compute_month_flow:
# expenses and transfer debits are negative, income and transfer credits
# are positive. Kept here as module-level constants so the categories and
# hashtags queries stay textually identical (drift here is a real bug
# class — the two aggregates would disagree on what a transfer means).
_SIGNED_CENTS_SQL = """
    CASE
        WHEN t.transaction_type = 2 THEN  t.amount_cents
        WHEN t.transaction_type = 3 AND t.transfer_direction = 2 THEN  t.amount_cents
        WHEN t.transaction_type = 1 THEN -t.amount_cents
        WHEN t.transaction_type = 3 AND t.transfer_direction = 1 THEN -t.amount_cents
        ELSE 0
    END
"""

_SIGNED_HOME_CENTS_SQL = """
    CASE
        WHEN t.transaction_type = 2 THEN  COALESCE(t.amount_home_cents, t.amount_cents)
        WHEN t.transaction_type = 3 AND t.transfer_direction = 2 THEN  COALESCE(t.amount_home_cents, t.amount_cents)
        WHEN t.transaction_type = 1 THEN -COALESCE(t.amount_home_cents, t.amount_cents)
        WHEN t.transaction_type = 3 AND t.transfer_direction = 1 THEN -COALESCE(t.amount_home_cents, t.amount_cents)
        ELSE 0
    END
"""


async def _load_archived_categories(
    conn: asyncpg.Connection,
    user_id: str,
) -> list[dict]:
    """Lifetime signed flow per archived (non-deleted) category.

    Categories with no transactions ever appear with zero totals — the
    LEFT JOIN preserves them. Sort matches the active categories panel
    so the client can render both lists identically.
    """
    rows = await conn.fetch(
        f"""
        SELECT
            c.id,
            c.name,
            COALESCE(SUM({_SIGNED_CENTS_SQL}), 0)::bigint      AS lifetime_spent_cents,
            COALESCE(SUM({_SIGNED_HOME_CENTS_SQL}), 0)::bigint AS lifetime_spent_home_cents
        FROM expense_categories c
        LEFT JOIN expense_transactions t
               ON t.category_id = c.id
              AND t.user_id     = c.user_id
              AND t.deleted_at IS NULL
        WHERE c.user_id     = $1
          AND c.is_archived = true
          AND c.deleted_at IS NULL
        GROUP BY c.id, c.name, c.sort_order
        ORDER BY c.sort_order ASC, c.name ASC
        """,
        user_id,
    )
    return [
        {
            "id": str(r["id"]),
            "name": r["name"],
            "lifetime_spent_cents": int(r["lifetime_spent_cents"]),
            "lifetime_spent_home_cents": int(r["lifetime_spent_home_cents"]),
        }
        for r in rows
    ]


async def _load_archived_hashtags(
    conn: asyncpg.Connection,
    user_id: str,
) -> list[dict]:
    """Lifetime signed flow per archived (non-deleted) hashtag.

    Joins through `expense_transaction_hashtags` (ledger-side rows only,
    `transaction_source = 1`). A transaction with N hashtags is counted
    once under each — the lifetime totals across hashtags don't sum to
    the flow total, by design (each hashtag's view is independent).
    """
    rows = await conn.fetch(
        f"""
        SELECT
            h.id,
            h.name,
            COALESCE(SUM({_SIGNED_CENTS_SQL}), 0)::bigint      AS lifetime_spent_cents,
            COALESCE(SUM({_SIGNED_HOME_CENTS_SQL}), 0)::bigint AS lifetime_spent_home_cents
        FROM expense_hashtags h
        LEFT JOIN expense_transaction_hashtags th
               ON th.hashtag_id          = h.id
              AND th.user_id             = h.user_id
              AND th.transaction_source  = 1
              AND th.deleted_at IS NULL
        LEFT JOIN expense_transactions t
               ON t.id         = th.transaction_id
              AND t.user_id    = h.user_id
              AND t.deleted_at IS NULL
        WHERE h.user_id     = $1
          AND h.is_archived = true
          AND h.deleted_at IS NULL
        GROUP BY h.id, h.name, h.sort_order
        ORDER BY h.sort_order ASC, h.name ASC
        """,
        user_id,
    )
    return [
        {
            "id": str(r["id"]),
            "name": r["name"],
            "lifetime_spent_cents": int(r["lifetime_spent_cents"]),
            "lifetime_spent_home_cents": int(r["lifetime_spent_home_cents"]),
        }
        for r in rows
    ]


@router.get("")
async def get_dashboard(
    auth_user: CurrentUser,
    include_archived: bool = Query(
        False,
        description=(
            "When true, response includes `archived_accounts`, "
            "`archived_categories`, `archived_hashtags` panels with lifetime "
            "totals. When false (default), those fields are returned as null."
        ),
    ),
    debit_as_negative: bool = Query(
        False,
        description=(
            "Accepted for API consistency with other read endpoints. Dashboard "
            "aggregates are already signed by construction (per-category "
            "spent_cents is positive for income and negative for expense; "
            "totals return split positive inflow/outflow). The flag is a no-op."
        ),
    ),
):
    # debit_as_negative is intentionally unused here — see docstring above.
    del debit_as_negative
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

        archived_accounts: Optional[list[dict]] = None
        archived_categories: Optional[list[dict]] = None
        archived_hashtags: Optional[list[dict]] = None
        if include_archived:
            archived_accounts = await _load_accounts(
                conn, auth_user.id, settings["main_currency"],
                is_person=False, archived=True,
            )
            archived_categories = await _load_archived_categories(conn, auth_user.id)
            archived_hashtags = await _load_archived_hashtags(conn, auth_user.id)

    return DashboardResponse(
        month={"year": year, "month": month},
        bank_accounts=bank_accounts,
        people=people,
        categories=flow["categories"],
        totals=flow["totals"],
        archived_accounts=archived_accounts,
        archived_categories=archived_categories,
        archived_hashtags=archived_hashtags,
    ).model_dump(mode="json")
