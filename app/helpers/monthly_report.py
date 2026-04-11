"""Monthly flow aggregation shared by /dashboard (current month) and /reports/monthly (any month).

One source of truth for the SQL so the two endpoints are identical by construction:
every non-deleted category (even zero-flow ones), hashtag_breakdown grouped by
(category_id, sorted hashtag-id array), and totals for inflow/outflow/net.

Signed semantics: every row contributes a signed amount derived from transaction_type
and transfer_direction. Expenses and transfer debits are negative (outflow); income
and transfer credits are positive (inflow). Categories sum the signed amounts, so a
real-to-real transfer naturally cancels to zero under @Transfer, and a loan to a
person shows as negative @Transfer (real leg) + positive @Debt (person leg). Totals
split the signed values into inflow (positive) and outflow (|negative|); net is
inflow - outflow and is unaffected by internal movement volume.

`spent_cents` on a category can therefore be negative — the field name is retained
for spec-contract reasons but semantically it is "signed net flow through this
category this month".
"""
from datetime import date as date_type, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import asyncpg


def compute_month_bounds(
    display_timezone: str,
    year: Optional[int] = None,
    month: Optional[int] = None,
) -> tuple[int, int, datetime, datetime]:
    """Return (year, month, start_utc, end_utc) for a calendar month in the user's timezone.

    end_utc is exclusive (first instant of the following month). When year/month are
    omitted, returns the current month in display_timezone.
    """
    try:
        tz = ZoneInfo(display_timezone)
    except Exception:
        tz = ZoneInfo("UTC")

    if year is None or month is None:
        now_local = datetime.now(tz)
        year = now_local.year
        month = now_local.month

    start_local = datetime(year, month, 1, 0, 0, 0, tzinfo=tz)
    if month == 12:
        end_local = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=tz)
    else:
        end_local = datetime(year, month + 1, 1, 0, 0, 0, tzinfo=tz)

    return year, month, start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


async def compute_month_flow(
    conn: asyncpg.Connection,
    user_id: str,
    start_utc: datetime,
    end_utc: datetime,
) -> dict:
    """Run the monthly flow queries for a user and return {categories, totals}.

    - categories: every non-deleted category, sorted by sort_order, with hashtag_breakdown
      rows that sum exactly to the category's spent_cents (invariant enforced by construction
      — the category total is computed from the breakdown, not separately).
    - totals: inflow/outflow/net in both native and home currency. Transfers excluded.
    """
    categories_rows = await conn.fetch(
        """
        SELECT id, name, sort_order
        FROM expense_categories
        WHERE user_id = $1 AND deleted_at IS NULL
        ORDER BY sort_order ASC, name ASC
        """,
        user_id,
    )

    breakdown_rows = await conn.fetch(
        """
        WITH signed_txns AS (
            SELECT
                t.id,
                t.category_id,
                CASE
                    WHEN t.transaction_type = 2 THEN  t.amount_cents
                    WHEN t.transaction_type = 3 AND t.transfer_direction = 2 THEN  t.amount_cents
                    WHEN t.transaction_type = 1 THEN -t.amount_cents
                    WHEN t.transaction_type = 3 AND t.transfer_direction = 1 THEN -t.amount_cents
                    ELSE 0
                END AS signed_cents,
                CASE
                    WHEN t.transaction_type = 2 THEN  COALESCE(t.amount_home_cents, t.amount_cents)
                    WHEN t.transaction_type = 3 AND t.transfer_direction = 2 THEN  COALESCE(t.amount_home_cents, t.amount_cents)
                    WHEN t.transaction_type = 1 THEN -COALESCE(t.amount_home_cents, t.amount_cents)
                    WHEN t.transaction_type = 3 AND t.transfer_direction = 1 THEN -COALESCE(t.amount_home_cents, t.amount_cents)
                    ELSE 0
                END AS signed_home_cents,
                COALESCE(
                    (
                        SELECT array_agg(th.hashtag_id::text ORDER BY th.hashtag_id::text)
                        FROM expense_transaction_hashtags th
                        WHERE th.transaction_id = t.id
                          AND th.deleted_at IS NULL
                    ),
                    ARRAY[]::text[]
                ) AS hashtag_ids
            FROM expense_transactions t
            WHERE t.user_id = $1
              AND t.deleted_at IS NULL
              AND t.date >= $2
              AND t.date <  $3
        )
        SELECT
            category_id,
            hashtag_ids,
            SUM(signed_cents)::bigint      AS spent_cents,
            SUM(signed_home_cents)::bigint AS spent_home_cents
        FROM signed_txns
        GROUP BY category_id, hashtag_ids
        ORDER BY category_id, hashtag_ids
        """,
        user_id,
        start_utc,
        end_utc,
    )

    breakdowns_by_category: dict[str, list[dict]] = {}
    for row in breakdown_rows:
        cat_id = str(row["category_id"])
        breakdowns_by_category.setdefault(cat_id, []).append(
            {
                "hashtag_combination": list(row["hashtag_ids"]),
                "spent_cents": int(row["spent_cents"]),
                "spent_home_cents": int(row["spent_home_cents"]),
            }
        )

    categories: list[dict] = []
    for row in categories_rows:
        cat_id = str(row["id"])
        rows = breakdowns_by_category.get(cat_id, [])
        spent_cents = sum(r["spent_cents"] for r in rows)
        spent_home_cents = sum(r["spent_home_cents"] for r in rows)
        categories.append(
            {
                "id": cat_id,
                "name": row["name"],
                "spent_cents": spent_cents,
                "spent_home_cents": spent_home_cents,
                "hashtag_breakdown": rows,
            }
        )

    totals_row = await conn.fetchrow(
        """
        WITH signed_txns AS (
            SELECT
                CASE
                    WHEN t.transaction_type = 2 THEN  t.amount_cents
                    WHEN t.transaction_type = 3 AND t.transfer_direction = 2 THEN  t.amount_cents
                    WHEN t.transaction_type = 1 THEN -t.amount_cents
                    WHEN t.transaction_type = 3 AND t.transfer_direction = 1 THEN -t.amount_cents
                    ELSE 0
                END AS signed_cents,
                CASE
                    WHEN t.transaction_type = 2 THEN  COALESCE(t.amount_home_cents, t.amount_cents)
                    WHEN t.transaction_type = 3 AND t.transfer_direction = 2 THEN  COALESCE(t.amount_home_cents, t.amount_cents)
                    WHEN t.transaction_type = 1 THEN -COALESCE(t.amount_home_cents, t.amount_cents)
                    WHEN t.transaction_type = 3 AND t.transfer_direction = 1 THEN -COALESCE(t.amount_home_cents, t.amount_cents)
                    ELSE 0
                END AS signed_home_cents
            FROM expense_transactions t
            WHERE t.user_id = $1
              AND t.deleted_at IS NULL
              AND t.date >= $2
              AND t.date <  $3
        )
        SELECT
            COALESCE(SUM(CASE WHEN signed_cents      > 0 THEN  signed_cents      ELSE 0 END), 0)::bigint AS inflow_cents,
            COALESCE(SUM(CASE WHEN signed_home_cents > 0 THEN  signed_home_cents ELSE 0 END), 0)::bigint AS inflow_home_cents,
            COALESCE(SUM(CASE WHEN signed_cents      < 0 THEN -signed_cents      ELSE 0 END), 0)::bigint AS outflow_cents,
            COALESCE(SUM(CASE WHEN signed_home_cents < 0 THEN -signed_home_cents ELSE 0 END), 0)::bigint AS outflow_home_cents
        FROM signed_txns
        """,
        user_id,
        start_utc,
        end_utc,
    )

    inflow_cents = int(totals_row["inflow_cents"])
    inflow_home_cents = int(totals_row["inflow_home_cents"])
    outflow_cents = int(totals_row["outflow_cents"])
    outflow_home_cents = int(totals_row["outflow_home_cents"])

    totals = {
        "inflow_cents": inflow_cents,
        "inflow_home_cents": inflow_home_cents,
        "outflow_cents": outflow_cents,
        "outflow_home_cents": outflow_home_cents,
        "net_cents": inflow_cents - outflow_cents,
        "net_home_cents": inflow_home_cents - outflow_home_cents,
    }

    return {"categories": categories, "totals": totals}
