from datetime import datetime
from typing import Optional
from uuid import UUID

import asyncpg

from app.constants import (
    ActivityAction,
    HOME_CURRENCY,
    SystemCategoryKey,
)
from app.errors import conflict, settings_missing, validation_error
from app.helpers.activity_log import write_activity_log
from app.helpers.balance import apply_balance
from app.helpers.categories import ensure_system_category
from app.helpers.exchange_rate import lookup_exchange_rate
from app.schemas.transactions import infer_transaction_type, transaction_from_row


async def create_transfer_pair(
    conn: asyncpg.Connection,
    user_id: str,
    primary_id: UUID,
    sibling_id: UUID,
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
    # This guard is the ONLY thing enforcing "a transfer is not two outflows".
    # It cannot move to the database: after WP1 each leg is an ordinary row
    # whose direction is self-consistent, so the invariant spans two rows and
    # no CHECK constraint can express it. The inbox enforces the same rule at
    # its own write time (helpers/inbox._resolve_transfer_type), because that
    # is where both signs still exist side by side.
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

    primary_category_id = await ensure_system_category(
        conn,
        user_id,
        SystemCategoryKey.DEBT if primary_is_person else SystemCategoryKey.TRANSFER,
    )
    sibling_category_id = await ensure_system_category(
        conn,
        user_id,
        SystemCategoryKey.DEBT if transfer_is_person else SystemCategoryKey.TRANSFER,
    )

    # ------------------------------------------------------------------
    # 5. Normalize amounts and derive each leg's direction
    # ------------------------------------------------------------------
    # Each leg reads its OWN sign through the one shared rule. The guard above
    # has already established that the two signs oppose, so the pair is one
    # outflow and one inflow by construction rather than by assertion.
    primary_abs = abs(primary_amount_cents)
    primary_type = infer_transaction_type(primary_amount_cents)

    sibling_abs = abs(transfer_amount_cents)
    sibling_type = infer_transaction_type(transfer_amount_cents)

    # ------------------------------------------------------------------
    # 6. Exchange rates and amount_home_cents (dominant-side rule)
    # ------------------------------------------------------------------
    # Cross-currency transfers must net to zero in home currency. We achieve
    # this by forcing the non-dominant side's home value to equal the dominant
    # side's by direct assignment — never recomputed via rate — so integer
    # rounding can't introduce a net leak. The sibling's per-row exchange_rate
    # is then derived from that forced home value, for audit/display.
    #
    # The "dominant" side (the one whose home value is computed independently)
    # is picked in the order engine-spec.md §Transfers point 7 states:
    #
    #   1. The primary's currency == home  → primary dominant, rate 1.0.
    #   2. The sibling's currency == home  → sibling dominant, rate 1.0.
    #   3. Neither matches                 → primary dominant, at the caller's
    #      rate if supplied, else the market rate for its date.
    #
    # ⚠️ Branch order is load-bearing, and getting it wrong was open bug 1.3.
    # This block used to test the caller's rate override FIRST, which produced
    # two defects:
    #
    #   * a home-currency primary with a supplied rate computed
    #     `home = amount × rate` — wrong, since a PEN amount's home value is
    #     itself; and
    #   * there was no rule at all for case 3, so the block ended in
    #     `raise RuntimeError` and EVERY USD→USD transfer returned an uncaught
    #     500 under a PEN home currency.
    #
    # Case 3 is now a real rule rather than a dead end: the primary is valued
    # at the market rate through the same helper create_transaction already
    # uses, so a same-currency foreign transfer prices like any other row.
    #
    # This block is scheduled for deletion by docs/rework/WP2, which stops
    # storing home values altogether and surfaces the FX spread as @FX instead
    # of forcing it to zero. Until then the pair still nets to zero.
    primary_currency = primary_account["currency_code"]
    sibling_currency = transfer_account["currency_code"]

    # sql/018 locks main_currency to PEN, and helpers/home_currency.py's SQL
    # fragments interpolate HOME_CURRENCY as a literal on that basis. Asserting
    # the two agree costs nothing here and makes a lifted CHECK fail loudly
    # instead of silently pricing a non-PEN ledger in PEN. It also removes the
    # old `settings_row is None → main_currency = None → nothing matches` path,
    # which was the second route into the 500 above.
    settings_row = await conn.fetchrow(
        "SELECT main_currency FROM user_settings WHERE user_id = $1", user_id,
    )
    if settings_row is None:
        raise settings_missing()
    if settings_row["main_currency"] != HOME_CURRENCY:
        raise RuntimeError(
            f"user_settings.main_currency is {settings_row['main_currency']!r} "
            f"but the engine converts to {HOME_CURRENCY!r} (app.constants."
            "HOME_CURRENCY). sql/018 is supposed to make this unreachable; if "
            "that CHECK was lifted, helpers/home_currency.py must be revisited "
            "at the same time."
        )

    if primary_currency == HOME_CURRENCY:
        primary_exchange_rate = 1.0
        primary_home = primary_abs
        sibling_home = primary_home
        sibling_exchange_rate = sibling_home / sibling_abs
    elif sibling_currency == HOME_CURRENCY:
        sibling_exchange_rate = 1.0
        sibling_home = sibling_abs
        primary_home = sibling_home
        primary_exchange_rate = primary_home / primary_abs
    else:
        # Neither leg is in the home currency — e.g. USD→USD under a PEN home.
        # The primary is dominant, valued at the caller's rate if they gave one
        # and otherwise at the market rate on its date.
        if primary_exchange_rate is None:
            primary_exchange_rate = await lookup_exchange_rate(
                conn, primary_account_id, primary_date, user_id
            )
        primary_home = round(primary_abs * primary_exchange_rate)
        sibling_home = primary_home
        sibling_exchange_rate = sibling_home / sibling_abs

    if primary_id == sibling_id:
        raise validation_error(
            "Transfer id collision.",
            {"transfer.id": "Must differ from the primary transaction id."},
        )

    # ------------------------------------------------------------------
    # 7. Insert primary transaction
    # ------------------------------------------------------------------
    try:
        primary_row = await conn.fetchrow(
            """
            INSERT INTO expense_transactions
                (id, user_id, title, description, amount_cents, amount_home_cents,
                 transaction_type, date, account_id, category_id,
                 exchange_rate, cleared, inbox_id, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, now(), now())
            RETURNING *
            """,
            primary_id,
            user_id,
            primary_title,
            primary_description,
            primary_abs,
            primary_home,
            primary_type,
            primary_date,
            primary_account_id,
            primary_category_id,
            primary_exchange_rate,
            primary_cleared,
            inbox_id,
        )
    except asyncpg.UniqueViolationError:
        raise conflict(f"A transaction with id '{primary_id}' already exists.")

    # ------------------------------------------------------------------
    # 8. Insert sibling transaction (linked to primary)
    # ------------------------------------------------------------------
    try:
        sibling_row = await conn.fetchrow(
            """
            INSERT INTO expense_transactions
                (id, user_id, title, description, amount_cents, amount_home_cents,
                 transaction_type, date, account_id, category_id,
                 exchange_rate, cleared, transfer_transaction_id, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, now(), now())
            RETURNING *
            """,
            sibling_id,
            user_id,
            primary_title,
            primary_description,
            sibling_abs,
            sibling_home,
            sibling_type,
            primary_date,
            transfer_account_id,
            sibling_category_id,
            sibling_exchange_rate,
            primary_cleared,
            primary_id,
        )
    except asyncpg.UniqueViolationError:
        raise conflict(f"A transaction with id '{sibling_id}' already exists.")

    primary_id_str = str(primary_id)
    sibling_id_str = str(sibling_id)

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
    # 10. Update balances on both accounts via the shared helper so the
    #     sign matrix lives in one place (helpers/balance.py).
    # ------------------------------------------------------------------
    await apply_balance(
        conn, primary_account_id, user_id, primary_abs, primary_type,
    )
    await apply_balance(
        conn, transfer_account_id, user_id, sibling_abs, sibling_type,
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
        conn, user_id, "transaction", primary_id_str, ActivityAction.CREATED,
        after_snapshot=primary_response,
    )
    await write_activity_log(
        conn, user_id, "transaction", sibling_id_str, ActivityAction.CREATED,
        after_snapshot=sibling_response,
    )

    return primary_response, sibling_response
