"""Home-currency recalculation when main_currency changes.

Called from PUT /auth/settings when the old and new main_currency differ.
Runs inside the caller's existing DB transaction.

Three passes:
  1. Regular transactions — look up rate, recompute exchange_rate + amount_home_cents.
  2. Transfer pairs — reapply dominant-side rule so both legs net to zero.
  3. Pending inbox items — recompute exchange_rate for future promotions.

Returns a summary dict for the activity log.

Activity log — deliberate aggregation exception: the per-row mutations
(bulk UPDATEs on expense_transactions and expense_transaction_inbox) do
NOT write individual ``activity_log`` entries. Instead, the triggering
call in ``helpers/auth.py::update_settings`` writes a single UPDATED
entry on ``user_settings`` carrying a ``recalculation`` summary block
(rows touched per pass). A full recalc on a busy user can rewrite tens
of thousands of rows in a single request; per-row entries would inflate
``activity_log`` by orders of magnitude without answering useful audit
questions. The triggering user_settings entry is the canonical record.
"""
from __future__ import annotations

from datetime import date as date_type, datetime, timezone

import asyncpg

from app.constants import TransferDirection
from app.helpers.exchange_rate import get_rate


async def recalculate_home_currency(
    conn: asyncpg.Connection,
    user_id: str,
    new_main_currency: str,
) -> dict:
    today = datetime.now(timezone.utc).date()

    # Rate cache keyed by (currency, date). Without this, a user with 5,000
    # transactions that share (say) 20 distinct (currency, date) pairs would
    # hit the exchange_rates table 5,000 times. With it, at most 20.
    rate_cache: dict[tuple[str, date_type], float] = {}

    async def _cached_rate(currency: str, as_of: date_type) -> float:
        cache_key = (currency, as_of)
        if cache_key not in rate_cache:
            result = await get_rate(conn, currency, new_main_currency, as_of)
            rate_cache[cache_key] = result[0] if result else 1.0
        return rate_cache[cache_key]

    # ── Pass 1: Regular transactions (non-transfer) ──────────────────────
    regular = await conn.fetch(
        """
        SELECT t.id, t.amount_cents, t.date, a.currency_code
        FROM expense_transactions t
        JOIN expense_bank_accounts a ON a.id = t.account_id
        WHERE t.user_id = $1
          AND t.deleted_at IS NULL
          AND t.transfer_transaction_id IS NULL
        """,
        user_id,
    )

    regular_count = 0
    for row in regular:
        currency = row["currency_code"]
        tx_date = row["date"].date() if isinstance(row["date"], datetime) else row["date"]

        rate = await _cached_rate(currency, tx_date)

        new_home = round(row["amount_cents"] * rate)
        await conn.execute(
            """
            UPDATE expense_transactions
            SET exchange_rate = $1, amount_home_cents = $2,
                updated_at = now(), version = version + 1
            WHERE id = $3
            """,
            rate,
            new_home,
            row["id"],
        )
        regular_count += 1

    # ── Pass 2: Transfer pairs ───────────────────────────────────────────
    transfers = await conn.fetch(
        """
        SELECT t.id, t.amount_cents, t.date, t.transfer_transaction_id,
               t.transfer_direction, a.currency_code
        FROM expense_transactions t
        JOIN expense_bank_accounts a ON a.id = t.account_id
        WHERE t.user_id = $1
          AND t.deleted_at IS NULL
          AND t.transfer_transaction_id IS NOT NULL
        ORDER BY t.id
        """,
        user_id,
    )

    by_id = {str(r["id"]): r for r in transfers}
    seen = set()
    transfer_count = 0

    for row in transfers:
        row_id = str(row["id"])
        if row_id in seen:
            continue

        sibling_id = str(row["transfer_transaction_id"])
        sibling = by_id.get(sibling_id)
        if sibling is None:
            continue

        seen.add(row_id)
        seen.add(sibling_id)

        leg_a, leg_b = row, sibling
        a_currency = leg_a["currency_code"]
        b_currency = leg_b["currency_code"]
        tx_date = leg_a["date"].date() if isinstance(leg_a["date"], datetime) else leg_a["date"]

        if a_currency == new_main_currency:
            dom, non_dom = leg_a, leg_b
        elif b_currency == new_main_currency:
            dom, non_dom = leg_b, leg_a
        else:
            # 3-currency case: debit side dominant via market rate
            if leg_a["transfer_direction"] == TransferDirection.DEBIT:
                dom, non_dom = leg_a, leg_b
            else:
                dom, non_dom = leg_b, leg_a

        dom_currency = dom["currency_code"]
        if dom_currency == new_main_currency:
            dom_rate = 1.0
            dom_home = dom["amount_cents"]
        else:
            dom_rate = await _cached_rate(dom_currency, tx_date)
            dom_home = round(dom["amount_cents"] * dom_rate)

        non_dom_home = dom_home
        non_dom_amount = non_dom["amount_cents"]
        non_dom_rate = non_dom_home / non_dom_amount if non_dom_amount != 0 else 1.0

        await conn.execute(
            """
            UPDATE expense_transactions
            SET exchange_rate = $1, amount_home_cents = $2,
                updated_at = now(), version = version + 1
            WHERE id = $3
            """,
            dom_rate, dom_home, dom["id"],
        )
        await conn.execute(
            """
            UPDATE expense_transactions
            SET exchange_rate = $1, amount_home_cents = $2,
                updated_at = now(), version = version + 1
            WHERE id = $3
            """,
            non_dom_rate, non_dom_home, non_dom["id"],
        )
        transfer_count += 2

    # ── Pass 3: Pending inbox items ──────────────────────────────────────
    inbox = await conn.fetch(
        """
        SELECT i.id, i.date, a.currency_code
        FROM expense_transaction_inbox i
        JOIN expense_bank_accounts a ON a.id = i.account_id
        WHERE i.user_id = $1
          AND i.deleted_at IS NULL
          AND i.status = 1
          AND i.account_id IS NOT NULL
        """,
        user_id,
    )

    inbox_count = 0
    for row in inbox:
        currency = row["currency_code"]
        if row["date"] is not None:
            ix_date = row["date"].date() if isinstance(row["date"], datetime) else row["date"]
        else:
            ix_date = today

        rate = await _cached_rate(currency, ix_date)

        await conn.execute(
            """
            UPDATE expense_transaction_inbox
            SET exchange_rate = $1, updated_at = now(), version = version + 1
            WHERE id = $2
            """,
            rate,
            row["id"],
        )
        inbox_count += 1

    return {
        "regular_transactions": regular_count,
        "transfer_transactions": transfer_count,
        "inbox_items": inbox_count,
        "total": regular_count + transfer_count + inbox_count,
    }
