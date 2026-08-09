"""Account routes 422 SETTINGS_MISSING when user_settings is absent.

Owner decision 2026-08-08 (bloat-audit §15): before the home-balance
consolidation, `/accounts` silently emitted `current_balance_home_cents:
null` when the settings row was missing, while `/dashboard` refused loudly.
The three conversion copies became one (`account_balance.fetch_home_balance`
/ `fetch_home_balances`, reading settings via
`settings.get_user_report_settings`), so every home-converting surface now
fails the same way. Unreachable in normal use — bootstrap creates settings —
which is exactly why blanks would have gone unexplained.
"""
import pytest

from app import db


@pytest.fixture
async def settings_deleted(test_data):
    """Snapshot + delete the user's settings row, restoring on exit."""
    async with db.pool.acquire() as conn:
        original = await conn.fetchrow(
            "SELECT * FROM user_settings WHERE user_id = $1", test_data.user_id
        )
        assert original is not None, "test_data should have user_settings"
        await conn.execute(
            "DELETE FROM user_settings WHERE user_id = $1", test_data.user_id
        )
    yield
    async with db.pool.acquire() as conn:
        cols = list(original.keys())
        placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
        await conn.execute(
            f"INSERT INTO user_settings ({', '.join(cols)}) VALUES ({placeholders})",
            *[original[c] for c in cols],
        )


@pytest.mark.asyncio
async def test_account_list_and_detail_422_settings_missing(
    client, test_data, settings_deleted
):
    for url in ("/v1/accounts", f"/v1/accounts/{test_data.account_id}"):
        r = await client.get(url)
        assert r.status_code == 422, (url, r.text)
        error = r.json()["error"]
        assert error["code"] == "SETTINGS_MISSING", (url, error)
