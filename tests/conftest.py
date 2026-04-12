"""Shared fixtures for integration tests.

Tests run against the real Supabase dev database. Auth is bypassed via FastAPI
dependency override — no real JWT required.
"""
import asyncio
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
TEST_EMAIL = "test-sync@expense-world.dev"


@dataclass
class TestData:
    user_id: str
    account_id: str
    category_id: str
    hashtag_id: str
    hashtag2_id: str
    transaction_id: str
    inbox_id: str


_test_data_created = False
_test_data_instance = None


def _build_test_data() -> TestData:
    global _test_data_instance
    if _test_data_instance is None:
        _test_data_instance = TestData(
            user_id=TEST_USER_ID,
            account_id=str(uuid.uuid4()),
            category_id=str(uuid.uuid4()),
            hashtag_id=str(uuid.uuid4()),
            hashtag2_id=str(uuid.uuid4()),
            transaction_id=str(uuid.uuid4()),
            inbox_id=str(uuid.uuid4()),
        )
    return _test_data_instance


async def _ensure_test_data(conn, data: TestData):
    """Create test resources if they don't exist yet."""
    global _test_data_created
    if _test_data_created:
        return

    exists = await conn.fetchval(
        "SELECT 1 FROM users WHERE id = $1", data.user_id
    )
    if exists:
        _test_data_created = True
        return

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
    _test_data_created = True


async def _cleanup_test_data(data: TestData):
    """Remove all test resources from the DB."""
    conn = await asyncpg.connect(settings.supabase_db_url)
    try:
        async with conn.transaction():
            await conn.execute("DELETE FROM sync_checkpoints WHERE user_id = $1", data.user_id)
            await conn.execute("DELETE FROM expense_transaction_hashtags WHERE user_id = $1", data.user_id)
            await conn.execute("DELETE FROM expense_transactions WHERE user_id = $1", data.user_id)
            await conn.execute("DELETE FROM expense_transaction_inbox WHERE user_id = $1", data.user_id)
            await conn.execute("DELETE FROM expense_reconciliations WHERE user_id = $1", data.user_id)
            await conn.execute("DELETE FROM expense_bank_accounts WHERE user_id = $1", data.user_id)
            await conn.execute("DELETE FROM expense_categories WHERE user_id = $1", data.user_id)
            await conn.execute("DELETE FROM expense_hashtags WHERE user_id = $1", data.user_id)
            await conn.execute("DELETE FROM user_settings WHERE user_id = $1", data.user_id)
            await conn.execute("DELETE FROM users WHERE id = $1", data.user_id)
    finally:
        await conn.close()


def pytest_sessionfinish(session, exitstatus):
    """Cleanup test data after all tests complete."""
    data = _build_test_data()
    if _test_data_created:
        asyncio.get_event_loop().run_until_complete(_cleanup_test_data(data))


@pytest.fixture
async def test_data():
    return _build_test_data()


@pytest.fixture
async def client(test_data):
    """Async HTTP client with auth bypassed to the test user."""

    async def mock_user():
        return AuthUser(id=test_data.user_id, email=TEST_EMAIL)

    app.dependency_overrides[get_current_user] = mock_user

    # Each test runs on its own event loop, so the pool must be created fresh.
    db.pool = await asyncpg.create_pool(settings.supabase_db_url)

    async with db.pool.acquire() as conn:
        await _ensure_test_data(conn, test_data)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
    await db.pool.close()
    db.pool = None
