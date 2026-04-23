"""Tests for Personal Access Tokens.

Covers two distinct concerns:

1. **Endpoint behavior** (via the standard ``client`` fixture, which
   overrides auth in ``conftest.py``): POST /auth/pat, DELETE
   /auth/pat/{id}, idempotency replay, activity-log safety.

2. **Middleware branching** (direct ``get_current_user`` calls,
   bypassing the fixture's dependency override): verifies the real
   PAT resolution path hits the DB and correctly accepts valid PATs,
   rejects revoked/unknown ones, and leaves the JWT path intact.
"""

import uuid
from typing import List, Optional

import pytest
from jose import jwt

from app import db
from app.config import settings
from app.deps import get_current_user
from app.errors import AppError
from app.helpers.auth_token import PAT_PREFIX, hash_pat


# ---------------------------------------------------------------------------
# 1. Endpoint behavior (auth overridden by fixture)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_pat_returns_plaintext_once(client, test_data):
    pat_id = None
    try:
        r = await client.post(
            "/v1/auth/pat",
            json={"name": "laptop"},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 201, r.text
        body = r.json()

        assert body["token"].startswith(PAT_PREFIX)
        assert body["token_prefix"] == body["token"][:len(PAT_PREFIX) + 4]
        assert body["name"] == "laptop"
        assert body["user_id"] == test_data.user_id
        assert body["revoked_at"] is None

        pat_id = body["id"]

        # DB stores the hash, never the plaintext.
        async with db.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT token_hash FROM personal_access_tokens WHERE id = $1",
                pat_id,
            )
        assert row["token_hash"] == hash_pat(body["token"])
        assert body["token"] not in row["token_hash"]
    finally:
        await _cleanup_pat(pat_id, test_data.user_id)


@pytest.mark.asyncio
async def test_create_pat_with_null_name(client, test_data):
    pat_id = None
    try:
        r = await client.post(
            "/v1/auth/pat",
            json={},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["name"] is None
        pat_id = body["id"]
    finally:
        await _cleanup_pat(pat_id, test_data.user_id)


@pytest.mark.asyncio
async def test_create_pat_replay_returns_identical_body(client, test_data):
    idempotency_key = str(uuid.uuid4())
    pat_id = None
    try:
        first = await client.post(
            "/v1/auth/pat",
            json={"name": "replay"},
            headers={"X-Idempotency-Key": idempotency_key},
        )
        assert first.status_code == 201, first.text
        first_body = first.json()
        pat_id = first_body["id"]

        second = await client.post(
            "/v1/auth/pat",
            json={"name": "replay"},
            headers={"X-Idempotency-Key": idempotency_key},
        )
        assert second.status_code == 201
        # Byte-for-byte equality — including the one-shot token value,
        # which is the whole point of caching the full response.
        assert second.json() == first_body

        # Exactly one PAT row — replay didn't double-insert.
        async with db.pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT count(*) FROM personal_access_tokens WHERE user_id = $1",
                test_data.user_id,
            )
        assert count == 1
    finally:
        await _cleanup_pat(pat_id, test_data.user_id, idempotency_keys=[idempotency_key])


@pytest.mark.asyncio
async def test_activity_log_snapshot_omits_token_secrets(client, test_data):
    """The audit trail must never carry token_hash or plaintext."""
    pat_id = None
    try:
        r = await client.post(
            "/v1/auth/pat",
            json={"name": "audit-check"},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        pat_id = body["id"]

        async with db.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT action, after_snapshot
                FROM activity_log
                WHERE resource_type = 'personal_access_token'
                  AND resource_id = $1 AND user_id = $2
                """,
                pat_id, test_data.user_id,
            )

        assert len(rows) == 1
        snapshot_text = rows[0]["after_snapshot"]  # jsonb comes back as str
        assert "token_hash" not in snapshot_text
        # Match the random suffix specifically, not the constant prefix
        # (PAT_PREFIX is fine to appear — token_prefix is in the snapshot).
        plaintext_suffix = body["token"].removeprefix(PAT_PREFIX)
        assert plaintext_suffix not in snapshot_text
    finally:
        await _cleanup_pat(pat_id, test_data.user_id)


@pytest.mark.asyncio
async def test_revoke_pat(client, test_data):
    pat_id = None
    try:
        create_r = await client.post(
            "/v1/auth/pat",
            json={"name": "to-revoke"},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert create_r.status_code == 201
        pat_id = create_r.json()["id"]

        revoke_r = await client.delete(
            f"/v1/auth/pat/{pat_id}",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert revoke_r.status_code == 200, revoke_r.text
        assert revoke_r.json()["revoked_at"] is not None

        async with db.pool.acquire() as conn:
            revoked_at = await conn.fetchval(
                "SELECT revoked_at FROM personal_access_tokens WHERE id = $1",
                pat_id,
            )
        assert revoked_at is not None

        # Activity log carries a DELETED entry with before/after snapshots.
        async with db.pool.acquire() as conn:
            actions = await conn.fetch(
                """
                SELECT action FROM activity_log
                WHERE resource_type = 'personal_access_token'
                  AND resource_id = $1 AND user_id = $2
                ORDER BY created_at ASC
                """,
                pat_id, test_data.user_id,
            )
        assert [r["action"] for r in actions] == [1, 3]  # CREATED, DELETED
    finally:
        await _cleanup_pat(pat_id, test_data.user_id)


@pytest.mark.asyncio
async def test_revoke_already_revoked_returns_404(client, test_data):
    pat_id = None
    try:
        create_r = await client.post(
            "/v1/auth/pat",
            json={},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert create_r.status_code == 201
        pat_id = create_r.json()["id"]

        first_revoke = await client.delete(
            f"/v1/auth/pat/{pat_id}",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert first_revoke.status_code == 200

        second_revoke = await client.delete(
            f"/v1/auth/pat/{pat_id}",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert second_revoke.status_code == 404
    finally:
        await _cleanup_pat(pat_id, test_data.user_id)


@pytest.mark.asyncio
async def test_revoke_nonexistent_returns_404(client):
    unknown_id = str(uuid.uuid4())
    r = await client.delete(
        f"/v1/auth/pat/{unknown_id}",
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 2. Middleware branching (bypass the fixture's dependency override)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_middleware_resolves_valid_pat(client, test_data):
    """Seed a PAT in the DB and call get_current_user directly.

    ``client`` is requested for its side-effect of setting up the DB
    pool + test data — but we don't make HTTP calls in this test, so
    the dependency override does not apply. ``get_current_user`` runs
    the real code path against the real DB.
    """
    plaintext = f"{PAT_PREFIX}test-valid-{uuid.uuid4().hex}"
    token_hash = hash_pat(plaintext)
    pat_id = str(uuid.uuid4())

    async with db.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO personal_access_tokens
                (id, user_id, token_hash, token_prefix, name, created_at)
            VALUES ($1, $2, $3, $4, 'middleware-test', now())
            """,
            pat_id, test_data.user_id, token_hash, plaintext[:12],
        )

    try:
        auth_user = await get_current_user(authorization=f"Bearer {plaintext}")
        assert auth_user.id == test_data.user_id
        assert auth_user.email is None
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM personal_access_tokens WHERE id = $1", pat_id,
            )


@pytest.mark.asyncio
async def test_middleware_rejects_revoked_pat(client, test_data):
    plaintext = f"{PAT_PREFIX}test-revoked-{uuid.uuid4().hex}"
    pat_id = str(uuid.uuid4())

    async with db.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO personal_access_tokens
                (id, user_id, token_hash, token_prefix, name, created_at, revoked_at)
            VALUES ($1, $2, $3, $4, NULL, now(), now())
            """,
            pat_id, test_data.user_id, hash_pat(plaintext), plaintext[:12],
        )

    try:
        with pytest.raises(AppError) as exc_info:
            await get_current_user(authorization=f"Bearer {plaintext}")
        assert exc_info.value.status_code == 401
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM personal_access_tokens WHERE id = $1", pat_id,
            )


@pytest.mark.asyncio
async def test_middleware_rejects_unknown_pat(client):
    # Well-formed prefix, random suffix, not in DB.
    with pytest.raises(AppError) as exc_info:
        await get_current_user(
            authorization=f"Bearer {PAT_PREFIX}not-a-real-token-{uuid.uuid4().hex}",
        )
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_middleware_still_accepts_jwt(client, test_data):
    """Regression guard: adding the PAT branch must not break JWTs."""
    payload = {"sub": test_data.user_id, "email": "jwt-regression@test.dev"}
    token = jwt.encode(payload, settings.supabase_jwt_secret, algorithm="HS256")

    auth_user = await get_current_user(authorization=f"Bearer {token}")
    assert auth_user.id == test_data.user_id
    assert auth_user.email == "jwt-regression@test.dev"


# ---------------------------------------------------------------------------
# Shared cleanup
# ---------------------------------------------------------------------------


async def _cleanup_pat(
    pat_id: Optional[str],
    user_id: str,
    idempotency_keys: Optional[List[str]] = None,
) -> None:
    async with db.pool.acquire() as conn:
        if pat_id is not None:
            await conn.execute(
                """
                DELETE FROM activity_log
                WHERE resource_type = 'personal_access_token'
                  AND resource_id = $1 AND user_id = $2
                """,
                pat_id, user_id,
            )
            await conn.execute(
                "DELETE FROM personal_access_tokens WHERE id = $1 AND user_id = $2",
                pat_id, user_id,
            )
        if idempotency_keys:
            await conn.execute(
                "DELETE FROM idempotency_keys WHERE user_id = $1 AND key = ANY($2::text[])",
                user_id, idempotency_keys,
            )
