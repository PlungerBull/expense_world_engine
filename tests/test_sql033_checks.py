"""Pins for sql/033 — the junction table's two-source constraints.

sql/033 shipped with the inbox hashtag writer and changed two things about
`expense_transaction_hashtags`:

  * hashtags_transaction_source_valid widened from `= 1` to `IN (1, 2)`.
    1 = ledger, 2 = inbox — the mapping the code has always written, not the
    pre-WP7 schema doc's inverted description of it. A third value stays
    rejected: the enum is closed, and admitting an unwritten value is the
    failure sql/027's header warned about.

  * The UNIQUE key gained `transaction_source`. The old two-column key
    asserted a (parent, hashtag) pair was unique ACROSS parent kinds, which
    stopped being true the moment a second kind existed — and the id spaces
    overlap by design, since `POST /inbox/{id}/promote` lets a client hand the
    new ledger row the draft's own uuid. sql/033's header works through the
    silent failure that produced.

Run: .venv/bin/pytest tests/test_sql033_checks.py -v
"""
import uuid

import asyncpg
import pytest

from app import db


async def _insert(conn, *, transaction_id, source, hashtag_id, user_id):
    await conn.execute(
        """INSERT INTO expense_transaction_hashtags
            (transaction_id, transaction_source, hashtag_id, user_id, created_at, updated_at)
           VALUES ($1, $2, $3, $4, now(), now())""",
        transaction_id, source, hashtag_id, user_id,
    )


@pytest.mark.asyncio
async def test_inbox_source_is_admitted(test_data):
    """2 is storable — the half sql/027 deliberately withheld until the writer."""
    parent_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        try:
            await _insert(
                conn, transaction_id=parent_id, source=2,
                hashtag_id=test_data.hashtag_id, user_id=test_data.user_id,
            )
        finally:
            await conn.execute(
                "DELETE FROM expense_transaction_hashtags WHERE transaction_id = $1",
                parent_id,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_source", [0, 3, -1])
async def test_unknown_sources_stay_rejected(test_data, bad_source):
    """The enum is closed at two. A third parent table would ship its own
    migration, its own reader predicate and its own entry in TransactionSource
    — never a value that simply appears."""
    async with db.pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert(
                conn, transaction_id=str(uuid.uuid4()), source=bad_source,
                hashtag_id=test_data.hashtag_id, user_id=test_data.user_id,
            )


@pytest.mark.asyncio
async def test_one_uuid_can_carry_the_same_hashtag_in_both_sources(test_data):
    """The UNIQUE fix, stated as data.

    Under the old two-column key the second insert here raised
    UniqueViolationError — and in the real flow it did something worse than
    raise: promote's `ON CONFLICT` matched the inbox row, found it active, and
    left the promoted ledger row untagged with no error at all.
    """
    shared_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        try:
            await _insert(
                conn, transaction_id=shared_id, source=1,
                hashtag_id=test_data.hashtag_id, user_id=test_data.user_id,
            )
            await _insert(
                conn, transaction_id=shared_id, source=2,
                hashtag_id=test_data.hashtag_id, user_id=test_data.user_id,
            )
            rows = await conn.fetch(
                "SELECT transaction_source FROM expense_transaction_hashtags "
                "WHERE transaction_id = $1 ORDER BY transaction_source",
                shared_id,
            )
            assert [r["transaction_source"] for r in rows] == [1, 2]
        finally:
            await conn.execute(
                "DELETE FROM expense_transaction_hashtags WHERE transaction_id = $1",
                shared_id,
            )


@pytest.mark.asyncio
async def test_a_pair_is_still_unique_within_one_source(test_data):
    """Widening the key must not have loosened what it was actually for:
    one live junction row per (parent, hashtag) within a source — the property
    `sync_hashtags`' ON CONFLICT upsert relies on to re-activate rather than
    duplicate."""
    parent_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        try:
            await _insert(
                conn, transaction_id=parent_id, source=2,
                hashtag_id=test_data.hashtag_id, user_id=test_data.user_id,
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await _insert(
                    conn, transaction_id=parent_id, source=2,
                    hashtag_id=test_data.hashtag_id, user_id=test_data.user_id,
                )
        finally:
            await conn.execute(
                "DELETE FROM expense_transaction_hashtags WHERE transaction_id = $1",
                parent_id,
            )
