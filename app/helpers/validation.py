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

from typing import Optional

import asyncpg
from pydantic import BaseModel

from app.errors import validation_error


def extract_update_fields(
    body: BaseModel,
    nullable: Optional[set[str]] = None,
) -> dict:
    """Extract fields explicitly set by a PUT request body.

    Uses ``model_dump(exclude_unset=True)`` so callers can distinguish
    "field omitted" from "field explicitly null". Nulls on fields NOT listed
    in ``nullable`` raise 422 — this enforces the spec rule that clients
    cannot clear non-nullable fields by sending null, while preserving
    legitimate "clear me" / "unassign me" semantics for fields that opt in.

    The distinction matters for immutability checks: ``currency_code: null``
    should be treated as "caller included the immutable field", not as
    "caller omitted the field", so the service's immutability guard fires.
    """
    raw = body.model_dump(exclude_unset=True)
    nullable = nullable or set()
    violations = {
        key: "Must not be null."
        for key, value in raw.items()
        if value is None and key not in nullable
    }
    if violations:
        raise validation_error(
            "Request contains null values for non-nullable fields.",
            violations,
        )
    return raw


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


def normalize_name(name: Optional[str], field: str = "name") -> str:
    """Strip whitespace and reject empty names with a 422 field error.

    Returns the trimmed name. The caller is responsible for any
    case-insensitive uniqueness check against storage.
    """
    if name is None or not name.strip():
        raise validation_error(
            f"{field.capitalize()} must not be empty.",
            {field: "Must not be empty."},
        )
    return name.strip()


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
