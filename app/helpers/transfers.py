from datetime import datetime
from typing import Optional
from uuid import UUID

import asyncpg

from app.constants import (
    ActivityAction,
    SystemCategoryKey,
    TransactionType,
    TransferDirection,
)
from app.errors import conflict, validation_error
from app.helpers.activity_log import write_activity_log
from app.helpers.balance import apply_balance
from app.helpers.categories import ensure_system_category
from app.schemas.transactions import transaction_from_row


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
    # 5. Normalize amounts and determine transfer_direction
    # ------------------------------------------------------------------
    primary_abs = abs(primary_amount_cents)
    primary_direction = TransferDirection.DEBIT if primary_amount_cents < 0 else TransferDirection.CREDIT

    sibling_abs = abs(transfer_amount_cents)
    sibling_direction = TransferDirection.DEBIT if transfer_amount_cents < 0 else TransferDirection.CREDIT

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
    #
    # Phase 1 supports only USD and PEN (sql/015 CHECK), so main_currency
    # always matches one of the two legs — no 3-currency fallback is needed.
    # The auth trigger (sql/006) guarantees user_settings exists for every
    # authenticated user, so settings_row is None only under corrupted state;
    # we raise loudly in that case rather than silently picking a fallback.
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
        raise RuntimeError(
            f"Transfer dominant-side rule failed: neither leg ({primary_currency}, "
            f"{sibling_currency}) matches main_currency ({main_currency!r}). "
            "Under the Phase 1 PEN/USD-only policy (sql/015) this state is "
            "unreachable for valid data — likely indicates missing user_settings."
        )

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
                 transaction_type, transfer_direction, date, account_id, category_id,
                 exchange_rate, cleared, inbox_id, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, 3, $7, $8, $9, $10, $11, $12, $13, now(), now())
            RETURNING *
            """,
            primary_id,
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
                 transaction_type, transfer_direction, date, account_id, category_id,
                 exchange_rate, cleared, transfer_transaction_id, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, 3, $7, $8, $9, $10, $11, $12, $13, now(), now())
            RETURNING *
            """,
            sibling_id,
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
    #     transfer-direction sign matrix lives in one place (helpers/balance.py).
    # ------------------------------------------------------------------
    await apply_balance(
        conn,
        primary_account_id,
        user_id,
        primary_abs,
        TransactionType.TRANSFER,
        transfer_direction=primary_direction,
    )
    await apply_balance(
        conn,
        transfer_account_id,
        user_id,
        sibling_abs,
        TransactionType.TRANSFER,
        transfer_direction=sibling_direction,
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
