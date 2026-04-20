"""Reconciliation domain logic.

Service-layer functions for expense_reconciliations, called from
routers/reconciliations.py. Routers stay thin (HTTP glue + idempotency) and
delegate business logic here.

See ``app/helpers/balance.py`` for the convention: these functions do NOT
open their own ``conn.transaction()`` — callers own transaction boundaries.
"""

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import asyncpg

from app.constants import ActivityAction, ReconciliationStatus
from app.errors import conflict, not_found, validation_error
from app.helpers.activity_log import write_activity_log
from app.helpers.exchange_rate import get_rate
from app.helpers.query_builder import dynamic_update, restore, soft_delete
from app.helpers.validation import validate_active_account
from app.schemas.reconciliations import reconciliation_from_row


async def resolve_home_rates(
    conn: asyncpg.Connection,
    user_id: str,
    rows: list,
) -> dict[str, Optional[float]]:
    """Resolve the account-currency → home-currency rate for each reconciliation row.

    Returns ``{reconciliation_id: rate|None}``. ``None`` means no rate is
    available (main currency missing or no rate row), in which case the
    serializer emits ``null`` for the ``_home_cents`` fields.

    Deduplicates by ``(account_currency, date)`` so the rate helper is
    hit once per distinct pair, not once per reconciliation. A list of
    N reconciliations across K currencies produces at most K cache
    lookups, not N.
    """
    if not rows:
        return {}

    settings_row = await conn.fetchrow(
        "SELECT main_currency FROM user_settings WHERE user_id = $1", user_id
    )
    main_currency = settings_row["main_currency"] if settings_row else None
    if main_currency is None:
        return {str(row["id"]): None for row in rows}

    # Pull currency for every referenced account in a single query.
    account_ids = {str(row["account_id"]) for row in rows}
    currency_rows = await conn.fetch(
        "SELECT id, currency_code FROM expense_bank_accounts WHERE id = ANY($1::uuid[])",
        list(account_ids),
    )
    currency_by_account = {str(r["id"]): r["currency_code"] for r in currency_rows}

    today = datetime.now(timezone.utc).date()

    # Cache per (currency, date) pair so cross-currency reconciliations
    # on the same end-date reuse a single rate lookup.
    rate_cache: dict[tuple[str, object], Optional[float]] = {}

    async def _rate_for(currency: str, as_of) -> Optional[float]:
        key = (currency, as_of)
        if key in rate_cache:
            return rate_cache[key]
        if currency == main_currency:
            rate_cache[key] = 1.0
            return 1.0
        result = await get_rate(conn, currency, main_currency, as_of)
        value = result[0] if result is not None else None
        rate_cache[key] = value
        return value

    out: dict[str, Optional[float]] = {}
    for row in rows:
        currency = currency_by_account.get(str(row["account_id"]))
        if currency is None:
            out[str(row["id"])] = None
            continue
        as_of = row["date_end"].date() if row["date_end"] is not None else today
        out[str(row["id"])] = await _rate_for(currency, as_of)
    return out


async def create_reconciliation(
    conn: asyncpg.Connection,
    user_id: str,
    reconciliation_id: UUID,
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

    try:
        row = await conn.fetchrow(
            """
            INSERT INTO expense_reconciliations
                (id, user_id, account_id, name, date_start, date_end, status,
                 beginning_balance_cents, ending_balance_cents, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, 1, $7, $8, now(), now())
            RETURNING *
            """,
            reconciliation_id,
            user_id,
            account_id,
            name.strip(),
            date_start,
            date_end,
            beginning,
            ending,
        )
    except asyncpg.UniqueViolationError:
        raise conflict(
            f"A reconciliation with id '{reconciliation_id}' already exists."
        )

    rate_by_id = await resolve_home_rates(conn, user_id, [row])
    response = reconciliation_from_row(row, rate_by_id.get(str(row["id"])))

    await write_activity_log(
        conn, user_id, "reconciliation", str(row["id"]), ActivityAction.CREATED,
        after_snapshot=response,
    )
    return response


# Fields that cannot be edited once a reconciliation is COMPLETED.
# A completed batch is a historical record of the balance the user
# confirmed at a point in time — changing the range or the starting/
# ending balances after the fact would rewrite that history. Cosmetic
# fields (name) stay editable so users can re-label archived batches.
_LOCKED_FIELDS_WHEN_COMPLETED = frozenset(
    {"beginning_balance_cents", "ending_balance_cents", "date_start", "date_end"}
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

    Raises:
        not_found: no active reconciliation with that id for this user.
        validation_error: name is provided but empty after stripping, or a
            locked field is edited while status=COMPLETED.
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
        rate_by_id = await resolve_home_rates(conn, user_id, [row])
        return reconciliation_from_row(row, rate_by_id.get(str(row["id"])))

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

    before_rate = await resolve_home_rates(conn, user_id, [before_row])
    before = reconciliation_from_row(before_row, before_rate.get(str(before_row["id"])))

    after_row = await dynamic_update(conn, "expense_reconciliations", fields, reconciliation_id, user_id)
    if after_row is None:
        raise not_found("reconciliation")

    after_rate = await resolve_home_rates(conn, user_id, [after_row])
    after = reconciliation_from_row(after_row, after_rate.get(str(after_row["id"])))

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
        rate_by_id = await resolve_home_rates(conn, user_id, [row])
        return reconciliation_from_row(row, rate_by_id.get(str(row["id"])))

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

    before_rate = await resolve_home_rates(conn, user_id, [row])
    before = reconciliation_from_row(row, before_rate.get(str(row["id"])))

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

    # Bump version on every assigned transaction so delta-sync clients
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

    after_rate = await resolve_home_rates(conn, user_id, [after_row])
    after = reconciliation_from_row(after_row, after_rate.get(str(after_row["id"])))

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
        rate_by_id = await resolve_home_rates(conn, user_id, [row])
        return reconciliation_from_row(row, rate_by_id.get(str(row["id"])))

    # Mirror complete_reconciliation: lock assigned txns before flipping
    # state so concurrent edits serialize behind the revert, and sync
    # clients see the same tick bump the txn versions.
    await conn.fetch(
        """
        SELECT id FROM expense_transactions
        WHERE reconciliation_id = $1 AND user_id = $2 AND deleted_at IS NULL
        FOR UPDATE
        """,
        reconciliation_id,
        user_id,
    )

    before_rate = await resolve_home_rates(conn, user_id, [row])
    before = reconciliation_from_row(row, before_rate.get(str(row["id"])))

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

    await conn.execute(
        """
        UPDATE expense_transactions
        SET version = version + 1, updated_at = now()
        WHERE reconciliation_id = $1 AND user_id = $2 AND deleted_at IS NULL
        """,
        reconciliation_id,
        user_id,
    )

    after_rate = await resolve_home_rates(conn, user_id, [after_row])
    after = reconciliation_from_row(after_row, after_rate.get(str(after_row["id"])))

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

    before_rate = await resolve_home_rates(conn, user_id, [row])
    before = reconciliation_from_row(row, before_rate.get(str(row["id"])))

    after_row = await soft_delete(conn, "expense_reconciliations", reconciliation_id, user_id)

    after_rate = await resolve_home_rates(conn, user_id, [after_row])
    after = reconciliation_from_row(after_row, after_rate.get(str(after_row["id"])))

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
    before_row = await conn.fetchrow(
        "SELECT * FROM expense_reconciliations WHERE id = $1 AND user_id = $2 AND deleted_at IS NOT NULL",
        reconciliation_id,
        user_id,
    )
    if before_row is None:
        raise not_found("reconciliation")

    before_rate = await resolve_home_rates(conn, user_id, [before_row])
    before = reconciliation_from_row(before_row, before_rate.get(str(before_row["id"])))

    after_row = await restore(conn, "expense_reconciliations", reconciliation_id, user_id)
    after_rate = await resolve_home_rates(conn, user_id, [after_row])
    after = reconciliation_from_row(after_row, after_rate.get(str(after_row["id"])))

    await write_activity_log(
        conn, user_id, "reconciliation", reconciliation_id, ActivityAction.RESTORED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after
