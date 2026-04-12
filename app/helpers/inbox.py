"""Inbox domain logic.

Service-layer functions for expense_transaction_inbox, called from
routers/inbox.py. Routers stay thin (HTTP glue + idempotency) and
delegate business logic here.

## Promote flow (the interesting one)

``promote_inbox_item`` branches on whether the inbox row has transfer
fields set:

  * Transfer branch: delegates to ``create_transfer_pair`` which handles
    all 12 steps of the transfer orchestration (zero-sum validation,
    dominant-side FX rule, dual-insert, dual-balance update).
  * Non-transfer branch: inserts a single expense_transactions row and
    applies a single balance delta via ``helpers.balance.apply_balance``.

Both branches converge on shared cleanup: the inbox row is soft-deleted
with ``status = 2`` (PROMOTED) and an activity log entry is written.
This shared cleanup MUST happen in the same call (not the caller's
responsibility) — otherwise a partial failure could orphan an inbox row
with its status still set to PENDING.

The inbox row is locked with ``FOR UPDATE`` at the start of the promote
flow so two concurrent promotes can't create duplicate transactions
from the same inbox item.
"""

from typing import Optional

import asyncpg

from app.constants import ActivityAction, TransactionType
from app.errors import not_found, validation_error
from app.helpers.activity_log import write_activity_log
from app.helpers.balance import apply_balance
from app.helpers.exchange_rate import lookup_exchange_rate
from app.helpers.query_builder import dynamic_update, soft_delete
from app.schemas.inbox import InboxCreateRequest, InboxUpdateRequest, inbox_from_row
from app.schemas.transactions import infer_transaction_type, transaction_from_row


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

async def create_inbox_item(
    conn: asyncpg.Connection,
    user_id: str,
    body: InboxCreateRequest,
) -> dict:
    """Create an inbox item.

    Inbox items can have sparse data — amount, date, account, category
    are all optional. The service normalises what's provided (sign →
    transaction_type, abs the amount) and auto-populates the exchange
    rate if both account and date are known.

    Transfer fields (if provided) override ``transaction_type`` to
    ``TRANSFER`` and stash the sibling account + signed amount for later
    use when the item is promoted.
    """
    # Infer transaction_type and normalize amount
    amount_cents = body.amount_cents
    transaction_type: Optional[int] = None
    if amount_cents is not None:
        if amount_cents == 0:
            raise validation_error(
                "amount_cents must not be zero.",
                {"amount_cents": "Must not be zero."},
            )
        transaction_type = infer_transaction_type(amount_cents)
        amount_cents = abs(amount_cents)

    # Transfer fields
    transfer_account_id: Optional[str] = None
    transfer_amount_cents: Optional[int] = None
    if body.transfer is not None:
        if body.transfer.amount_cents == 0:
            raise validation_error(
                "transfer.amount_cents must not be zero.",
                {"transfer.amount_cents": "Must not be zero."},
            )
        transfer_account_id = body.transfer.account_id
        transfer_amount_cents = body.transfer.amount_cents  # stored signed
        transaction_type = TransactionType.TRANSFER  # override to transfer

    # Auto-populate exchange_rate if both account_id and date are present
    exchange_rate = body.exchange_rate
    if exchange_rate is None and body.account_id and body.date:
        exchange_rate = await lookup_exchange_rate(conn, body.account_id, body.date, user_id)

    row = await conn.fetchrow(
        """
        INSERT INTO expense_transaction_inbox
            (user_id, title, description, amount_cents, transaction_type,
             date, account_id, category_id, exchange_rate,
             transfer_account_id, transfer_amount_cents,
             created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, COALESCE($9, 1.0), $10, $11, now(), now())
        RETURNING *
        """,
        user_id,
        body.title,
        body.description,
        amount_cents,
        transaction_type,
        body.date,
        body.account_id,
        body.category_id,
        exchange_rate,
        transfer_account_id,
        transfer_amount_cents,
    )

    response = inbox_from_row(row)

    await write_activity_log(
        conn, user_id, "inbox", str(row["id"]), ActivityAction.CREATED,
        after_snapshot=response,
    )
    return response


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

async def update_inbox_item(
    conn: asyncpg.Connection,
    user_id: str,
    inbox_id: str,
    body: InboxUpdateRequest,
) -> dict:
    """Partial update of an inbox item.

    Handles the same transfer-field flattening and amount normalisation
    as ``create_inbox_item``, plus auto-relookup of the exchange rate
    when ``date`` changes.

    Empty updates (no fields after Pydantic exclude-none) short-circuit
    to a fetch-and-return — matches the prior router behaviour and the
    pattern established by other domain helpers.
    """
    fields = body.model_dump(exclude_none=True)

    # Extract transfer before passing to dynamic UPDATE builder
    transfer = fields.pop("transfer", None)
    if transfer is not None:
        if transfer["amount_cents"] == 0:
            raise validation_error(
                "transfer.amount_cents must not be zero.",
                {"transfer.amount_cents": "Must not be zero."},
            )
        fields["transfer_account_id"] = transfer["account_id"]
        fields["transfer_amount_cents"] = transfer["amount_cents"]  # stored signed
        fields["transaction_type"] = TransactionType.TRANSFER

    # Empty update — return current
    if not fields:
        row = await conn.fetchrow(
            "SELECT * FROM expense_transaction_inbox WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
            inbox_id,
            user_id,
        )
        if row is None:
            raise not_found("inbox item")
        return inbox_from_row(row)

    # Process amount_cents: infer transaction_type, normalize to abs
    if "amount_cents" in fields:
        if fields["amount_cents"] == 0:
            raise validation_error(
                "amount_cents must not be zero.",
                {"amount_cents": "Must not be zero."},
            )
        fields["transaction_type"] = infer_transaction_type(fields["amount_cents"])
        fields["amount_cents"] = abs(fields["amount_cents"])

    before_row = await conn.fetchrow(
        "SELECT * FROM expense_transaction_inbox WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
        inbox_id,
        user_id,
    )
    if before_row is None:
        raise not_found("inbox item")

    before = inbox_from_row(before_row)

    # Auto-populate exchange_rate if date changes and account_id is set
    # (unless user explicitly supplied exchange_rate in this request)
    if "date" in fields and "exchange_rate" not in fields:
        account_id = fields.get("account_id") or (
            str(before_row["account_id"]) if before_row["account_id"] else None
        )
        if account_id and fields["date"]:
            fields["exchange_rate"] = await lookup_exchange_rate(
                conn, account_id, fields["date"], user_id
            )

    after_row = await dynamic_update(conn, "expense_transaction_inbox", fields, inbox_id, user_id)
    if after_row is None:
        raise not_found("inbox item")

    after = inbox_from_row(after_row)

    await write_activity_log(
        conn, user_id, "inbox", inbox_id, ActivityAction.UPDATED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

async def delete_inbox_item(
    conn: asyncpg.Connection,
    user_id: str,
    inbox_id: str,
) -> dict:
    """Soft-delete a pending inbox item.

    This is distinct from the PROMOTED end-state which also sets
    ``deleted_at`` but keeps ``status = 2``. A plain delete just marks
    the row ``deleted_at`` without touching ``status``.
    """
    row = await conn.fetchrow(
        "SELECT * FROM expense_transaction_inbox WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
        inbox_id,
        user_id,
    )
    if row is None:
        raise not_found("inbox item")

    before = inbox_from_row(row)

    after_row = await soft_delete(conn, "expense_transaction_inbox", inbox_id, user_id)
    after = inbox_from_row(after_row)

    await write_activity_log(
        conn, user_id, "inbox", inbox_id, ActivityAction.DELETED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after


# ---------------------------------------------------------------------------
# Promote
# ---------------------------------------------------------------------------

async def promote_inbox_item(
    conn: asyncpg.Connection,
    user_id: str,
    inbox_id: str,
) -> dict:
    """Promote a pending inbox item into a ledger transaction.

    The flow:

      1. Lock the inbox row (``FOR UPDATE``) so two concurrent promotes
         can't create duplicate ledger transactions.
      2. Validate that all fields required for promotion are present
         and reference active resources (account, category).
      3. Branch on transfer vs non-transfer:
           - Transfer: delegate to ``create_transfer_pair``, passing
             ``inbox_id`` so both ledger legs are linked back to the
             inbox row for audit.
           - Non-transfer: insert a single ledger row, apply the
             balance delta, write an activity log for the new
             transaction.
      4. Shared cleanup: soft-delete the inbox row with
         ``status = 2`` (PROMOTED) and write an activity log.

    Returns the newly-created ledger transaction (or the primary leg of
    the transfer pair).
    """
    # 1. Fetch inbox item with row-level lock
    # Lock the inbox row for update — prevents two concurrent
    # promotes from creating duplicate transactions from the same
    # inbox item. The lock releases when the transaction commits.
    inbox_row = await conn.fetchrow(
        """
        SELECT * FROM expense_transaction_inbox
        WHERE id = $1 AND user_id = $2 AND status = 1 AND deleted_at IS NULL
        FOR UPDATE
        """,
        inbox_id,
        user_id,
    )
    if inbox_row is None:
        raise not_found("inbox item")

    inbox_before = inbox_from_row(inbox_row)

    # 2. Detect transfer promotion
    is_transfer = (
        inbox_row["transfer_account_id"] is not None
        and inbox_row["transfer_amount_cents"] is not None
    )

    # 3. Validate shared required fields — collect all failures
    errors: dict = {}

    if not inbox_row["title"] or inbox_row["title"] == "UNTITLED":
        errors["title"] = "Must be present and not 'UNTITLED'."

    if inbox_row["amount_cents"] is None or inbox_row["amount_cents"] == 0:
        errors["amount_cents"] = "Must be present and not zero."

    if inbox_row["date"] is None:
        errors["date"] = "Must be present and not in the future."
    elif inbox_row["date"] > await conn.fetchval("SELECT now()"):
        errors["date"] = "Must be present and not in the future."

    if inbox_row["account_id"] is None:
        errors["account_id"] = "Must reference an active, non-archived account."
    else:
        account = await conn.fetchrow(
            """
            SELECT id, currency_code FROM expense_bank_accounts
            WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL AND is_archived = false
            """,
            inbox_row["account_id"],
            user_id,
        )
        if account is None:
            errors["account_id"] = "Must reference an active, non-archived account."

    # Category validation only for non-transfers (transfers auto-assign)
    if not is_transfer:
        if inbox_row["category_id"] is None:
            errors["category_id"] = "Must reference an active category."
        else:
            category = await conn.fetchrow(
                "SELECT id FROM expense_categories WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                inbox_row["category_id"],
                user_id,
            )
            if category is None:
                errors["category_id"] = "Must reference an active category."

    if errors:
        raise validation_error("Inbox item is not ready to promote.", errors)

    # 4a. Transfer promotion branch
    if is_transfer:
        # Imported lazily to avoid circular-import complications
        from app.helpers.transfers import create_transfer_pair

        # Reconstruct signed primary amount from transfer_amount_cents sign:
        # if transfer side is positive, primary must be negative, and vice versa.
        transfer_amt = inbox_row["transfer_amount_cents"]
        primary_signed = -inbox_row["amount_cents"] if transfer_amt > 0 else inbox_row["amount_cents"]

        txn_response, _sibling = await create_transfer_pair(
            conn=conn,
            user_id=user_id,
            primary_title=inbox_row["title"],
            primary_description=inbox_row["description"],
            primary_amount_cents=primary_signed,
            primary_account_id=str(inbox_row["account_id"]),
            primary_date=inbox_row["date"],
            primary_exchange_rate=float(inbox_row["exchange_rate"]),
            primary_cleared=False,
            transfer_account_id=str(inbox_row["transfer_account_id"]),
            transfer_amount_cents=transfer_amt,
            inbox_id=str(inbox_row["id"]),
        )

    # 4b. Normal (non-transfer) promotion branch
    else:
        # Determine transaction_type — use stored value, or default to expense
        transaction_type = inbox_row["transaction_type"] or TransactionType.EXPENSE

        # Compute amount_home_cents
        exchange_rate = float(inbox_row["exchange_rate"])
        amount_home_cents = round(inbox_row["amount_cents"] * exchange_rate)

        # Create expense_transactions row
        txn_row = await conn.fetchrow(
            """
            INSERT INTO expense_transactions
                (user_id, title, description, amount_cents, amount_home_cents,
                 transaction_type, date, account_id, category_id, exchange_rate,
                 inbox_id, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now(), now())
            RETURNING *
            """,
            user_id,
            inbox_row["title"],
            inbox_row["description"],
            inbox_row["amount_cents"],
            amount_home_cents,
            transaction_type,
            inbox_row["date"],
            inbox_row["account_id"],
            inbox_row["category_id"],
            inbox_row["exchange_rate"],
            inbox_row["id"],
        )

        txn_response = transaction_from_row(txn_row)

        # Update account balance via the shared helper so the
        # expense/income sign matrix stays in one place.
        await apply_balance(
            conn,
            str(inbox_row["account_id"]),
            user_id,
            inbox_row["amount_cents"],
            transaction_type,
        )

        # Activity log: transaction created
        await write_activity_log(
            conn, user_id, "transaction", str(txn_row["id"]), ActivityAction.CREATED,
            after_snapshot=txn_response,
        )

    # 5. Shared cleanup: soft-delete inbox row with status = 2 (PROMOTED)
    # This is NOT a plain soft_delete() because it also sets status = 2.
    inbox_after_row = await conn.fetchrow(
        """
        UPDATE expense_transaction_inbox
        SET status = 2, deleted_at = now(), updated_at = now(), version = version + 1
        WHERE id = $1 AND user_id = $2
        RETURNING *
        """,
        inbox_id,
        user_id,
    )
    inbox_after = inbox_from_row(inbox_after_row)

    await write_activity_log(
        conn, user_id, "inbox", inbox_id, ActivityAction.DELETED,
        before_snapshot=inbox_before,
        after_snapshot=inbox_after,
    )

    return txn_response
