"""Inbox domain logic.

Service-layer functions for expense_transaction_inbox, called from
routers/inbox.py. Routers stay thin (HTTP glue + idempotency) and
delegate business logic here.

## Promote flow (the interesting one)

``promote_inbox_item`` validates readiness, inserts the
expense_transactions row, then cleans up: the inbox row is soft-deleted
with ``status = 2`` (PROMOTED) and an activity log entry is written.
The cleanup MUST happen in the same call (not the caller's
responsibility) — otherwise a partial failure could orphan an inbox row
with its status still set to PENDING.

No step touches an account balance. Balances are the signed sum of the
ledger (sql/022), so writing the row IS the balance change.

The inbox row is locked with ``FOR UPDATE`` at the start of the promote
flow so two concurrent promotes can't create duplicate transactions
from the same inbox item.

## No restore (owner decision 2026-08-14)

There is no ``restore_inbox_item`` and no ``POST /inbox/{id}/restore``.
Every other soft-deletable resource has one — the spec's "Restore
semantics" convention names the inbox as its single exception, because
a draft is the one thing here that is not a financial record. A draft
is something you wrote; deciding it was wrong and dismissing it is the
end of it, not a state to come back from.

Delete stays a *soft* delete regardless: the row keeps its data,
``?include_deleted=true`` still lists it, and the activity log still
holds the before-snapshot. Nothing is erased — there is simply no route
that un-dismisses. (The route that used to exist also carried a 409 arm
for promoted rows, guarding against a second promote of the same draft;
removing the route removes that hazard rather than managing it.)
"""

from typing import Optional
from uuid import UUID

import asyncpg

from app.constants import ActivityAction, InboxStatus, TransactionSource
from app.errors import conflict, not_found, validation_error
from app.helpers.activity_log import write_activity_log
from app.helpers.hashtag_links import (
    attach_hashtag_ids,
    cascade_delete,
    fetch_hashtag_ids_map,
    sync_hashtags,
)
from app.helpers.query_builder import (
    dynamic_update,
    fetch_owned_row_or_404,
    soft_delete,
)
from app.helpers.transactions import insert_transaction_row
from app.helpers.validation import (
    MSG_ACTIVE_ACCOUNT,
    MSG_ACTIVE_CATEGORY,
    MSG_USER_CATEGORY,
    active_account_row,
    active_category_row,
    clean_name,
    db_now,
    extract_update_fields,
    reject_zero_amount,
    validate_active_account,
    validate_active_category,
)
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
    transaction_type, abs the amount).

    ``hashtag_ids`` is stored as junction rows under
    ``TransactionSource.INBOX``, and every id must reference an active
    hashtag (the one reference rule the inbox does not relax — see
    ``hashtag_links.sync_hashtags``). A rejection there aborts the create:
    the caller's transaction rolls the INSERT back with it.
    """
    # Signs are consumed in this block and nowhere else. Everything below sees
    # only an absolute amount plus the encoded direction — the same contract
    # the ledger has.
    primary_signed = body.amount_cents
    reject_zero_amount(primary_signed)

    amount_cents = abs(primary_signed) if primary_signed is not None else None
    transaction_type: Optional[int] = None
    if primary_signed is not None:
        transaction_type = infer_transaction_type(primary_signed)

    # A supplied reference must point at an active, tenant-owned resource;
    # both fields stay optional. Deliberately no is_system arm, unlike the
    # ledger write path: a draft may carry a system category — promote is
    # where it gets refused (test_system_category_boundary pins this).
    if body.account_id is not None:
        await validate_active_account(conn, body.account_id, user_id)
    if body.category_id is not None:
        await validate_active_category(conn, body.category_id, user_id)

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
                 created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now(), now())
            RETURNING *
            """,
            body.id,
            user_id,
            # A whitespace-only title is an unfilled field, not a value. Stored
            # verbatim it was truthy, so it passed both readiness definitions
            # and promoted into the ledger as a blank-looking row — bypassing the
            # trim-and-reject rule every direct ledger write applies (bug
            # inbox-title, owner decision 2026-08-13). `clean_name` maps it to
            # NULL, which both readiness guards already handle correctly; the
            # draft simply stays not-ready until a real title is typed.
            #
            # `clean_name`, not `normalize_name`: the ledger rejects an empty
            # title because the column is NOT NULL and the row is final. A draft
            # is allowed to be incomplete — CLAUDE.md's inbox carve-out is
            # looseness about *which fields are null*, never about how a field
            # encodes its meaning, and this changes nullness only.
            #
            # `description` is deliberately untouched: the ledger stores it
            # verbatim, so normalizing it here would make the draft stricter
            # than the row it becomes.
            clean_name(body.title),
            body.description,
            amount_cents,
            transaction_type,
            body.date,
            body.account_id,
            body.category_id,
        )
    except asyncpg.UniqueViolationError:
        raise conflict(f"An inbox item with id '{body.id}' already exists.")

    if body.hashtag_ids:
        await sync_hashtags(
            conn, str(row["id"]), user_id, body.hashtag_ids, TransactionSource.INBOX,
        )

    response = inbox_from_row(row)
    # Resolve after the sync — the CREATED snapshot carries the draft's tags
    # per §6 aggregate exception #1, the same contract the ledger create has.
    await attach_hashtag_ids(conn, response, TransactionSource.INBOX)

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

    Handles the same amount normalisation as ``create_inbox_item``. No
    field accepts an explicit null — send a value or omit the key.

    ``hashtag_ids`` is pulled out of ``fields`` before the dynamic UPDATE
    (it is junction state, not a column) and applied with replacement
    semantics: omitted leaves the set alone, ``[]`` clears it. A tags-only
    edit still bumps ``version`` — ``dynamic_update`` with zero fields is
    exactly that bump — because the draft's wire shape changed.

    Empty updates short-circuit to a fetch-and-return — matches the prior
    router behaviour and the pattern established by other domain helpers.
    """
    fields = extract_update_fields(body)
    hashtag_ids = fields.pop("hashtag_ids", None)

    # Empty update — return current
    if not fields and hashtag_ids is None:
        row = await fetch_owned_row_or_404(
            conn, "expense_transaction_inbox", inbox_id, user_id, "inbox item"
        )
        response = inbox_from_row(row)
        await attach_hashtag_ids(conn, response, TransactionSource.INBOX)
        return response

    # Same title rule as create — see the comment on the INSERT bind. Applied
    # before the row is fetched because it is a property of the input, not of
    # the stored state. An explicit `{"title": null}` never reaches here:
    # extract_update_fields already rejects it 422, since null is not a verb on
    # this endpoint. Whitespace and explicit-null therefore differ, deliberately:
    # one is a field you left blank, the other is a clear operation the inbox
    # does not offer.
    if "title" in fields:
        fields["title"] = clean_name(fields["title"])

    # Collect the signed input. As on create, signs live only in this block.
    primary_signed: Optional[int] = None
    if "amount_cents" in fields:
        reject_zero_amount(fields["amount_cents"])
        primary_signed = fields["amount_cents"]
        fields["amount_cents"] = abs(primary_signed)

    # FOR UPDATE: the before/after activity-log snapshots must describe one
    # state of the row — same lost-update hazard as the transaction update
    # path, which locks its row for the same reason.
    before_row = await fetch_owned_row_or_404(
        conn, "expense_transaction_inbox", inbox_id, user_id, "inbox item",
        for_update=True,
    )

    before = inbox_from_row(before_row)
    # Pre-mutation tags — the UPDATED before_snapshot must show what was
    # attached prior to the edit (§6 aggregate exception #1).
    await attach_hashtag_ids(conn, before, TransactionSource.INBOX)

    # Same reference rule as create (and same deliberate lack of an
    # is_system arm). Sits after the ownership fetch so a nonexistent
    # inbox item is a 404 before any 422.
    if "account_id" in fields:
        await validate_active_account(conn, fields["account_id"], user_id)
    if "category_id" in fields:
        await validate_active_category(conn, fields["category_id"], user_id)

    if primary_signed is not None:
        fields["transaction_type"] = infer_transaction_type(primary_signed)

    # Nothing re-rates here. The `date`-keyed re-rate block that used to sit at
    # this point was the second half of open bug 1.4: because it fired only on a
    # date change, a draft that got its account_id filled in later kept the 1.0
    # it was created with, for good. Nothing derived is stored now.

    after_row = await dynamic_update(conn, "expense_transaction_inbox", fields, inbox_id, user_id)
    if after_row is None:
        raise not_found("inbox item")

    if hashtag_ids is not None:
        await sync_hashtags(
            conn, inbox_id, user_id, hashtag_ids, TransactionSource.INBOX,
        )

    after = inbox_from_row(after_row)
    # Post-mutation tags — applies whether this PUT rewrote them or not; an
    # edit to any other field still surfaces the current set.
    await attach_hashtag_ids(conn, after, TransactionSource.INBOX)

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
    """Dismiss a draft — soft-delete, with its tags.

    This is distinct from the PROMOTED end-state which also sets
    ``deleted_at`` but keeps ``status = 2``. A plain delete just marks
    the row ``deleted_at`` without touching ``status``.

    **Final** — there is no restore route (see the module docstring). The
    junction cascade is therefore one-way, which is the one place the inbox
    and the ledger genuinely differ here: ``delete_transaction`` leaves a
    ``deleted_at`` marker its restore inverts precisely, while a dismissed
    draft's tags simply close with it.

    Not routed through ``query_builder.soft_delete_with_audit``: the
    after-snapshot needs the *async* ``attach_hashtag_ids``, and it must run
    after the cascade so the snapshot shows the post-delete wire state
    (``[]``) — ``_mutate_with_audit``'s sync ``serialize`` can express
    neither. Same reason ``delete_transaction`` is hand-rolled.
    """
    row = await fetch_owned_row_or_404(
        conn, "expense_transaction_inbox", inbox_id, user_id, "inbox item"
    )

    before = inbox_from_row(row)
    # Captured BEFORE the cascade — otherwise the audit trail cannot say what
    # the dismissed draft was tagged with, and with no restore route this is
    # the only surviving record of it.
    await attach_hashtag_ids(conn, before, TransactionSource.INBOX)

    after_row = await soft_delete(
        conn, "expense_transaction_inbox", inbox_id, user_id
    )
    await cascade_delete(conn, user_id, inbox_id, TransactionSource.INBOX)

    after = inbox_from_row(after_row)
    await attach_hashtag_ids(conn, after, TransactionSource.INBOX)

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
    target_id: UUID,
) -> dict:
    """Promote a pending inbox item into a ledger transaction.

    The flow:

      1. Lock the inbox row (``FOR UPDATE``) so two concurrent promotes
         can't create duplicate ledger transactions.
      2. Validate that all fields required for promotion are present
         and reference active resources (account, category).
      3. Insert the ledger row, carry the draft's hashtags across to it,
         and write an activity log for the new transaction.
      4. Cleanup: soft-delete the inbox row with ``status = 2``
         (PROMOTED), close its junction rows, and write an activity log.

    **The tags move; they are not copied.** The draft's junction rows
    (source = 2) are cascaded closed in step 4 and equivalent ledger rows
    (source = 1) are written in step 3, so exactly one live set exists at
    any moment. Tags are not part of the readiness check — an untagged
    draft promotes fine, and a hashtag deleted between drafting and
    promoting has already had its junction dropped by ``delete_hashtag``,
    so the carry-over can only ever move live tags.

    Returns the newly-created ledger transaction.
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
    # The draft's live tags, read once under the lock and used twice: as the
    # set the ledger row inherits, and as the before-snapshot's content.
    draft_hashtag_ids = (
        await fetch_hashtag_ids_map(conn, [inbox_id], TransactionSource.INBOX)
    )[str(inbox_id)]
    inbox_before["hashtag_ids"] = draft_hashtag_ids

    # 2. Validate required fields — collect all failures
    #
    # Keep in step with the `?ready=true` predicate in routers/inbox.py: a row
    # that predicate returns must promote, and a row that promotes must appear
    # in it. They are two separate implementations of one definition, which is
    # how they drifted apart (WP7.2/7.3).
    errors: dict = {}

    if not inbox_row["title"] or inbox_row["title"] == "UNTITLED":
        errors["title"] = "Must be present and not 'UNTITLED'."

    if inbox_row["amount_cents"] is None or inbox_row["amount_cents"] == 0:
        errors["amount_cents"] = "Must be present and not zero."

    if inbox_row["date"] is None or inbox_row["date"] > await db_now(conn):
        errors["date"] = "Must be present and not in the future."

    if inbox_row["account_id"] is None or (
        await active_account_row(conn, inbox_row["account_id"], user_id) is None
    ):
        errors["account_id"] = MSG_ACTIVE_ACCOUNT

    if inbox_row["category_id"] is None:
        errors["category_id"] = MSG_ACTIVE_CATEGORY
    else:
        category = await active_category_row(
            conn, inbox_row["category_id"], user_id
        )
        if category is None:
            errors["category_id"] = MSG_ACTIVE_CATEGORY
        elif category["system_key"] is not None:
            # Promotion inserts the ledger row directly (not via
            # create_transaction), so it carries its own copy of the
            # system-category boundary (bug 6.7): only the engine files
            # rows under @Opening, and promotion is a user action.
            errors["category_id"] = MSG_USER_CATEGORY

    if errors:
        raise validation_error("Inbox item is not ready to promote.", errors)

    # 3. Insert the ledger row.
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

    # Promotion copies native amounts and nothing else — it used to carry
    # the draft's stored rate across, which is how a 1.0 written at capture
    # time became a permanent fact about a USD ledger row (open bug 1.4).
    txn_row = await insert_transaction_row(
        conn, user_id,
        transaction_id=target_id,
        title=inbox_row["title"],
        description=inbox_row["description"],
        amount_cents=inbox_row["amount_cents"],
        transaction_type=transaction_type,
        date=inbox_row["date"],
        account_id=inbox_row["account_id"],
        category_id=inbox_row["category_id"],
        inbox_id=inbox_row["id"],
    )

    # The tags follow the row into the ledger. Written under
    # TransactionSource.LEDGER against the NEW id — nothing is re-validated
    # here because these ids came off live junction rows a statement ago,
    # under the same lock; sync_hashtags re-checks them anyway, which is the
    # fail-closed half of reusing the one writer instead of an INSERT ... SELECT
    # that would copy whatever the junction table happened to hold.
    if draft_hashtag_ids:
        await sync_hashtags(
            conn, str(txn_row["id"]), user_id, draft_hashtag_ids,
            TransactionSource.LEDGER,
        )

    txn_response = transaction_from_row(txn_row)

    # No balance step: the INSERT above IS the balance change (sql/022).

    # Attached before the snapshot, not after the return: the CREATED entry
    # for the new ledger row must show the tags it was born with.
    await attach_hashtag_ids(conn, txn_response)

    await write_activity_log(
        conn, user_id, "transaction", str(txn_row["id"]), ActivityAction.CREATED,
        after_snapshot=txn_response,
    )

    # 4. Cleanup: soft-delete inbox row with status = PROMOTED
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

    # Close the draft's own junction rows. The set now lives on the ledger
    # row; leaving the inbox copy active would mean one logical tag held by
    # two rows, and a later DELETE /hashtags/{id} would have to bump a parent
    # that no longer exists as far as the user is concerned.
    await cascade_delete(conn, user_id, inbox_id, TransactionSource.INBOX)

    inbox_after = inbox_from_row(inbox_after_row)
    await attach_hashtag_ids(conn, inbox_after, TransactionSource.INBOX)

    await write_activity_log(
        conn, user_id, "inbox", inbox_id, ActivityAction.DELETED,
        before_snapshot=inbox_before,
        after_snapshot=inbox_after,
    )

    return txn_response
