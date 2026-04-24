"""Tests for PUT /v1/auth/profile — post-bootstrap identity mutation.

Bootstrap sets ``display_name`` only on first call; subsequent calls just bump
``last_login_at``. This endpoint is the single path for changing identity
fields on the ``users`` row after bootstrap. Tests verify the happy path,
validation (empty body, explicit null), idempotency replay, activity log, and
a cross-endpoint regression: bootstrap must not clobber a profile-set value.
"""

import uuid
from typing import List, Optional

import pytest

from app import db
from app.constants import ActivityAction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _read_user(user_id: str) -> dict:
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT display_name, last_login_at, updated_at FROM users WHERE id = $1",
            user_id,
        )
    return dict(row)


async def _cleanup_profile_state(
    user_id: str,
    idempotency_keys: Optional[List[str]] = None,
) -> None:
    """Reset users.display_name to NULL and delete UPDATED entries + idempotency keys.

    ``_ensure_test_data`` seeds ``display_name=NULL`` once per session, so
    tests that mutate it must restore that baseline to avoid bleed-over.
    """
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET display_name = NULL WHERE id = $1", user_id,
        )
        await conn.execute(
            """
            DELETE FROM activity_log
            WHERE resource_type = 'user' AND user_id = $1 AND action = $2
            """,
            user_id, ActivityAction.UPDATED,
        )
        if idempotency_keys:
            await conn.execute(
                "DELETE FROM idempotency_keys WHERE user_id = $1 AND key = ANY($2::text[])",
                user_id, idempotency_keys,
            )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_profile_sets_display_name(client, test_data):
    idempotency_key = str(uuid.uuid4())
    try:
        before = await _read_user(test_data.user_id)

        r = await client.put(
            "/v1/auth/profile",
            json={"display_name": "Alex"},
            headers={"X-Idempotency-Key": idempotency_key},
        )
        assert r.status_code == 200, r.text

        body = r.json()
        assert body["id"] == test_data.user_id
        assert body["display_name"] == "Alex"

        after = await _read_user(test_data.user_id)
        assert after["display_name"] == "Alex"
        assert after["updated_at"] > before["updated_at"]
        # Bootstrap's column must NOT be touched.
        assert after["last_login_at"] == before["last_login_at"]
    finally:
        await _cleanup_profile_state(test_data.user_id, [idempotency_key])


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_profile_empty_body_returns_422(client, test_data):
    r = await client.put(
        "/v1/auth/profile",
        json={},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert "display_name" in err["fields"]


@pytest.mark.asyncio
async def test_update_profile_explicit_null_returns_422(client, test_data):
    r = await client.put(
        "/v1/auth/profile",
        json={"display_name": None},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["fields"]["display_name"] == "Must not be null."


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_profile_writes_activity_log(client, test_data):
    idempotency_key = str(uuid.uuid4())
    try:
        before_user = await _read_user(test_data.user_id)

        r = await client.put(
            "/v1/auth/profile",
            json={"display_name": "Audit"},
            headers={"X-Idempotency-Key": idempotency_key},
        )
        assert r.status_code == 200, r.text

        async with db.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT action, before_snapshot, after_snapshot
                FROM activity_log
                WHERE resource_type = 'user'
                  AND resource_id = $1 AND user_id = $2
                  AND action = $3
                """,
                test_data.user_id, test_data.user_id, ActivityAction.UPDATED,
            )

        assert len(rows) == 1
        before_text = rows[0]["before_snapshot"]
        after_text = rows[0]["after_snapshot"]
        # jsonb comes back as str; use substring checks to avoid parsing.
        assert (
            '"display_name": null' in before_text
            if before_user["display_name"] is None
            else f'"display_name": "{before_user["display_name"]}"' in before_text
        )
        assert '"display_name": "Audit"' in after_text
    finally:
        await _cleanup_profile_state(test_data.user_id, [idempotency_key])


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_profile_replay_returns_identical_body(client, test_data):
    idempotency_key = str(uuid.uuid4())
    try:
        first = await client.put(
            "/v1/auth/profile",
            json={"display_name": "Replay"},
            headers={"X-Idempotency-Key": idempotency_key},
        )
        assert first.status_code == 200, first.text
        first_body = first.json()

        second = await client.put(
            "/v1/auth/profile",
            json={"display_name": "Replay"},
            headers={"X-Idempotency-Key": idempotency_key},
        )
        assert second.status_code == 200
        assert second.json() == first_body

        # Exactly one UPDATED entry — replay didn't double-log.
        async with db.pool.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT count(*) FROM activity_log
                WHERE resource_type = 'user' AND user_id = $1 AND action = $2
                """,
                test_data.user_id, ActivityAction.UPDATED,
            )
        assert count == 1
    finally:
        await _cleanup_profile_state(test_data.user_id, [idempotency_key])


@pytest.mark.asyncio
async def test_update_profile_replay_different_body_returns_cached(client, test_data):
    """Same key + different body → first response wins (idempotency contract)."""
    idempotency_key = str(uuid.uuid4())
    try:
        first = await client.put(
            "/v1/auth/profile",
            json={"display_name": "First"},
            headers={"X-Idempotency-Key": idempotency_key},
        )
        assert first.status_code == 200
        first_body = first.json()

        second = await client.put(
            "/v1/auth/profile",
            json={"display_name": "Second"},
            headers={"X-Idempotency-Key": idempotency_key},
        )
        assert second.status_code == 200
        # Cached response, NOT a fresh write.
        assert second.json() == first_body

        after = await _read_user(test_data.user_id)
        assert after["display_name"] == "First"
    finally:
        await _cleanup_profile_state(test_data.user_id, [idempotency_key])


# ---------------------------------------------------------------------------
# Cross-endpoint regression: bootstrap must not clobber profile-set value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_after_profile_preserves_display_name(client, test_data):
    profile_key = str(uuid.uuid4())
    bootstrap_key = str(uuid.uuid4())
    try:
        r = await client.put(
            "/v1/auth/profile",
            json={"display_name": "Profile-set"},
            headers={"X-Idempotency-Key": profile_key},
        )
        assert r.status_code == 200

        # Bootstrap with a DIFFERENT display_name. Idempotent semantics mean
        # it should NOT overwrite the existing row's display_name.
        b = await client.post(
            "/v1/auth/bootstrap",
            json={"display_name": "Bootstrap-wanted", "timezone": "UTC"},
            headers={"X-Idempotency-Key": bootstrap_key},
        )
        assert b.status_code == 200

        after = await _read_user(test_data.user_id)
        assert after["display_name"] == "Profile-set"
    finally:
        await _cleanup_profile_state(
            test_data.user_id, [profile_key, bootstrap_key],
        )
