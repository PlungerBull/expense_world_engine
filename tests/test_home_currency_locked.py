"""The home currency is PEN and is not changeable.

Replaces tests/test_home_currency_recalc.py (deleted 2026-08-01 with
app/helpers/recalculate_home_currency.py — see WP1.1 in
docs/audit-2026-08-01-remediation-plan.md).

These pin the lock at both layers, because either one alone is a trap:
the API rejection alone leaves the DB writable by any other path, and
the CHECK alone would surface as a 500 instead of a 422.

Run: .venv/bin/pytest tests/test_home_currency_locked.py -v
"""
import uuid

import asyncpg
import pytest

from app import db


@pytest.mark.asyncio
async def test_changing_main_currency_returns_422(client):
    """A switch request fails loudly rather than being silently ignored."""
    r = await client.put(
        "/v1/auth/settings",
        json={"main_currency": "USD"},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "main_currency" in body["error"]["fields"]


@pytest.mark.asyncio
async def test_setting_main_currency_to_current_value_also_rejected(client):
    """Even a no-op restatement is rejected — the field is not updatable at
    all, so callers can't discover a 'sometimes it works' behaviour."""
    r = await client.put(
        "/v1/auth/settings",
        json={"main_currency": "PEN"},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_other_settings_still_updatable(client):
    """The lock is scoped to main_currency; the rest of the PUT still works
    and the response no longer carries a `recalculation` field.

    display_timezone is the one remaining mutable field since sql/024
    dropped the six echo-only preference columns (sql/024, WP5)."""
    r = await client.put(
        "/v1/auth/settings",
        json={"display_timezone": "America/Lima"},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["display_timezone"] == "America/Lima"
    assert body["main_currency"] == "PEN"
    assert "recalculation" not in body


@pytest.mark.asyncio
async def test_db_check_constraint_blocks_direct_write(client, test_data):
    """sql/018 — the lock survives any path that bypasses the API."""
    async with db.pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE user_settings SET main_currency = 'USD' WHERE user_id = $1",
                    test_data.user_id,
                )
