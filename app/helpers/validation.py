"""Shared validation helpers for resource lookups.

Consolidates account/category validation that was duplicated across
transactions.py, inbox.py, and reconciliations.py.

Two flow styles, one implementation. The non-raising helpers
(``active_account_row`` / ``active_category_row``, and the vectorised
``active_account_ids`` / ``active_category_ids`` for batch flows) return
rows/sets; collect-all-errors flows check them and set
``errors[field] = MSG_ACTIVE_ACCOUNT`` / ``MSG_ACTIVE_CATEGORY``
themselves. The raising ``validate_active_*`` twins wrap them for flows
that short-circuit on the first bad reference (single create/update).
"""

from datetime import datetime
import re
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

import asyncpg
from pydantic import BaseModel

from app.errors import validation_error

# The single wording of the active-reference rule, shared by the raising
# helpers and every collect-all-errors flow (it was retyped 12× before).
MSG_ACTIVE_ACCOUNT = "Must reference an active, non-archived account."
MSG_ACTIVE_CATEGORY = "Must reference an active category."
# System categories (@Opening) are engine-assigned only. Public writes that
# name one get this; the engine's own path (create_opening_balance →
# create_transaction(allow_system_category=True)) is the sole exemption.
MSG_USER_CATEGORY = "Must not reference a system category."

# Field messages for the small re-typed predicates (bloat-audit Tier 2 §10).
# The collect-all-errors flows set ``errors[field] = MSG_*`` themselves;
# the raising twins below wrap the same strings.
MSG_NOT_EMPTY = "Must not be empty."
MSG_NOT_ZERO = "Must not be zero."
MSG_NOT_FUTURE = "Must not be in the future."
MSG_HEX_COLOR = "Must be a 6-digit hex color, e.g. '#3b82f6'."

# Kept beside the message it produces. Mirrored by sql/031's CHECK — the two are
# one rule in two places, so a change here needs a migration, and
# tests/test_sql031_color_checks.py asserts they still agree.
_HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")


def validate_timezone(value: str, field: str) -> None:
    """Reject a value that is not a valid IANA timezone name with a 422.

    Written for ``display_timezone``, which reaches SQL as a bind parameter
    inside ``AT TIME ZONE`` on every report read — Postgres 500s on an
    unknown zone, so a bad value must never be stored. ``field`` names the
    request field ("display_timezone" on PUT /auth/settings, "timezone" on
    POST /auth/bootstrap) so the error lands on what the caller sent.
    """
    if value is None or not value.strip():
        raise validation_error(
            "Invalid timezone.",
            {field: "Must not be empty."},
        )
    try:
        ZoneInfo(value)
    except Exception:
        raise validation_error(
            "Invalid timezone.",
            {field: "Must be a valid IANA timezone name (e.g. America/Lima)."},
        )


def resolve_timezone(value: str) -> str:
    """Return ``value`` if it is a valid IANA timezone name, else ``"UTC"``.

    The read-side twin of ``validate_timezone``: writes reject a bad zone,
    but rows stored before that guard existed may hold junk, and a read must
    tolerate them rather than 500 (an unknown zone reaching ``AT TIME ZONE``
    is a Postgres error). Every consumer of a stored ``display_timezone`` —
    Python ``ZoneInfo`` construction and SQL bind parameters alike — goes
    through this one fallback.
    """
    try:
        ZoneInfo(value)
    except Exception:
        return "UTC"
    return value


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


async def active_account_row(
    conn: asyncpg.Connection,
    account_id: str,
    user_id: str,
) -> Optional[asyncpg.Record]:
    """The active-account reference rule: active (not soft-deleted) AND
    non-archived, tenant-scoped. Returns the full row (the opening-balance
    guard reads ``is_person`` off it via ``validate_active_account``) or
    ``None`` — callers own their error handling.
    """
    return await conn.fetchrow(
        """
        SELECT * FROM expense_bank_accounts
        WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL AND is_archived = false
        """,
        account_id,
        user_id,
    )


async def active_account_ids(
    conn: asyncpg.Connection,
    account_ids: Iterable[str],
    user_id: str,
) -> set[str]:
    """Vectorised twin of ``active_account_row`` for batch flows: the subset
    of ``account_ids`` that reference an active, non-archived account, as
    strings. Empty input returns an empty set without querying.
    """
    ids = list(account_ids)
    if not ids:
        return set()
    rows = await conn.fetch(
        """
        SELECT id FROM expense_bank_accounts
        WHERE id = ANY($1::uuid[]) AND user_id = $2
          AND deleted_at IS NULL AND is_archived = false
        """,
        ids,
        user_id,
    )
    return {str(r["id"]) for r in rows}


async def validate_active_account(
    conn: asyncpg.Connection,
    account_id: str,
    user_id: str,
) -> asyncpg.Record:
    """Raising wrapper over ``active_account_row``: 422 on a miss.

    Returns the account row on success.
    """
    account = await active_account_row(conn, account_id, user_id)
    if account is None:
        raise validation_error(
            "Account validation failed.",
            {"account_id": MSG_ACTIVE_ACCOUNT},
        )
    return account


def clean_name(value: Optional[str]) -> Optional[str]:
    """Strip a name/title; ``None`` or whitespace-only becomes ``None``.

    The non-raising twin of ``normalize_name`` (see module docstring):
    collect-all-errors flows call this and set
    ``errors[field] = MSG_NOT_EMPTY`` on ``None`` themselves.
    """
    if value is None or not value.strip():
        return None
    return value.strip()


def normalize_name(name: Optional[str], field: str = "name") -> str:
    """Strip whitespace and reject empty names with a 422 field error.

    Returns the trimmed name. The caller is responsible for any
    case-insensitive uniqueness check against storage.
    """
    cleaned = clean_name(name)
    if cleaned is None:
        raise validation_error(
            f"{field.capitalize()} must not be empty.",
            {field: MSG_NOT_EMPTY},
        )
    return cleaned


def validate_color(value: Optional[str], field: str = "color") -> None:
    """Raise a 422 unless ``value`` is a 6-digit hex color. ``None`` passes.

    ``None`` means "not supplied", which is a separate question from "is this a
    color" — accounts fall back to a default, categories require the field at
    the schema. Same split as ``reject_zero_amount``.

    Nothing before this validated color at all: an empty string, ``banana`` and
    ``#12`` were all stored verbatim on the category paths, while on
    ``create_account`` an empty string collapsed to the default via ``color or
    …`` truthiness — the same class of collapse ``or 0`` caused for
    ``sort_order`` (bug account-color; owner decision 2026-08-13 is to reject).

    Deliberately narrow, per the fail-closed rule — enumerate what is permitted:

      * **no 3-digit shorthand.** ``#fff`` and ``#ffffff`` are one color under
        two spellings, and clients compare these as strings.
      * **no 8-digit alpha.** Nothing renders the channel, so it would be a
        stored value with no meaning.
      * **no case normalization.** ``#00AA00`` stays ``#00AA00``. Rewriting a
        caller's value is the silent mutation this fix exists to stop, and the
        engine never edits input it has accepted.

    ``sql/031`` carries the same rule as a CHECK on both columns. This runs
    first, so the constraint only ever fires on a path that has already skipped
    this — which is a defect, and a 500 is the right answer to one.
    """
    if value is None:
        return
    if not _HEX_COLOR_RE.fullmatch(value):
        raise validation_error(
            f"{field.capitalize()} must be a 6-digit hex color.",
            {field: MSG_HEX_COLOR},
        )


def reject_zero_amount(value: Optional[int], field: str = "amount_cents") -> None:
    """Raise a 422 iff ``value == 0``. ``None`` passes — presence is a
    separate rule (the inbox's optional amounts rely on that).

    The raising twin of the ``MSG_NOT_ZERO`` constant, wording identical
    to the collect-all-errors sites.
    """
    if value == 0:
        raise validation_error(
            f"{field} must not be zero.",
            {field: MSG_NOT_ZERO},
        )


async def db_now(conn: asyncpg.Connection) -> datetime:
    """The engine's clock — Postgres ``now()``, never the app server's.

    Future-date checks compare against this one round trip (hoist it
    outside loops in batch flows) and report ``MSG_NOT_FUTURE``.
    """
    return await conn.fetchval("SELECT now()")


async def currency_code_error(
    conn: asyncpg.Connection,
    code: str,
) -> Optional[str]:
    """Return the field message for an unsupported currency code, else None.

    Non-raising, so it serves both flow styles (see module docstring):
    short-circuit callers wrap the message in their own ``validation_error``,
    accumulate callers set ``errors[field]``. Single source of the
    supported-currency check — ``global_currencies``, locked to USD/PEN
    by ``sql/015``.
    """
    row = await conn.fetchrow(
        "SELECT code FROM global_currencies WHERE code = $1", code
    )
    if row is None:
        return f"'{code}' is not a valid currency code."
    return None


async def active_category_row(
    conn: asyncpg.Connection,
    category_id: str,
    user_id: str,
) -> Optional[asyncpg.Record]:
    """The active-category reference rule: active (not soft-deleted),
    tenant-scoped. Returns the row or ``None`` — callers own their error
    handling. (No archived arm — categories lost ``is_archived`` in sql/024.)
    """
    return await conn.fetchrow(
        """
        SELECT * FROM expense_categories
        WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL
        """,
        category_id,
        user_id,
    )


async def active_category_ids(
    conn: asyncpg.Connection,
    category_ids: Iterable[str],
    user_id: str,
) -> dict[str, bool]:
    """Vectorised twin of ``active_category_row`` for batch flows.

    Returns ``{id: is_system}`` for the active, tenant-scoped subset —
    membership answers "active?", the value answers "system?" (batch flows
    must reject system categories like the single-row paths do, bug 6.7).
    """
    ids = list(category_ids)
    if not ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT id, is_system FROM expense_categories
        WHERE id = ANY($1::uuid[]) AND user_id = $2 AND deleted_at IS NULL
        """,
        ids,
        user_id,
    )
    return {str(r["id"]): r["is_system"] for r in rows}


async def validate_active_category(
    conn: asyncpg.Connection,
    category_id: str,
    user_id: str,
) -> asyncpg.Record:
    """Raising wrapper over ``active_category_row``: 422 on a miss.

    Returns the category row on success.
    """
    category = await active_category_row(conn, category_id, user_id)
    if category is None:
        raise validation_error(
            "Category validation failed.",
            {"category_id": MSG_ACTIVE_CATEGORY},
        )
    return category
