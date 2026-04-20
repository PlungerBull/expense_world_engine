"""Account domain logic.

Service-layer functions for expense_bank_accounts, called from
routers/accounts.py. Routers stay thin (HTTP glue + idempotency) and
delegate business logic here.

See ``app/helpers/balance.py`` for the convention: these functions do NOT
open their own ``conn.transaction()`` — callers own transaction boundaries.
"""

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import asyncpg

from app.constants import ActivityAction
from app.errors import conflict, not_found, validation_error
from app.helpers.activity_log import write_activity_log
from app.helpers.exchange_rate import get_rate
from app.helpers.query_builder import dynamic_update, restore, soft_delete
from app.schemas.accounts import account_from_row


async def get_home_balance(
    conn: asyncpg.Connection,
    currency_code: str,
    balance_cents: int,
    user_id: str,
) -> Optional[int]:
    """Convert balance to home currency. Returns None if no rate available.

    Callers that need to convert many balances at once (e.g. the account
    list endpoint) should use ``batch_get_rates`` directly to avoid the
    N+1 query pattern this helper creates when called in a loop.
    """
    settings = await conn.fetchrow(
        "SELECT main_currency FROM user_settings WHERE user_id = $1", user_id
    )
    if settings is None:
        return None

    result = await get_rate(
        conn,
        from_currency=currency_code,
        to_currency=settings["main_currency"],
        as_of=datetime.now(timezone.utc).date(),
    )
    if result is None:
        return None

    return round(balance_cents * result[0])


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
        validation_error: ``currency_code`` is not in ``global_currencies``.
        conflict: a non-deleted account with the same ``(name, currency_code)``
            already exists for this user, OR a resource with the same id
            already exists.
    """
    # Validate currency_code
    currency = await conn.fetchrow(
        "SELECT code FROM global_currencies WHERE code = $1", currency_code
    )
    if currency is None:
        raise validation_error(
            "Invalid currency code.",
            {"currency_code": f"'{currency_code}' is not a valid currency code."},
        )

    # Check uniqueness
    existing = await conn.fetchrow(
        """
        SELECT id FROM expense_bank_accounts
        WHERE user_id = $1 AND name = $2 AND currency_code = $3 AND deleted_at IS NULL
        """,
        user_id,
        name,
        currency_code,
    )
    if existing is not None:
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
            color or "#3b82f6",
            sort_order or 0,
        )
    except asyncpg.UniqueViolationError:
        raise conflict(f"An account with id '{account_id}' already exists.")

    home = await get_home_balance(conn, row["currency_code"], row["current_balance_cents"], user_id)
    response = account_from_row(row, home)

    await write_activity_log(
        conn, user_id, "account", str(row["id"]), ActivityAction.CREATED,
        after_snapshot=response,
    )
    return response


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
        validation_error: attempting to change ``currency_code`` (immutable).
        not_found: no active account with that id for this user.
        conflict: another non-deleted account already uses the new name with
            the same currency.
    """
    # Reject currency_code changes
    if "currency_code" in fields:
        raise validation_error(
            "currency_code is immutable after creation.",
            {"currency_code": "Cannot be changed after account creation."},
        )

    # Empty update — return current state unchanged
    if not fields:
        row = await conn.fetchrow(
            "SELECT * FROM expense_bank_accounts WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
            account_id,
            user_id,
        )
        if row is None:
            raise not_found("account")
        home = await get_home_balance(conn, row["currency_code"], row["current_balance_cents"], user_id)
        return account_from_row(row, home)

    # Check name uniqueness if name is changing. Preserve the 2-step check:
    # first find any name match, then verify full (name, currency) uniqueness.
    if "name" in fields:
        existing = await conn.fetchrow(
            """
            SELECT id FROM expense_bank_accounts
            WHERE user_id = $1 AND name = $2 AND id != $3 AND deleted_at IS NULL
            """,
            user_id,
            fields["name"],
            account_id,
        )
        if existing is not None:
            # Need currency to check full uniqueness
            current = await conn.fetchrow(
                "SELECT currency_code FROM expense_bank_accounts WHERE id = $1 AND user_id = $2",
                account_id,
                user_id,
            )
            if current:
                dup = await conn.fetchrow(
                    """
                    SELECT id FROM expense_bank_accounts
                    WHERE user_id = $1 AND name = $2 AND currency_code = $3 AND id != $4 AND deleted_at IS NULL
                    """,
                    user_id,
                    fields["name"],
                    current["currency_code"],
                    account_id,
                )
                if dup is not None:
                    raise conflict(
                        f"An account named '{fields['name']}' with this currency already exists."
                    )

    before_row = await conn.fetchrow(
        "SELECT * FROM expense_bank_accounts WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
        account_id,
        user_id,
    )
    if before_row is None:
        raise not_found("account")

    home_before = await get_home_balance(
        conn, before_row["currency_code"], before_row["current_balance_cents"], user_id
    )
    before = account_from_row(before_row, home_before)

    after_row = await dynamic_update(conn, "expense_bank_accounts", fields, account_id, user_id)
    if after_row is None:
        raise not_found("account")

    home_after = await get_home_balance(
        conn, after_row["currency_code"], after_row["current_balance_cents"], user_id
    )
    after = account_from_row(after_row, home_after)

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
    row = await conn.fetchrow(
        "SELECT * FROM expense_bank_accounts WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
        account_id,
        user_id,
    )
    if row is None:
        raise not_found("account")

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

    home = await get_home_balance(conn, row["currency_code"], row["current_balance_cents"], user_id)
    before = account_from_row(row, home)

    after_row = await soft_delete(conn, "expense_bank_accounts", account_id, user_id)
    home_after = await get_home_balance(
        conn, after_row["currency_code"], after_row["current_balance_cents"], user_id
    )
    after = account_from_row(after_row, home_after)

    await write_activity_log(
        conn, user_id, "account", account_id, ActivityAction.DELETED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after


async def restore_account(
    conn: asyncpg.Connection,
    user_id: str,
    account_id: str,
) -> dict:
    """Undo a soft-delete on an account and log the restoration.

    Raises:
        not_found: no soft-deleted account with that id for this user.
    """
    before_row = await conn.fetchrow(
        "SELECT * FROM expense_bank_accounts WHERE id = $1 AND user_id = $2 AND deleted_at IS NOT NULL",
        account_id,
        user_id,
    )
    if before_row is None:
        raise not_found("account")

    home_before = await get_home_balance(
        conn, before_row["currency_code"], before_row["current_balance_cents"], user_id
    )
    before = account_from_row(before_row, home_before)

    after_row = await restore(conn, "expense_bank_accounts", account_id, user_id)
    home_after = await get_home_balance(
        conn, after_row["currency_code"], after_row["current_balance_cents"], user_id
    )
    after = account_from_row(after_row, home_after)

    await write_activity_log(
        conn, user_id, "account", account_id, ActivityAction.RESTORED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after


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
    before_row = await conn.fetchrow(
        "SELECT * FROM expense_bank_accounts WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
        account_id,
        user_id,
    )
    if before_row is None:
        raise not_found("account")

    home_before = await get_home_balance(
        conn, before_row["currency_code"], before_row["current_balance_cents"], user_id
    )
    before = account_from_row(before_row, home_before)

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

    home_after = await get_home_balance(
        conn, after_row["currency_code"], after_row["current_balance_cents"], user_id
    )
    after = account_from_row(after_row, home_after)

    await write_activity_log(
        conn, user_id, "account", account_id, ActivityAction.UPDATED,
        before_snapshot=before,
        after_snapshot=after,
    )
    return after
