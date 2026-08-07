from datetime import datetime
from typing import Optional
from uuid import UUID

import asyncpg

from app.constants import (
    ActivityAction,
    SystemCategoryKey,
)
from app.errors import conflict, validation_error
from app.helpers.activity_log import write_activity_log
from app.helpers.categories import ensure_system_category
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
    # 2. Same-account / same-id checks
    # ------------------------------------------------------------------
    if primary_account_id == transfer_account_id:
        errors["transfer.account_id"] = "Must be a different account."

    if primary_id == sibling_id:
        errors["transfer.id"] = "Must differ from the primary transaction id."

    # ------------------------------------------------------------------
    # 3. Validate both accounts
    # ------------------------------------------------------------------
    primary_account = await conn.fetchrow(
        """
        SELECT is_person FROM expense_bank_accounts
        WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL AND is_archived = false
        """,
        primary_account_id,
        user_id,
    )
    if primary_account is None:
        errors["account_id"] = "Must reference an active, non-archived account."

    transfer_account = await conn.fetchrow(
        """
        SELECT is_person FROM expense_bank_accounts
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
    # 6. No currency work. Each leg stores its own native amount, full stop.
    # ------------------------------------------------------------------
    # A ~75-line "dominant-side rule" used to live here. It picked one leg,
    # valued it in home currency, and then FORCED the other leg's home value
    # to equal it by direct assignment, so the pair always summed to exactly
    # zero. sql/021 deleted the columns it wrote, and with them the rule.
    #
    # What the forcing hid: send $1,000 and receive S/3,450 on a day the market
    # rate is 3.58, and the dollars were worth S/3,580. The S/130 difference is
    # a real cost really paid — the bank's spread — and assigning
    # `sibling_home = primary_home` made it vanish. Converting each leg at its
    # own date's rate now surfaces it, and it lands in @Transfer.
    #
    # So `@Transfer != 0` means exactly one of two things: an FX spread, or a
    # loan/repayment with a person (one leg in @Transfer, the other in @Debt,
    # nothing to cancel against). docs/currency-model-decision.md has the full
    # matrix, and records why a separate @FX category stays deferred — owner
    # decision, reaffirmed 2026-08-05.
    #
    # Two defects became unrepresentable rather than fixed: a USD→USD transfer
    # under a PEN home used to reach `raise RuntimeError` and return an
    # uncaught 500 (open bug 1.3), because the rule had no branch for "neither
    # leg is home currency". There is no rule now, so there is no dead end.
    # And the user_settings round-trip this block needed is gone with it — the
    # main_currency == HOME_CURRENCY assertion that guards home_currency.py's
    # interpolated literal moved to monthly_report.get_user_report_settings,
    # which is where conversion actually happens.

    # ------------------------------------------------------------------
    # 7. Insert primary transaction
    # ------------------------------------------------------------------
    try:
        primary_row = await conn.fetchrow(
            """
            INSERT INTO expense_transactions
                (id, user_id, title, description, amount_cents,
                 transaction_type, date, account_id, category_id,
                 cleared, inbox_id, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now(), now())
            RETURNING *
            """,
            primary_id,
            user_id,
            primary_title,
            primary_description,
            primary_abs,
            primary_type,
            primary_date,
            primary_account_id,
            primary_category_id,
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
                (id, user_id, title, description, amount_cents,
                 transaction_type, date, account_id, category_id,
                 cleared, transfer_transaction_id, inbox_id, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, now(), now())
            RETURNING *
            """,
            sibling_id,
            user_id,
            primary_title,
            primary_description,
            sibling_abs,
            sibling_type,
            primary_date,
            transfer_account_id,
            sibling_category_id,
            primary_cleared,
            primary_id,
            inbox_id,
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
    # 10. Build response dicts
    # ------------------------------------------------------------------
    # There is no balance step here any more. Both accounts' balances are the
    # signed sum of their rows (sql/022), and the two rows above are already
    # written — so both balances have already moved, atomically, by
    # construction. The step this replaced could be forgotten; a sum cannot.
    primary_response = transaction_from_row(primary_row)
    sibling_response = transaction_from_row(sibling_row)

    # ------------------------------------------------------------------
    # 11. Activity logs — one per transaction
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
