"""Every reader of ``expense_transaction_hashtags`` stays inside its own source.

The junction table is shared between two parents: ``transaction_source = 1``
rows hang off ``expense_transactions``, ``= 2`` rows off
``expense_transaction_inbox``. ``transaction_id`` carries no foreign key, and
the two tables have independent id spaces — so nothing but the source predicate
keeps a ledger query out of the inbox's rows.

**This file changed shape on 2026-08-14, when the inbox writer shipped
(sql/033).** Until then `sql/027` pinned the column to `1`, which made the bug
this file was written for — `?hashtag_id=` querying without the source
predicate every other reader carried (bug `hashtag-filter`) — impossible to
demonstrate: the excluded row was unstorable, so filtered and unfiltered
queries returned identical results for every row that could exist. The
assertions were therefore on SQL *text*, and the docstring left instructions to
replace them with real two-source rows once the CHECK widened. That is what the
behavioral half below now does.

The source-text half is kept, narrowed to what text can honestly claim: a
module that queries the table must name the discriminator somewhere. It catches
the one failure mode a behavioral test cannot — a *new* query written without
the column at all, in a code path no test happens to exercise.

Run: .venv/bin/pytest tests/test_hashtag_source_filter.py -v
"""
import inspect
import re
import uuid

import pytest

from app import db
from app.constants import TransactionSource
from app.helpers import (
    hashtag_links,
    hashtags as hashtags_service,
    monthly_report,
)
from app.routers import transactions as transactions_router

# Every module that reads or writes the junction table with a single source in
# mind. A new one that forgets the column is the regression this list exists to
# make visible.
JUNCTION_READERS = [
    ("helpers/hashtag_links.py", hashtag_links),
    ("helpers/monthly_report.py", monthly_report),
    ("routers/transactions.py", transactions_router),
]

# The one deliberate exception, listed rather than omitted so it stays a
# decision instead of a gap: DELETE /hashtags/{id} spans BOTH sources on
# purpose (deleting a hashtag removes it from everything it is on), so its
# UPDATE has no source predicate. It does read the column back — `RETURNING
# transaction_source` — to route each parent's version bump to the right table.
SOURCE_SPANNING = [("helpers/hashtags.py", hashtags_service)]

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


@pytest.mark.parametrize(
    "label,module", SOURCE_SPANNING, ids=[r[0] for r in SOURCE_SPANNING]
)
def test_the_source_spanning_writer_still_reads_the_column(label, module):
    """The exception must remain deliberate.

    `delete_hashtag` is allowed to skip the predicate, but it is NOT allowed to
    ignore the column: without `RETURNING transaction_source` its parent
    version-bump sends inbox ids to `expense_transactions`, where they match
    nothing, and a draft's `hashtag_ids` changes on the wire with a stale
    `version`. (That is exactly what the pre-2026-08-14 implementation did — it
    was correct only while inbox junction rows could not exist.)
    """
    source = inspect.getsource(module)
    assert "RETURNING transaction_id, transaction_source" in source, (
        f"{label} cascades across both sources without reading the source back; "
        "the per-table version bump cannot be correct without it."
    )
    assert "expense_transaction_inbox" in source, (
        f"{label} must bump inbox parents too, not only ledger ones."
    )


def test_the_hashtag_id_filter_specifically_carries_the_predicate():
    """The site the bug was actually in.

    The module-wide check above would stay green if this one query lost its
    predicate while another kept one, so the filter is pinned by name. Kept as
    a text assertion alongside the behavioral test below because the two fail
    differently: this one names the fix, that one proves the effect.
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
    # Ties the template above to the value sql/033's CHECK admits. Without this,
    # a renumbered enum would leave the source assertion green while the query
    # silently selected nothing.
    assert int(TransactionSource.LEDGER) == 1, (
        "sql/033 CHECKs transaction_source IN (1, 2) with 1 = ledger; "
        "the enum must still agree"
    )
    assert int(TransactionSource.INBOX) == 2


def test_no_writer_hardcodes_the_source_integer(request):
    """The value comes from the enum, so a renumbering cannot desync SQL from it.

    Same rule `helpers/home_currency.signed_expr` follows for `TransactionType`
    (audit WP9.9). An f-string interpolating `int(TransactionSource.LEDGER)` is
    correct; a bare `transaction_source = 1` in a query string is not.
    """
    offenders = [
        label
        for label, module in JUNCTION_READERS + SOURCE_SPANNING
        if re.search(r"transaction_source\s*=\s*[12]\b", inspect.getsource(module))
    ]
    assert not offenders, (
        f"{offenders} hardcode the source integer instead of using "
        "int(TransactionSource.<SOURCE>)"
    )


# ---------------------------------------------------------------------------
# The behavioral half — two real sources, no leakage in either direction
# ---------------------------------------------------------------------------


async def _tag_ids(parent_id: str, source: TransactionSource) -> list[str]:
    async with db.pool.acquire() as conn:
        return (await hashtag_links.fetch_hashtag_ids_map(conn, [parent_id], source))[
            parent_id
        ]


@pytest.fixture
async def two_source_pair(client, test_data):
    """A ledger transaction and an inbox draft, each tagged with #test-sync.

    Same hashtag, same owner, different sources — the arrangement every
    assertion below needs and the one that was unstorable before sql/033.
    """
    txn_id = str(uuid.uuid4())
    inbox_id = str(uuid.uuid4())

    txn_r = await client.post(
        "/v1/transactions",
        json={
            "id": txn_id,
            "title": f"ledger-{uuid.uuid4()}",
            "amount_cents": -1100,
            "date": "2026-05-04T12:00:00Z",
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
            "hashtag_ids": [test_data.hashtag_id],
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert txn_r.status_code == 201, txn_r.text

    inbox_r = await client.post(
        "/v1/inbox",
        json={
            "id": inbox_id,
            "title": f"draft-{uuid.uuid4()}",
            "amount_cents": -2200,
            "date": "2026-05-04T12:00:00Z",
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
            "hashtag_ids": [test_data.hashtag_id],
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert inbox_r.status_code == 201, inbox_r.text

    yield txn_id, inbox_id

    async with db.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM expense_transaction_hashtags WHERE transaction_id = ANY($1::uuid[])",
            [txn_id, inbox_id],
        )
        await conn.execute(
            "DELETE FROM activity_log WHERE resource_id = ANY($1::uuid[])",
            [txn_id, inbox_id],
        )
        await conn.execute("DELETE FROM expense_transactions WHERE id = $1", txn_id)
        await conn.execute("DELETE FROM expense_transaction_inbox WHERE id = $1", inbox_id)


@pytest.mark.asyncio
async def test_two_sources_can_now_hold_the_same_hashtag(two_source_pair, test_data):
    """The premise: both rows exist, both tagged, neither sees the other's."""
    txn_id, inbox_id = two_source_pair

    assert await _tag_ids(txn_id, TransactionSource.LEDGER) == [test_data.hashtag_id]
    assert await _tag_ids(inbox_id, TransactionSource.INBOX) == [test_data.hashtag_id]
    # Cross-source reads come back empty — the predicate, doing its job.
    assert await _tag_ids(txn_id, TransactionSource.INBOX) == []
    assert await _tag_ids(inbox_id, TransactionSource.LEDGER) == []


@pytest.mark.asyncio
async def test_an_inbox_tag_does_not_surface_a_draft_in_the_ledger_filter(
    two_source_pair, client, test_data
):
    """`GET /transactions?hashtag_id=` returns ledger rows only.

    The bug this file was created for, now demonstrable: without the source
    predicate the draft's junction row would match, and the filter would try to
    return an inbox id from the transactions table.
    """
    txn_id, inbox_id = two_source_pair

    r = await client.get(
        f"/v1/transactions?hashtag_id={test_data.hashtag_id}&limit=200"
    )
    assert r.status_code == 200, r.text
    ids = {item["id"] for item in r.json()["items"]}
    assert txn_id in ids
    assert inbox_id not in ids


@pytest.mark.asyncio
async def test_inbox_junctions_do_not_leak_into_the_month_report(
    two_source_pair, client, test_data
):
    """`compute_month_flow` aggregates ledger rows only.

    The draft is not in the report at all (it is not a ledger row), so its tag
    must not appear in `hashtag_breakdown` either — and the ledger row's own
    2200-vs-1100 amount is the tell if the two ever merged.
    """
    txn_id, inbox_id = two_source_pair

    r = await client.get("/v1/reports/monthly?year=2026&month=5")
    assert r.status_code == 200, r.text
    body = r.json()

    tagged_rows = [
        row
        for cat in body["categories"]
        for row in cat["hashtag_breakdown"]
        if test_data.hashtag_id in row["hashtag_ids"]
    ]
    total = sum(row["spent_home_cents"] for row in tagged_rows)
    assert total == -1100, (
        "the tagged figure must be the ledger row alone; a draft's junction row "
        f"leaking in would add its 2200 (got {total})"
    )
