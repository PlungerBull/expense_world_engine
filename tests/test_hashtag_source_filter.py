"""Every reader of ``expense_transaction_hashtags`` filters by transaction_source.

The junction table is shared: it carries a `transaction_source` column so inbox
drafts can eventually attach hashtags alongside ledger rows (owner decision
2026-08-07; `sql/027`'s header sequences the widened CHECK to ship *with* that
writer). Until then `sql/027` pins the column to `1`, so every stored row is a
ledger row.

That pin is exactly what makes this test necessary and unusual. `?hashtag_id=` on
`GET /transactions` queried the table **without** the source filter every other
reader carries (bug `hashtag-filter`). No behavioral test can catch that: the
CHECK makes the excluded row unstorable, so a query with the filter and a query
without it return identical results for every row that can exist. Trying to seed
a `transaction_source = 2` row raises `CheckViolationError` — pinned below, so
the day someone widens the CHECK this file tells them what else must move.

So the assertion is on the **SQL text**, which is the only place the difference is
observable today. Precedent: `tests/test_sql027_checks.py`, which likewise asserts
a constraint rather than a behavior.

When the inbox hashtag writer ships and the CHECK widens to `IN (1, 2)`:
replace the constraint test below with real two-source rows, and turn the
source-filter assertions into behavioral ones (an inbox-attached hashtag must not
surface a ledger transaction, and must not leak into `compute_month_flow`).

Run: .venv/bin/pytest tests/test_hashtag_source_filter.py -v
"""
import inspect
import re

import asyncpg
import pytest

from app import db
from app.constants import TransactionSource
from app.helpers import monthly_report, transactions as transactions_service
from app.routers import transactions as transactions_router

# Every module that reads or writes the junction table. A new one that forgets
# the column is the regression this list exists to make visible.
JUNCTION_READERS = [
    ("routers/transactions.py", transactions_router),
    ("helpers/transactions.py", transactions_service),
    ("helpers/monthly_report.py", monthly_report),
]

# `expense_transaction_hashtags` is aliased differently per site (`th`, bare), so
# match the column rather than a fixed prefix.
_SOURCE_PREDICATE = re.compile(r"transaction_source\s*(=|IN)")
_JUNCTION_TABLE = "expense_transaction_hashtags"


@pytest.mark.parametrize(
    "label,module", JUNCTION_READERS, ids=[r[0] for r in JUNCTION_READERS]
)
def test_every_junction_query_constrains_transaction_source(label, module):
    """A module that names the junction table must also name the column.

    Deliberately coarse — it proves the predicate is present in the module, not
    that it guards the right query. That is all a source-text check can honestly
    claim, and it is enough to catch the actual failure mode here: a query
    written without the column at all.
    """
    source = inspect.getsource(module)
    assert _JUNCTION_TABLE in source, f"{label}: fixture assumption — table not referenced"
    assert _SOURCE_PREDICATE.search(source), (
        f"{label} queries {_JUNCTION_TABLE} without constraining transaction_source. "
        "Every reader must scope to its own source; see this file's docstring."
    )


def test_the_hashtag_id_filter_specifically_carries_the_predicate():
    """The site the bug was actually in.

    The module-wide check above would stay green if this one query lost its
    predicate while another kept one, so the filter is pinned by name.
    """
    source = inspect.getsource(transactions_router.list_transactions)
    assert _JUNCTION_TABLE in source, "fixture assumption — the filter moved"
    # The predicate is built by an f-string, so the source holds the *template*,
    # not the rendered `= 1`. Asserting the template is also what keeps this in
    # step with the no-hardcoded-integer rule below: the literal form would fail
    # that test, so the two assertions can only be satisfied together.
    assert "transaction_source = {int(TransactionSource.LEDGER)}" in source, (
        "?hashtag_id= must scope to ledger junction rows, via the enum"
    )
    # Ties the template above to the value sql/027's CHECK actually pins. Without
    # this, a renumbered enum would leave the source assertion green while the
    # query silently selected nothing.
    assert int(TransactionSource.LEDGER) == 1, (
        "sql/027 CHECKs transaction_source = 1; the enum must still agree"
    )


def test_no_writer_hardcodes_the_source_integer(request):
    """The value comes from the enum, so a renumbering cannot desync SQL from it.

    Same rule `helpers/home_currency.signed_expr` follows for `TransactionType`
    (audit WP9.9). An f-string interpolating `int(TransactionSource.LEDGER)` is
    correct; a bare `transaction_source = 1` in a query string is not.
    """
    offenders = [
        label
        for label, module in JUNCTION_READERS
        if re.search(r"transaction_source\s*=\s*1\b", inspect.getsource(module))
    ]
    assert not offenders, (
        f"{offenders} hardcode the source integer instead of using "
        "int(TransactionSource.LEDGER)"
    )


@pytest.mark.asyncio
async def test_a_non_ledger_junction_row_is_still_unstorable(test_data):
    """Why the tests above are source-text assertions and not behavioral ones.

    While this passes, the filtered and unfiltered queries are provably
    indistinguishable, so no fixture can demonstrate the bug. When this test
    starts failing, the CHECK has widened — and the assertions above should
    become real ones.
    """
    async with db.pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """INSERT INTO expense_transaction_hashtags
                    (transaction_id, transaction_source, hashtag_id, user_id,
                     created_at, updated_at)
                   VALUES ($1, 2, $2, $3, now(), now())""",
                test_data.transaction_id, test_data.hashtag2_id, test_data.user_id,
            )
