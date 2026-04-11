from datetime import datetime
from typing import Optional

import asyncpg

from app.errors import validation_error
from app.helpers.activity_log import write_activity_log
from app.helpers.categories import ensure_system_category
from app.helpers.exchange_rate import lookup_exchange_rate
from app.schemas.transactions import transaction_from_row


async def create_transfer_pair(
    conn: asyncpg.Connection,
    user_id: str,
    primary_title: str,
    primary_description: Optional[str],
    primary_amount_cents: int,
    primary_account_id: str,
    primary_date: datetime,
    primary_exchange_rate: Optional[float],
    primary_cleared: bool,
    transfer_account_id: str,
    transfer_amount_cents: int,
    inbox_id: Optional[str] = None,
) -> tuple[dict, dict]:
    """Create a paired transfer atomically.

    Must be called inside an ``async with conn.transaction()`` block.
    Returns ``(primary_response, sibling_response)`` as dicts.
    """

    # ------------------------------------------------------------------
    # 1. Zero-sum validation — opposite signs, neither zero
    # ------------------------------------------------------------------
    errors: dict = {}

    if primary_amount_cents == 0:
        errors["amount_cents"] = "Must not be zero."
    if transfer_amount_cents == 0:
        errors["transfer.amount_cents"] = "Must not be zero."

    if primary_amount_cents != 0 and transfer_amount_cents != 0:
        same_sign = (primary_amount_cents > 0) == (transfer_amount_cents > 0)
        if same_sign:
            errors["transfer.amount_cents"] = (
                "Must have opposite sign to primary amount_cents."
            )

    # ------------------------------------------------------------------
    # 2. Same-account check
    # ------------------------------------------------------------------
    if primary_account_id == transfer_account_id:
        errors["transfer.account_id"] = "Must be a different account."

    # ------------------------------------------------------------------
    # 3. Validate both accounts
    # ------------------------------------------------------------------
    primary_account = await conn.fetchrow(
        """
        SELECT id, currency_code, is_person FROM expense_bank_accounts
        WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL AND is_archived = false
        """,
        primary_account_id,
        user_id,
    )
    if primary_account is None:
        errors["account_id"] = "Must reference an active, non-archived account."

    transfer_account = await conn.fetchrow(
        """
        SELECT id, currency_code, is_person FROM expense_bank_accounts
        WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL AND is_archived = false
        """,
        transfer_account_id,
        user_id,
    )
    if transfer_account is None:
        errors["transfer.account_id"] = "Must reference an active, non-archived account."

    if errors:
        raise validation_error("Transfer validation failed.", errors)

    # ------------------------------------------------------------------
    # 4. Auto-assign categories based on is_person
    # ------------------------------------------------------------------
    primary_is_person = primary_account["is_person"]
    transfer_is_person = transfer_account["is_person"]

    if primary_is_person:
        primary_category_id = await ensure_system_category(conn, user_id, "@Debt")
    else:
        primary_category_id = await ensure_system_category(conn, user_id, "@Transfer")

    if transfer_is_person:
        sibling_category_id = await ensure_system_category(conn, user_id, "@Debt")
    else:
        sibling_category_id = await ensure_system_category(conn, user_id, "@Transfer")

    # ------------------------------------------------------------------
    # 5. Normalize amounts and determine transfer_direction
    # ------------------------------------------------------------------
    primary_abs = abs(primary_amount_cents)
    primary_direction = 1 if primary_amount_cents < 0 else 2  # 1=debit, 2=credit

    sibling_abs = abs(transfer_amount_cents)
    sibling_direction = 1 if transfer_amount_cents < 0 else 2

    # ------------------------------------------------------------------
    # 6. Exchange rates and amount_home_cents (dominant-side rule)
    # ------------------------------------------------------------------
    # Cross-currency transfers must net to zero in home currency. We achieve
    # this by forcing the sibling's home value to equal the primary's, and
    # deriving the sibling's rate from that. The "dominant" side (the one
    # whose home value is computed independently) is picked in this order:
    #
    #   1. If the caller supplied a primary_exchange_rate, the primary wins.
    #   2. If the primary's currency == main currency, the primary wins (rate 1.0).
    #   3. If the sibling's currency  == main currency, the sibling wins (rate 1.0).
    #   4. Otherwise (3-currency edge case, neither side matches main), the
    #      debit side wins via market rate lookup.
    #
    # In every case the non-dominant side's amount_home_cents is forced to
    # equal the dominant side's by direct assignment — never recomputed via
    # rate — so integer rounding can't introduce a net leak. Per-row
    # exchange_rate is still stored for audit/display, derived from the
    # forced home value.
    primary_currency = primary_account["currency_code"]
    sibling_currency = transfer_account["currency_code"]

    settings_row = await conn.fetchrow(
        "SELECT main_currency FROM user_settings WHERE user_id = $1", user_id,
    )
    main_currency = settings_row["main_currency"] if settings_row else None

    if primary_exchange_rate is not None:
        # Caller override: primary dominant with the given rate
        primary_home = round(primary_abs * primary_exchange_rate)
        sibling_home = primary_home
        sibling_exchange_rate = sibling_home / sibling_abs
    elif primary_currency == main_currency:
        primary_exchange_rate = 1.0
        primary_home = primary_abs
        sibling_home = primary_home
        sibling_exchange_rate = sibling_home / sibling_abs
    elif sibling_currency == main_currency:
        sibling_exchange_rate = 1.0
        sibling_home = sibling_abs
        primary_home = sibling_home
        primary_exchange_rate = primary_home / primary_abs
    else:
        # Neither side is main currency — rare 3-currency case.
        # Use market rate on the debit side, force the credit side to match.
        if primary_direction == 1:  # primary is the debit side
            primary_exchange_rate = await lookup_exchange_rate(
                conn, primary_account_id, primary_date, user_id,
            )
            primary_home = round(primary_abs * primary_exchange_rate)
            sibling_home = primary_home
            sibling_exchange_rate = sibling_home / sibling_abs
        else:  # sibling is the debit side
            sibling_exchange_rate = await lookup_exchange_rate(
                conn, transfer_account_id, primary_date, user_id,
            )
            sibling_home = round(sibling_abs * sibling_exchange_rate)
            primary_home = sibling_home
            primary_exchange_rate = primary_home / primary_abs

    # ------------------------------------------------------------------
    # 7. Insert primary transaction
    # ------------------------------------------------------------------
    primary_row = await conn.fetchrow(
        """
        INSERT INTO expense_transactions
            (user_id, title, description, amount_cents, amount_home_cents,
             transaction_type, transfer_direction, date, account_id, category_id,
             exchange_rate, cleared, inbox_id, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, 3, $6, $7, $8, $9, $10, $11, $12, now(), now())
        RETURNING *
        """,
        user_id,
        primary_title,
        primary_description,
        primary_abs,
        primary_home,
        primary_direction,
        primary_date,
        primary_account_id,
        primary_category_id,
        primary_exchange_rate,
        primary_cleared,
        inbox_id,
    )
    primary_id = str(primary_row["id"])

    # ------------------------------------------------------------------
    # 8. Insert sibling transaction (linked to primary)
    # ------------------------------------------------------------------
    sibling_row = await conn.fetchrow(
        """
        INSERT INTO expense_transactions
            (user_id, title, description, amount_cents, amount_home_cents,
             transaction_type, transfer_direction, date, account_id, category_id,
             exchange_rate, cleared, transfer_transaction_id, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, 3, $6, $7, $8, $9, $10, $11, $12, now(), now())
        RETURNING *
        """,
        user_id,
        primary_title,
        primary_description,
        sibling_abs,
        sibling_home,
        sibling_direction,
        primary_date,
        transfer_account_id,
        sibling_category_id,
        sibling_exchange_rate,
        primary_cleared,
        primary_id,
    )
    sibling_id = str(sibling_row["id"])

    # ------------------------------------------------------------------
    # 9. Link primary → sibling
    # ------------------------------------------------------------------
    primary_row = await conn.fetchrow(
        """
        UPDATE expense_transactions
        SET transfer_transaction_id = $1, updated_at = now(), version = version + 1
        WHERE id = $2 AND user_id = $3
        RETURNING *
        """,
        sibling_id,
        primary_id,
        user_id,
    )

    # ------------------------------------------------------------------
    # 10. Update balances on both accounts
    # ------------------------------------------------------------------
    primary_delta = -primary_abs if primary_direction == 1 else primary_abs
    await conn.execute(
        """
        UPDATE expense_bank_accounts
        SET current_balance_cents = current_balance_cents + $1,
            updated_at = now(), version = version + 1
        WHERE id = $2 AND user_id = $3
        """,
        primary_delta,
        primary_account_id,
        user_id,
    )

    sibling_delta = -sibling_abs if sibling_direction == 1 else sibling_abs
    await conn.execute(
        """
        UPDATE expense_bank_accounts
        SET current_balance_cents = current_balance_cents + $1,
            updated_at = now(), version = version + 1
        WHERE id = $2 AND user_id = $3
        """,
        sibling_delta,
        transfer_account_id,
        user_id,
    )

    # ------------------------------------------------------------------
    # 11. Build response dicts
    # ------------------------------------------------------------------
    primary_response = transaction_from_row(primary_row)
    sibling_response = transaction_from_row(sibling_row)

    # ------------------------------------------------------------------
    # 12. Activity logs — one per transaction
    # ------------------------------------------------------------------
    await write_activity_log(
        conn, user_id, "transaction", primary_id, 1,
        after_snapshot=primary_response,
    )
    await write_activity_log(
        conn, user_id, "transaction", sibling_id, 1,
        after_snapshot=sibling_response,
    )

    return primary_response, sibling_response
