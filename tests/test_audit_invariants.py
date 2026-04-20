"""Regression tests for the cross-cutting invariants from Sprints 1-4.

Pins down behaviors that span every endpoint and have no natural home
in a resource-specific test file:

  * Client-supplied UUID 409 — a second POST with the same id returns
    CONFLICT, not a silent duplicate.
  * VALIDATION_ERROR.fields is always a dict (never null) so clients
    can uniformly iterate Object.keys.
  * SETTINGS_MISSING returns 422 with the dedicated factory shape (not
    the old ad-hoc 409).
  * Global exception handlers reshape Starlette's defaults into the
    canonical {error: {code, message, fields}} envelope for unrouted
    paths and method-not-allowed responses.
  * extract_update_fields rejects explicit null on non-nullable fields
    so callers can't bypass spec rules by sending {"name": null}.

Run: .venv/bin/pytest tests/test_audit_invariants.py -v
"""
import uuid

import pytest

from app import db


async def _cleanup_account(account_id: str, user_id: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
            account_id, user_id,
        )
        await conn.execute(
            "DELETE FROM expense_bank_accounts WHERE id = $1 AND user_id = $2",
            account_id, user_id,
        )


# ---------------------------------------------------------------------------
# Client-supplied UUID — duplicate id returns 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_with_duplicate_id_returns_409(client, test_data):
    """Two POSTs with the same client-supplied id (and DIFFERENT
    idempotency keys, so this is NOT an idempotency replay) must return
    409 CONFLICT on the second call. The id-uniqueness guarantee is
    what makes idempotent retries by id safe in offline-first clients.
    """
    account_id = str(uuid.uuid4())
    payload = {
        "id": account_id,
        "name": f"uuid-dup-{uuid.uuid4()}",
        "currency_code": "PEN",
    }

    first = await client.post(
        "/v1/accounts",
        json=payload,
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert first.status_code == 201, first.text

    try:
        # Different idempotency key + different name — only the id collides.
        second = await client.post(
            "/v1/accounts",
            json={**payload, "name": f"uuid-dup-2-{uuid.uuid4()}"},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert second.status_code == 409, second.text
        error = second.json()["error"]
        assert error["code"] == "CONFLICT"
        assert account_id in error["message"], (
            f"409 message should reference the conflicting id; got {error['message']!r}"
        )

    finally:
        await _cleanup_account(account_id, test_data.user_id)


# ---------------------------------------------------------------------------
# VALIDATION_ERROR.fields is always a dict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pydantic_validation_error_fields_is_always_dict(client):
    """A POST with missing required fields triggers Pydantic validation
    via FastAPI; the global handler must reshape into our canonical
    envelope with `fields` as a dict (possibly empty), never null.
    """
    r = await client.post(
        "/v1/accounts",
        json={},  # all required fields missing
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert isinstance(error["fields"], dict), (
        f"fields must be a dict on VALIDATION_ERROR, got {type(error['fields']).__name__}"
    )
    # Pydantic should report at least one of the missing required fields.
    assert any(k in error["fields"] for k in ("id", "name", "currency_code")), (
        f"Expected required fields in error.fields, got {error['fields']}"
    )


@pytest.mark.asyncio
async def test_app_error_validation_factory_emits_object_fields(client, test_data):
    """An AppError raised via the validation_error() factory with no
    explicit fields argument should still emit `fields: {}` on the wire,
    not `fields: null`. Triggered here via the report endpoint with no
    query params.
    """
    r = await client.get("/v1/reports/monthly")
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert isinstance(error["fields"], dict), (
        f"validation_error factory must emit dict, got {type(error['fields']).__name__}"
    )


# ---------------------------------------------------------------------------
# SETTINGS_MISSING — 422 with dedicated code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_missing_returns_422_settings_missing(client, test_data):
    """If user_settings is missing, /dashboard returns 422 with the
    dedicated SETTINGS_MISSING code (not the old ad-hoc 409 CONFLICT).
    The fields dict points the client at the bootstrap remediation.
    """
    # Snapshot + delete the row, then restore in finally.
    async with db.pool.acquire() as conn:
        original = await conn.fetchrow(
            "SELECT * FROM user_settings WHERE user_id = $1",
            test_data.user_id,
        )
        assert original is not None, "test_data should have user_settings"
        await conn.execute(
            "DELETE FROM user_settings WHERE user_id = $1",
            test_data.user_id,
        )

    try:
        r = await client.get("/v1/dashboard")
        assert r.status_code == 422, r.text
        error = r.json()["error"]
        assert error["code"] == "SETTINGS_MISSING"
        assert isinstance(error["fields"], dict)
        assert "user_settings" in error["fields"], (
            f"SETTINGS_MISSING fields should mention user_settings; got {error['fields']}"
        )
        # Message should redirect the client to bootstrap.
        assert "bootstrap" in error["message"].lower()

    finally:
        async with db.pool.acquire() as conn:
            cols = list(original.keys())
            placeholders = ", ".join(f"${i+1}" for i in range(len(cols)))
            await conn.execute(
                f"INSERT INTO user_settings ({', '.join(cols)}) VALUES ({placeholders})",
                *[original[c] for c in cols],
            )


# ---------------------------------------------------------------------------
# Global exception handlers — Starlette defaults reshaped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unrouted_path_returns_canonical_404_envelope(client):
    """Hitting a path with no registered route returns the canonical
    {error: {code, message, fields}} envelope, NOT Starlette's default
    {"detail": "Not Found"}. The StarletteHTTPException handler
    registered in main.py is what makes this happen.
    """
    r = await client.get(f"/v1/no-such-route-{uuid.uuid4()}")
    assert r.status_code == 404
    body = r.json()
    assert "error" in body, (
        f"Expected canonical envelope with 'error' key, got top-level keys {list(body.keys())}"
    )
    assert "detail" not in body, (
        f"Starlette's default 'detail' shape must not leak; got {body}"
    )
    error = body["error"]
    assert error["code"] == "NOT_FOUND"
    assert error["fields"] is None
    assert isinstance(error["message"], str)


@pytest.mark.asyncio
async def test_wrong_method_returns_canonical_405_envelope(client):
    """Method-not-allowed responses also flow through the Starlette
    handler and emit the canonical envelope.
    """
    # /v1/dashboard is GET-only; DELETE has no handler.
    r = await client.delete("/v1/dashboard")
    assert r.status_code == 405
    body = r.json()
    assert "error" in body
    assert body["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert body["error"]["fields"] is None


# ---------------------------------------------------------------------------
# extract_update_fields — null on non-nullable field returns 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_with_explicit_null_on_non_nullable_field_returns_422(
    client, test_data,
):
    """extract_update_fields distinguishes "field omitted" (allowed,
    no-op) from "field explicitly null" (rejected unless the field
    opts in via the `nullable` allowlist).

    Sending {"name": null} on a PUT must return 422 with the field-level
    error — clients can't bypass the spec rule that name is a required
    string by sending null.
    """
    account_id = str(uuid.uuid4())
    create_r = await client.post(
        "/v1/accounts",
        json={
            "id": account_id,
            "name": f"null-rej-{uuid.uuid4()}",
            "currency_code": "PEN",
        },
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert create_r.status_code == 201, create_r.text

    try:
        r = await client.put(
            f"/v1/accounts/{account_id}",
            json={"name": None},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 422, r.text
        error = r.json()["error"]
        assert error["code"] == "VALIDATION_ERROR"
        assert "name" in error["fields"], (
            f"Expected 'name' in fields, got {error['fields']}"
        )

        # Compare: the same PUT with {} (field omitted) is a clean no-op.
        ok_r = await client.put(
            f"/v1/accounts/{account_id}",
            json={},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert ok_r.status_code == 200, ok_r.text

    finally:
        await _cleanup_account(account_id, test_data.user_id)
