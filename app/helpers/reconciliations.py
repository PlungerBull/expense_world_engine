"""Reconciliation domain logic.

Service-layer functions for expense_reconciliations, called from
routers/reconciliations.py and routers/accounts.py (reorder endpoint).
Routers stay thin (HTTP glue + idempotency) and delegate business logic
here.

See ``app/helpers/balance.py`` for the convention: these functions do NOT
open their own ``conn.transaction()`` — callers own transaction boundaries.
"""

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import asyncpg

from app.constants import (
    ActivityAction,
    BeginningBalanceSource,
    ReconciliationStatus,
)
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


# ---------------------------------------------------------------------------
# Ordering / chaining primitives
# ---------------------------------------------------------------------------


async def _previous_chained_neighbor(
    conn: asyncpg.Connection,
    user_id: str,
    account_id: str,
    sort_order: int,
    exclude_id: Optional[str] = None,
) -> Optional[asyncpg.Record]:
    """Return the active reconciliation immediately before ``sort_order``.

    "Immediately before" = highest ``sort_order`` strictly less than the
    target on this account, scoped to the user, ignoring soft-deleted
    rows. Returns ``None`` when no such row exists (e.g. the target is
    the first in the chain).

    ``exclude_id`` lets callers exclude themselves when re-deriving the
    neighbor for a row that already lives at ``sort_order`` (otherwise
    the row would find itself).
    """
    if exclude_id is None:
        return await conn.fetchrow(
            """
            SELECT * FROM expense_reconciliations
            WHERE user_id = $1 AND account_id = $2
              AND deleted_at IS NULL
              AND sort_order < $3
            ORDER BY sort_order DESC
            LIMIT 1
            """,
            user_id,
            account_id,
            sort_order,
        )
    return await conn.fetchrow(
        """
        SELECT * FROM expense_reconciliations
        WHERE user_id = $1 AND account_id = $2
          AND deleted_at IS NULL
          AND sort_order < $3
          AND id <> $4
        ORDER BY sort_order DESC
        LIMIT 1
        """,
        user_id,
        account_id,
        sort_order,
        exclude_id,
    )


async def _next_sort_order(
    conn: asyncpg.Connection,
    user_id: str,
    account_id: str,
) -> int:
    """Return ``max(sort_order) + 1`` for the account, or ``1`` if empty.

    Counts soft-deleted rows so a deleted-then-restored row never collides
    with a freshly appended row.
    """
    row = await conn.fetchval(
        """
        SELECT COALESCE(MAX(sort_order), 0) FROM expense_reconciliations
        WHERE user_id = $1 AND account_id = $2
        """,
        user_id,
        account_id,
    )
    return int(row) + 1


async def _shift_sort_orders_at_or_above(
    conn: asyncpg.Connection,
    user_id: str,
    account_id: str,
    threshold: int,
) -> None:
    """Make room for an insertion at ``threshold`` by bumping every row
    at or above that position by ``+1``. Operates on all rows including
    soft-deleted so the relative order survives a future restore."""
    await conn.execute(
        """
        UPDATE expense_reconciliations
        SET sort_order = sort_order + 1, updated_at = now(), version = version + 1
        WHERE user_id = $1 AND account_id = $2 AND sort_order >= $3
        """,
        user_id,
        account_id,
        threshold,
    )


async def _cascade_chained_recalc(
    conn: asyncpg.Connection,
    user_id: str,
    account_id: str,
    starting_sort_order: int,
) -> int:
    """Walk downstream chained rows recomputing beginning_balance_cents.

    Starts at the smallest active ``sort_order`` strictly greater than
    ``starting_sort_order``. For each row with
    ``beginning_balance_source = CHAINED``, recomputes
    ``beginning_balance_cents`` from the previous active neighbor's
    ``ending_balance_cents``. Stops early at the first row whose value
    didn't change — a no-op there means no further downstream change is
    possible (chained rows only change when their direct upstream changes).

    ``MANUAL`` rows are skipped (their ending_balance_cents still matters
    for the next downstream chained row, but the manual row itself is
    never recomputed). The walk does not "stop" at a manual row — it
    continues past it, since a downstream chained row that points to the
    manual row's ending balance still needs evaluation.

    Writes one ``UPDATED`` activity log entry per row whose value
    actually changed (with full before/after snapshots). Returns the
    number of chained rows actually rewritten.
    """
    # Pull the active rows downstream of the starting position once,
    # in order. The walk is in-memory after this single fetch — each
    # row's "previous neighbor" ending balance is the prior iteration's
    # (post-update) ending_balance_cents.
    rows = await conn.fetch(
        """
        SELECT * FROM expense_reconciliations
        WHERE user_id = $1 AND account_id = $2
          AND deleted_at IS NULL
          AND sort_order > $3
        ORDER BY sort_order ASC
        """,
        user_id,
        account_id,
        starting_sort_order,
    )
    if not rows:
        return 0

    # Seed "previous neighbor's ending_balance" with the row immediately
    # before the first downstream row — typically the row that just
    # changed (its ending_balance is the current one in the DB). When
    # no seed exists (the first downstream row is the absolute first in
    # the chain on this account), ``has_upstream`` stays False until the
    # walk processes its first row; chained rows encountered before any
    # upstream is established are LEFT ALONE — never silently rewritten
    # to 0. This matches the engine-spec rule: "When chained is requested
    # but no previous neighbor exists, the existing value is left alone."
    seed = await conn.fetchrow(
        """
        SELECT ending_balance_cents FROM expense_reconciliations
        WHERE user_id = $1 AND account_id = $2
          AND deleted_at IS NULL
          AND sort_order <= $3
        ORDER BY sort_order DESC
        LIMIT 1
        """,
        user_id,
        account_id,
        starting_sort_order,
    )
    has_upstream = seed is not None
    prev_ending = int(seed["ending_balance_cents"]) if has_upstream else 0

    recalculated = 0
    for row in rows:
        if row["beginning_balance_source"] == BeginningBalanceSource.CHAINED and not has_upstream:
            # Chained row with no upstream — leave its stored value
            # alone. Its own ending_balance becomes the upstream for
            # the next row in the walk, so flip has_upstream and move on.
            has_upstream = True
            prev_ending = int(row["ending_balance_cents"])
            continue
        if row["beginning_balance_source"] == BeginningBalanceSource.CHAINED:
            new_beginning = prev_ending
            current_beginning = int(row["beginning_balance_cents"])
            if new_beginning == current_beginning:
                # No-op for this row → no downstream chained row can
                # change either, since the chain only propagates value
                # diffs. Stop early.
                return recalculated

            before_rate = await resolve_home_rates(conn, user_id, [row])
            before = reconciliation_from_row(
                row,
                before_rate.get(str(row["id"])),
                chained_from_reconciliation_id=None,
            )

            updated_row = await conn.fetchrow(
                """
                UPDATE expense_reconciliations
                SET beginning_balance_cents = $3,
                    updated_at = now(),
                    version = version + 1
                WHERE id = $1 AND user_id = $2
                RETURNING *
                """,
                row["id"],
                user_id,
                new_beginning,
            )

            after_rate = await resolve_home_rates(conn, user_id, [updated_row])
            # Look up the actual neighbor id for the after-snapshot.
            neighbor = await _previous_chained_neighbor(
                conn, user_id, account_id, updated_row["sort_order"],
                exclude_id=str(updated_row["id"]),
            )
            after = reconciliation_from_row(
                updated_row,
                after_rate.get(str(updated_row["id"])),
                chained_from_reconciliation_id=str(neighbor["id"]) if neighbor else None,
            )
            await write_activity_log(
                conn, user_id, "reconciliation", str(updated_row["id"]),
                ActivityAction.UPDATED,
                before_snapshot=before,
                after_snapshot=after,
            )
            recalculated += 1
            # This row's own ending_balance_cents feeds the next row
            # downstream (chained or manual; the chain pointer follows
            # the linear sort_order, not status).
            prev_ending = int(updated_row["ending_balance_cents"])
        else:
            # Manual row: its stored ending_balance_cents feeds the next
            # downstream row regardless. The walk does not terminate
            # here. A manual row at the head of the walk also establishes
            # an upstream for any subsequent chained row.
            has_upstream = True
            prev_ending = int(row["ending_balance_cents"])

    return recalculated


async def _serialize_with_neighbor(
    conn: asyncpg.Connection,
    user_id: str,
    row: asyncpg.Record,
) -> dict:
    """Serialize a reconciliation row, resolving its chained-from neighbor.

    Manual rows always emit ``chained_from_reconciliation_id: null``.
    Chained rows look up the previous active neighbor by sort_order
    (could be ``null`` if this row is at position #1 of an empty
    upstream).
    """
    chained_from: Optional[str] = None
    if row["beginning_balance_source"] == BeginningBalanceSource.CHAINED:
        neighbor = await _previous_chained_neighbor(
            conn, user_id, str(row["account_id"]), row["sort_order"],
            exclude_id=str(row["id"]),
        )
        if neighbor is not None:
            chained_from = str(neighbor["id"])
    rate_by_id = await resolve_home_rates(conn, user_id, [row])
    return reconciliation_from_row(
        row,
        rate_by_id.get(str(row["id"])),
        chained_from_reconciliation_id=chained_from,
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
    beginning_balance_cents: Optional[int],
    ending_balance_cents: Optional[int],
    sort_order: Optional[int] = None,
) -> dict:
    """Validate inputs, insert a DRAFT reconciliation, and log the creation.

    Source of truth for ``beginning_balance_cents``:
      * Caller provided a value → source = MANUAL, value stored verbatim.
      * Caller omitted the value → source = CHAINED, value computed from
        the previous active neighbor in ``sort_order`` (defaulting to 0
        when no neighbor exists).

    Sort position:
      * ``sort_order`` omitted → append (max+1 for the account).
      * ``sort_order`` provided → insert at that position; existing rows
        at >= sort_order shift +1.

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

    # Resolve sort position. Insert-at-position shifts everyone at or
    # above the target up by 1 to make room.
    if sort_order is None:
        target_sort = await _next_sort_order(conn, user_id, account_id)
    else:
        if sort_order < 1:
            raise validation_error(
                "sort_order must be a positive integer.",
                {"sort_order": "Must be >= 1."},
            )
        await _shift_sort_orders_at_or_above(conn, user_id, account_id, sort_order)
        target_sort = sort_order

    # Resolve source + beginning balance.
    if beginning_balance_cents is not None:
        source = BeginningBalanceSource.MANUAL
        beginning = beginning_balance_cents
    else:
        source = BeginningBalanceSource.CHAINED
        prev = await _previous_chained_neighbor(
            conn, user_id, account_id, target_sort,
        )
        beginning = int(prev["ending_balance_cents"]) if prev else 0

    ending = ending_balance_cents if ending_balance_cents is not None else 0

    try:
        row = await conn.fetchrow(
            """
            INSERT INTO expense_reconciliations
                (id, user_id, account_id, name, date_start, date_end, status,
                 sort_order, beginning_balance_cents, beginning_balance_source,
                 ending_balance_cents, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, 1, $7, $8, $9, $10, now(), now())
            RETURNING *
            """,
            reconciliation_id,
            user_id,
            account_id,
            name.strip(),
            date_start,
            date_end,
            target_sort,
            beginning,
            int(source),
            ending,
        )
    except asyncpg.UniqueViolationError:
        raise conflict(
            f"A reconciliation with id '{reconciliation_id}' already exists."
        )

    response = await _serialize_with_neighbor(conn, user_id, row)

    await write_activity_log(
        conn, user_id, "reconciliation", str(row["id"]), ActivityAction.CREATED,
        after_snapshot=response,
    )

    # If we inserted into the middle, the row now sitting downstream may
    # be CHAINED and its "previous neighbor" just changed — cascade from
    # the new row's position so any chained downstream row catches up.
    if sort_order is not None:
        await _cascade_chained_recalc(conn, user_id, account_id, target_sort)

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
        "beginning_balance_source",
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

    Source toggle semantics:
      * ``beginning_balance_cents`` provided → forces source to MANUAL,
        value stored verbatim. Any explicit ``beginning_balance_source``
        in the same body is overridden.
      * ``beginning_balance_source = "chained"`` (without a value) →
        re-derive from the current previous neighbor. If no neighbor
        exists, leave the value alone (never silently rewrite to 0).
      * ``beginning_balance_source = "manual"`` (without a value) →
        freeze the current value as manual.

    Cascade: when ``ending_balance_cents`` changes, downstream chained
    rows recalculate per the chain rule (single transaction).

    Raises:
        not_found: no active reconciliation with that id for this user.
        validation_error: name is provided but empty after stripping, a
            locked field is edited while status=COMPLETED, or sort_order
            is included in the body.
    """
    # sort_order is reorder-endpoint-only.
    if "sort_order" in fields:
        raise validation_error(
            "sort_order cannot be edited via PUT /reconciliations/{id}.",
            {"sort_order": "Use PUT /accounts/{id}/reconciliations/order to reorder."},
        )

    # Empty update — return current state unchanged
    if not fields:
        row = await conn.fetchrow(
            "SELECT * FROM expense_reconciliations WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
            reconciliation_id,
            user_id,
        )
        if row is None:
            raise not_found("reconciliation")
        return await _serialize_with_neighbor(conn, user_id, row)

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

    # Resolve source toggle BEFORE handing off to dynamic_update. The
    # column is a smallint internally; the wire is "manual"|"chained".
    explicit_value = "beginning_balance_cents" in fields
    source_label = fields.pop("beginning_balance_source", None)

    if explicit_value:
        # Explicit value always wins — source becomes MANUAL.
        fields["beginning_balance_source"] = int(BeginningBalanceSource.MANUAL)
    elif source_label is not None:
        if source_label == "manual":
            fields["beginning_balance_source"] = int(BeginningBalanceSource.MANUAL)
        elif source_label == "chained":
            # Re-derive from current previous neighbor. Skip silently if
            # none exists — never rewrite a stored balance to 0.
            neighbor = await _previous_chained_neighbor(
                conn, user_id, str(before_row["account_id"]),
                before_row["sort_order"], exclude_id=str(before_row["id"]),
            )
            fields["beginning_balance_source"] = int(BeginningBalanceSource.CHAINED)
            if neighbor is not None:
                fields["beginning_balance_cents"] = int(neighbor["ending_balance_cents"])

    before = await _serialize_with_neighbor(conn, user_id, before_row)

    after_row = await dynamic_update(
        conn, "expense_reconciliations", fields, reconciliation_id, user_id,
    )
    if after_row is None:
        raise not_found("reconciliation")

    after = await _serialize_with_neighbor(conn, user_id, after_row)

    await write_activity_log(
        conn, user_id, "reconciliation", reconciliation_id, ActivityAction.UPDATED,
        before_snapshot=before,
        after_snapshot=after,
    )

    # Cascade if the ending balance moved.
    if int(before_row["ending_balance_cents"]) != int(after_row["ending_balance_cents"]):
        await _cascade_chained_recalc(
            conn, user_id, str(after_row["account_id"]), after_row["sort_order"],
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
        return await _serialize_with_neighbor(conn, user_id, row)

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

    before = await _serialize_with_neighbor(conn, user_id, row)

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

    after = await _serialize_with_neighbor(conn, user_id, after_row)

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
        return await _serialize_with_neighbor(conn, user_id, row)

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

    before = await _serialize_with_neighbor(conn, user_id, row)

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

    after = await _serialize_with_neighbor(conn, user_id, after_row)

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

    Cascade: the deleted row's ending balance no longer participates in
    the chain; downstream chained rows recalculate from whatever new
    previous neighbor they now resolve to.

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

    before = await _serialize_with_neighbor(conn, user_id, row)

    after_row = await soft_delete(conn, "expense_reconciliations", reconciliation_id, user_id)

    after = await _serialize_with_neighbor(conn, user_id, after_row)

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

    # Downstream chained rows now resolve to a different upstream than
    # the one their stored beginning_balance reflects. Cascade from the
    # deleted row's sort_order minus 1 so the row at the deleted position
    # itself is re-evaluated.
    await _cascade_chained_recalc(
        conn, user_id, str(row["account_id"]), int(row["sort_order"]) - 1,
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

    Cascade: the restored row re-enters the chain at its original
    ``sort_order``; downstream chained rows recalculate from the new
    upstream landscape.

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

    before = await _serialize_with_neighbor(conn, user_id, before_row)

    after_row = await restore(conn, "expense_reconciliations", reconciliation_id, user_id)
    after = await _serialize_with_neighbor(conn, user_id, after_row)

    await write_activity_log(
        conn, user_id, "reconciliation", reconciliation_id, ActivityAction.RESTORED,
        before_snapshot=before,
        after_snapshot=after,
    )

    # Cascade from one slot above the restored row's position so the
    # restored row itself is re-evaluated (if it's chained, its beginning
    # balance now needs to match the current upstream's ending balance).
    await _cascade_chained_recalc(
        conn, user_id, str(after_row["account_id"]), int(after_row["sort_order"]) - 1,
    )
    return after


# ---------------------------------------------------------------------------
# Bulk reorder
# ---------------------------------------------------------------------------


async def reorder_reconciliations(
    conn: asyncpg.Connection,
    user_id: str,
    account_id: str,
    ordered_ids: list,
) -> dict:
    """Bulk-reorder a subset of an account's reconciliations atomically.

    The ``ordered_ids`` array is the desired final order for the rows it
    lists. The engine reuses the ``sort_order`` slots currently held by
    the submitted rows (sorted ASC) and reassigns them in the new order;
    rows not in the array are untouched. Then runs the chained-balance
    cascade starting at the smallest affected sort_order.

    Raises:
        validation_error: account_id is invalid OR any id in ordered_ids
            doesn't belong to (user_id, account_id), is soft-deleted, or
            appears more than once.
    """
    # Validate account_id (raises 422 on invalid).
    await validate_active_account(conn, account_id, user_id)

    # Detect duplicates with an exact UUID match.
    seen: set[str] = set()
    str_ids: list[str] = []
    for raw in ordered_ids:
        sid = str(raw)
        if sid in seen:
            raise validation_error(
                "Reorder list contains duplicate ids.",
                {"ordered_ids": f"Duplicate id {sid} in ordered_ids."},
            )
        seen.add(sid)
        str_ids.append(sid)

    # Pull every submitted row in one query. Includes soft-deleted so we
    # can return a precise error rather than a generic "not found".
    rows = await conn.fetch(
        """
        SELECT * FROM expense_reconciliations
        WHERE user_id = $1 AND id = ANY($2::uuid[])
        """,
        user_id,
        str_ids,
    )
    by_id = {str(r["id"]): r for r in rows}

    # Validate ownership, account scope, and not-deleted.
    for sid in str_ids:
        r = by_id.get(sid)
        if r is None or str(r["account_id"]) != str(account_id):
            raise validation_error(
                "Reorder list contains an id that does not belong to this account.",
                {"ordered_ids": f"Reconciliation {sid} does not belong to this account."},
            )
        if r["deleted_at"] is not None:
            raise validation_error(
                "Reorder list contains a soft-deleted reconciliation.",
                {"ordered_ids": f"Reconciliation {sid} is soft-deleted and cannot be reordered."},
            )

    # The slots we reuse are the rows' current sort_order values, sorted ASC.
    current_slots = sorted(int(by_id[sid]["sort_order"]) for sid in str_ids)
    smallest_slot = current_slots[0]

    # Build before-snapshots for the rows whose position will actually change
    # (and capture the rows that don't change so we can return them too).
    before_by_id: dict[str, dict] = {}
    for sid in str_ids:
        before_by_id[sid] = await _serialize_with_neighbor(conn, user_id, by_id[sid])

    # Apply the new ordering. Two-phase to avoid unique-conflict-style
    # collisions if we ever add a uniqueness constraint: temporarily push
    # the affected rows to negative sort_order values, then write the
    # final values. This is overkill today (no uniqueness constraint) but
    # cheap and future-proofs the migration story.
    for offset, sid in enumerate(str_ids):
        await conn.execute(
            """
            UPDATE expense_reconciliations
            SET sort_order = $3, updated_at = now(), version = version + 1
            WHERE id = $1 AND user_id = $2
            """,
            sid,
            user_id,
            -(offset + 1),  # negative tmp slot, distinct per row
        )
    for offset, sid in enumerate(str_ids):
        new_slot = current_slots[offset]
        await conn.execute(
            """
            UPDATE expense_reconciliations
            SET sort_order = $3, updated_at = now(), version = version + 1
            WHERE id = $1 AND user_id = $2
            """,
            sid,
            user_id,
            new_slot,
        )

    # Re-fetch the affected rows in their new order and write a per-row
    # UPDATED activity log entry for each one whose sort_order actually
    # changed (no spam for "moved to the same slot it already had").
    after_rows = await conn.fetch(
        """
        SELECT * FROM expense_reconciliations
        WHERE user_id = $1 AND id = ANY($2::uuid[])
        ORDER BY sort_order ASC
        """,
        user_id,
        str_ids,
    )

    for after_row in after_rows:
        sid = str(after_row["id"])
        before_snap = before_by_id[sid]
        if before_snap["sort_order"] == after_row["sort_order"]:
            continue
        after_snap = await _serialize_with_neighbor(conn, user_id, after_row)
        await write_activity_log(
            conn, user_id, "reconciliation", sid, ActivityAction.UPDATED,
            before_snapshot=before_snap,
            after_snapshot=after_snap,
        )

    # Cascade chained balances starting just above the smallest affected
    # slot — i.e. the row at `smallest_slot` itself is re-evaluated.
    recalculated = await _cascade_chained_recalc(
        conn, user_id, account_id, smallest_slot - 1,
    )

    # One bulk activity entry on the account so the audit trail records
    # the user's intent at request granularity, not just the per-row
    # diffs above.
    await write_activity_log(
        conn, user_id, "account", str(account_id), ActivityAction.UPDATED,
        after_snapshot={"reconciliations_reordered": str_ids},
    )

    # Final response: every affected row in its new order, plus the
    # recalculated_count from the cascade walk.
    final_rows = await conn.fetch(
        """
        SELECT * FROM expense_reconciliations
        WHERE user_id = $1 AND id = ANY($2::uuid[])
        ORDER BY sort_order ASC
        """,
        user_id,
        str_ids,
    )
    serialized = [
        await _serialize_with_neighbor(conn, user_id, r) for r in final_rows
    ]
    return {
        "reconciliations": serialized,
        "recalculated_count": recalculated,
    }


