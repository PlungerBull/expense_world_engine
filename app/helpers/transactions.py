"""Transaction domain logic.

Service-layer functions for expense_transactions, called from routers/transactions.py.
Routers stay thin (HTTP glue + idempotency) and delegate business logic here.

This module is the most complex service in the codebase because transactions
intersect with every other domain:

  * account balances (via helpers.balance)
  * exchange rates (via helpers.exchange_rate)
  * hashtag junction rows (via the private ``_sync_hashtags``)
  * transfer pair atomicity (via helpers.transfers.create_transfer_pair)
  * reconciliation field-locking and cascade unassignment

## Transaction boundaries and locks

Like every other helper, these functions do NOT open their own
``conn.transaction()`` — callers own transaction boundaries. The
``FOR UPDATE`` locks in ``update_transaction`` and ``delete_transaction``
acquire row-level locks on the transaction row so that a concurrent
modification can't change ``amount_cents`` between our read and our
balance reversal. Those locks release when the caller's transaction
commits — which is why the lock acquisition MUST stay inside this
service call (not split across service and caller).

## "No-split zones"

Several flows are flagged as tight atomic units that must not be
decomposed further:

  * ``create_transfer_pair`` (12-step transfer orchestration — stays
    intact in ``app.helpers.transfers``)
  * The balance-delta accumulation loop in ``create_batch`` — extracting
    per-item balance writes would break the "K UPDATEs for N items" optimisation
  * The dynamic field-mutation chain in ``update_transaction`` — each
    conditional depends on whether specific keys are present in ``fields``
"""

from typing import Optional

import asyncpg

from app.constants import ActivityAction, ReconciliationStatus, TransactionType
from app.errors import conflict, not_found, validation_error
from app.helpers.activity_log import write_activity_log
from app.helpers.balance import apply_balance, reverse_balance
from app.helpers.exchange_rate import lookup_exchange_rate
from app.helpers.query_builder import dynamic_update
from app.helpers.validation import validate_active_account, validate_active_category
from app.schemas.transactions import (
    TransactionBatchRequest,
    TransactionCreateRequest,
    TransactionUpdateRequest,
    infer_transaction_type,
    transaction_from_row,
)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

async def _sync_hashtags(
    conn: asyncpg.Connection,
    transaction_id: str,
    user_id: str,
    hashtag_ids: Optional[list[str]],
) -> None:
    """Validate hashtags and replace junction rows for a ledger transaction.

    Uses ``transaction_source = 1`` to identify ledger junction rows
    (the source of the junction row, not the parent table). Soft-deletes
    all existing rows for this transaction first, then inserts the new
    set. Idempotent as long as ``hashtag_ids`` is the desired final state.

    Activity log — deliberate aggregation exception: junction rows are
    mutated here (soft-delete + insert) without per-row ``activity_log``
    entries. Instead, the parent transaction's UPDATED snapshot carries
    the new ``hashtag_ids`` list, so the change IS captured — just at
    parent-row granularity, not per link. This trade is intentional:
    per-link entries would multiply the audit row count by the average
    number of hashtags per transaction without adding answerable audit
    questions. If a future forensic need emerges ("when exactly was
    hashtag X unlinked from transaction Y?"), revisit this choice.
    """
    if hashtag_ids:
        # Archived hashtags are filtered here too — same parity rule that
        # applies to accounts and categories. An archived row is "retired,
        # do not attach", not "hidden in pickers but still wireable".
        valid = await conn.fetch(
            """
            SELECT id FROM expense_hashtags
            WHERE id = ANY($1::uuid[])
              AND user_id = $2
              AND deleted_at IS NULL
              AND is_archived = false
            """,
            hashtag_ids,
            user_id,
        )
        valid_ids = {str(r["id"]) for r in valid}
        invalid = [h for h in hashtag_ids if h not in valid_ids]
        if invalid:
            raise validation_error(
                "Some hashtag IDs are invalid.",
                {"hashtag_ids": f"Invalid IDs: {', '.join(invalid)}"},
            )

    # Soft-delete existing junction rows
    await conn.execute(
        """
        UPDATE expense_transaction_hashtags
        SET deleted_at = now(), updated_at = now(), version = version + 1
        WHERE transaction_id = $1 AND transaction_source = 1 AND user_id = $2 AND deleted_at IS NULL
        """,
        transaction_id,
        user_id,
    )

    # Insert new rows in a single statement. Previously this was a
    # per-hashtag INSERT loop — a transaction with 50 hashtags fired 50
    # round-trips. Unnesting an array lets us batch to one round-trip
    # regardless of count.
    if hashtag_ids:
        await conn.execute(
            """
            INSERT INTO expense_transaction_hashtags
                (transaction_id, transaction_source, hashtag_id, user_id, created_at, updated_at)
            SELECT $1, 1, hashtag_id, $2, now(), now()
            FROM unnest($3::uuid[]) AS hashtag_id
            """,
            transaction_id,
            user_id,
            hashtag_ids,
        )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

async def create_transaction(
    conn: asyncpg.Connection,
    user_id: str,
    body: TransactionCreateRequest,
) -> dict:
    """Create a transaction (either a normal ledger entry or a transfer pair).

    Branches on ``body.transfer`` — if present, delegates to
    ``create_transfer_pair`` (which handles the dual-insert + dual-balance
    update atomically) and then syncs hashtags on the primary leg.

    Otherwise validates account/category existence, infers
    ``transaction_type`` from the sign of ``amount_cents``, looks up the
    exchange rate if not provided, inserts the row, applies the balance
    delta, syncs hashtags, and writes an activity log entry.

    Raises:
        validation_error: any field validation or referential check fails.
    """
    # Validate shared fields — collect all failures
    errors: dict = {}

    if not body.title or not body.title.strip():
        errors["title"] = "Must not be empty."

    if body.amount_cents == 0:
        errors["amount_cents"] = "Must not be zero."

    # Date must be <= now() — use DB clock so we don't drift with app-server clock skew
    now = await conn.fetchval("SELECT now()")
    if body.date > now:
        errors["date"] = "Must not be in the future."

    if errors:
        raise validation_error("Transaction validation failed.", errors)

    # ----- Transfer branch -----
    if body.transfer is not None:
        # Imported lazily to avoid a circular import: transfers.py itself
        # imports transaction_from_row from schemas, not from this module,
        # but keeping the import local makes the dependency obvious.
        from app.helpers.transfers import create_transfer_pair

        primary_response, _sibling = await create_transfer_pair(
            conn=conn,
            user_id=user_id,
            primary_id=body.id,
            sibling_id=body.transfer.id,
            primary_title=body.title.strip(),
            primary_description=body.description,
            primary_amount_cents=body.amount_cents,
            primary_account_id=body.account_id,
            primary_date=body.date,
            primary_exchange_rate=body.exchange_rate,
            primary_cleared=body.cleared if body.cleared is not None else False,
            transfer_account_id=body.transfer.account_id,
            transfer_amount_cents=body.transfer.amount_cents,
        )

        # Hashtags on primary only
        if body.hashtag_ids:
            await _sync_hashtags(conn, primary_response["id"], user_id, body.hashtag_ids)

        return primary_response

    # ----- Normal (non-transfer) branch -----

    # Validate account and category via shared helpers. These raise
    # AppError on failure, which the router surfaces as a 422 — matches
    # the prior inline behaviour, just with a consistent top-level
    # message ("Account validation failed." / "Category validation
    # failed.") instead of the previous "Transaction validation failed."
    # The field-level error remains the authoritative signal for clients.
    await validate_active_account(conn, body.account_id, user_id)
    await validate_active_category(conn, body.category_id, user_id)

    # Infer transaction_type and normalize amount to positive storage form
    transaction_type = infer_transaction_type(body.amount_cents)
    amount_cents = abs(body.amount_cents)

    # Exchange rate — use caller override or fetch from rate table
    exchange_rate = body.exchange_rate
    if exchange_rate is None:
        exchange_rate = await lookup_exchange_rate(conn, body.account_id, body.date, user_id)
    amount_home_cents = round(amount_cents * exchange_rate)

    # Insert
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO expense_transactions
                (id, user_id, title, description, amount_cents, amount_home_cents,
                 transaction_type, date, account_id, category_id, exchange_rate,
                 cleared, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, now(), now())
            RETURNING *
            """,
            body.id,
            user_id,
            body.title.strip(),
            body.description,
            amount_cents,
            amount_home_cents,
            transaction_type,
            body.date,
            body.account_id,
            body.category_id,
            exchange_rate,
            body.cleared if body.cleared is not None else False,
        )
    except asyncpg.UniqueViolationError:
        raise conflict(f"A transaction with id '{body.id}' already exists.")

    response = transaction_from_row(row)

    # Update account balance
    await apply_balance(conn, body.account_id, user_id, amount_cents, transaction_type)

    # Hashtags
    if body.hashtag_ids:
        await _sync_hashtags(conn, str(row["id"]), user_id, body.hashtag_ids)

    # Activity log
    await write_activity_log(
        conn, user_id, "transaction", str(row["id"]), ActivityAction.CREATED,
        after_snapshot=response,
    )

    return response


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

async def update_transaction(
    conn: asyncpg.Connection,
    user_id: str,
    transaction_id: str,
    fields: dict,
    hashtag_ids: Optional[list[str]],
    recon_id_provided: bool,
    recon_id_value: Optional[str],
) -> dict:
    """Apply a partial update to a transaction.

    This is the most intricate service function in the codebase. The
    ``fields`` dict is mutated in place as derived columns (``transaction_type``,
    ``amount_home_cents``, re-fetched ``exchange_rate``) are computed from
    the requested changes. Balance reversal + re-apply happens in the
    middle of the flow so the account balance reflects the new state
    before the final dynamic UPDATE runs.

    Row-level lock: the initial ``before_row`` fetch uses ``FOR UPDATE``
    so a concurrent update can't change ``amount_cents`` between our read
    and the balance reversal. This lock MUST live inside this function
    (not the caller) so it stays within the same transaction scope as the
    subsequent mutations — otherwise the lock would be released prematurely.

    Reconciliation field-locking: if the transaction is assigned to a
    completed reconciliation, certain fields are immutable and the
    service raises 422 rather than silently dropping them.

    Transfer edit guard: if this transaction is part of a transfer pair,
    ``amount_cents`` and ``account_id`` changes are rejected (transfers
    are edited by deleting and recreating).

    Args:
        fields: columns to update, after ``hashtag_ids`` and
            ``reconciliation_id`` have been removed by the caller.
        hashtag_ids: if not None, replaces the set of linked hashtags.
            Use an empty list to clear, None to leave unchanged.
        recon_id_provided: True if the caller explicitly sent
            ``reconciliation_id`` in the body (even as null — this is how
            clients unassign). Distinguishes "omitted" from "set to null".
        recon_id_value: the assigned value (may be None for unassign).
    """
    # Empty update — return current state unchanged
    if not fields and hashtag_ids is None and not recon_id_provided:
        row = await conn.fetchrow(
            "SELECT * FROM expense_transactions WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
            transaction_id,
            user_id,
        )
        if row is None:
            raise not_found("transaction")
        return transaction_from_row(row)

    # Fetch before-state under a row-level lock. Without FOR UPDATE a
    # concurrent update could change `amount_cents` between our read
    # and our balance reversal below, causing a lost-update and
    # silently corrupting the account balance. The lock is released
    # automatically when the surrounding transaction commits.
    before_row = await conn.fetchrow(
        "SELECT * FROM expense_transactions WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL FOR UPDATE",
        transaction_id,
        user_id,
    )
    if before_row is None:
        raise not_found("transaction")

    before = transaction_from_row(before_row)

    # Field locking check — reconciliation completed
    if before_row["reconciliation_id"] is not None:
        recon = await conn.fetchrow(
            "SELECT status FROM expense_reconciliations WHERE id = $1",
            before_row["reconciliation_id"],
        )
        if recon and recon["status"] == 2:
            locked = {"amount_cents", "account_id", "title", "date"}
            attempted = locked & fields.keys()
            if attempted:
                raise validation_error(
                    "Transaction belongs to a completed reconciliation. These fields are locked.",
                    {f: "Locked by completed reconciliation." for f in attempted},
                )

    # Validate reconciliation_id assignment
    if recon_id_provided and recon_id_value is not None:
        recon = await conn.fetchrow(
            """
            SELECT id, account_id, status FROM expense_reconciliations
            WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
            """,
            recon_id_value,
            user_id,
        )
        if recon is None:
            raise validation_error(
                "Reconciliation validation failed.",
                {"reconciliation_id": "Must reference an active reconciliation."},
            )
        effective_account_id = fields.get("account_id") or str(before_row["account_id"])
        if str(recon["account_id"]) != effective_account_id:
            raise validation_error(
                "Reconciliation validation failed.",
                {"reconciliation_id": "Reconciliation account does not match transaction account."},
            )
        if recon["status"] == 2:
            raise validation_error(
                "Reconciliation validation failed.",
                {"reconciliation_id": "Cannot assign transactions to a completed reconciliation."},
            )

    # Transfer edit guard — reject changes that would desync the pair.
    # date / exchange_rate / amount_home_cents are blocked because this
    # PUT path mutates only the edited leg: a date change re-fetches the
    # rate for this leg but leaves the sibling on its original rate, so
    # the pair stops netting to zero in home currency (and lands on two
    # different days in the ledger). Transfers are edited by delete +
    # recreate.
    if before_row["transfer_transaction_id"] is not None:
        blocked = {
            "amount_cents", "account_id",
            "date", "exchange_rate", "amount_home_cents",
        } & fields.keys()
        if blocked:
            raise validation_error(
                "Transfer edits not yet supported.",
                {f: "Cannot modify on a transfer transaction." for f in blocked},
            )

    # Track whether balance needs updating
    needs_balance_update = False

    # Process amount_cents change
    if "amount_cents" in fields:
        if fields["amount_cents"] == 0:
            raise validation_error(
                "amount_cents must not be zero.",
                {"amount_cents": "Must not be zero."},
            )
        fields["transaction_type"] = infer_transaction_type(fields["amount_cents"])
        fields["amount_cents"] = abs(fields["amount_cents"])
        needs_balance_update = True

    # Process date change — re-fetch exchange rate (unless user provided one)
    if "date" in fields and "exchange_rate" not in fields:
        effective_account_id = fields.get("account_id") or str(before_row["account_id"])
        new_rate = await lookup_exchange_rate(conn, effective_account_id, fields["date"], user_id)
        fields["exchange_rate"] = new_rate

    # Recalculate amount_home_cents when amount or exchange_rate changes
    if "amount_cents" in fields or "exchange_rate" in fields:
        effective_amount = fields.get("amount_cents", before_row["amount_cents"])
        effective_rate = fields.get("exchange_rate", float(before_row["exchange_rate"]))
        fields["amount_home_cents"] = round(effective_amount * effective_rate)

    # Validate new account_id if changing
    if "account_id" in fields:
        await validate_active_account(conn, fields["account_id"], user_id)
        needs_balance_update = True

    # Validate new category_id if changing
    if "category_id" in fields:
        await validate_active_category(conn, fields["category_id"], user_id)

    # Validate title if changing
    if "title" in fields and (not fields["title"] or not fields["title"].strip()):
        raise validation_error(
            "Title validation failed.",
            {"title": "Must not be empty."},
        )
    if "title" in fields:
        fields["title"] = fields["title"].strip()

    # Validate date if changing
    if "date" in fields:
        now = await conn.fetchval("SELECT now()")
        if fields["date"] > now:
            raise validation_error(
                "Date validation failed.",
                {"date": "Must not be in the future."},
            )

    # Balance update: reverse old, apply new
    if needs_balance_update:
        await reverse_balance(
            conn, str(before_row["account_id"]), user_id,
            before_row["amount_cents"], before_row["transaction_type"],
            transfer_direction=before_row["transfer_direction"],
        )
        effective_account_id = fields.get("account_id") or str(before_row["account_id"])
        effective_amount = fields.get("amount_cents", before_row["amount_cents"])
        effective_type = fields.get("transaction_type", before_row["transaction_type"])
        await apply_balance(
            conn, effective_account_id, user_id,
            effective_amount, effective_type,
            transfer_direction=before_row["transfer_direction"],
        )

    if fields:
        after_row = await dynamic_update(conn, "expense_transactions", fields, transaction_id, user_id)
        if after_row is None:
            raise not_found("transaction")
    else:
        # Only hashtag changes, no column updates — still bump version
        after_row = await conn.fetchrow(
            """
            UPDATE expense_transactions
            SET updated_at = now(), version = version + 1
            WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
            RETURNING *
            """,
            transaction_id,
            user_id,
        )
        if after_row is None:
            raise not_found("transaction")

    # Sync hashtags if provided
    if hashtag_ids is not None:
        await _sync_hashtags(conn, transaction_id, user_id, hashtag_ids)

    # Apply reconciliation_id change
    if recon_id_provided:
        after_row = await conn.fetchrow(
            """
            UPDATE expense_transactions
            SET reconciliation_id = $1, updated_at = now(), version = version + 1
            WHERE id = $2 AND user_id = $3 AND deleted_at IS NULL
            RETURNING *
            """,
            recon_id_value,
            transaction_id,
            user_id,
        )

    after = transaction_from_row(after_row)

    # Activity log
    await write_activity_log(
        conn, user_id, "transaction", transaction_id, ActivityAction.UPDATED,
        before_snapshot=before,
        after_snapshot=after,
    )

    return after


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

async def delete_transaction(
    conn: asyncpg.Connection,
    user_id: str,
    transaction_id: str,
) -> dict:
    """Soft-delete a transaction, reverse its balance, and cascade the transfer sibling.

    If the target transaction is part of a transfer pair, the sibling is
    also soft-deleted and its balance is also reversed (the whole pair
    disappears atomically, which matches the invariant that transfer
    pairs are never orphaned).

    If the transaction was assigned to a completed reconciliation, the
    response is augmented with a ``warning`` field so clients can surface
    that the reconciliation totals may now be stale.

    Both the primary and the sibling (if any) are locked with
    ``FOR UPDATE`` before their balance is reversed — same hazard as
    ``update_transaction``, same mitigation.
    """
    # Fetch under a row-level lock. Previously this fetch lived
    # outside the transaction, so a concurrent update could change
    # `amount_cents` before we reversed the balance, causing a
    # lost-update and silently corrupting the account balance.
    row = await conn.fetchrow(
        "SELECT * FROM expense_transactions WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL FOR UPDATE",
        transaction_id,
        user_id,
    )
    if row is None:
        raise not_found("transaction")

    before = transaction_from_row(row)

    # Soft-delete
    after_row = await conn.fetchrow(
        """
        UPDATE expense_transactions
        SET deleted_at = now(), updated_at = now(), version = version + 1
        WHERE id = $1 AND user_id = $2
        RETURNING *
        """,
        transaction_id,
        user_id,
    )
    after = transaction_from_row(after_row)

    # Reverse balance
    await reverse_balance(
        conn, str(row["account_id"]), user_id,
        row["amount_cents"], row["transaction_type"],
        transfer_direction=row["transfer_direction"],
    )

    # Soft-delete junction rows
    await conn.execute(
        """
        UPDATE expense_transaction_hashtags
        SET deleted_at = now(), updated_at = now(), version = version + 1
        WHERE transaction_id = $1 AND transaction_source = 1 AND user_id = $2 AND deleted_at IS NULL
        """,
        transaction_id,
        user_id,
    )

    # Handle transfer sibling — also lock the sibling row so its
    # amount_cents can't change between our read and the reversal.
    if row["transfer_transaction_id"] is not None:
        sibling_id = str(row["transfer_transaction_id"])
        sibling_row = await conn.fetchrow(
            "SELECT * FROM expense_transactions WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL FOR UPDATE",
            sibling_id,
            user_id,
        )
        if sibling_row is not None:
            sibling_before = transaction_from_row(sibling_row)

            sibling_after_row = await conn.fetchrow(
                """
                UPDATE expense_transactions
                SET deleted_at = now(), updated_at = now(), version = version + 1
                WHERE id = $1 AND user_id = $2
                RETURNING *
                """,
                sibling_id,
                user_id,
            )
            sibling_after = transaction_from_row(sibling_after_row)

            await reverse_balance(
                conn, str(sibling_row["account_id"]), user_id,
                sibling_row["amount_cents"], sibling_row["transaction_type"],
                transfer_direction=sibling_row["transfer_direction"],
            )

            await conn.execute(
                """
                UPDATE expense_transaction_hashtags
                SET deleted_at = now(), updated_at = now(), version = version + 1
                WHERE transaction_id = $1 AND transaction_source = 1 AND user_id = $2 AND deleted_at IS NULL
                """,
                sibling_id,
                user_id,
            )

            await write_activity_log(
                conn, user_id, "transaction", sibling_id, ActivityAction.DELETED,
                before_snapshot=sibling_before,
                after_snapshot=sibling_after,
            )

    # Activity log for primary transaction
    await write_activity_log(
        conn, user_id, "transaction", transaction_id, ActivityAction.DELETED,
        before_snapshot=before,
        after_snapshot=after,
    )

    # Warnings channel — always present (null-over-omission). Currently emits
    # one value when the deleted row belonged to a completed reconciliation,
    # but the list shape leaves room for future additions without changing
    # the response contract.
    warnings: list[str] = []
    if row["reconciliation_id"] is not None:
        recon = await conn.fetchrow(
            "SELECT status FROM expense_reconciliations WHERE id = $1",
            row["reconciliation_id"],
        )
        if recon and recon["status"] == 2:
            warnings.append(
                "Transaction belonged to a completed reconciliation. "
                "Reconciliation totals may be stale."
            )

    return {**after, "warnings": warnings}


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

async def restore_transaction(
    conn: asyncpg.Connection,
    user_id: str,
    transaction_id: str,
) -> dict:
    """Undo a soft-delete on a transaction, atomically with its sibling.

    Inverse of ``delete_transaction``. Re-applies the balance impact,
    re-activates the cascaded hashtag junction rows (matched by exact
    ``deleted_at`` timestamp), and cascades to the transfer sibling for
    transfer pairs. The whole flow is atomic — caller owns
    ``conn.transaction()``.

    **Reconciliation handling (per leg).** The transaction's
    ``reconciliation_id`` survived the delete on the soft-deleted row.
    On restore the link is conditionally cleared:

      * recon is null                         → no action
      * recon is missing or soft-deleted      → unlink, emit warning
      * recon ``status = COMPLETED``          → unlink, emit warning
      * recon is DRAFT and active             → keep the link

    The COMPLETED case must unlink because completed reconciliations
    lock four fields (``amount_cents``, ``account_id``, ``title``,
    ``date``) on assigned transactions. Silently re-linking would
    leave the restored row immutable, which the user wouldn't expect.
    The DRAFT-and-active case is the user's good-path expectation —
    they were reconciling, deleted by mistake, and want the row back
    in the same batch without a re-assignment ceremony.

    **Junction rows.** Restored precisely: ``WHERE deleted_at = $marker``
    with ``$marker`` bound to the parent's pre-restore ``deleted_at``.
    Because Postgres ``now()`` returns ``transaction_timestamp()`` (one
    value per DB transaction), the cascade UPDATE inside ``delete_transaction``
    set the junctions to the same timestamp as the parent. Exact match
    catches only those rows, not pre-existing soft-deleted junctions
    from earlier ``_sync_hashtags`` runs.

    This intentionally differs from ``restore_hashtag`` /
    ``restore_reconciliation`` which both opt NOT to cascade-restore.
    The asymmetry is correct: hashtag-restore would silently re-tag
    dozens of transactions (high blast radius), but transaction-restore
    re-tags ONE transaction and matches the user's "undo the delete"
    mental model.

    Validation runs BEFORE any mutation, so a 422 leaves the soft-deleted
    state untouched.

    Raises:
        not_found: no soft-deleted transaction with that id.
        conflict: the row is part of a transfer pair but the sibling is
            missing or no longer soft-deleted (integrity break — refuse
            to restore an asymmetric pair).
        validation_error: account/category (or sibling's) is no longer
            active or non-archived. All field-level errors collected
            into one ``fields`` dict before raising.
    """
    # 1. Lock the soft-deleted primary row.
    row = await conn.fetchrow(
        """
        SELECT * FROM expense_transactions
        WHERE id = $1 AND user_id = $2 AND deleted_at IS NOT NULL FOR UPDATE
        """,
        transaction_id,
        user_id,
    )
    if row is None:
        raise not_found("transaction")

    is_transfer = row["transfer_transaction_id"] is not None

    # 2. Lock the sibling (transfer case). Both must be soft-deleted —
    #    refuse to restore one leg of a half-deleted pair, which would
    #    be an integrity violation on the transfer invariant.
    sibling_row = None
    sibling_id: Optional[str] = None
    if is_transfer:
        sibling_id = str(row["transfer_transaction_id"])
        sibling_row = await conn.fetchrow(
            """
            SELECT * FROM expense_transactions
            WHERE id = $1 AND user_id = $2 AND deleted_at IS NOT NULL FOR UPDATE
            """,
            sibling_id,
            user_id,
        )
        if sibling_row is None:
            raise conflict(
                "Transfer sibling row could not be located in a soft-deleted "
                "state. Refusing to restore one leg of an asymmetric pair."
            )

    # 3. Validate prerequisites (collect-all-failures pattern).
    errors: dict = {}

    primary_account = await conn.fetchrow(
        """
        SELECT id FROM expense_bank_accounts
        WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL AND is_archived = false
        """,
        row["account_id"],
        user_id,
    )
    if primary_account is None:
        errors["account_id"] = "Must reference an active, non-archived account."

    primary_category = await conn.fetchrow(
        """
        SELECT id FROM expense_categories
        WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL AND is_archived = false
        """,
        row["category_id"],
        user_id,
    )
    if primary_category is None:
        errors["category_id"] = "Must reference an active, non-archived category."

    if is_transfer and sibling_row is not None:
        sibling_account = await conn.fetchrow(
            """
            SELECT id FROM expense_bank_accounts
            WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL AND is_archived = false
            """,
            sibling_row["account_id"],
            user_id,
        )
        if sibling_account is None:
            errors["transfer.account_id"] = "Must reference an active, non-archived account."

        sibling_category = await conn.fetchrow(
            """
            SELECT id FROM expense_categories
            WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL AND is_archived = false
            """,
            sibling_row["category_id"],
            user_id,
        )
        if sibling_category is None:
            errors["transfer.category_id"] = "Must reference an active, non-archived category."

    if errors:
        raise validation_error(
            "Cannot restore transaction: prerequisites failed.", errors
        )

    # 4. Resolve the reconciliation decision per leg.
    async def _resolve_recon_unlink(recon_id) -> tuple[bool, Optional[str]]:
        if recon_id is None:
            return False, None
        recon = await conn.fetchrow(
            "SELECT status, deleted_at FROM expense_reconciliations WHERE id = $1",
            recon_id,
        )
        if recon is None or recon["deleted_at"] is not None:
            return True, (
                "Transaction's previous reconciliation no longer exists. "
                "Link removed on restore."
            )
        if recon["status"] == ReconciliationStatus.COMPLETED:
            return True, (
                "Transaction's previous reconciliation is completed. "
                "Link removed on restore — reassign manually if needed."
            )
        return False, None

    primary_unlink, primary_warning = await _resolve_recon_unlink(row["reconciliation_id"])
    sibling_unlink = False
    sibling_warning: Optional[str] = None
    if is_transfer and sibling_row is not None:
        sibling_unlink, sibling_warning = await _resolve_recon_unlink(
            sibling_row["reconciliation_id"]
        )

    # 5. Restore primary row (conditionally clearing reconciliation_id).
    before = transaction_from_row(row)
    primary_deleted_at = row["deleted_at"]

    if primary_unlink:
        after_row = await conn.fetchrow(
            """
            UPDATE expense_transactions
            SET deleted_at = NULL, reconciliation_id = NULL,
                updated_at = now(), version = version + 1
            WHERE id = $1 AND user_id = $2
            RETURNING *
            """,
            transaction_id,
            user_id,
        )
    else:
        after_row = await conn.fetchrow(
            """
            UPDATE expense_transactions
            SET deleted_at = NULL, updated_at = now(), version = version + 1
            WHERE id = $1 AND user_id = $2
            RETURNING *
            """,
            transaction_id,
            user_id,
        )
    after = transaction_from_row(after_row)

    # 6. Re-apply primary balance.
    await apply_balance(
        conn, str(row["account_id"]), user_id,
        row["amount_cents"], row["transaction_type"],
        transfer_direction=row["transfer_direction"],
    )

    # 7. Re-activate cascaded junction rows on the primary.
    await conn.execute(
        """
        UPDATE expense_transaction_hashtags
        SET deleted_at = NULL, updated_at = now(), version = version + 1
        WHERE transaction_id = $1 AND transaction_source = 1
          AND user_id = $2 AND deleted_at = $3
        """,
        transaction_id,
        user_id,
        primary_deleted_at,
    )

    # 8. Sibling cascade (mirror steps 5-7).
    if is_transfer and sibling_row is not None and sibling_id is not None:
        sibling_before = transaction_from_row(sibling_row)
        sibling_deleted_at = sibling_row["deleted_at"]

        if sibling_unlink:
            sibling_after_row = await conn.fetchrow(
                """
                UPDATE expense_transactions
                SET deleted_at = NULL, reconciliation_id = NULL,
                    updated_at = now(), version = version + 1
                WHERE id = $1 AND user_id = $2
                RETURNING *
                """,
                sibling_id,
                user_id,
            )
        else:
            sibling_after_row = await conn.fetchrow(
                """
                UPDATE expense_transactions
                SET deleted_at = NULL, updated_at = now(), version = version + 1
                WHERE id = $1 AND user_id = $2
                RETURNING *
                """,
                sibling_id,
                user_id,
            )
        sibling_after = transaction_from_row(sibling_after_row)

        await apply_balance(
            conn, str(sibling_row["account_id"]), user_id,
            sibling_row["amount_cents"], sibling_row["transaction_type"],
            transfer_direction=sibling_row["transfer_direction"],
        )

        await conn.execute(
            """
            UPDATE expense_transaction_hashtags
            SET deleted_at = NULL, updated_at = now(), version = version + 1
            WHERE transaction_id = $1 AND transaction_source = 1
              AND user_id = $2 AND deleted_at = $3
            """,
            sibling_id,
            user_id,
            sibling_deleted_at,
        )

        # Sibling activity log first (matches delete_transaction's order).
        await write_activity_log(
            conn, user_id, "transaction", sibling_id, ActivityAction.RESTORED,
            before_snapshot=sibling_before,
            after_snapshot=sibling_after,
        )

    # 9. Primary activity log.
    await write_activity_log(
        conn, user_id, "transaction", transaction_id, ActivityAction.RESTORED,
        before_snapshot=before,
        after_snapshot=after,
    )

    # 10. Build warnings list (always present; empty when restore is clean).
    warnings: list[str] = []
    if primary_warning is not None:
        warnings.append(primary_warning)
    if sibling_warning is not None:
        warnings.append("Transfer sibling: " + sibling_warning)

    return {**after, "warnings": warnings}


# ---------------------------------------------------------------------------
# Batch create
# ---------------------------------------------------------------------------

async def create_batch(
    conn: asyncpg.Connection,
    user_id: str,
    body: TransactionBatchRequest,
) -> dict:
    """Atomic batch create.

    Validates the entire batch first (collects per-item errors and fails
    fast if any), then inserts all rows and applies balance deltas as a
    single dict-aggregated update per account. This is a "no-split zone"
    — the balance-delta accumulation and per-item INSERT must stay in a
    single loop or the optimisation (K UPDATEs for N items, where K is
    distinct accounts) is lost.

    Transfers are NOT supported in batch creates — they're rejected at
    the validation phase with a clear error. Transfers require the full
    ``create_transfer_pair`` orchestration which doesn't compose cleanly
    with the batch's delta-accumulation model.

    Returns a dict ``{"created": list[dict]}`` — the caller wraps this
    in a JSONResponse with status 201.
    """
    if not body.transactions:
        raise validation_error(
            "Batch must contain at least one transaction.",
            {"transactions": "Must not be empty."},
        )

    # Transfers are not supported in batch creates
    for i, item in enumerate(body.transactions):
        if item.transfer is not None:
            raise validation_error(
                "Transfers are not supported in batch creates.",
                {f"transactions[{i}].transfer": "Must not be present in batch."},
            )

    now = await conn.fetchval("SELECT now()")

    # Pre-validate all items. Account and category existence checks
    # are vectorised: instead of firing 2 queries per item (2N total),
    # we collect the distinct IDs referenced across the whole batch and
    # validate them in 2 queries. Membership is then checked in memory.
    # A 100-item batch drops from 200 validation queries to 2.
    requested_account_ids = {item.account_id for item in body.transactions}
    requested_category_ids = {item.category_id for item in body.transactions}

    valid_account_rows = await conn.fetch(
        """
        SELECT id FROM expense_bank_accounts
        WHERE id = ANY($1::uuid[])
          AND user_id = $2
          AND deleted_at IS NULL
          AND is_archived = false
        """,
        list(requested_account_ids),
        user_id,
    )
    valid_account_ids = {str(r["id"]) for r in valid_account_rows}

    valid_category_rows = await conn.fetch(
        """
        SELECT id FROM expense_categories
        WHERE id = ANY($1::uuid[])
          AND user_id = $2
          AND deleted_at IS NULL
          AND is_archived = false
        """,
        list(requested_category_ids),
        user_id,
    )
    valid_category_ids = {str(r["id"]) for r in valid_category_rows}

    all_errors = []
    seen_ids: set[str] = set()
    for i, item in enumerate(body.transactions):
        item_errors: dict = {}

        if not item.title or not item.title.strip():
            item_errors["title"] = "Must not be empty."
        if item.amount_cents == 0:
            item_errors["amount_cents"] = "Must not be zero."
        if item.date > now:
            item_errors["date"] = "Must not be in the future."

        if item.account_id not in valid_account_ids:
            item_errors["account_id"] = "Must reference an active, non-archived account."

        if item.category_id not in valid_category_ids:
            item_errors["category_id"] = "Must reference an active, non-archived category."

        item_id_str = str(item.id)
        if item_id_str in seen_ids:
            item_errors["id"] = "Duplicate id within batch."
        seen_ids.add(item_id_str)

        if item_errors:
            all_errors.append({"index": i, "fields": item_errors})

    if all_errors:
        raise validation_error(
            "Batch validation failed.",
            {"items": all_errors},
        )

    # Process all items — accumulate balance deltas
    created = []
    balance_deltas: dict[str, int] = {}

    for item in body.transactions:
        transaction_type = infer_transaction_type(item.amount_cents)
        amount_cents = abs(item.amount_cents)

        exchange_rate = item.exchange_rate
        if exchange_rate is None:
            exchange_rate = await lookup_exchange_rate(conn, item.account_id, item.date, user_id)
        amount_home_cents = round(amount_cents * exchange_rate)

        try:
            row = await conn.fetchrow(
                """
                INSERT INTO expense_transactions
                    (id, user_id, title, description, amount_cents, amount_home_cents,
                     transaction_type, date, account_id, category_id, exchange_rate,
                     cleared, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, now(), now())
                RETURNING *
                """,
                item.id,
                user_id,
                item.title.strip(),
                item.description,
                amount_cents,
                amount_home_cents,
                transaction_type,
                item.date,
                item.account_id,
                item.category_id,
                exchange_rate,
                item.cleared if item.cleared is not None else False,
            )
        except asyncpg.UniqueViolationError:
            raise conflict(f"A transaction with id '{item.id}' already exists.")

        response = transaction_from_row(row)
        created.append(response)

        # Accumulate balance delta. Batch rejects transfers above, so
        # we only need the expense/income branch of the sign matrix;
        # this is a deliberate simplification vs ``apply_balance`` which
        # handles the full matrix but would require K individual
        # UPDATEs instead of the optimised accumulate-then-apply.
        if transaction_type == TransactionType.EXPENSE:
            delta = -amount_cents
        else:
            delta = amount_cents
        balance_deltas[item.account_id] = balance_deltas.get(item.account_id, 0) + delta

        # Hashtags
        if item.hashtag_ids:
            await _sync_hashtags(conn, str(row["id"]), user_id, item.hashtag_ids)

        # Activity log
        await write_activity_log(
            conn, user_id, "transaction", str(row["id"]), ActivityAction.CREATED,
            after_snapshot=response,
        )

    # Apply accumulated balance deltas — one UPDATE per distinct account
    for acct_id, delta in balance_deltas.items():
        await conn.execute(
            """
            UPDATE expense_bank_accounts
            SET current_balance_cents = current_balance_cents + $1,
                updated_at = now(), version = version + 1
            WHERE id = $2 AND user_id = $3
            """,
            delta,
            acct_id,
            user_id,
        )

    return {"created": created}
