"""Monthly flow aggregation shared by /dashboard (current month) and /reports/monthly (any month).

One source of truth for the SQL so the two endpoints are identical by construction:
every non-deleted category (even zero-flow ones), hashtag_breakdown grouped by
(category_id, sorted hashtag-id array), and totals for inflow/outflow/net.

Signed semantics: every row contributes a signed amount derived from
transaction_type alone — outflows negative, inflows positive. The expressions come
from ``helpers/home_currency.signed_expr``, the engine's single rendering of that
rule; this module used to carry two literal copies of a four-branch version, which
is audit finding WP9.1. Categories sum the signed amounts, so a same-currency
real-to-real transfer naturally cancels to zero under @Transfer, and a loan to a
person shows as negative @Transfer (real leg) + positive @Debt (person leg). Totals
split the signed values into inflow (positive) and outflow (|negative|); net is
inflow - outflow and is unaffected by internal movement volume.

`spent_home_cents` on a category can therefore be negative — the field name is
retained for spec-contract reasons but semantically it is "signed net flow through
this category this month".

Everything reported here is in the HOME currency, and only in the home currency.
There are no native cross-account aggregates, because ``GROUP BY category_id`` has
no currency partition: a category holding $15 and S/25 would report 4000, a number
in no currency at all. Conversion happens exactly where figures are combined.

@Transfer no longer always reads zero. Both legs of a cross-currency transfer are
converted at their own account's rate for the day, so a $1,000 → S/3,450 exchange on
a day the market rate is 3.58 reports −S/130: the spread the bank actually charged.
It used to be forced to zero by assignment at write time. So `@Transfer != 0` means
one of exactly two things — an FX spread, or a loan/repayment with a person, whose
other leg landed in @Debt with nothing to cancel against. See
docs/currency-model-decision.md.

Opening balances (transactions under the ``opening_balance`` system category) are
excluded entirely: no category row in the panel, no contribution to totals. An
opening balance is where tracking starts, not money that moved — including it
would report phantom income in the seed month. Exclusion keys off ``system_key``,
so renaming the @Opening display name never breaks it. Transfers, by contrast,
ARE included: both legs carry signed amounts (gross inflow/outflow do include
internal movement volume).

Unconvertible rows
------------------

A row whose date has no resolvable rate has no home value, and NULL is the signal
for that — never a native amount wearing a home label. But a per-row NULL does not
survive aggregation: ``SUM`` skips NULLs, and ``SUM(CASE WHEN x > 0 THEN x ELSE 0
END)`` scores a NULL row as *zero*, which cannot even fail loudly. So every home
SUM here is paired with ``SUM(is_unconvertible)``, and a non-zero count makes the
figure ``null`` rather than a partial total. Both the SQL and the Python rollup
enforce that, because the category total is summed from its breakdown rows in
Python.
"""
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import asyncpg

from app.constants import HOME_CURRENCY
from app.errors import settings_missing
from app.helpers.validation import resolve_timezone
from app.helpers.home_currency import (
    SIGNED_HOME_CENTS_EXPR,
    UNCONVERTIBLE_FLAG_EXPR,
    home_rate_join,
)

# SIGNED_HOME_CENTS_EXPR is imported, never re-derived here via
# signed_expr(HOME_CENTS_EXPR) — the two are byte-identical, but only the
# exported constant is what tests/test_home_currency_parity.py pins, so
# importing it is what makes that coverage apply to the SQL this module runs.
# (Until sql/021 the magnitude here read
# ``COALESCE(t.amount_home_cents, t.amount_cents)``, which did not convert —
# it relabelled, reading USD cents as PEN cents.)

# Both queries below bind $1 user_id, $2 start, $3 end — so the timezone, which
# must be BOUND and never interpolated (it is unvalidated user input), is $4.
_HOME_RATE_JOIN = home_rate_join("$4")


async def get_user_report_settings(
    conn: asyncpg.Connection,
    user_id: str,
) -> dict:
    """Load main_currency + display_timezone for a user, or 422 if they haven't bootstrapped.

    Also asserts that the user's ``main_currency`` is the currency
    ``helpers/home_currency.py`` interpolates into its SQL as a literal. That
    module cannot bind the value — its fragments are spliced into queries with
    differing ``$N`` numbering — and interpolation is only safe because sql/018
    locks ``main_currency`` to ``'PEN'``. The obligation to check is stated in
    that module's docstring; this is the chokepoint for it, because every query
    that converts reaches SQL through a caller of this function.

    Before WP2 the assertion lived in helpers/transfers.py, which was the only
    place holding both values at once. Conversion has moved to the read path, so
    the check moved with it.
    """
    row = await conn.fetchrow(
        "SELECT main_currency, display_timezone FROM user_settings WHERE user_id = $1",
        user_id,
    )
    if row is None:
        raise settings_missing()
    if row["main_currency"] != HOME_CURRENCY:
        raise RuntimeError(
            f"user_settings.main_currency is {row['main_currency']!r} but the "
            f"engine converts to {HOME_CURRENCY!r} (app.constants.HOME_CURRENCY). "
            "sql/018 is supposed to make this unreachable; if that CHECK was "
            "lifted, helpers/home_currency.py must be revisited at the same time."
        )
    return {"main_currency": row["main_currency"], "display_timezone": row["display_timezone"]}


def compute_month_bounds(
    display_timezone: str,
    year: Optional[int] = None,
    month: Optional[int] = None,
) -> tuple[int, int, datetime, datetime]:
    """Return (year, month, start_utc, end_utc) for a calendar month in the user's timezone.

    end_utc is exclusive (first instant of the following month). When year/month are
    omitted, returns the current month in display_timezone.
    """
    tz = ZoneInfo(resolve_timezone(display_timezone))

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
    display_timezone: str,
) -> dict:
    """Run the monthly flow queries for a user and return {categories, totals}.

    - categories: every non-deleted category except the opening_balance system row,
      sorted by sort_order, with hashtag_breakdown rows that sum exactly to the
      category's spent_home_cents (invariant enforced by construction — the category
      total is computed from the breakdown, not separately).
    - totals: inflow/outflow/net in home currency. Transfers are included;
      opening-balance transactions are excluded.

    ``display_timezone`` decides which calendar day each row is priced on, and it
    is the same zone ``compute_month_bounds`` uses to bucket months — so a
    transaction at 2026-03-31T23:00-05:00 is counted in March *and* priced at the
    March 31 rate. Callers already hold it from ``get_user_report_settings``; it is
    a parameter rather than a second settings lookup for that reason.

    Any figure derived from unconvertible rows is ``None``, never a partial total,
    and the object carrying it reports how many rows could not be converted.
    """
    # $4 reaches AT TIME ZONE, and Postgres errors on an unknown zone — the
    # same junk-tolerates-as-UTC fallback compute_month_bounds applies must
    # cover the SQL binding too, or a pre-validation settings row 500s every
    # report and dashboard read.
    display_timezone = resolve_timezone(display_timezone)
    categories_rows = await conn.fetch(
        """
        SELECT id, name, sort_order
        FROM expense_categories
        WHERE user_id = $1 AND deleted_at IS NULL
          AND system_key IS DISTINCT FROM 'opening_balance'
        ORDER BY sort_order ASC, name ASC
        """,
        user_id,
    )

    # The `a` and `r` aliases the fragments reference exist only inside this CTE,
    # so the unconvertible flag is projected here and summed by name outside —
    # interpolating it into the outer SELECT is a hard SQL error, not a style
    # choice. See helpers/home_currency.py, "The aggregation contract".
    breakdown_rows = await conn.fetch(
        f"""
        WITH signed_txns AS (
            SELECT
                t.id,
                t.category_id,
                {SIGNED_HOME_CENTS_EXPR} AS signed_home_cents,
                {UNCONVERTIBLE_FLAG_EXPR} AS is_unconvertible,
                COALESCE(
                    (
                        SELECT array_agg(th.hashtag_id::text ORDER BY th.hashtag_id::text)
                        FROM expense_transaction_hashtags th
                        WHERE th.transaction_id = t.id
                          AND th.transaction_source = 1
                          AND th.deleted_at IS NULL
                    ),
                    ARRAY[]::text[]
                ) AS hashtag_ids
            FROM expense_transactions t
            LEFT JOIN expense_bank_accounts a ON a.id = t.account_id
            {_HOME_RATE_JOIN}
            WHERE t.user_id = $1
              AND t.deleted_at IS NULL
              AND t.date >= $2
              AND t.date <  $3
              AND NOT EXISTS (
                  SELECT 1 FROM expense_categories c
                  WHERE c.id = t.category_id
                    AND c.system_key = 'opening_balance'
              )
        )
        SELECT
            category_id,
            hashtag_ids,
            SUM(signed_home_cents)::bigint AS spent_home_cents,
            SUM(is_unconvertible)::bigint  AS unconverted_count
        FROM signed_txns
        GROUP BY category_id, hashtag_ids
        ORDER BY category_id, hashtag_ids
        """,
        user_id,
        start_utc,
        end_utc,
        display_timezone,
    )

    breakdowns_by_category: dict[str, list[dict]] = {}
    for row in breakdown_rows:
        cat_id = str(row["category_id"])
        unconverted = int(row["unconverted_count"])
        breakdowns_by_category.setdefault(cat_id, []).append(
            {
                "hashtag_ids": list(row["hashtag_ids"]),
                # A group with even one unconvertible row reports nothing rather
                # than a total that silently omits it.
                "spent_home_cents": (
                    None if unconverted else int(row["spent_home_cents"])
                ),
                "unconverted_count": unconverted,
            }
        )

    categories: list[dict] = []
    for row in categories_rows:
        cat_id = str(row["id"])
        rows = breakdowns_by_category.get(cat_id, [])
        unconverted = sum(r["unconverted_count"] for r in rows)
        categories.append(
            {
                "id": cat_id,
                "name": row["name"],
                "spent_home_cents": (
                    None if unconverted else sum(r["spent_home_cents"] for r in rows)
                ),
                "unconverted_count": unconverted,
                "hashtag_breakdown": rows,
            }
        )

    totals_row = await conn.fetchrow(
        f"""
        WITH signed_txns AS (
            SELECT
                {SIGNED_HOME_CENTS_EXPR} AS signed_home_cents,
                {UNCONVERTIBLE_FLAG_EXPR} AS is_unconvertible
            FROM expense_transactions t
            LEFT JOIN expense_bank_accounts a ON a.id = t.account_id
            {_HOME_RATE_JOIN}
            WHERE t.user_id = $1
              AND t.deleted_at IS NULL
              AND t.date >= $2
              AND t.date <  $3
              AND NOT EXISTS (
                  SELECT 1 FROM expense_categories c
                  WHERE c.id = t.category_id
                    AND c.system_key = 'opening_balance'
              )
        )
        SELECT
            COALESCE(SUM(CASE WHEN signed_home_cents > 0 THEN  signed_home_cents ELSE 0 END), 0)::bigint AS inflow_home_cents,
            COALESCE(SUM(CASE WHEN signed_home_cents < 0 THEN -signed_home_cents ELSE 0 END), 0)::bigint AS outflow_home_cents,
            COALESCE(SUM(is_unconvertible), 0)::bigint AS unconverted_count
        FROM signed_txns
        """,
        user_id,
        start_utc,
        end_utc,
        display_timezone,
    )

    # This is the shape the aggregation contract exists for. `NULL > 0` is NULL,
    # not true, so an unconvertible row takes the ELSE arm and scores zero — a
    # month where nothing converted would report 0, not null, and look like a
    # month where nothing happened. The count is the only thing that can tell
    # them apart.
    unconverted = int(totals_row["unconverted_count"])
    if unconverted:
        totals = {
            "inflow_home_cents": None,
            "outflow_home_cents": None,
            "net_home_cents": None,
            "unconverted_count": unconverted,
        }
    else:
        inflow_home_cents = int(totals_row["inflow_home_cents"])
        outflow_home_cents = int(totals_row["outflow_home_cents"])
        totals = {
            "inflow_home_cents": inflow_home_cents,
            "outflow_home_cents": outflow_home_cents,
            "net_home_cents": inflow_home_cents - outflow_home_cents,
            "unconverted_count": 0,
        }

    return {"categories": categories, "totals": totals}
