"""Account domain logic.

Service-layer functions for expense_bank_accounts, called from
routers/accounts.py. Routers stay thin (HTTP glue + idempotency) and
delegate business logic here.

See ``app/helpers/idempotency.run_idempotent`` for the convention: these
functions do NOT open their own ``conn.transaction()`` — callers own transaction
boundaries.

Account balances are computed from the ledger, never stored (sql/022). None of
the mutations here can change a balance — renaming, archiving, soft-deleting or
restoring an account moves no money — so each fetches the balance once and reuses
that one value for the before-snapshot, the after-snapshot and the response.
Reading it twice would let a concurrent ledger write land between them and make
the activity-log pair disagree about a value neither mutation touched.
"""

from typing import Optional
from uuid import UUID

import asyncpg

from app.constants import ActivityAction, DEFAULT_ACCOUNT_COLOR, SystemCategoryKey
from app.errors import conflict, not_found, validation_error
from app.helpers.account_balance import fetch_balance, fetch_home_balance
from app.helpers.activity_log import write_activity_log
from app.helpers.categories import ensure_system_category
from app.helpers.query_builder import (
    dynamic_update,
    fetch_owned_row_or_404,
    restore_with_audit,
    soft_delete_with_audit,
)
from app.helpers.reference_data import name_taken, next_sort_order
from app.helpers.validation import currency_code_error, normalize_name, validate_color
from app.schemas.accounts import account_from_row


async def create_account(
    conn: asyncpg.Connection,
    user_id: str,
    account_id: UUID,
    name: str,
    currency_code: str,
    color: Optional[str],
    sort_order: Optional[int],
) -> dict:
    """Validate currency and uniqueness, insert, and log the creation.

    Raises:
        validation_error: ``currency_code`` is not in ``global_currencies``,
            the name is empty/whitespace, or ``color`` is not a 6-digit hex
            value. An omitted ``color`` is not an invalid one — it falls to
            ``DEFAULT_ACCOUNT_COLOR``.
        conflict: a non-deleted account with the same ``(name, currency_code)``
            already exists for this user (case-insensitive since sql/028), OR
            a resource with the same id already exists.
    """
    name = normalize_name(name)
    validate_color(color)

    # Validate currency_code
    message = await currency_code_error(conn, currency_code)
    if message is not None:
        raise validation_error("Invalid currency code.", {"currency_code": message})

    # Check uniqueness
    if await name_taken(
        conn, "expense_bank_accounts", user_id, name, currency_code=currency_code
    ):
        raise conflict(
            f"An account named '{name}' with currency '{currency_code}' already exists."
        )

    try:
        row = await conn.fetchrow(
            """
            INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, color, sort_order, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, now(), now())
            RETURNING *
            """,
            account_id,
            user_id,
            name,
            currency_code,
            # `is not None`, not `or` — the same collapse that `or 0` caused for
            # sort_order below, which was fixed while this one survived: an
            # explicitly-sent color quietly became the default instead of being
            # honoured or refused (bug account-color). Junk never reaches here now;
            # validate_color above rejects it, so the only thing this expresses is
            # omitted-vs-supplied.
            color if color is not None else DEFAULT_ACCOUNT_COLOR,
            # Omitted sort_order appends; an explicit value (including 0) is
            # respected verbatim (the old `or 0` collapsed explicit zeros too).
            sort_order
            if sort_order is not None
            else await next_sort_order(conn, "expense_bank_accounts", user_id),
        )
    except asyncpg.UniqueViolationError:
        raise conflict(f"An account with id '{account_id}' already exists.")

    # A brand-new account has no transactions, so its balance is 0 by
    # construction — querying the ledger to learn that would be a round-trip to
    # confirm what the INSERT guarantees. The fetch_home_balance call stays,
    # though: round(0 * rate) is 0, but "no rate available for this currency" is
    # null, and that distinction is wire-visible.
    home = await fetch_home_balance(conn, user_id, row["currency_code"], 0)
    response = account_from_row(row, 0, home)

    await write_activity_log(
        conn, user_id, "account", str(row["id"]), ActivityAction.CREATED,
        after_snapshot=response,
    )
    return response


async def create_opening_balance(
    conn: asyncpg.Connection,
    user_id: str,
    account_id: str,
    body,
) -> dict:
    """Seed an account's opening balance as a transaction under @Opening.

    The seed is an ordinary transaction (editable/deletable like any other);
    what makes it special is the ``opening_balance`` system category, which
    flow reports exclude — an opening balance is where tracking starts, not
    money that moved. Delegates the insert to ``create_transaction`` so
    validation, rate lookup, balance update, and activity logging stay in
    one place.

    Raises:
        validation_error: account missing/archived (via
            ``validate_active_account``), person account, or any
            transaction-level failure (zero amount, future date).
        conflict: the account already has an active opening balance, or a
            transaction with ``body.transaction_id`` already exists.
    """
    from app.helpers.transactions import create_transaction  # avoid import cycle
    from app.helpers.validation import validate_active_account
    from app.schemas.transactions import TransactionCreateRequest

    account = await validate_active_account(conn, account_id, user_id)
    if account["is_person"]:
        raise validation_error(
            "Account validation failed.",
            {"account_id": "Person accounts cannot carry an opening balance."},
        )

    # At most one active opening balance per account — "opening" is singular.
    # To adjust one, edit or delete the existing seed transaction.
    existing = await conn.fetchrow(
        """
        SELECT t.id
        FROM expense_transactions t
        JOIN expense_categories c ON c.id = t.category_id
        WHERE t.user_id = $1 AND t.account_id = $2 AND t.deleted_at IS NULL
          AND c.system_key = $3 AND c.deleted_at IS NULL
        LIMIT 1
        """,
        user_id,
        account_id,
        SystemCategoryKey.OPENING_BALANCE.value,
    )
    if existing is not None:
        raise conflict(
            "This account already has an opening balance. "
            "Edit or delete the existing opening-balance transaction instead."
        )

    category_id = await ensure_system_category(
        conn, user_id, SystemCategoryKey.OPENING_BALANCE
    )

    title = (body.title or "").strip() or "Opening balance"
    return await create_transaction(
        conn,
        user_id,
        TransactionCreateRequest(
            id=body.transaction_id,
            title=title,
            amount_cents=body.amount_cents,
            date=body.date,
            # account_id arrives as the UUID path param; the request model's
            # FK fields are UUID-typed since open-bugs 6.6 closed (2026-08-08).
            account_id=account_id,
            category_id=category_id,
        ),
        # The one caller allowed to file under a system category — this IS
        # the engine assigning @Opening; public writes are rejected (bug 6.7).
        allow_system_category=True,
    )


async def update_account(
    conn: asyncpg.Connection,
    user_id: str,
    account_id: str,
    fields: dict,
) -> dict:
    """Apply field updates, rejecting currency changes and enforcing name uniqueness.

    Returns the unchanged account (with home balance) if ``fields`` is empty
    — matches the prior router behaviour of treating empty-update as a fetch.

    Raises:
        validation_error: attempting to change ``currency_code`` (immutable),
            or ``color`` is not a 6-digit hex value.
        not_found: no active account with that id for this user.
        conflict: another non-deleted account already uses the new name with
            the same currency.
    """
    # Same colour rule as create. Sits with the other field-shape checks, ahead
    # of the ownership fetch, because it is a fact about the request rather than
    # about the stored row — a 404 for someone else's account must not be
    # preceded by a 422 that reveals nothing was wrong with the id.
    if "color" in fields:
        validate_color(fields["color"])

    # Reject currency_code changes
    if "currency_code" in fields:
        raise validation_error(
            "currency_code is immutable after creation.",
            {"currency_code": "Cannot be changed after account creation."},
        )

    # Empty update — return current state unchanged
    if not fields:
        row = await fetch_owned_row_or_404(
            conn, "expense_bank_accounts", account_id, user_id, "account"
        )
        balance_cents = await fetch_balance(conn, user_id, account_id)
        home = await fetch_home_balance(conn, user_id, row["currency_code"], balance_cents)
        return account_from_row(row, balance_cents, home)

    before_row = await fetch_owned_row_or_404(
        conn, "expense_bank_accounts", account_id, user_id, "account"
    )

    # Check name uniqueness if name is changing — one check against the row
    # already in hand (this replaced a 3-query dance that re-read the currency
    # it was holding). Case-insensitive within (user, currency) per sql/028.
    if "name" in fields:
        fields["name"] = normalize_name(fields["name"])
        if await name_taken(
            conn, "expense_bank_accounts", user_id, fields["name"],
            exclude_id=account_id,
            currency_code=before_row["currency_code"],
        ):
            raise conflict(
                f"An account named '{fields['name']}' with this currency already exists."
            )

    # Fetched once and reused for both snapshots. A PUT cannot move money, and
    # currency_code is rejected above, so both the native and the home value are
    # identical before and after by construction — reading them twice would only
    # create a window for a concurrent ledger write to make the audit pair
    # disagree about a field this mutation never touched.
    balance_cents = await fetch_balance(conn, user_id, account_id)
    home = await fetch_home_balance(
        conn, user_id, before_row["currency_code"], balance_cents
    )
    before = account_from_row(before_row, balance_cents, home)

    after_row = await dynamic_update(conn, "expense_bank_accounts", fields, account_id, user_id)
    if after_row is None:
        raise not_found("account")

    after = account_from_row(after_row, balance_cents, home)

    await write_activity_log(
        conn, user_id, "account", account_id, ActivityAction.UPDATED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after


async def delete_account(
    conn: asyncpg.Connection,
    user_id: str,
    account_id: str,
) -> dict:
    """Soft-delete an account after checking for active transactions.

    Raises:
        not_found: no active account with that id for this user.
        conflict: account is still referenced by active transactions.
    """
    row = await fetch_owned_row_or_404(
        conn, "expense_bank_accounts", account_id, user_id, "account"
    )

    # Check for active transactions
    has_txns = await conn.fetchval(
        """
        SELECT 1 FROM expense_transactions
        WHERE account_id = $1 AND user_id = $2 AND deleted_at IS NULL
        LIMIT 1
        """,
        account_id,
        user_id,
    )
    if has_txns:
        raise conflict("Account has active transactions. Archive instead.")

    # Soft-deleting the account does not soft-delete its transactions, so the
    # balance is unchanged across the mutation. Fetched once, used for both
    # snapshots via the serializer closure.
    # (It need not be zero: the guard above only rejects *active* transactions,
    # so an account whose rows were all soft-deleted can still carry a figure.)
    balance_cents = await fetch_balance(conn, user_id, account_id)
    home = await fetch_home_balance(conn, user_id, row["currency_code"], balance_cents)

    return await soft_delete_with_audit(
        conn, user_id, "expense_bank_accounts", "account", row,
        lambda r: account_from_row(r, balance_cents, home),
    )


async def restore_account(
    conn: asyncpg.Connection,
    user_id: str,
    account_id: str,
) -> dict:
    """Undo a soft-delete on an account and log the restoration.

    Checks for name collisions with active accounts before clearing
    deleted_at — since sql/028 a deleted account releases its (name,
    currency), so an active account may have retaken it.

    Raises:
        not_found: no soft-deleted account with that id for this user.
        conflict: an active account already uses the same name and currency.
    """
    before_row = await fetch_owned_row_or_404(
        conn, "expense_bank_accounts", account_id, user_id, "account", deleted=True
    )

    if await name_taken(
        conn, "expense_bank_accounts", user_id, before_row["name"],
        exclude_id=account_id,
        currency_code=before_row["currency_code"],
    ):
        raise conflict(
            f"Cannot restore account: an active account named "
            f"'{before_row['name']}' with this currency already exists."
        )

    # Restoring the account row does not restore any transaction, so the balance
    # is the same on both sides of the mutation. Fetched once, both snapshots.
    balance_cents = await fetch_balance(conn, user_id, account_id)
    home = await fetch_home_balance(
        conn, user_id, before_row["currency_code"], balance_cents
    )

    return await restore_with_audit(
        conn, user_id, "expense_bank_accounts", "account", before_row,
        lambda r: account_from_row(r, balance_cents, home),
    )


async def archive_account(
    conn: asyncpg.Connection,
    user_id: str,
    account_id: str,
) -> dict:
    """Set ``is_archived = true`` on an account and log the change.

    Uses a direct UPDATE (not ``dynamic_update``) so the archive flag is
    set in a single statement regardless of what the caller passed.

    Raises:
        not_found: no active account with that id for this user.
    """
    return await _set_account_archive(conn, user_id, account_id, archived=True)


async def unarchive_account(
    conn: asyncpg.Connection,
    user_id: str,
    account_id: str,
) -> dict:
    """Clear ``is_archived`` on an account and log the change.

    Mirror of ``archive_account``. Targets active rows (``deleted_at IS NULL``)
    regardless of the current archive state.

    Raises:
        not_found: no active account with that id for this user.
    """
    return await _set_account_archive(conn, user_id, account_id, archived=False)


async def _set_account_archive(
    conn: asyncpg.Connection,
    user_id: str,
    account_id: str,
    archived: bool,
) -> dict:
    before_row = await fetch_owned_row_or_404(
        conn, "expense_bank_accounts", account_id, user_id, "account"
    )

    # Archiving moves no money — an archived account still holds a real balance
    # and still reports it. One fetch, both snapshots.
    balance_cents = await fetch_balance(conn, user_id, account_id)
    home = await fetch_home_balance(
        conn, user_id, before_row["currency_code"], balance_cents
    )
    before = account_from_row(before_row, balance_cents, home)

    after_row = await conn.fetchrow(
        """
        UPDATE expense_bank_accounts
        SET is_archived = $3, updated_at = now(), version = version + 1
        WHERE id = $1 AND user_id = $2
        RETURNING *
        """,
        account_id,
        user_id,
        archived,
    )

    after = account_from_row(after_row, balance_cents, home)

    await write_activity_log(
        conn, user_id, "account", account_id, ActivityAction.UPDATED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after
