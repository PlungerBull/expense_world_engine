"""Pins for sql/031 — colour columns hold 6-digit hex, and the app agrees.

Bug account-color. Nothing validated colour anywhere before this: `create_account`
collapsed an explicit `""` to the default via `color or …` truthiness, while
`create_category` — whose `color` is *required* — stored `""` and `banana`
verbatim, and both update paths reached `dynamic_update` unchecked.

Three separate things can rot independently, so each gets its own section:

  * the **app** rule (`validate_color`), which produces the 422 a client sees;
  * the **schema** rule (sql/031's CHECKs), the backstop a future writer cannot
    skip;
  * the **agreement** between them, plus between `DEFAULT_ACCOUNT_COLOR` and the
    column DEFAULT it restates.

That last section is the one worth keeping honest. The Python constant exists
only because the accounts INSERT has a fixed column list and cannot express
"omit this and let the DEFAULT apply", so it is a second copy of a value owned by
sql/003 — and two copies of a value with no assertion between them is the drift
this codebase keeps finding. Reading `information_schema` is what closes it.

Run: .venv/bin/pytest tests/test_sql031_color_checks.py -v
"""
import re
import uuid

import asyncpg
import pytest

from app import db
from app.constants import DEFAULT_ACCOUNT_COLOR
from app.errors import AppError
from app.helpers.validation import _HEX_COLOR_RE, validate_color

# Everything a colour must not be. Each is a shape that was silently accepted
# somewhere before this migration.
BAD_COLORS = [
    ("", "empty string — the reported bug"),
    ("   ", "whitespace"),
    ("banana", "not hex at all"),
    ("#12", "too short"),
    ("#fff", "3-digit shorthand — one colour, two spellings"),
    ("#ff00ff00", "8-digit alpha — a channel nothing renders"),
    ("3b82f6", "missing the #"),
    ("#3b82fg", "g is not a hex digit"),
    ("#3b82f6 ", "trailing space"),
    (" #3b82f6", "leading space"),
    ("#3b82f6\n", "trailing newline — fullmatch, not match"),
]

GOOD_COLORS = ["#3b82f6", "#000000", "#FFFFFF", "#00AA00", "#abc123", DEFAULT_ACCOUNT_COLOR]


# ---------------------------------------------------------------------------
# The app rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,why", BAD_COLORS, ids=[c[0] or "(empty)" for c in BAD_COLORS])
def test_validate_color_rejects(value, why):
    with pytest.raises(AppError) as exc_info:
        validate_color(value)
    assert exc_info.value.status_code == 422, why
    assert "color" in exc_info.value.fields, why


@pytest.mark.parametrize("value", GOOD_COLORS)
def test_validate_color_accepts(value):
    validate_color(value)  # must not raise


def test_none_passes_because_absence_is_a_different_question():
    """`None` is "not supplied", which accounts answer with a default and
    categories forbid at the schema. Same split as `reject_zero_amount`."""
    validate_color(None)


def test_the_field_name_travels_into_the_error():
    """So a future caller validating a differently-named colour field gets a
    message about that field, not about "color"."""
    with pytest.raises(AppError) as exc_info:
        validate_color("nope", field="accent")
    assert "accent" in exc_info.value.fields


# ---------------------------------------------------------------------------
# The schema rule
# ---------------------------------------------------------------------------

COLOR_TABLES = [
    ("expense_bank_accounts", "accounts_color_is_hex"),
    ("expense_categories", "categories_color_is_hex"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("table,constraint", COLOR_TABLES, ids=[t[0] for t in COLOR_TABLES])
async def test_the_check_rejects_junk_even_when_the_app_is_bypassed(
    table, constraint, test_data
):
    """A direct INSERT is the future writer that forgets `validate_color`.

    Only the two shapes that matter are exercised here — the app section above
    owns the full matrix. This asserts the backstop exists and bites.
    """
    async with db.pool.acquire() as conn:
        for bad in ("", "banana"):
            with pytest.raises(asyncpg.CheckViolationError):
                if table == "expense_bank_accounts":
                    await conn.execute(
                        """INSERT INTO expense_bank_accounts
                            (id, user_id, name, currency_code, color, sort_order,
                             created_at, updated_at)
                           VALUES ($1, $2, 'sql031', 'PEN', $3, 99, now(), now())""",
                        str(uuid.uuid4()), test_data.user_id, bad,
                    )
                else:
                    await conn.execute(
                        """INSERT INTO expense_categories
                            (id, user_id, name, color, sort_order,
                             created_at, updated_at)
                           VALUES ($1, $2, 'sql031', $3, 99, now(), now())""",
                        str(uuid.uuid4()), test_data.user_id, bad,
                    )


@pytest.mark.asyncio
@pytest.mark.parametrize("table,constraint", COLOR_TABLES, ids=[t[0] for t in COLOR_TABLES])
async def test_the_check_spells_out_is_not_null(table, constraint):
    """CLAUDE.md's standing warning: a CHECK rejects a row only when it
    evaluates to FALSE, and NULL passes. Both columns are NOT NULL today, so the
    arm is redundant — and writing it means dropping that NOT NULL later cannot
    silently reopen the hole. Asserted on the stored definition so the habit
    survives someone rewriting the migration."""
    async with db.pool.acquire() as conn:
        definition = await conn.fetchval(
            """SELECT pg_get_constraintdef(oid) FROM pg_constraint
               WHERE conname = $1 AND conrelid = $2::regclass""",
            constraint, table,
        )
    assert definition is not None, f"{constraint} is missing from {table}"
    assert "IS NOT NULL" in definition, definition


# ---------------------------------------------------------------------------
# Agreement between the copies
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("table,constraint", COLOR_TABLES, ids=[t[0] for t in COLOR_TABLES])
async def test_the_check_uses_the_same_pattern_as_the_python_validator(table, constraint):
    """One rule, two languages — assert they still say the same thing.

    Compares the regex literal itself rather than behaviour, because behavioural
    equivalence of two regex dialects is not something a test can establish and
    the realistic failure is someone loosening one side and forgetting the other.
    """
    async with db.pool.acquire() as conn:
        definition = await conn.fetchval(
            """SELECT pg_get_constraintdef(oid) FROM pg_constraint
               WHERE conname = $1 AND conrelid = $2::regclass""",
            constraint, table,
        )
    assert f"'^{_HEX_COLOR_RE.pattern}$'" in definition.replace('"', "'"), (
        f"sql/031's pattern and helpers.validation._HEX_COLOR_RE have drifted:\n"
        f"  SQL:    {definition}\n"
        f"  Python: {_HEX_COLOR_RE.pattern}"
    )


@pytest.mark.asyncio
async def test_the_python_default_matches_the_column_default():
    """`DEFAULT_ACCOUNT_COLOR` restates sql/003's column DEFAULT.

    The constant exists only because the accounts INSERT has a fixed column list
    and so cannot omit the column and let the DEFAULT apply. That makes it a
    second copy of a value the schema owns — and a silent drift would hand new
    accounts a colour the schema says they do not have. This is the assertion
    that makes the duplication safe rather than merely small.
    """
    async with db.pool.acquire() as conn:
        column_default = await conn.fetchval(
            """SELECT column_default FROM information_schema.columns
               WHERE table_name = 'expense_bank_accounts' AND column_name = 'color'"""
        )
    assert column_default is not None, "expense_bank_accounts.color lost its DEFAULT"
    # Stored as e.g. "'#3b82f6'::text" — pull the literal back out.
    match = re.match(r"^'([^']*)'", column_default)
    assert match, f"unexpected column_default shape: {column_default!r}"
    assert match.group(1) == DEFAULT_ACCOUNT_COLOR, (
        f"constants.DEFAULT_ACCOUNT_COLOR is {DEFAULT_ACCOUNT_COLOR!r} but "
        f"sql/003's column DEFAULT is {match.group(1)!r}"
    )


@pytest.mark.asyncio
async def test_both_column_defaults_would_survive_their_own_check():
    """A DEFAULT that the CHECK rejects is an unusable column — and the two are
    in different migrations (sql/003 and sql/031), so nothing else pairs them."""
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT table_name, column_default FROM information_schema.columns
               WHERE column_name = 'color'
                 AND table_name IN ('expense_bank_accounts', 'expense_categories')"""
        )
    assert len(rows) == 2, "expected exactly two colour columns"
    for row in rows:
        literal = re.match(r"^'([^']*)'", row["column_default"] or "")
        assert literal, f"{row['table_name']}: no DEFAULT literal"
        assert _HEX_COLOR_RE.fullmatch(literal.group(1)), (
            f"{row['table_name']}'s DEFAULT {literal.group(1)!r} fails the colour rule"
        )
