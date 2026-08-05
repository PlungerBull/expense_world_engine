"""Real authentication, over HTTP, with no dependency override.

Every other test module uses the `client` fixture, which replaces
`get_current_user` with a stub returning the test user (`conftest.py:219-232`).
That is the right call for domain tests — but it means the entire suite ran
green while the auth layer accepted forged tokens for any user, which is
exactly how audit finding 2.1 stayed invisible from the day it shipped.

This module deliberately does NOT use that fixture. It drives the real
dependency through a real request, so a regression in the auth path fails here
rather than in production.

Covered:
  * no Authorization header            -> 401
  * wrong scheme / malformed header    -> 401
  * a forged HS256 JWT                 -> 401  (the 2.1 bypass)
  * an unstructured bearer token       -> 401
  * a well-formed but unknown PAT      -> 401
  * a valid PAT                        -> 200, resolving to its owner
  * a revoked PAT                      -> 401
"""

import hashlib
import secrets
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app import db
from app.helpers.auth_token import PAT_PREFIX, hash_pat
from app.main import app

# A protected route that does no work beyond resolving the caller.
PROTECTED = "/v1/auth/me"


@pytest.fixture
async def raw_client(db_pool):
    """HTTP client with the REAL auth dependency — no override."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _issue_pat(user_id: str) -> tuple[str, str]:
    """Mint a PAT directly, the way deploy/local/README.md documents."""
    token = f"{PAT_PREFIX}{secrets.token_urlsafe(32)}"
    pat_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO personal_access_tokens
                (id, user_id, name, token_hash, token_prefix, created_at)
            VALUES ($1, $2, $3, $4, $5, now())
            """,
            pat_id, user_id, f"wire-test-{uuid.uuid4().hex[:8]}",
            hash_pat(token), token[:16],
        )
    return pat_id, token


async def _revoke(pat_id: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE personal_access_tokens SET revoked_at = now() WHERE id = $1",
            pat_id,
        )


async def _delete(pat_id: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute("DELETE FROM personal_access_tokens WHERE id = $1", pat_id)


# ---------------------------------------------------------------------------
# Rejection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_header_is_401(raw_client):
    r = await raw_client.get(PROTECTED)
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [
    "",
    "Token abc",
    "Basic dXNlcjpwYXNz",
    "Bearer",
    "bearer-no-space",
])
async def test_malformed_authorization_header_is_401(raw_client, header):
    r = await raw_client.get(PROTECTED, headers={"Authorization": header})
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_forged_hs256_jwt_is_401(raw_client, test_data):
    """Audit 2.1 — the bypass, driven end to end.

    `local-unused` was the committed value of SUPABASE_JWT_SECRET, published in
    `.env.example`. Before 2026-08-03 this request returned 200 as `sub`.
    """
    token = jwt.encode(
        {"sub": test_data.user_id, "email": "forged@test.dev"},
        "local-unused",
        algorithm="HS256",
    )
    r = await raw_client.get(PROTECTED, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_forged_jwt_with_arbitrary_subject_is_401(raw_client):
    """The bypass was worse than impersonating a known user — `sub` was free."""
    token = jwt.encode({"sub": str(uuid.uuid4())}, "local-unused", algorithm="HS256")
    r = await raw_client.get(PROTECTED, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_unstructured_bearer_token_is_401(raw_client):
    r = await raw_client.get(
        PROTECTED, headers={"Authorization": f"Bearer {secrets.token_urlsafe(40)}"}
    )
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_unknown_but_well_formed_pat_is_401(raw_client):
    token = f"{PAT_PREFIX}{secrets.token_urlsafe(32)}"
    r = await raw_client.get(PROTECTED, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_pat_authenticates_as_its_owner(raw_client, test_data):
    pat_id, token = await _issue_pat(test_data.user_id)
    try:
        r = await raw_client.get(
            PROTECTED, headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 200, r.text
        # /auth/me returns a bootstrap envelope: {"user": ..., "settings": ...}
        assert r.json()["user"]["id"] == test_data.user_id
    finally:
        await _delete(pat_id)


@pytest.mark.asyncio
async def test_revoked_pat_is_401(raw_client, test_data):
    pat_id, token = await _issue_pat(test_data.user_id)
    try:
        r = await raw_client.get(
            PROTECTED, headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 200, r.text

        await _revoke(pat_id)

        r = await raw_client.get(
            PROTECTED, headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 401, r.text
    finally:
        await _delete(pat_id)


@pytest.mark.asyncio
async def test_pat_is_stored_hashed_never_in_plaintext(raw_client, test_data):
    """The DB must hold only a SHA-256 of the token."""
    pat_id, token = await _issue_pat(test_data.user_id)
    try:
        async with db.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT token_hash FROM personal_access_tokens WHERE id = $1", pat_id
            )
        assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
        assert token not in row["token_hash"]
    finally:
        await _delete(pat_id)
