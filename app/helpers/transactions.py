"""Transaction domain logic.

Service-layer functions for expense_transactions, called from routers/transactions.py.
Routers stay thin (HTTP glue + idempotency) and delegate business logic here.

This module is the most complex service in the codebase because transactions
intersect with every other domain:

  * hashtag junction rows (via the private ``_sync_hashtags``)
  * transfer pair atomicity (via helpers.transfers.create_transfer_pair)
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
stored balance, which is now unrepresentable. What they still protect:

  * **The activity-log pair.** Every one of these flows reads a ``before_row``,
    mutates, then writes both snapshots. Without the lock the two halves can
    describe two different states and the audit trail records a change that
    never happened.
  * **Transfer-pair invariants.** ``restore_transaction`` refuses to restore one
    leg of an asymmetric pair; that check is only sound if the sibling is pinned
    while it runs.

Stated explicitly because the stale rationale would otherwise invite the next
reader to delete the locks as vestigial. They are not.

## "No-split zones"

Two flows are tight atomic units that must not be decomposed further:

  * ``create_transfer_pair`` (transfer orchestration — stays intact in
    ``app.helpers.transfers``)
  * The dynamic field-mutation chain in ``update_transaction`` — each
    conditional depends on whether specific keys are present in ``fields``

``create_batch``'s balance-delta accumulation loop used to be a third. It is
gone: it existed only to turn N per-item balance writes into K UPDATEs, and it
carried its own hand-rolled copy of the sign matrix to do so.
"""

from typing import Optional

import asyncpg

from app.constants import ActivityAction, ReconciliationStatus
from app.errors import conflict, not_found, validation_error
from app.helpers.activity_log import write_activity_log
from app.helpers.query_builder import dynamic_update
from app.helpers.validation import validate_active_account, validate_active_category
from app.schemas.transactions import (
    TransactionBatchRequest,
    TransactionCreateRequest,
    infer_transaction_type,
    transaction_from_row,
)

# Fields a transfer leg may change via PUT — everything else in ``fields`` is
# rejected by the guard in ``update_transaction``. See the comment there for
# why these three are safe and why ``hashtag_ids``/``reconciliation_id`` are
# absent (they never enter ``fields``).
ALLOWED_ON_TRANSFER_LEG = {"title", "description", "cleared"}


# ---------------------------------------------------------------------------
# hashtag_ids attach helpers (shared by every transaction-returning endpoint)
# ---------------------------------------------------------------------------

async def fetch_hashtag_ids_map(
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
          AND transaction_source = 1
          AND deleted_at IS NULL
        ORDER BY transaction_id, hashtag_id
        """,
        transaction_ids,
    )
    result: dict[str, list[str]] = {tid: [] for tid in transaction_ids}
    for r in rows:
        result.setdefault(str(r["transaction_id"]), []).append(str(r["hashtag_id"]))
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
    hashtag_map = await fetch_hashtag_ids_map(conn, ids)
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

async def _sync_hashtags(
    conn: asyncpg.Connection,
    transaction_id: str,
    user_id: str,
    hashtag_ids: Optional[list[str]],
) -> None:
    """Make the transaction's active hashtag set exactly ``hashtag_ids``.

    Replacement semantics, not delta semantics. The active set after this
    call equals ``hashtag_ids`` regardless of what was attached before.
    Uses ``transaction_source = 1`` to identify ledger junction rows.

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
        invalid = [h for h in hashtag_ids if h not in valid_ids]
        if invalid:
            raise validation_error(
                "Some hashtag IDs are invalid.",
                {"hashtag_ids": f"Invalid IDs: {', '.join(invalid)}"},
            )

    # Step 1: soft-delete the junctions *leaving* the active set.
    # ``hashtag_id <> ALL($3)`` skips rows that should stay attached,
    # so they don't get an updated_at bump for nothing. An empty
    # array makes ``<> ALL`` vacuously TRUE for every row — clearing all
    # hashtags still works (the original "soft-delete everything" case).
    await conn.execute(
        """
        UPDATE expense_transaction_hashtags
        SET deleted_at = now(), updated_at = now()
        WHERE transaction_id = $1
          AND transaction_source = 1
          AND user_id = $2
          AND deleted_at IS NULL
          AND hashtag_id <> ALL($3::uuid[])
        """,
        transaction_id,
        user_id,
        hashtag_ids or [],
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
            SELECT $1, 1, hashtag_id, $2, now(), now()
            FROM unnest($3::uuid[]) AS hashtag_id
            ON CONFLICT (transaction_id, hashtag_id) DO UPDATE
            SET deleted_at = NULL,
                updated_at = now()
            WHERE expense_transaction_hashtags.deleted_at IS NOT NULL
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
    ``transaction_type`` from the sign of ``amount_cents``, inserts the row,
    applies the balance delta, syncs hashtags, and writes an activity log entry.

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

    if not body.title or not body.title.strip():
        errors["title"] = "Must not be empty."

    if body.amount_cents == 0:
        errors["amount_cents"] = "Must not be zero."

    # category_id is required for normal transactions but ignored for
    # transfers — the transfer engine auto-assigns @Transfer/@Debt and
    # discards any category_id passed in. Only enforce it on the non-transfer
    # path so callers aren't forced to send a value the engine throws away.
    if body.transfer is None and not body.category_id:
        errors["category_id"] = "Required for non-transfer transactions."

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
            primary_cleared=body.cleared if body.cleared is not None else False,
            transfer_account_id=body.transfer.account_id,
            transfer_amount_cents=body.transfer.amount_cents,
        )

        # Hashtags on primary only
        if body.hashtag_ids:
            await _sync_hashtags(conn, primary_response["id"], user_id, body.hashtag_ids)

        await attach_hashtag_ids(conn, primary_response)
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

    # Insert
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO expense_transactions
                (id, user_id, title, description, amount_cents,
                 transaction_type, date, account_id, category_id,
                 cleared, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, now(), now())
            RETURNING *
            """,
            body.id,
            user_id,
            body.title.strip(),
            body.description,
            amount_cents,
            transaction_type,
            body.date,
            body.account_id,
            body.category_id,
            body.cleared if body.cleared is not None else False,
        )
    except asyncpg.UniqueViolationError:
        raise conflict(f"A transaction with id '{body.id}' already exists.")

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

    Transfer edit guard: if this transaction is part of a transfer pair,
    only the fields in ``ALLOWED_ON_TRANSFER_LEG`` (title, description,
    cleared) may change; anything else is rejected with 422 (transfers
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
        response = transaction_from_row(row)
        await attach_hashtag_ids(conn, response)
        return response

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

    # Transfer edit guard — allow-list. On a transfer leg, only fields with
    # no cross-leg invariant may change; everything else is rejected, so a
    # column added to TransactionUpdateRequest later is blocked here by
    # default. Transfers are otherwise edited by delete + recreate.
    #
    # Why the allowed three are safe: ``title`` and ``description`` are pure
    # display; ``cleared`` is genuinely per-leg (each leg clears at its own
    # bank on its own schedule). ``hashtag_ids`` and ``reconciliation_id``
    # never appear in ``fields`` — the router passes them as separate
    # parameters — and both are per-leg too (a reconciliation is scoped to
    # one account, and the legs are on different accounts), so they need no
    # handling here.
    #
    # Why the rest are blocked: a one-sided ``amount_cents`` change breaks
    # the pair's netting; ``account_id`` moves a leg to an account the pair
    # was never between; ``date`` lands the legs in different months, which
    # is what would let @Transfer report a spread that was never paid; and
    # ``category_id`` moves one leg out of @Transfer/@Debt, stranding the
    # sibling with nothing to cancel against. Blocked outright rather than
    # mirrored — the legs legitimately hold *different* categories (@Debt on
    # a person leg, @Transfer on the real one).
    if before_row["transfer_transaction_id"] is not None:
        blocked = set(fields) - ALLOWED_ON_TRANSFER_LEG
        if blocked:
            raise validation_error(
                "Transfer edits not yet supported.",
                {f: "Cannot modify on a transfer transaction." for f in sorted(blocked)},
            )

    # Process amount_cents change
    if "amount_cents" in fields:
        if fields["amount_cents"] == 0:
            raise validation_error(
                "amount_cents must not be zero.",
                {"amount_cents": "Must not be zero."},
            )
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

    # No balance step. A reverse-then-apply pair used to run here, guarded by a
    # `needs_balance_update` flag set wherever amount_cents or account_id
    # changed — a flag that had to be kept in step with every future field that
    # might affect a balance. Moving the row IS moving both balances now: the
    # old account stops counting it and the new one starts, in the same UPDATE.

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

    The response carries ``warnings: list[str]`` — always present on this
    endpoint, empty when the delete is clean. A transaction assigned to a
    completed reconciliation contributes a note so clients can surface that
    the reconciliation totals may now be stale.

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
    # Pre-delete hashtag_ids — must be captured BEFORE the junction-row
    # cascade-soft-delete below, otherwise the snapshot is empty and the
    # audit trail can't tell what was attached prior to delete.
    await attach_hashtag_ids(conn, before)

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

    # No balance reversal: the sum that defines the balance already excludes
    # soft-deleted rows, so setting deleted_at IS the reversal.

    # Soft-delete junction rows
    await conn.execute(
        """
        UPDATE expense_transaction_hashtags
        SET deleted_at = now(), updated_at = now()
        WHERE transaction_id = $1 AND transaction_source = 1 AND user_id = $2 AND deleted_at IS NULL
        """,
        transaction_id,
        user_id,
    )

    # Handle transfer sibling — locked so its state can't change between the
    # before-snapshot and the after-snapshot written below.
    if row["transfer_transaction_id"] is not None:
        sibling_id = str(row["transfer_transaction_id"])
        sibling_row = await conn.fetchrow(
            "SELECT * FROM expense_transactions WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL FOR UPDATE",
            sibling_id,
            user_id,
        )
        if sibling_row is not None:
            sibling_before = transaction_from_row(sibling_row)
            await attach_hashtag_ids(conn, sibling_before)

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

            await conn.execute(
                """
                UPDATE expense_transaction_hashtags
                SET deleted_at = now(), updated_at = now()
                WHERE transaction_id = $1 AND transaction_source = 1 AND user_id = $2 AND deleted_at IS NULL
                """,
                sibling_id,
                user_id,
            )
            # Sibling after-snapshot — junctions are now soft-deleted, so
            # this resolves to [] (matches the post-delete wire state).
            await attach_hashtag_ids(conn, sibling_after)

            await write_activity_log(
                conn, user_id, "transaction", sibling_id, ActivityAction.DELETED,
                before_snapshot=sibling_before,
                after_snapshot=sibling_after,
            )

    # Primary after-snapshot — junctions soft-deleted above, resolves to [].
    await attach_hashtag_ids(conn, after)

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
        recon = await fetch_recon_status(conn, user_id, row["reconciliation_id"])
        if recon and recon["status"] == ReconciliationStatus.COMPLETED:
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
            active (or the account is archived). All field-level errors
            collected into one ``fields`` dict before raising.
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
        WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
        """,
        row["category_id"],
        user_id,
    )
    if primary_category is None:
        errors["category_id"] = "Must reference an active category."

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
            WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
            """,
            sibling_row["category_id"],
            user_id,
        )
        if sibling_category is None:
            errors["transfer.category_id"] = "Must reference an active category."

    if errors:
        raise validation_error(
            "Cannot restore transaction: prerequisites failed.", errors
        )

    # 4. Resolve the reconciliation decision per leg.
    async def _resolve_recon_unlink(recon_id) -> tuple[bool, Optional[str]]:
        if recon_id is None:
            return False, None
        recon = await fetch_recon_status(conn, user_id, recon_id)
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
    # Soft-deleted state: cascade-soft-deleted junctions resolve to [] here.
    await attach_hashtag_ids(conn, before)
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

    # 6. Re-activate cascaded junction rows on the primary.
    # (There is no balance step to mirror the delete's reversal: clearing
    # deleted_at above puts the row back into the sum that defines the balance.)
    await conn.execute(
        """
        UPDATE expense_transaction_hashtags
        SET deleted_at = NULL, updated_at = now()
        WHERE transaction_id = $1 AND transaction_source = 1
          AND user_id = $2 AND deleted_at = $3
        """,
        transaction_id,
        user_id,
        primary_deleted_at,
    )

    # 7. Sibling cascade (mirror steps 5-6).
    if is_transfer and sibling_row is not None and sibling_id is not None:
        sibling_before = transaction_from_row(sibling_row)
        # Soft-deleted state → []; attach for §6 audit consistency.
        await attach_hashtag_ids(conn, sibling_before)
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

        await conn.execute(
            """
            UPDATE expense_transaction_hashtags
            SET deleted_at = NULL, updated_at = now()
            WHERE transaction_id = $1 AND transaction_source = 1
              AND user_id = $2 AND deleted_at = $3
            """,
            sibling_id,
            user_id,
            sibling_deleted_at,
        )
        # Post-restore: junctions are active again → resolves to the restored set.
        await attach_hashtag_ids(conn, sibling_after)

        # Sibling activity log first (matches delete_transaction's order).
        await write_activity_log(
            conn, user_id, "transaction", sibling_id, ActivityAction.RESTORED,
            before_snapshot=sibling_before,
            after_snapshot=sibling_after,
        )

    # Primary post-restore: junctions are active again → restored set.
    await attach_hashtag_ids(conn, after)

    # 8. Primary activity log.
    await write_activity_log(
        conn, user_id, "transaction", transaction_id, ActivityAction.RESTORED,
        before_snapshot=before,
        after_snapshot=after,
    )

    # 9. Build warnings list (always present; empty when restore is clean).
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
    # category_id may be None now that the schema makes it optional; drop None
    # from the lookup set (missing-category items are caught by the presence
    # check below) so we never pass NULL into the uuid[] membership query.
    requested_category_ids = {
        item.category_id for item in body.transactions if item.category_id
    }

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

        # category_id is optional on the schema (transfers waive it), but batch
        # rejects transfers, so it's always required here. Report a clean
        # "required" message instead of a misleading referential error.
        if not item.category_id:
            item_errors["category_id"] = "Required for non-transfer transactions."
        elif item.category_id not in valid_category_ids:
            item_errors["category_id"] = "Must reference an active category."

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
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO expense_transactions
                    (id, user_id, title, description, amount_cents,
                     transaction_type, date, account_id, category_id,
                     cleared, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, now(), now())
                RETURNING *
                """,
                item.id,
                user_id,
                item.title.strip(),
                item.description,
                amount_cents,
                transaction_type,
                item.date,
                item.account_id,
                item.category_id,
                item.cleared if item.cleared is not None else False,
            )
        except asyncpg.UniqueViolationError:
            raise conflict(f"A transaction with id '{item.id}' already exists.")

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
