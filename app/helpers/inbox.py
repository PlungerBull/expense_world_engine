"""Inbox domain logic.

Service-layer functions for expense_transaction_inbox, called from
routers/inbox.py. Routers stay thin (HTTP glue + idempotency) and
delegate business logic here.

## Promote flow (the interesting one)

``promote_inbox_item`` branches on whether the inbox row has transfer
fields set:

  * Transfer branch: delegates to ``create_transfer_pair``, which handles the
    zero-sum validation, the category assignment and the dual insert.
  * Non-transfer branch: inserts a single expense_transactions row.

Neither branch touches an account balance. Balances are the signed sum of the
ledger (sql/022), so writing the row IS the balance change.

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
from uuid import UUID

import asyncpg

from app.constants import ActivityAction, InboxStatus, TransactionType
from app.errors import conflict, not_found, validation_error
from app.helpers.activity_log import write_activity_log
from app.helpers.query_builder import (
    dynamic_update,
    fetch_owned_row_or_404,
    restore,
    soft_delete,
)
from app.helpers.transactions import attach_hashtag_ids
from app.helpers.validation import extract_update_fields
from app.schemas.inbox import InboxCreateRequest, InboxUpdateRequest, inbox_from_row
from app.schemas.transactions import (
    infer_transaction_type,
    transaction_from_row,
)


def _resolve_transfer_type(
    primary_signed: Optional[int],
    sibling_signed: int,
) -> int:
    """Derive the PRIMARY leg's ``transaction_type`` from the signed inputs.

    The inbox row *is* the primary leg, so its direction is the same field the
    ledger carries — after WP1 that is ``transaction_type`` itself, not a
    separate column. The sibling's direction is the inverse and is never
    stored.

    Rules, in order:

    1. Both amounts known and pointing the same way → ``422``. This is the check
       the old encoding could not make: the primary's sign was thrown away by
       ``abs()`` on write and re-derived at promote time as the negation of the
       sibling, so two outflows became a valid-looking transfer with one leg
       silently flipped (WP7.2, spec §546).
    2. The primary's own sign wins whenever it is known.
    3. Otherwise the primary's sign is the opposite of the sibling's — which is
       what keeps a sparse draft (a transfer whose primary amount has not been
       filled in yet) directional.

    Callers must reject zero amounts first.
    """
    if primary_signed is not None and (primary_signed > 0) == (sibling_signed > 0):
        raise validation_error(
            "Transfer validation failed.",
            {"transfer.amount_cents": "Must have opposite sign to amount_cents."},
        )
    if primary_signed is not None:
        return infer_transaction_type(primary_signed)
    return infer_transaction_type(-sibling_signed)


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

    Transfer fields (if provided) stash the sibling account and its absolute
    amount for later use when the item is promoted. The primary leg's
    direction lands in ``transaction_type`` like any other row's — a transfer
    draft is not a third kind of thing.
    """
    # Signs are consumed in this block and nowhere else. Everything below sees
    # only absolute amounts plus the encoded direction — the same contract the
    # ledger has. transaction_type is assigned exactly once, from whichever
    # sign is authoritative, so the two amounts can never each write it and
    # disagree.
    primary_signed = body.amount_cents
    if primary_signed is not None and primary_signed == 0:
        raise validation_error(
            "amount_cents must not be zero.",
            {"amount_cents": "Must not be zero."},
        )

    sibling_signed: Optional[int] = None
    transfer_account_id: Optional[str] = None
    if body.transfer is not None:
        if body.transfer.amount_cents == 0:
            raise validation_error(
                "transfer.amount_cents must not be zero.",
                {"transfer.amount_cents": "Must not be zero."},
            )
        sibling_signed = body.transfer.amount_cents
        transfer_account_id = body.transfer.account_id

    amount_cents = abs(primary_signed) if primary_signed is not None else None
    transfer_amount_cents = abs(sibling_signed) if sibling_signed is not None else None

    # Assigned exactly once. On a transfer draft the sibling's sign
    # participates (it is what keeps a draft with no primary amount
    # directional); otherwise the primary's sign is the whole answer.
    transaction_type: Optional[int] = None
    if sibling_signed is not None:
        transaction_type = _resolve_transfer_type(primary_signed, sibling_signed)
    elif primary_signed is not None:
        transaction_type = infer_transaction_type(primary_signed)

    # No rate is looked up and none is stored. This is where open bug 1.4 was:
    # the lookup fired only when BOTH account_id and date were present, and the
    # column's `DEFAULT 1.0` (plus a `COALESCE($10, 1.0)` right here) covered
    # the gap — so the ordinary capture case, a receipt with no date yet, wrote
    # rate 1.0 and a $100 draft promoted as 100 PEN cents. It failed closed when
    # it looked and found nothing, and failed open when it did not look at all.
    # sql/021 deleted the column; a draft now carries its native amount only.
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO expense_transaction_inbox
                (id, user_id, title, description, amount_cents, transaction_type,
                 date, account_id, category_id,
                 transfer_account_id, transfer_amount_cents,
                 created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now(), now())
            RETURNING *
            """,
            body.id,
            user_id,
            body.title,
            body.description,
            amount_cents,
            transaction_type,
            body.date,
            body.account_id,
            body.category_id,
            transfer_account_id,
            transfer_amount_cents,
        )
    except asyncpg.UniqueViolationError:
        raise conflict(f"An inbox item with id '{body.id}' already exists.")

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

    ``transfer`` is the one field that accepts an explicit null: sending
    ``{"transfer": null}`` clears the sibling account, its amount and the
    direction, and the draft reverts to a plain expense/income. Without it a
    draft marked as a transfer could never be un-marked — the only escape was
    deleting the row and retyping it. Every other field still rejects null with
    422 (spec: PUT fields must not be explicit-null).

    Empty updates short-circuit to a fetch-and-return — matches the prior
    router behaviour and the pattern established by other domain helpers.
    """
    fields = extract_update_fields(body, nullable={"transfer"})

    # `transfer` is not a column — pop it before the dynamic UPDATE builder sees
    # it. `transfer_given` separates "transfer: null" (clear it) from "transfer
    # omitted" (leave it alone); extract_update_fields returns only keys the
    # caller actually set, so presence in `fields` is the signal.
    transfer_given = "transfer" in fields
    transfer = fields.pop("transfer", None)

    # Empty update — return current
    if not fields and not transfer_given:
        row = await fetch_owned_row_or_404(
            conn, "expense_transaction_inbox", inbox_id, user_id, "inbox item"
        )
        return inbox_from_row(row)

    # Collect the signed inputs. As on create, signs live only in this block.
    primary_signed: Optional[int] = None
    if "amount_cents" in fields:
        if fields["amount_cents"] == 0:
            raise validation_error(
                "amount_cents must not be zero.",
                {"amount_cents": "Must not be zero."},
            )
        primary_signed = fields["amount_cents"]
        fields["amount_cents"] = abs(primary_signed)

    sibling_signed: Optional[int] = None
    if transfer is not None:
        if transfer["amount_cents"] == 0:
            raise validation_error(
                "transfer.amount_cents must not be zero.",
                {"transfer.amount_cents": "Must not be zero."},
            )
        sibling_signed = transfer["amount_cents"]

    # FOR UPDATE: the merged-state resolution below reads this row and derives
    # transaction_type from it — same lost-update hazard as the transaction
    # update path, which locks its row for the same reason.
    before_row = await fetch_owned_row_or_404(
        conn, "expense_transaction_inbox", inbox_id, user_id, "inbox item",
        for_update=True,
    )

    before = inbox_from_row(before_row)

    # Resolve direction against the MERGED state (stored row + this patch),
    # assigning transaction_type exactly once. Deriving it in one place is what
    # stops an `amount_cents` in the same request from clobbering the transfer
    # columns — previously the amount block ran last and won, leaving transfer
    # columns set on a row whose type disagreed with them.
    if transfer_given and transfer is None:
        # Explicit null — the draft stops being a transfer.
        #
        # transaction_type needs no repair here. Before WP1 it held the value
        # 3, so clearing the transfer columns destroyed the row's only record
        # of which way it pointed and the direction had to be recovered from
        # transfer_direction on the way out. Now transaction_type already IS
        # that direction, so leaving it untouched is correct — dropping the
        # counterparty does not change which way the money moved.
        fields["transfer_account_id"] = None
        fields["transfer_amount_cents"] = None
        if primary_signed is not None:
            fields["transaction_type"] = infer_transaction_type(primary_signed)
        elif before_row["amount_cents"] is None:
            # No amount on either side of the merge — nothing to have a
            # direction about.
            fields["transaction_type"] = None
    elif transfer is not None:
        fields["transfer_account_id"] = transfer["account_id"]
        fields["transfer_amount_cents"] = abs(sibling_signed)
        fields["transaction_type"] = _resolve_transfer_type(
            primary_signed, sibling_signed
        )
    elif primary_signed is not None:
        # Covers both cases: restating the primary's sign on an existing
        # transfer draft flips both legs (the sibling's amount is absolute and
        # its direction is implied), and on an ordinary draft it is simply the
        # direction. One rule, because a transfer leg is now an ordinary row.
        fields["transaction_type"] = infer_transaction_type(primary_signed)

    # Nothing re-rates here. The `date`-keyed re-rate block that used to sit at
    # this point was the second half of open bug 1.4: because it fired only on a
    # date change, a draft that got its account_id filled in later kept the 1.0
    # it was created with, for good. Nothing derived is stored now.

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
    row = await fetch_owned_row_or_404(
        conn, "expense_transaction_inbox", inbox_id, user_id, "inbox item"
    )

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
# Restore
# ---------------------------------------------------------------------------

async def restore_inbox_item(
    conn: asyncpg.Connection,
    user_id: str,
    inbox_id: str,
) -> dict:
    """Undo a soft-delete on a PENDING inbox item.

    Only restores rows whose status is still PENDING — i.e., rows that
    were dismissed before being promoted. Promoted rows (status = 2) are
    deliberately NOT restorable here: the ledger transaction they created
    still exists, so "restoring" the inbox side would leave the user one
    promote-click away from a duplicate ledger row. To undo a promotion
    the correct path is to delete the ledger transaction; once transaction
    restore ships, clients can chain the two operations themselves.

    The status guard uses ``!= PENDING`` (not ``== PROMOTED``) so any
    future status value the spec adds is rejected by default rather than
    silently accepted.

    Raises:
        not_found: no soft-deleted inbox row with that id for this user.
        conflict: the row is soft-deleted but was promoted — restore is
            not the right gesture; client should delete the ledger row
            instead.
    """
    row = await fetch_owned_row_or_404(
        conn, "expense_transaction_inbox", inbox_id, user_id, "inbox item",
        deleted=True,
    )

    if row["status"] != InboxStatus.PENDING:
        raise conflict(
            "Inbox item was promoted to the ledger. Delete the ledger "
            "transaction to undo the promotion."
        )

    before = inbox_from_row(row)

    after_row = await restore(conn, "expense_transaction_inbox", inbox_id, user_id)
    after = inbox_from_row(after_row)

    await write_activity_log(
        conn, user_id, "inbox", inbox_id, ActivityAction.RESTORED,
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
    target_id: UUID,
    target_transfer_id: Optional[UUID],
) -> dict:
    """Promote a pending inbox item into a ledger transaction.

    The flow:

      1. Lock the inbox row (``FOR UPDATE``) so two concurrent promotes
         can't create duplicate ledger transactions.
      2. Validate that all fields required for promotion are present
         and reference active resources (account, category).
      3. Branch on transfer vs non-transfer:
           - Transfer: delegate to ``create_transfer_pair``, passing
             ``inbox_id`` so both legs link back to the inbox row —
             the draft produced the pair, so lineage is a fact about
             both rows (amended 2026-08-07; the sibling previously
             carried no backlink).
           - Non-transfer: insert a single ledger row and write an
             activity log for the new
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
    # Deliberately not query_builder.fetch_owned_row: the extra
    # ``status = $3`` arm has no place in the shared predicate.
    inbox_row = await conn.fetchrow(
        """
        SELECT * FROM expense_transaction_inbox
        WHERE id = $1 AND user_id = $2 AND status = $3 AND deleted_at IS NULL
        FOR UPDATE
        """,
        inbox_id,
        user_id,
        InboxStatus.PENDING,
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
    #
    # Keep in step with the `?ready=true` predicate in routers/inbox.py: a row
    # that predicate returns must promote, and a row that promotes must appear
    # in it. They are two separate implementations of one definition, which is
    # how they drifted apart (WP7.2/7.3); tests/test_inbox_transfers.py pins them.
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
            SELECT id FROM expense_bank_accounts
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
                """
                SELECT id FROM expense_categories
                WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
                """,
                inbox_row["category_id"],
                user_id,
            )
            if category is None:
                errors["category_id"] = "Must reference an active category."

    # Transfers: the sibling account gets the same check the primary does.
    # create_transfer_pair validates it too, but only after this function has
    # committed to the transfer branch — checking here keeps the failure inside
    # the accumulate-all-errors response and matches what ?ready=true reports.
    if is_transfer:
        transfer_account = await conn.fetchrow(
            """
            SELECT id FROM expense_bank_accounts
            WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL AND is_archived = false
            """,
            inbox_row["transfer_account_id"],
            user_id,
        )
        if transfer_account is None:
            errors["transfer.account_id"] = (
                "Must reference an active, non-archived account."
            )

    # transfer_id must be present for a transfer and absent otherwise (spec §383).
    # The mismatch case used to be silently discarded.
    if is_transfer and target_transfer_id is None:
        errors["transfer_id"] = "Must be present for transfer promotions."
    elif not is_transfer and target_transfer_id is not None:
        errors["transfer_id"] = "Must be null for non-transfer promotions."
    elif is_transfer and target_transfer_id == target_id:
        # Same reason as the sibling-account check above: create_transfer_pair
        # rejects this too, but only after this function has committed to the
        # transfer branch — checking here keeps it in the accumulated response.
        errors["transfer_id"] = "Must differ from transaction_id."

    if errors:
        raise validation_error("Inbox item is not ready to promote.", errors)

    # 4a. Transfer promotion branch
    if is_transfer:
        # Imported lazily to avoid circular-import complications
        from app.helpers.transfers import create_transfer_pair

        # Re-sign both legs from the stored direction, which describes the
        # primary. create_transfer_pair takes signed amounts because that is the
        # shape POST /transactions hands it; here the signs are reconstructed
        # from an explicit column rather than inferred from the sibling, so its
        # opposite-sign guard passes because the stored row is well-formed —
        # not because promote forced the primary to agree. That column is now
        # transaction_type; sql/020's coherence CHECK guarantees it is 1 or 2
        # on any row carrying transfer columns.
        primary_abs = inbox_row["amount_cents"]
        sibling_abs = inbox_row["transfer_amount_cents"]
        if inbox_row["transaction_type"] == TransactionType.OUTFLOW:
            primary_signed, sibling_signed = -primary_abs, sibling_abs
        else:
            primary_signed, sibling_signed = primary_abs, -sibling_abs

        txn_response, _sibling = await create_transfer_pair(
            conn=conn,
            user_id=user_id,
            primary_id=target_id,
            sibling_id=target_transfer_id,
            primary_title=inbox_row["title"],
            primary_description=inbox_row["description"],
            primary_amount_cents=primary_signed,
            primary_account_id=str(inbox_row["account_id"]),
            primary_date=inbox_row["date"],
            primary_cleared=False,
            transfer_account_id=str(inbox_row["transfer_account_id"]),
            transfer_amount_cents=sibling_signed,
            inbox_id=str(inbox_row["id"]),
        )

    # 4b. Normal (non-transfer) promotion branch
    else:
        # amount_cents is a promote prerequisite (validated above), and every
        # write path that sets it sets transaction_type in the same statement,
        # so a null here means the row was written by something that is not
        # this engine. Fail closed rather than substituting a direction.
        transaction_type = inbox_row["transaction_type"]
        if transaction_type is None:
            raise validation_error(
                "Inbox item is not ready to promote.",
                {"transaction_type": "Missing direction on a row with an amount."},
            )

        # Create expense_transactions row. Promotion copies native amounts and
        # nothing else — it used to carry the draft's stored rate across, which
        # is how a 1.0 written at capture time became a permanent fact about a
        # USD ledger row (open bug 1.4).
        try:
            txn_row = await conn.fetchrow(
                """
                INSERT INTO expense_transactions
                    (id, user_id, title, description, amount_cents,
                     transaction_type, date, account_id, category_id,
                     inbox_id, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, now(), now())
                RETURNING *
                """,
                target_id,
                user_id,
                inbox_row["title"],
                inbox_row["description"],
                inbox_row["amount_cents"],
                transaction_type,
                inbox_row["date"],
                inbox_row["account_id"],
                inbox_row["category_id"],
                inbox_row["id"],
            )
        except asyncpg.UniqueViolationError:
            raise conflict(f"A transaction with id '{target_id}' already exists.")

        txn_response = transaction_from_row(txn_row)

        # No balance step: the INSERT above IS the balance change (sql/022).

        # Activity log: transaction created
        await write_activity_log(
            conn, user_id, "transaction", str(txn_row["id"]), ActivityAction.CREATED,
            after_snapshot=txn_response,
        )

    # 5. Shared cleanup: soft-delete inbox row with status = PROMOTED
    # This is NOT a plain soft_delete() because it also flips the status.
    inbox_after_row = await conn.fetchrow(
        """
        UPDATE expense_transaction_inbox
        SET status = $3, deleted_at = now(), updated_at = now(), version = version + 1
        WHERE id = $1 AND user_id = $2
        RETURNING *
        """,
        inbox_id,
        user_id,
        InboxStatus.PROMOTED,
    )
    inbox_after = inbox_from_row(inbox_after_row)

    await write_activity_log(
        conn, user_id, "inbox", inbox_id, ActivityAction.DELETED,
        before_snapshot=inbox_before,
        after_snapshot=inbox_after,
    )

    # Promoted transactions are freshly created — no junctions exist yet,
    # so this resolves to []. Attaching uniformly is still the right call
    # so the wire shape matches every other transaction-returning endpoint.
    await attach_hashtag_ids(conn, txn_response)

    return txn_response
