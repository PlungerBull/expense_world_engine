"""Transaction domain logic.

Service-layer functions for expense_transactions, called from routers/transactions.py.
Routers stay thin (HTTP glue + idempotency) and delegate business logic here.

This module is the most complex service in the codebase because transactions
intersect with every other domain:

  * hashtag junction rows (via the private ``_sync_hashtags``)
  * reconciliation field-locking and cascade unassignment

## Account balances are not written here

Nothing in this module touches ``expense_bank_accounts``. An account's balance
is the signed sum of its non-deleted transactions (sql/022,
``helpers/account_balance``), so inserting, editing, soft-deleting or restoring
a row below IS the balance change — atomically, with no second write to keep in
step and no path that can forget one.

## Transaction boundaries and locks

Like every other helper, these functions do NOT open their own
``conn.transaction()`` — callers own transaction boundaries
(``helpers/idempotency.run_idempotent``).

The ``FOR UPDATE`` locks in ``update_transaction``, ``delete_transaction`` and
``restore_transaction`` are still load-bearing, but **not for the reason they
were added.** They were introduced to stop a concurrent write changing
``amount_cents`` between our read and our balance reversal — a lost update on a
stored balance, which is now unrepresentable. What they still protect: **the
activity-log pair.** Every one of these flows reads a ``before_row``, mutates,
then writes both snapshots. Without the lock the two halves can describe two
different states and the audit trail records a change that never happened.

Stated explicitly because the stale rationale would otherwise invite the next
reader to delete the locks as vestigial. They are not.

## "No-split zones"

One flow is a tight atomic unit that must not be decomposed further: the
dynamic field-mutation chain in ``update_transaction`` — each conditional
depends on whether specific keys are present in ``fields``.

``create_batch``'s balance-delta accumulation loop used to be a second. It is
gone: it existed only to turn N per-item balance writes into K UPDATEs, and it
carried its own hand-rolled copy of the sign matrix to do so.
"""

from typing import Optional

import asyncpg

from app.constants import ActivityAction, ReconciliationStatus, TransactionSource
from app.errors import conflict, not_found, validation_error
from app.helpers.activity_log import write_activity_log
from app.helpers.query_builder import (
    dynamic_update,
    fetch_owned_row_or_404,
    soft_delete,
)
from app.helpers.validation import (
    MSG_ACTIVE_ACCOUNT,
    MSG_ACTIVE_CATEGORY,
    MSG_NOT_EMPTY,
    MSG_NOT_FUTURE,
    MSG_NOT_ZERO,
    active_account_ids,
    active_account_row,
    active_category_ids,
    active_category_row,
    clean_name,
    db_now,
    normalize_name,
    reject_zero_amount,
    validate_active_account,
    validate_active_category,
)
from app.schemas.transactions import (
    TransactionBatchRequest,
    TransactionCreateRequest,
    infer_transaction_type,
    transaction_from_row,
)

# ---------------------------------------------------------------------------
# hashtag_ids attach helpers (shared by every transaction-returning endpoint)
# ---------------------------------------------------------------------------

async def _fetch_hashtag_ids_map(
    conn: asyncpg.Connection,
    transaction_ids: list[str],
) -> dict[str, list[str]]:
    """Resolve active hashtag IDs for a set of ledger transactions.

    Returns ``{transaction_id: [hashtag_id, ...]}`` with each list sorted
    ascending by UUID (one stable convention everywhere). Soft-deleted
    junction rows are excluded — when a transaction is soft-deleted its
    junctions cascade-soft-delete, so deleted transactions resolve to ``[]``.

    Returns an empty mapping for an empty input — never queries.
    """
    if not transaction_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT transaction_id, hashtag_id
        FROM expense_transaction_hashtags
        WHERE transaction_id = ANY($1::uuid[])
          AND transaction_source = $2
          AND deleted_at IS NULL
        ORDER BY transaction_id, hashtag_id
        """,
        transaction_ids,
        int(TransactionSource.LEDGER),
    )
    result: dict[str, list[str]] = {str(tid): [] for tid in transaction_ids}
    for r in rows:
        result[str(r["transaction_id"])].append(str(r["hashtag_id"]))
    return result


async def attach_hashtag_ids(conn: asyncpg.Connection, payload) -> None:
    """Mutate one transaction dict (or a list of them) to include ``hashtag_ids``.

    Per api-design-principles.md §3a, every transaction-returning endpoint
    flattens the junction relationship to an embedded array. Call this at
    each response site after building the transaction dict via
    ``transaction_from_row``. One query covers a whole list — list endpoints
    pay a single round trip regardless of page size.
    """
    items = [payload] if isinstance(payload, dict) else list(payload)
    if not items:
        return
    ids = [item["id"] for item in items]
    hashtag_map = await _fetch_hashtag_ids_map(conn, ids)
    for item in items:
        item["hashtag_ids"] = hashtag_map.get(item["id"], [])


async def fetch_recon_status(
    conn: asyncpg.Connection,
    user_id: str,
    recon_id,
) -> Optional[asyncpg.Record]:
    """Fetch ``(status, deleted_at)`` for a reconciliation, tenant-scoped.

    Post-fetch coherence read: ``recon_id`` always comes off an
    already-scoped transaction row, so a ``user_id`` mismatch is impossible
    by construction — but the predicate is still mandatory (a missing
    ``user_id`` filter is a security defect, not a tidiness one).

    Soft-deleted rows ARE returned: ``restore_transaction`` distinguishes
    "recon deleted" from "recon completed", so this must not filter on
    ``deleted_at`` — callers that only care about COMPLETED ignore it.

    Returns ``None`` when no row the caller owns exists; callers treat that
    exactly as "no reconciliation" (no field lock, no warning, unlink on
    restore) — the same behaviour a genuinely missing row gets.
    """
    return await conn.fetchrow(
        "SELECT status, deleted_at FROM expense_reconciliations"
        " WHERE id = $1 AND user_id = $2",
        recon_id,
        user_id,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

async def _cascade_junctions_delete(
    conn: asyncpg.Connection,
    user_id: str,
    transaction_id: str,
    *,
    keep_hashtag_ids: Optional[list[str]] = None,
) -> None:
    """Soft-delete the transaction's active ledger junction rows.

    The single producer of junction ``deleted_at`` markers — used by the
    delete cascade and by ``_sync_hashtags`` step 1.

    **The marker is load-bearing.** Postgres ``now()`` returns
    ``transaction_timestamp()`` — one value per DB transaction — so every
    junction row soft-deleted here carries the exact timestamp the parent
    row got in the same transaction. ``_cascade_junctions_restore``
    re-activates by exact ``deleted_at`` match against the parent's
    marker, which catches precisely the rows this cascade dropped and not
    soft-deleted junctions left by earlier ``_sync_hashtags`` runs.

    ``keep_hashtag_ids`` narrows the cascade to rows *leaving* the active
    set (rows staying attached get no updated_at bump for nothing). An
    empty or omitted list makes ``<> ALL`` vacuously TRUE — everything
    active is dropped.

    Deliberate non-adopter: ``hashtags.delete_hashtag`` pivots on
    ``hashtag_id`` across all transactions, skips the
    ``transaction_source`` filter, and needs ``RETURNING transaction_id``
    — a different operation, not another copy of this one.
    """
    await conn.execute(
        """
        UPDATE expense_transaction_hashtags
        SET deleted_at = now(), updated_at = now()
        WHERE transaction_id = $1
          AND transaction_source = $4
          AND user_id = $2
          AND deleted_at IS NULL
          AND hashtag_id <> ALL($3::uuid[])
        """,
        transaction_id,
        user_id,
        keep_hashtag_ids or [],
        int(TransactionSource.LEDGER),
    )


async def _cascade_junctions_restore(
    conn: asyncpg.Connection,
    user_id: str,
    transaction_id: str,
    deleted_at_marker,
) -> None:
    """Re-activate the junction rows cascaded by THIS transaction's delete.

    Matches ``deleted_at = $marker`` exactly, with ``$marker`` bound to
    the parent's pre-restore ``deleted_at`` — see
    ``_cascade_junctions_delete`` for why the equality is precise.
    """
    await conn.execute(
        """
        UPDATE expense_transaction_hashtags
        SET deleted_at = NULL, updated_at = now()
        WHERE transaction_id = $1 AND transaction_source = $4
          AND user_id = $2 AND deleted_at = $3
        """,
        transaction_id,
        user_id,
        deleted_at_marker,
        int(TransactionSource.LEDGER),
    )


async def _sync_hashtags(
    conn: asyncpg.Connection,
    transaction_id: str,
    user_id: str,
    hashtag_ids: Optional[list[str]],
) -> None:
    """Make the transaction's active hashtag set exactly ``hashtag_ids``.

    Replacement semantics, not delta semantics. The active set after this
    call equals ``hashtag_ids`` regardless of what was attached before.
    Uses ``TransactionSource.LEDGER`` to identify ledger junction rows.

    Implementation: a narrowed soft-delete drops only the rows *leaving*
    the active set, and an ``ON CONFLICT DO UPDATE`` upsert handles the
    rows joining or staying. Two key properties fall out:

      1. **Re-attach safety.** The junction table's
         ``UNIQUE (transaction_id, hashtag_id)`` is unconditional —
         soft-deleted rows still occupy the slot. The previous "soft-
         delete-everything + plain INSERT" pattern hit a UNIQUE
         violation any time the new set overlapped with the old set
         (e.g. PUT ``[A]`` → ``[A, B]``) or re-attached a
         previously-deleted hashtag. ``ON CONFLICT DO UPDATE`` flips
         ``deleted_at`` back to NULL on the existing row instead.

      2. **Stable junction IDs.** Attach → detach → re-attach cycles
         keep the same junction row (one row per logical pair forever),
         instead of accumulating N+1 rows per cycle — a single junction
         lifecycle, not phantom rows.

    The ``DO UPDATE`` clause only fires on rows that were soft-deleted
    (``WHERE expense_transaction_hashtags.deleted_at IS NOT NULL``), so
    rows that are already active are left fully untouched — no
    ``version`` or ``updated_at`` churn on no-op transitions.

    Activity log — deliberate aggregation exception: junction rows are
    mutated here without per-row ``activity_log`` entries. The parent
    transaction's UPDATED snapshot carries the new ``hashtag_ids`` list,
    so the change is captured at parent granularity. See
    api-design-principles.md §6 exception #1.
    """
    if hashtag_ids:
        valid = await conn.fetch(
            """
            SELECT id FROM expense_hashtags
            WHERE id = ANY($1::uuid[])
              AND user_id = $2
              AND deleted_at IS NULL
            """,
            hashtag_ids,
            user_id,
        )
        valid_ids = {str(r["id"]) for r in valid}
        # str() both sides: request models supply uuid.UUID since open-bugs
        # 6.6 closed; a UUID is never `in` a set[str].
        invalid = [str(h) for h in hashtag_ids if str(h) not in valid_ids]
        if invalid:
            raise validation_error(
                "Some hashtag IDs are invalid.",
                {"hashtag_ids": f"Invalid IDs: {', '.join(invalid)}"},
            )

    # Step 1: soft-delete the junctions *leaving* the active set.
    await _cascade_junctions_delete(
        conn, user_id, transaction_id, keep_hashtag_ids=hashtag_ids,
    )

    # Step 2: upsert the new set in one statement. ON CONFLICT re-activates
    # rows that exist but were soft-deleted; rows that don't exist get
    # plain INSERT semantics; rows that are already active are skipped via
    # the WHERE on DO UPDATE (no churn).
    if hashtag_ids:
        await conn.execute(
            """
            INSERT INTO expense_transaction_hashtags
                (transaction_id, transaction_source, hashtag_id, user_id, created_at, updated_at)
            SELECT $1, $4, hashtag_id, $2, now(), now()
            FROM unnest($3::uuid[]) AS hashtag_id
            ON CONFLICT (transaction_id, hashtag_id) DO UPDATE
            SET deleted_at = NULL,
                updated_at = now()
            WHERE expense_transaction_hashtags.deleted_at IS NOT NULL
            """,
            transaction_id,
            user_id,
            hashtag_ids,
            int(TransactionSource.LEDGER),
        )


async def insert_transaction_row(
    conn: asyncpg.Connection,
    user_id: str,
    *,
    transaction_id,
    title: str,
    description: Optional[str],
    amount_cents: int,
    transaction_type: int,
    date,
    account_id,
    category_id,
    cleared: bool = False,
    inbox_id=None,
) -> asyncpg.Record:
    """The one INSERT INTO expense_transactions (create, batch, promote).
    Every column always appears in the column list — absent concepts bind
    NULL/False — so a future column cannot be silently missed at one of the
    sites (the create_batch sign-matrix incident's failure shape).
    ``reconciliation_id`` is deliberately not a parameter: no insert path
    assigns it; it is only ever set by later UPDATEs.

    Values are the STORAGE forms: ``amount_cents`` positive with
    ``transaction_type`` carrying direction, ``title`` as the caller wants
    it stored (create/batch strip request input; promote copies the inbox
    title verbatim — see open-bugs "inbox-title").

    Owns the single UniqueViolation → 409 translation for this table.
    """
    try:
        return await conn.fetchrow(
            """
            INSERT INTO expense_transactions
                (id, user_id, title, description, amount_cents,
                 transaction_type, date, account_id, category_id,
                 cleared, inbox_id,
                 created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                    now(), now())
            RETURNING *
            """,
            transaction_id,
            user_id,
            title,
            description,
            amount_cents,
            transaction_type,
            date,
            account_id,
            category_id,
            cleared,
            inbox_id,
        )
    except asyncpg.UniqueViolationError:
        raise conflict(f"A transaction with id '{transaction_id}' already exists.")


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

async def create_transaction(
    conn: asyncpg.Connection,
    user_id: str,
    body: TransactionCreateRequest,
) -> dict:
    """Create a transaction in the ledger.

    Validates account/category existence, infers ``transaction_type`` from
    the sign of ``amount_cents``, inserts the row, syncs hashtags, and
    writes an activity log entry.

    **No currency work happens here.** The row is stored in its account's own
    currency and converted at read time (helpers/home_currency.py). Recording
    what happened is never blocked by a rate lookup — so a cross-currency write
    succeeds while the FX job is stale, and a transaction dated before the
    provider floor is recordable. sql/021 has the reasoning.

    Raises:
        validation_error: any field validation or referential check fails.
    """
    # Validate shared fields — collect all failures
    errors: dict = {}

    title = clean_name(body.title)
    if title is None:
        errors["title"] = MSG_NOT_EMPTY

    if body.amount_cents == 0:
        errors["amount_cents"] = MSG_NOT_ZERO

    if body.date > await db_now(conn):
        errors["date"] = MSG_NOT_FUTURE

    if errors:
        raise validation_error("Transaction validation failed.", errors)

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

    row = await insert_transaction_row(
        conn, user_id,
        transaction_id=body.id,
        title=title,
        description=body.description,
        amount_cents=amount_cents,
        transaction_type=transaction_type,
        date=body.date,
        account_id=body.account_id,
        category_id=body.category_id,
        cleared=body.cleared if body.cleared is not None else False,
    )

    response = transaction_from_row(row)

    # Hashtags
    if body.hashtag_ids:
        await _sync_hashtags(conn, str(row["id"]), user_id, body.hashtag_ids)

    # Resolve hashtag_ids before snapshotting — the activity-log after_snapshot
    # carries the new hashtag set per §6 aggregate exception #1.
    await attach_hashtag_ids(conn, response)

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
    ``fields`` dict is mutated in place as derived columns (``transaction_type``
    from the sign of ``amount_cents``) are computed from the requested
    changes. Balance reversal + re-apply happens in the
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
        row = await fetch_owned_row_or_404(
            conn, "expense_transactions", transaction_id, user_id, "transaction"
        )
        response = transaction_from_row(row)
        await attach_hashtag_ids(conn, response)
        return response

    # Fetch before-state under a row-level lock. Without FOR UPDATE a
    # concurrent update could change `amount_cents` between our read
    # and our balance reversal below, causing a lost-update and
    # silently corrupting the account balance. The lock is released
    # automatically when the surrounding transaction commits.
    before_row = await fetch_owned_row_or_404(
        conn, "expense_transactions", transaction_id, user_id, "transaction",
        for_update=True,
    )

    before = transaction_from_row(before_row)
    # Capture pre-mutation hashtag_ids — activity-log before_snapshot must
    # reflect the prior state per §6 aggregate exception #1.
    await attach_hashtag_ids(conn, before)

    # Field locking check — reconciliation completed
    if before_row["reconciliation_id"] is not None:
        recon = await fetch_recon_status(conn, user_id, before_row["reconciliation_id"])
        if recon and recon["status"] == ReconciliationStatus.COMPLETED:
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
        # str() the whole expression: fields["account_id"] is uuid.UUID since
        # open-bugs 6.6 closed, and str != UUID is always True.
        effective_account_id = str(fields.get("account_id") or before_row["account_id"])
        if str(recon["account_id"]) != effective_account_id:
            raise validation_error(
                "Reconciliation validation failed.",
                {"reconciliation_id": "Reconciliation account does not match transaction account."},
            )
        if recon["status"] == ReconciliationStatus.COMPLETED:
            raise validation_error(
                "Reconciliation validation failed.",
                {"reconciliation_id": "Cannot assign transactions to a completed reconciliation."},
            )

    # Process amount_cents change
    if "amount_cents" in fields:
        reject_zero_amount(fields["amount_cents"])
        fields["transaction_type"] = infer_transaction_type(fields["amount_cents"])
        fields["amount_cents"] = abs(fields["amount_cents"])

    # No re-rating happens here, and that is the whole of open bug 1.5.
    # This is where a `date`-keyed re-rate block used to live: it refreshed
    # `exchange_rate` and recomputed `amount_home_cents`, but only when the
    # request changed `date`. The ACCOUNT decides the currency, so an
    # `account_id`-only PUT moved the balance correctly and left the stored
    # conversion pointing at the old currency forever. sql/021 deleted the
    # columns; there is no longer a derived value to keep in sync.

    # Validate new account_id if changing
    if "account_id" in fields:
        await validate_active_account(conn, fields["account_id"], user_id)

    # Validate new category_id if changing
    if "category_id" in fields:
        await validate_active_category(conn, fields["category_id"], user_id)

    # Validate title if changing
    if "title" in fields:
        fields["title"] = normalize_name(fields["title"], field="title")

    # Validate date if changing
    if "date" in fields:
        if fields["date"] > await db_now(conn):
            raise validation_error(
                "Date validation failed.",
                {"date": MSG_NOT_FUTURE},
            )

    # No balance step. A reverse-then-apply pair used to run here, guarded by a
    # `needs_balance_update` flag set wherever amount_cents or account_id
    # changed — a flag that had to be kept in step with every future field that
    # might affect a balance. Moving the row IS moving both balances now: the
    # old account stops counting it and the new one starts, in the same UPDATE.

    # Empty `fields` (hashtag- or reconciliation-only change) still bumps
    # version: dynamic_update always appends updated_at/version, so the
    # zero-field call is exactly the bump.
    after_row = await dynamic_update(conn, "expense_transactions", fields, transaction_id, user_id)
    if after_row is None:
        raise not_found("transaction")

    # Sync hashtags if provided
    if hashtag_ids is not None:
        await _sync_hashtags(conn, transaction_id, user_id, hashtag_ids)

    # Apply reconciliation_id change
    if recon_id_provided:
        after_row = await dynamic_update(
            conn,
            "expense_transactions",
            {"reconciliation_id": recon_id_value},
            transaction_id,
            user_id,
        )
        # Unreachable while the FOR UPDATE lock above holds, but fail closed
        # rather than serialize a None row if that ever changes.
        if after_row is None:
            raise not_found("transaction")

    after = transaction_from_row(after_row)
    # Post-mutation hashtag_ids — applies whether hashtag_ids was rewritten
    # this PUT or not (other field edits still surface the current set).
    await attach_hashtag_ids(conn, after)

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

async def _recon_warning(conn: asyncpg.Connection, user_id: str, recon_id) -> Optional[str]:
    """Warning text when a deleted leg belonged to a COMPLETED reconciliation.

    Deletion is never blocked by the reconciliation — the warning tells
    the client the reconciliation's totals may now be stale.
    """
    if recon_id is None:
        return None
    recon = await fetch_recon_status(conn, user_id, recon_id)
    if recon and recon["status"] == ReconciliationStatus.COMPLETED:
        return (
            "Transaction belonged to a completed reconciliation. "
            "Reconciliation totals may be stale."
        )
    return None


async def delete_transaction(
    conn: asyncpg.Connection,
    user_id: str,
    transaction_id: str,
) -> dict:
    """Soft-delete a transaction.

    The response carries ``warnings: list[str]`` — always present on this
    endpoint, empty when the delete is clean: a transaction sitting in a
    completed reconciliation contributes a staleness note.

    The row is locked with ``FOR UPDATE`` before mutation so the
    activity-log before/after snapshots describe one state of the row —
    same hazard as ``update_transaction``, same mitigation.

    Not routed through ``query_builder.soft_delete_with_audit``: the
    after-snapshot needs the *async* ``attach_hashtag_ids``, and it must
    run after the junction cascade so the snapshot shows the post-delete
    wire state (``[]``) — ``_mutate_with_audit``'s sync ``serialize`` can
    express neither.
    """
    row = await fetch_owned_row_or_404(
        conn, "expense_transactions", transaction_id, user_id, "transaction",
        for_update=True,
    )

    before = transaction_from_row(row)
    # Pre-delete hashtag_ids — captured BEFORE the junction cascade below,
    # otherwise the snapshot is empty and the audit trail can't tell what
    # was attached prior to delete.
    await attach_hashtag_ids(conn, before)

    after_row = await soft_delete(conn, "expense_transactions", transaction_id, user_id)
    after = transaction_from_row(after_row)

    # No balance reversal: the sum that defines the balance already excludes
    # soft-deleted rows, so setting deleted_at IS the reversal.
    await _cascade_junctions_delete(conn, user_id, transaction_id)

    # After-snapshot — junctions soft-deleted above, resolves to []
    # (matches the post-delete wire state).
    await attach_hashtag_ids(conn, after)

    await write_activity_log(
        conn, user_id, "transaction", transaction_id, ActivityAction.DELETED,
        before_snapshot=before,
        after_snapshot=after,
    )

    # Warnings channel — always present (null-over-omission).
    warnings: list[str] = []
    warning = await _recon_warning(conn, user_id, row["reconciliation_id"])
    if warning is not None:
        warnings.append(warning)

    return {**after, "warnings": warnings}


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

async def restore_transaction(
    conn: asyncpg.Connection,
    user_id: str,
    transaction_id: str,
) -> dict:
    """Undo a soft-delete on a transaction.

    Inverse of ``delete_transaction``. Re-activates the cascaded hashtag
    junction rows (matched by exact ``deleted_at`` timestamp). The whole
    flow is atomic — caller owns ``conn.transaction()``.

    **Reconciliation handling.** The transaction's ``reconciliation_id``
    survived the delete on the soft-deleted row. On restore the link is
    conditionally cleared:

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

    **Junction rows.** Restored precisely by exact ``deleted_at`` match —
    see ``_cascade_junctions_delete`` for the marker contract.

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
        validation_error: account/category is no longer active (or the
            account is archived). All field-level errors collected into
            one ``fields`` dict before raising.
    """
    # 1. Lock the soft-deleted row. The lock is why the UPDATE below needs
    #    no ``deleted_at IS NOT NULL`` predicate — the row is known
    #    soft-deleted and can't change under us.
    row = await fetch_owned_row_or_404(
        conn, "expense_transactions", transaction_id, user_id, "transaction",
        deleted=True, for_update=True,
    )

    # 2. Validate prerequisites (collect-all-failures pattern).
    errors: dict = {}

    if await active_account_row(conn, row["account_id"], user_id) is None:
        errors["account_id"] = MSG_ACTIVE_ACCOUNT

    if await active_category_row(conn, row["category_id"], user_id) is None:
        errors["category_id"] = MSG_ACTIVE_CATEGORY

    if errors:
        raise validation_error(
            "Cannot restore transaction: prerequisites failed.", errors
        )

    # 3. Resolve the reconciliation decision.
    unlink = False
    warning: Optional[str] = None
    if row["reconciliation_id"] is not None:
        recon = await fetch_recon_status(conn, user_id, row["reconciliation_id"])
        if recon is None or recon["deleted_at"] is not None:
            unlink = True
            warning = (
                "Transaction's previous reconciliation no longer exists. "
                "Link removed on restore."
            )
        elif recon["status"] == ReconciliationStatus.COMPLETED:
            unlink = True
            warning = (
                "Transaction's previous reconciliation is completed. "
                "Link removed on restore — reassign manually if needed."
            )

    # 4. Restore: snapshot, un-delete (conditionally clearing
    #    ``reconciliation_id``), re-activate junctions, log.
    #    ``query_builder.restore`` is not used because it hard-codes its
    #    SET list and cannot express the conditional unlink.
    before = transaction_from_row(row)
    # Soft-deleted state: cascade-soft-deleted junctions resolve to [] here.
    await attach_hashtag_ids(conn, before)
    deleted_at_marker = row["deleted_at"]

    after_row = await conn.fetchrow(
        """
        UPDATE expense_transactions
        SET deleted_at = NULL,
            reconciliation_id = CASE WHEN $3 THEN NULL ELSE reconciliation_id END,
            updated_at = now(), version = version + 1
        WHERE id = $1 AND user_id = $2
        RETURNING *
        """,
        transaction_id,
        user_id,
        unlink,
    )
    after = transaction_from_row(after_row)

    # (There is no balance step to mirror the delete's reversal: clearing
    # deleted_at above puts the row back into the sum that defines the balance.)
    await _cascade_junctions_restore(conn, user_id, transaction_id, deleted_at_marker)

    # Post-restore: junctions are active again → resolves to the restored set.
    await attach_hashtag_ids(conn, after)

    await write_activity_log(
        conn, user_id, "transaction", transaction_id, ActivityAction.RESTORED,
        before_snapshot=before,
        after_snapshot=after,
    )

    # 5. Build warnings list (always present; empty when restore is clean).
    warnings: list[str] = []
    if warning is not None:
        warnings.append(warning)

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
    fast if any), then inserts all rows in one loop.

    Returns a dict ``{"created": list[dict]}`` — the caller wraps this
    in a JSONResponse with status 201.
    """
    if not body.transactions:
        raise validation_error(
            "Batch must contain at least one transaction.",
            {"transactions": "Must not be empty."},
        )

    # One clock read for the whole batch, not one per item.
    now = await db_now(conn)

    # Pre-validate all items. Account and category existence checks
    # are vectorised: instead of firing 2 queries per item (2N total),
    # we collect the distinct IDs referenced across the whole batch and
    # validate them in 2 queries. Membership is then checked in memory.
    # A 100-item batch drops from 200 validation queries to 2.
    requested_account_ids = {item.account_id for item in body.transactions}
    requested_category_ids = {item.category_id for item in body.transactions}

    valid_account_ids = await active_account_ids(
        conn, requested_account_ids, user_id
    )
    valid_category_ids = await active_category_ids(
        conn, requested_category_ids, user_id
    )

    all_errors = []
    seen_ids: set[str] = set()
    for i, item in enumerate(body.transactions):
        item_errors: dict = {}

        if clean_name(item.title) is None:
            item_errors["title"] = MSG_NOT_EMPTY
        if item.amount_cents == 0:
            item_errors["amount_cents"] = MSG_NOT_ZERO
        if item.date > now:
            item_errors["date"] = MSG_NOT_FUTURE

        # str(): item FKs are uuid.UUID since open-bugs 6.6 closed, and the
        # valid_* sets hold strings.
        if str(item.account_id) not in valid_account_ids:
            item_errors["account_id"] = MSG_ACTIVE_ACCOUNT

        if str(item.category_id) not in valid_category_ids:
            item_errors["category_id"] = MSG_ACTIVE_CATEGORY

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

    # Process all items
    created = []

    for item in body.transactions:
        transaction_type = infer_transaction_type(item.amount_cents)
        amount_cents = abs(item.amount_cents)

        # No rate lookup per item. This used to fire one `lookup_exchange_rate`
        # round-trip for every transaction in the batch — an N-query loop inside
        # the one path built specifically to avoid them (see the balance-delta
        # accumulation below, which does K UPDATEs for N items).
        row = await insert_transaction_row(
            conn, user_id,
            transaction_id=item.id,
            title=clean_name(item.title),
            description=item.description,
            amount_cents=amount_cents,
            transaction_type=transaction_type,
            date=item.date,
            account_id=item.account_id,
            category_id=item.category_id,
            cleared=item.cleared if item.cleared is not None else False,
        )

        response = transaction_from_row(row)
        created.append(response)

        # Hashtags
        if item.hashtag_ids:
            await _sync_hashtags(conn, str(row["id"]), user_id, item.hashtag_ids)

        # Activity log
        await write_activity_log(
            conn, user_id, "transaction", str(row["id"]), ActivityAction.CREATED,
            after_snapshot=response,
        )

    # The accumulate-then-apply balance loop that used to close this function is
    # gone. It existed to collapse N per-item balance writes into K UPDATEs, and
    # to do that it carried its OWN copy of the sign matrix, inline — a third
    # rendering that the `apply_balance` grep never surfaced and that no test
    # covered. Exactly the drift CLAUDE.md's "one rendering of the sign matrix"
    # rule is about. Nothing replaces it: the K UPDATEs were the optimisation,
    # and there is no longer anything to optimise.

    # Single round-trip resolves hashtag_ids for the whole batch.
    await attach_hashtag_ids(conn, created)

    return {"created": created}
