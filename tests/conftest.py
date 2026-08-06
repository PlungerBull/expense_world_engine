"""Shared fixtures for integration tests.

Tests run against a DEDICATED database (`expense_world_test`), never the one
holding the real ledger. Create it with `deploy/local/create-test-db.sh`, and
re-run that with --force after any schema change. Auth is bypassed via FastAPI
dependency override — no real JWT required.

Until 2026-07-31 the suite ran against `expense_world` itself. The fixtures
below insert and delete rows, and the exchange_rates seed in _ensure_test_data
is global (no user_id column to scope cleanup by), so a test run could leave a
synthetic USD->PEN rate in the ledger that the daily fetch job could never
correct afterwards — ON CONFLICT DO NOTHING means the first writer wins for
that date. Pointing the suite elsewhere removes that whole class of hazard.

All tests and async fixtures share one session-scoped event loop (see
pytest.ini), so the connection pool is created once per session instead of
per test.
"""
import os
import uuid
from dataclasses import dataclass
from urllib.parse import urlparse

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

# Databases this suite is permitted to touch. An allowlist rather than a
# denylist: a new database name is untrusted by default, which is the safe way
# round when the cost of being wrong is a mangled ledger.
_TEST_DB_ALLOWLIST = frozenset({"expense_world_test"})

# Socket-style URL — no host, no username baked in, so it works on any machine
# with the database present. app/config.py treats an empty hostname as local by
# construction, so its non-local-host guard is satisfied too.
_DEFAULT_TEST_DB_URL = "postgresql:///expense_world_test"

# Set BEFORE app.config is imported: Settings() reads the environment at import
# time, and an env var takes precedence over the .env file that points at the
# real ledger. TEST_DATABASE_URL overrides, for CI or a second checkout.
os.environ["SUPABASE_DB_URL"] = os.environ.get("TEST_DATABASE_URL", _DEFAULT_TEST_DB_URL)

from app.config import settings  # noqa: E402  (must follow the env write above)
from app.deps import AuthUser, get_current_user  # noqa: E402
from app.main import app  # noqa: E402
from app import db  # noqa: E402

# Fail closed. The env write above is the mechanism; this is the guarantee. A
# stray SUPABASE_DB_URL in the environment, a future refactor that moves the
# import order, or a typo'd TEST_DATABASE_URL all land here and abort the run
# instead of quietly deleting real rows. Mirrors the non-local-host guard in
# app/config.py — same reasoning, one level narrower.
_TARGET_DB = urlparse(settings.supabase_db_url).path.lstrip("/")
if _TARGET_DB not in _TEST_DB_ALLOWLIST:
    raise RuntimeError(
        f"Refusing to run the test suite against database {_TARGET_DB!r}. "
        f"These fixtures insert and delete rows, so they may only target "
        f"{', '.join(sorted(_TEST_DB_ALLOWLIST))}. Create it with "
        f"deploy/local/create-test-db.sh, and unset any SUPABASE_DB_URL / "
        f"TEST_DATABASE_URL override pointing elsewhere."
    )


TEST_USER_ID = str(uuid.uuid4())
# Worker-unique display_name: the orphan sweep in db_pool deletes every user
# with this display_name except its own, so sharing one across xdist workers
# would let each worker delete the others' live test users. (This discriminator
# was users.email until sql/024 dropped the column.)
_XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
TEST_DISPLAY_NAME = f"test-sync-{_XDIST_WORKER}"


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
            "INSERT INTO users (id, display_name, created_at, updated_at) VALUES ($1, $2, now(), now())",
            data.user_id, TEST_DISPLAY_NAME,
        )
        await conn.execute(
            "INSERT INTO user_settings (user_id, created_at, updated_at) VALUES ($1, now(), now())",
            data.user_id,
        )
        # No balance is seeded. Before sql/022 this INSERT set
        # current_balance_cents = 100000 while the account's only transaction was
        # a 5000 outflow — a fixture asserting a number its own rows contradicted.
        # The balance is computed now, so the seed account is worth exactly what
        # is seeded below it: -5000. Nothing asserts the old figure; every balance
        # assertion in the suite is either relative (before/after a mutation) or
        # on an account the test creates itself.
        await conn.execute(
            """INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, is_person, color,
                 is_archived, sort_order, created_at, updated_at)
               VALUES ($1, $2, 'Test Account', 'PEN', false, '#000000',
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
                (id, user_id, title, amount_cents, transaction_type,
                 date, account_id, category_id, cleared,
                 created_at, updated_at)
               VALUES ($1, $2, 'Test Tx', 5000, 1,
                 now(), $3, $4, false, now(), now())""",
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
                (id, user_id, title, status, created_at, updated_at)
               VALUES ($1, $2, 'Test Inbox', 1, now(), now())""",
            data.inbox_id, data.user_id,
        )
        # Seed USD→PEN so USD-account rows convert. Since sql/021 this feeds the
        # READ path (helpers/home_currency.py's lateral, and get_rate for account
        # balances) — no write resolves a rate any more.
        await conn.execute(
            """INSERT INTO exchange_rates (base_currency, target_currency, rate, rate_date, created_at)
               VALUES ('USD', 'PEN', 3.4, CURRENT_DATE, now())
               ON CONFLICT (base_currency, target_currency, rate_date) DO NOTHING""",
        )


async def _cleanup_test_data(conn, user_id: str):
    """Remove all resources belonging to one test user."""
    async with conn.transaction():
        await conn.execute("DELETE FROM idempotency_keys WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM activity_log WHERE user_id = $1", user_id)
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
    # Every pool slot pins a real backend connection on a direct local
    # connection, and 4 xdist workers at the configured size would eat most of
    # Postgres's max_connections. Tests never run more than 2 requests
    # concurrently per worker, so a tiny pool suffices.
    settings.db_pool_min_size = 1
    settings.db_pool_max_size = 3
    await db.connect()
    async with db.pool.acquire() as conn:
        await _ensure_test_data(conn, test_data)
        # Sweep orphans from previous runs that were killed before teardown.
        orphans = await conn.fetch(
            "SELECT id FROM users WHERE display_name = $1 AND id != $2",
            TEST_DISPLAY_NAME, test_data.user_id,
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
        return AuthUser(id=test_data.user_id)

    app.dependency_overrides[get_current_user] = mock_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
