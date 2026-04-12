"""Shared validation helpers for resource lookups.

Consolidates account/category validation that was duplicated across
transactions.py, inbox.py, reconciliations.py, and transfers.py.

These helpers RAISE ``AppError`` on failure. Use them when your flow
wants to short-circuit on the first bad reference (e.g. single-resource
create/update endpoints).

If your flow collects multiple field errors into a dict and raises
once at the end (e.g. ``promote_inbox_item``, ``create_transfer_pair``,
``create_batch``'s vectorised path), do NOT use these helpers — use
inline fetches that set ``errors[field]`` without raising.
"""

import asyncpg

from app.errors import validation_error


async def validate_active_account(
    conn: asyncpg.Connection,
    account_id: str,
    user_id: str,
) -> asyncpg.Record:
    """Fetch an active, non-archived account or raise 422.

    Returns the account row on success.
    """
    account = await conn.fetchrow(
        """
        SELECT * FROM expense_bank_accounts
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
    return account


async def validate_active_category(
    conn: asyncpg.Connection,
    category_id: str,
    user_id: str,
) -> asyncpg.Record:
    """Fetch an active category or raise 422.

    Returns the category row on success.
    """
    category = await conn.fetchrow(
        "SELECT * FROM expense_categories WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
        category_id,
        user_id,
    )
    if category is None:
        raise validation_error(
            "Category validation failed.",
            {"category_id": "Must reference an active category."},
        )
    return category
