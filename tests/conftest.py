"""Shared fixtures for integration tests.

Tests run against the real Supabase dev database. Auth is bypassed via FastAPI
dependency override — no real JWT required.

All tests and async fixtures share one session-scoped event loop (see
pytest.ini), so the connection pool is created once per session instead of
per test.
"""
import os
import uuid
from dataclasses import dataclass

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.deps import AuthUser, get_current_user
from app.main import app
from app import db


TEST_USER_ID = str(uuid.uuid4())
# Worker-unique email: the orphan sweep in db_pool deletes every user with
# this email except its own, so sharing one email across xdist workers would
# let each worker delete the others' live test users.
_XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
TEST_EMAIL = f"test-sync-{_XDIST_WORKER}@expense-world.dev"


@dataclass
class TestData:
    user_id: str
    account_id: str
    category_id: str
    hashtag_id: str
    hashtag2_id: str
    transaction_id: str
    inbox_id: str


async def _ensure_test_data(conn, data: TestData):
    """Create the session's seed resources."""
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO users (id, email, created_at, updated_at) VALUES ($1, $2, now(), now())",
            data.user_id, TEST_EMAIL,
        )
        await conn.execute(
            "INSERT INTO user_settings (user_id, created_at, updated_at) VALUES ($1, now(), now())",
            data.user_id,
        )
        await conn.execute(
            """INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, is_person, color, current_balance_cents,
                 is_archived, sort_order, created_at, updated_at)
               VALUES ($1, $2, 'Test Account', 'PEN', false, '#000000', 100000,
                 false, 1, now(), now())""",
            data.account_id, data.user_id,
        )
        await conn.execute(
            """INSERT INTO expense_categories
                (id, user_id, name, color, is_system, sort_order, created_at, updated_at)
               VALUES ($1, $2, 'Test Category', '#FF0000', false, 1, now(), now())""",
            data.category_id, data.user_id,
        )
        await conn.execute(
            """INSERT INTO expense_hashtags
                (id, user_id, name, sort_order, created_at, updated_at)
               VALUES ($1, $2, '#test-sync', 1, now(), now())""",
            data.hashtag_id, data.user_id,
        )
        await conn.execute(
            """INSERT INTO expense_hashtags
                (id, user_id, name, sort_order, created_at, updated_at)
               VALUES ($1, $2, '#test-sync-2', 2, now(), now())""",
            data.hashtag2_id, data.user_id,
        )
        await conn.execute(
            """INSERT INTO expense_transactions
                (id, user_id, title, amount_cents, amount_home_cents, transaction_type,
                 date, account_id, category_id, exchange_rate, cleared,
                 created_at, updated_at)
               VALUES ($1, $2, 'Test Tx', 5000, 5000, 1,
                 now(), $3, $4, 1.0, false, now(), now())""",
            data.transaction_id, data.user_id, data.account_id, data.category_id,
        )
        await conn.execute(
            """INSERT INTO expense_transaction_hashtags
                (transaction_id, transaction_source, hashtag_id, user_id, created_at, updated_at)
               VALUES ($1, 1, $2, $3, now(), now())""",
            data.transaction_id, data.hashtag_id, data.user_id,
        )
        await conn.execute(
            """INSERT INTO expense_transaction_inbox
                (id, user_id, title, exchange_rate, status, created_at, updated_at)
               VALUES ($1, $2, 'Test Inbox', 1.0, 1, now(), now())""",
            data.inbox_id, data.user_id,
        )
        # Seed exchange rate so get_rate(PEN, USD, ...) works in recalc tests
        await conn.execute(
            """INSERT INTO exchange_rates (base_currency, target_currency, rate, rate_date, created_at)
               VALUES ('USD', 'PEN', 3.75, CURRENT_DATE, now())
               ON CONFLICT (base_currency, target_currency, rate_date) DO NOTHING""",
        )


async def _cleanup_test_data(conn, user_id: str):
    """Remove all resources belonging to one test user."""
    async with conn.transaction():
        await conn.execute("DELETE FROM idempotency_keys WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM activity_log WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM sync_checkpoints WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM expense_transaction_hashtags WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM expense_transactions WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM expense_transaction_inbox WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM expense_reconciliations WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM expense_bank_accounts WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM expense_categories WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM expense_hashtags WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM user_settings WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)


@pytest.fixture(scope="session")
def test_data():
    return TestData(
        user_id=TEST_USER_ID,
        account_id=str(uuid.uuid4()),
        category_id=str(uuid.uuid4()),
        hashtag_id=str(uuid.uuid4()),
        hashtag2_id=str(uuid.uuid4()),
        transaction_id=str(uuid.uuid4()),
        inbox_id=str(uuid.uuid4()),
    )


@pytest.fixture(scope="session", autouse=True)
async def db_pool(test_data):
    """Session-wide connection pool + seed data.

    ASGITransport never runs the app lifespan, so this fixture substitutes for
    it (the lifespan is db.connect()/db.disconnect()). Tests must only use
    `async with db.pool.acquire()` — pool.close() at teardown waits on
    outstanding connections.
    """
    # .env points at the Supavisor session-mode pooler (port 5432), where
    # every pool slot pins a real backend connection. The prod sizing
    # (5..50 per process) targets the transaction pooler; 4 xdist workers
    # at that size exhaust the project's backend pool cap. Tests never run
    # more than 2 requests concurrently per worker, so a tiny pool suffices.
    settings.db_pool_min_size = 1
    settings.db_pool_max_size = 3
    await db.connect()
    async with db.pool.acquire() as conn:
        await _ensure_test_data(conn, test_data)
        # Sweep orphans from previous runs that were killed before teardown.
        orphans = await conn.fetch(
            "SELECT id FROM users WHERE email = $1 AND id != $2",
            TEST_EMAIL, test_data.user_id,
        )
        for row in orphans:
            await _cleanup_test_data(conn, str(row["id"]))

    yield db.pool

    try:
        # Cleanup on a fresh direct connection: it must succeed even if the
        # pool is unhealthy by session end.
        conn = await asyncpg.connect(settings.supabase_db_url)
        try:
            await _cleanup_test_data(conn, test_data.user_id)
        finally:
            await conn.close()
    finally:
        await db.disconnect()


@pytest.fixture
async def client(test_data, db_pool):
    """Async HTTP client with auth bypassed to the test user."""

    async def mock_user():
        return AuthUser(id=test_data.user_id, email=TEST_EMAIL)

    app.dependency_overrides[get_current_user] = mock_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
