"""Reconciliation domain logic.

Service-layer functions for expense_reconciliations, called from
routers/reconciliations.py. Routers stay thin (HTTP glue + idempotency)
and delegate business logic here.

See ``app/helpers/idempotency.run_idempotent`` for the convention: these
functions do NOT open their own ``conn.transaction()`` — callers own
transaction boundaries.

The feature: name a period, record the beginning and ending balance from
your statement, assign transactions to it, and check that they add up.
Complete it when they do; revert if you were wrong. Both balances are
facts the user reads off a statement — the engine never derives either.

Chaining used to live here (deleted by sql/025, WP6 of the deletion program): a
"chained" reconciliation took its beginning balance from the previous
row's ending balance and a cascade rewrote downstream rows on every
upstream edit. The cascade had no status predicate, so editing a DRAFT
silently rewrote a COMPLETED row's beginning_balance_cents — the exact
mutation the completion field-lock exists to refuse. With it went
``sort_order`` and the bulk-reorder endpoint: ordering is by period date
now, and dates went from pure labels to the thing that orders the list.

Whether a reconciliation adds up is ``difference_cents`` — computed at
read time from the ledger on every response, never stored, exactly like
account balances (sql/022). Completing with a non-zero difference is
allowed: the check informs, the user decides.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

import asyncpg

from app.constants import ActivityAction, ReconciliationStatus
from app.errors import conflict, not_found, validation_error
from app.helpers.activity_log import write_activity_log
from app.helpers.home_currency import SIGNED_CENTS_EXPR
from app.helpers.query_builder import dynamic_update, restore, soft_delete
from app.helpers.validation import validate_active_account
from app.schemas.reconciliations import reconciliation_from_row


# ---------------------------------------------------------------------------
# The one way to SELECT a reconciliation
# ---------------------------------------------------------------------------

# The sign matrix is NOT rewritten here — SIGNED_CENTS_EXPR is the single
# rendering in the engine and references only the ``t`` alias, so the
# correlated sum needs no account join and no rate lateral. The figure is in
# the account's native currency by construction: every assigned transaction
# lives on the reconciliation's own account's currency or another single
# account the user chose — either way each row is summed unconverted, and a
# reconciliation compares against statement balances in that same currency.
#
# Served by idx_expense_transactions_user_reconciliation (sql/022), whose
# partial predicate matches this WHERE exactly.
_DIFFERENCE_CENTS_SQL = f"""(
    (rec.ending_balance_cents - rec.beginning_balance_cents)
    - COALESCE((
        SELECT SUM({SIGNED_CENTS_EXPR})::bigint
        FROM expense_transactions t
        WHERE t.reconciliation_id = rec.id
          AND t.user_id = rec.user_id
          AND t.deleted_at IS NULL
    ), 0)
)"""

# Every read of a reconciliation goes through this projection so
# ``difference_cents`` is present on every row that reaches
# ``reconciliation_from_row`` — list, detail, and the before/after snapshots
# of every mutation. A bare ``SELECT *`` would KeyError at serialization
# rather than ship a response with the figure missing.
RECONCILIATION_SELECT = f"""SELECT rec.*,
       {_DIFFERENCE_CENTS_SQL} AS difference_cents
FROM expense_reconciliations rec
"""


async def fetch_reconciliation(
    conn: asyncpg.Connection,
    user_id: str,
    reconciliation_id: str,
    *,
    deleted: bool = False,
) -> Optional[asyncpg.Record]:
    """Fetch one reconciliation row with its computed ``difference_cents``.

    ``deleted=False`` (the default) resolves only active rows;
    ``deleted=True`` resolves only soft-deleted rows (the restore path).
    Returns ``None`` when no row matches.
    """
    predicate = "IS NOT NULL" if deleted else "IS NULL"
    return await conn.fetchrow(
        f"""{RECONCILIATION_SELECT}
        WHERE rec.id = $1 AND rec.user_id = $2 AND rec.deleted_at {predicate}
        """,
        reconciliation_id,
        user_id,
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def create_reconciliation(
    conn: asyncpg.Connection,
    user_id: str,
    reconciliation_id: UUID,
    account_id: str,
    name: str,
    date_start: Optional[datetime],
    date_end: Optional[datetime],
    beginning_balance_cents: int,
    ending_balance_cents: Optional[int],
) -> dict:
    """Validate inputs, insert a DRAFT reconciliation, and log the creation.

    ``beginning_balance_cents`` is required at the schema layer — there is
    no derived mode and no prefill (owner decision 2026-08-06, superseding
    open-bugs decision D3's one-time-prefill sketch).

    Raises:
        validation_error: account reference is invalid or name is empty.
        conflict: a reconciliation with the same id already exists.
    """
    # Validate account_id via shared helper (raises 422 on invalid).
    await validate_active_account(conn, account_id, user_id)

    # Validate name
    if not name or not name.strip():
        raise validation_error(
            "Name must not be empty.",
            {"name": "Must not be empty."},
        )

    ending = ending_balance_cents if ending_balance_cents is not None else 0

    try:
        await conn.execute(
            """
            INSERT INTO expense_reconciliations
                (id, user_id, account_id, name, date_start, date_end, status,
                 beginning_balance_cents, ending_balance_cents,
                 created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $9, $7, $8, now(), now())
            """,
            reconciliation_id,
            user_id,
            account_id,
            name.strip(),
            date_start,
            date_end,
            beginning_balance_cents,
            ending,
            ReconciliationStatus.DRAFT,
        )
    except asyncpg.UniqueViolationError:
        raise conflict(
            f"A reconciliation with id '{reconciliation_id}' already exists."
        )

    row = await fetch_reconciliation(conn, user_id, str(reconciliation_id))
    response = reconciliation_from_row(row)

    await write_activity_log(
        conn, user_id, "reconciliation", str(row["id"]), ActivityAction.CREATED,
        after_snapshot=response,
    )
    return response


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


# Fields that cannot be edited once a reconciliation is COMPLETED.
# A completed batch is a historical record of the balance the user
# confirmed at a point in time — changing the range or the starting/
# ending balances after the fact would rewrite that history. Cosmetic
# fields (name) stay editable so users can re-label archived batches.
_LOCKED_FIELDS_WHEN_COMPLETED = frozenset(
    {
        "beginning_balance_cents",
        "ending_balance_cents",
        "date_start",
        "date_end",
    }
)


async def update_reconciliation(
    conn: asyncpg.Connection,
    user_id: str,
    reconciliation_id: str,
    fields: dict,
) -> dict:
    """Apply field updates to a reconciliation.

    Returns the unchanged reconciliation if ``fields`` is empty (matches the
    prior router behaviour of treating empty-update as a fetch).

    An edit here changes this row and nothing else — no other
    reconciliation's balances move, in any status. That locality is the
    invariant sql/025 exists for, and tests/test_wp6_* pins it.

    Raises:
        not_found: no active reconciliation with that id for this user.
        validation_error: name is provided but empty after stripping, or a
            locked field is edited while status=COMPLETED.
    """
    # Empty update — return current state unchanged
    if not fields:
        row = await fetch_reconciliation(conn, user_id, reconciliation_id)
        if row is None:
            raise not_found("reconciliation")
        return reconciliation_from_row(row)

    # Validate name if changing
    if "name" in fields:
        if not fields["name"] or not fields["name"].strip():
            raise validation_error(
                "Name must not be empty.",
                {"name": "Must not be empty."},
            )
        fields["name"] = fields["name"].strip()

    before_row = await fetch_reconciliation(conn, user_id, reconciliation_id)
    if before_row is None:
        raise not_found("reconciliation")

    # Once COMPLETED, the balance range is frozen. Reject edits to any
    # locked field with a field-level error so clients can highlight the
    # offending keys. The user must /revert first.
    if before_row["status"] == ReconciliationStatus.COMPLETED:
        attempted = _LOCKED_FIELDS_WHEN_COMPLETED & fields.keys()
        if attempted:
            raise validation_error(
                "Reconciliation is completed. Revert to draft before editing these fields.",
                {f: "Locked while reconciliation is completed." for f in attempted},
            )

    before = reconciliation_from_row(before_row)

    updated = await dynamic_update(
        conn, "expense_reconciliations", fields, reconciliation_id, user_id,
    )
    if updated is None:
        raise not_found("reconciliation")

    after_row = await fetch_reconciliation(conn, user_id, reconciliation_id)
    after = reconciliation_from_row(after_row)

    await write_activity_log(
        conn, user_id, "reconciliation", reconciliation_id, ActivityAction.UPDATED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------


async def complete_reconciliation(
    conn: asyncpg.Connection,
    user_id: str,
    reconciliation_id: str,
) -> dict:
    """Transition a reconciliation from DRAFT to COMPLETED.

    Idempotent no-op if already COMPLETED: returns the current row without
    writing a new activity log entry.

    A non-zero ``difference_cents`` does not block completion — the figure
    informs, the user decides. The activity-log snapshots carry it, so the
    audit trail records what the difference was at the moment of completion.

    Raises:
        not_found: no active reconciliation with that id for this user.
        validation_error: no transactions are assigned to this reconciliation.
    """
    row = await fetch_reconciliation(conn, user_id, reconciliation_id)
    if row is None:
        raise not_found("reconciliation")

    # Already completed — return idempotently (no activity log)
    if row["status"] == ReconciliationStatus.COMPLETED:
        return reconciliation_from_row(row)

    # Lock and count assigned transactions in one shot. FOR UPDATE
    # serializes concurrent transaction edits against this status flip —
    # without it, a transaction could be reassigned away or edited
    # between the count check and the status update, leaving the client's
    # view of "what's locked" inconsistent with what actually got locked.
    assigned_txns = await conn.fetch(
        """
        SELECT id FROM expense_transactions
        WHERE reconciliation_id = $1 AND user_id = $2 AND deleted_at IS NULL
        FOR UPDATE
        """,
        reconciliation_id,
        user_id,
    )
    if not assigned_txns:
        raise validation_error(
            "Cannot complete reconciliation with no assigned transactions.",
            {"transactions": "At least one transaction must be assigned."},
        )

    before = reconciliation_from_row(row)

    await conn.execute(
        """
        UPDATE expense_reconciliations
        SET status = $3, updated_at = now(), version = version + 1
        WHERE id = $1 AND user_id = $2
        """,
        reconciliation_id,
        user_id,
        ReconciliationStatus.COMPLETED,
    )

    # Bump version on every assigned transaction so clients and auditors
    # see them flip into the "fields locked" state in the same tick as
    # the reconciliation itself.
    await conn.execute(
        """
        UPDATE expense_transactions
        SET version = version + 1, updated_at = now()
        WHERE reconciliation_id = $1 AND user_id = $2 AND deleted_at IS NULL
        """,
        reconciliation_id,
        user_id,
    )

    after_row = await fetch_reconciliation(conn, user_id, reconciliation_id)
    after = reconciliation_from_row(after_row)

    await write_activity_log(
        conn, user_id, "reconciliation", reconciliation_id, ActivityAction.UPDATED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after


async def revert_reconciliation(
    conn: asyncpg.Connection,
    user_id: str,
    reconciliation_id: str,
) -> dict:
    """Transition a reconciliation from COMPLETED back to DRAFT.

    Idempotent no-op if already DRAFT: returns the current row without
    writing a new activity log entry.

    Raises:
        not_found: no active reconciliation with that id for this user.
    """
    row = await fetch_reconciliation(conn, user_id, reconciliation_id)
    if row is None:
        raise not_found("reconciliation")

    # Already draft — return idempotently (no activity log)
    if row["status"] == ReconciliationStatus.DRAFT:
        return reconciliation_from_row(row)

    # Mirror complete_reconciliation: lock assigned txns before flipping
    # state so concurrent edits serialize behind the revert, and readers
    # see the same tick bump the txn versions.
    await conn.fetch(
        """
        SELECT id FROM expense_transactions
        WHERE reconciliation_id = $1 AND user_id = $2 AND deleted_at IS NULL
        FOR UPDATE
        """,
        reconciliation_id,
        user_id,
    )

    before = reconciliation_from_row(row)

    await conn.execute(
        """
        UPDATE expense_reconciliations
        SET status = $3, updated_at = now(), version = version + 1
        WHERE id = $1 AND user_id = $2
        """,
        reconciliation_id,
        user_id,
        ReconciliationStatus.DRAFT,
    )

    await conn.execute(
        """
        UPDATE expense_transactions
        SET version = version + 1, updated_at = now()
        WHERE reconciliation_id = $1 AND user_id = $2 AND deleted_at IS NULL
        """,
        reconciliation_id,
        user_id,
    )

    after_row = await fetch_reconciliation(conn, user_id, reconciliation_id)
    after = reconciliation_from_row(after_row)

    await write_activity_log(
        conn, user_id, "reconciliation", reconciliation_id, ActivityAction.UPDATED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after


# ---------------------------------------------------------------------------
# Soft delete / restore
# ---------------------------------------------------------------------------


async def delete_reconciliation(
    conn: asyncpg.Connection,
    user_id: str,
    reconciliation_id: str,
) -> dict:
    """Soft-delete a reconciliation and cascade-unassign its transactions.

    Raises:
        not_found: no active reconciliation with that id for this user.
        conflict: reconciliation is COMPLETED (must be reverted first).
    """
    row = await fetch_reconciliation(conn, user_id, reconciliation_id)
    if row is None:
        raise not_found("reconciliation")

    if row["status"] == ReconciliationStatus.COMPLETED:
        raise conflict("Cannot delete a completed reconciliation. Revert to draft first.")

    before = reconciliation_from_row(row)

    await soft_delete(conn, "expense_reconciliations", reconciliation_id, user_id)

    # Unassign all transactions from this batch
    await conn.execute(
        """
        UPDATE expense_transactions
        SET reconciliation_id = NULL, updated_at = now(), version = version + 1
        WHERE reconciliation_id = $1 AND user_id = $2 AND deleted_at IS NULL
        """,
        reconciliation_id,
        user_id,
    )

    # After-snapshot is taken AFTER the unassignment so its
    # difference_cents reflects the emptied batch (ending − beginning),
    # not a membership that no longer exists.
    after_row = await fetch_reconciliation(
        conn, user_id, reconciliation_id, deleted=True,
    )
    after = reconciliation_from_row(after_row)

    await write_activity_log(
        conn, user_id, "reconciliation", reconciliation_id, ActivityAction.DELETED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after


async def restore_reconciliation(
    conn: asyncpg.Connection,
    user_id: str,
    reconciliation_id: str,
) -> dict:
    """Undo a soft-delete on a reconciliation and log the restoration.

    The transactions that were unassigned during delete are NOT re-linked.
    The restored reconciliation comes back empty and the user must
    manually re-assign transactions if desired. Re-linking would risk
    touching transactions that have since been reassigned to other
    reconciliations or edited in ways that break the original balance
    assumptions.

    Raises:
        not_found: no soft-deleted reconciliation with that id for this user.
    """
    before_row = await fetch_reconciliation(
        conn, user_id, reconciliation_id, deleted=True,
    )
    if before_row is None:
        raise not_found("reconciliation")

    before = reconciliation_from_row(before_row)

    await restore(conn, "expense_reconciliations", reconciliation_id, user_id)

    after_row = await fetch_reconciliation(conn, user_id, reconciliation_id)
    after = reconciliation_from_row(after_row)

    await write_activity_log(
        conn, user_id, "reconciliation", reconciliation_id, ActivityAction.RESTORED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after
