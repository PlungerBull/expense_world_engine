"""Reconciliation domain logic.

Service-layer functions for expense_reconciliations, called from
routers/reconciliations.py. Routers stay thin (HTTP glue + idempotency) and
delegate business logic here.

See ``app/helpers/balance.py`` for the convention: these functions do NOT
open their own ``conn.transaction()`` — callers own transaction boundaries.
"""

from datetime import datetime
from typing import Optional

import asyncpg

from app.constants import ActivityAction, ReconciliationStatus
from app.errors import conflict, not_found, validation_error
from app.helpers.activity_log import write_activity_log
from app.helpers.query_builder import dynamic_update, soft_delete
from app.schemas.reconciliations import reconciliation_from_row


async def create_reconciliation(
    conn: asyncpg.Connection,
    user_id: str,
    account_id: str,
    name: str,
    date_start: Optional[datetime],
    date_end: Optional[datetime],
    beginning_balance_cents: Optional[int],
    ending_balance_cents: Optional[int],
) -> dict:
    """Validate inputs, insert a DRAFT reconciliation, and log the creation.

    Auto-prefills ``beginning_balance_cents`` from the most recent
    reconciliation's ``ending_balance_cents`` if omitted, defaulting to 0
    when no previous batch exists. ``ending_balance_cents`` defaults to 0.

    Raises:
        validation_error: account reference is invalid or name is empty.
    """
    # Validate account_id
    account = await conn.fetchrow(
        """
        SELECT id FROM expense_bank_accounts
        WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL AND is_archived = false
        """,
        account_id,
        user_id,
    )
    if account is None:
        raise validation_error(
            "Account validation failed.",
            {"account_id": "Must reference an active, non-archived account."},
        )

    # Validate name
    if not name or not name.strip():
        raise validation_error(
            "Name must not be empty.",
            {"name": "Must not be empty."},
        )

    # Auto-prefill beginning_balance_cents from previous batch
    beginning = beginning_balance_cents
    if beginning is None:
        prev = await conn.fetchrow(
            """
            SELECT ending_balance_cents FROM expense_reconciliations
            WHERE account_id = $1 AND user_id = $2 AND deleted_at IS NULL
            ORDER BY created_at DESC
            LIMIT 1
            """,
            account_id,
            user_id,
        )
        beginning = prev["ending_balance_cents"] if prev else 0

    ending = ending_balance_cents if ending_balance_cents is not None else 0

    row = await conn.fetchrow(
        """
        INSERT INTO expense_reconciliations
            (user_id, account_id, name, date_start, date_end, status,
             beginning_balance_cents, ending_balance_cents, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, 1, $6, $7, now(), now())
        RETURNING *
        """,
        user_id,
        account_id,
        name.strip(),
        date_start,
        date_end,
        beginning,
        ending,
    )

    response = reconciliation_from_row(row)

    await write_activity_log(
        conn, user_id, "reconciliation", str(row["id"]), ActivityAction.CREATED,
        after_snapshot=response,
    )
    return response


async def update_reconciliation(
    conn: asyncpg.Connection,
    user_id: str,
    reconciliation_id: str,
    fields: dict,
) -> dict:
    """Apply field updates to a reconciliation.

    Returns the unchanged reconciliation if ``fields`` is empty (matches the
    prior router behaviour of treating empty-update as a fetch).

    Raises:
        not_found: no active reconciliation with that id for this user.
        validation_error: name is provided but empty after stripping.
    """
    # Empty update — return current state unchanged
    if not fields:
        row = await conn.fetchrow(
            "SELECT * FROM expense_reconciliations WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
            reconciliation_id,
            user_id,
        )
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

    before_row = await conn.fetchrow(
        "SELECT * FROM expense_reconciliations WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
        reconciliation_id,
        user_id,
    )
    if before_row is None:
        raise not_found("reconciliation")

    before = reconciliation_from_row(before_row)

    after_row = await dynamic_update(conn, "expense_reconciliations", fields, reconciliation_id, user_id)
    if after_row is None:
        raise not_found("reconciliation")

    after = reconciliation_from_row(after_row)

    await write_activity_log(
        conn, user_id, "reconciliation", reconciliation_id, ActivityAction.UPDATED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after


async def complete_reconciliation(
    conn: asyncpg.Connection,
    user_id: str,
    reconciliation_id: str,
) -> dict:
    """Transition a reconciliation from DRAFT to COMPLETED.

    Idempotent no-op if already COMPLETED: returns the current row without
    writing a new activity log entry.

    Raises:
        not_found: no active reconciliation with that id for this user.
        validation_error: no transactions are assigned to this reconciliation.
    """
    row = await conn.fetchrow(
        "SELECT * FROM expense_reconciliations WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
        reconciliation_id,
        user_id,
    )
    if row is None:
        raise not_found("reconciliation")

    # Already completed — return idempotently (no activity log)
    if row["status"] == ReconciliationStatus.COMPLETED:
        return reconciliation_from_row(row)

    # Must have at least one assigned transaction
    txn_count = await conn.fetchval(
        """
        SELECT count(*) FROM expense_transactions
        WHERE reconciliation_id = $1 AND user_id = $2 AND deleted_at IS NULL
        """,
        reconciliation_id,
        user_id,
    )
    if txn_count == 0:
        raise validation_error(
            "Cannot complete reconciliation with no assigned transactions.",
            {"transactions": "At least one transaction must be assigned."},
        )

    before = reconciliation_from_row(row)

    after_row = await conn.fetchrow(
        """
        UPDATE expense_reconciliations
        SET status = 2, updated_at = now(), version = version + 1
        WHERE id = $1 AND user_id = $2
        RETURNING *
        """,
        reconciliation_id,
        user_id,
    )

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
    row = await conn.fetchrow(
        "SELECT * FROM expense_reconciliations WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
        reconciliation_id,
        user_id,
    )
    if row is None:
        raise not_found("reconciliation")

    # Already draft — return idempotently (no activity log)
    if row["status"] == ReconciliationStatus.DRAFT:
        return reconciliation_from_row(row)

    before = reconciliation_from_row(row)

    after_row = await conn.fetchrow(
        """
        UPDATE expense_reconciliations
        SET status = 1, updated_at = now(), version = version + 1
        WHERE id = $1 AND user_id = $2
        RETURNING *
        """,
        reconciliation_id,
        user_id,
    )

    after = reconciliation_from_row(after_row)

    await write_activity_log(
        conn, user_id, "reconciliation", reconciliation_id, ActivityAction.UPDATED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after


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
    row = await conn.fetchrow(
        "SELECT * FROM expense_reconciliations WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
        reconciliation_id,
        user_id,
    )
    if row is None:
        raise not_found("reconciliation")

    if row["status"] == ReconciliationStatus.COMPLETED:
        raise conflict("Cannot delete a completed reconciliation. Revert to draft first.")

    before = reconciliation_from_row(row)

    after_row = await soft_delete(conn, "expense_reconciliations", reconciliation_id, user_id)

    after = reconciliation_from_row(after_row)

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

    await write_activity_log(
        conn, user_id, "reconciliation", reconciliation_id, ActivityAction.DELETED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after
