"""Integration tests for GET /v1/sync.

Run: .venv/bin/pytest tests/test_sync.py -v
"""
import uuid

import pytest
import asyncpg

from app import db

CLIENT_ID = str(uuid.uuid4())
HEADERS = {"X-Client-Id": CLIENT_ID}


# ─── Validation / Error Tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_client_id(client):
    r = await client.get("/v1/sync", params={"sync_token": "*"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "X-Client-Id" in body["error"]["fields"]


@pytest.mark.asyncio
async def test_invalid_client_id(client):
    r = await client.get(
        "/v1/sync",
        params={"sync_token": "*"},
        headers={"X-Client-Id": "not-a-uuid"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_missing_sync_token(client):
    r = await client.get("/v1/sync", headers=HEADERS)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_invalid_sync_token(client):
    r = await client.get(
        "/v1/sync",
        params={"sync_token": str(uuid.uuid4())},
        headers=HEADERS,
    )
    assert r.status_code == 422
    assert "sync_token" in r.json()["error"]["fields"]


@pytest.mark.asyncio
async def test_non_uuid_sync_token(client):
    r = await client.get(
        "/v1/sync",
        params={"sync_token": "garbage"},
        headers=HEADERS,
    )
    assert r.status_code == 422


# ─── Wildcard Sync (Full Fetch) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wildcard_returns_all_keys(client):
    r = await client.get("/v1/sync", params={"sync_token": "*"}, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()

    expected_keys = {
        "sync_token", "accounts", "categories", "hashtags",
        "inbox", "transactions", "reconciliations", "settings",
    }
    assert set(body.keys()) == expected_keys


@pytest.mark.asyncio
async def test_wildcard_returns_test_data(client, test_data):
    r = await client.get("/v1/sync", params={"sync_token": "*"}, headers=HEADERS)
    body = r.json()

    assert len(body["accounts"]) >= 1
    assert any(a["id"] == test_data.account_id for a in body["accounts"])

    assert len(body["categories"]) >= 1
    assert any(c["id"] == test_data.category_id for c in body["categories"])

    assert len(body["hashtags"]) >= 2
    assert any(h["id"] == test_data.hashtag_id for h in body["hashtags"])

    assert len(body["transactions"]) >= 1
    assert any(t["id"] == test_data.transaction_id for t in body["transactions"])

    assert len(body["inbox"]) >= 1
    assert any(i["id"] == test_data.inbox_id for i in body["inbox"])

    assert body["settings"] is not None
    assert body["settings"]["user_id"] == test_data.user_id


@pytest.mark.asyncio
async def test_wildcard_no_tombstones(client, test_data):
    r = await client.get("/v1/sync", params={"sync_token": "*"}, headers=HEADERS)
    body = r.json()

    for key in ["accounts", "categories", "hashtags", "inbox", "transactions", "reconciliations"]:
        for row in body[key]:
            assert row["deleted_at"] is None, f"Tombstone found in wildcard {key}: {row['id']}"


@pytest.mark.asyncio
async def test_wildcard_returns_opaque_uuid_token(client):
    r = await client.get("/v1/sync", params={"sync_token": "*"}, headers=HEADERS)
    token = r.json()["sync_token"]
    uuid.UUID(token)  # Raises if not a valid UUID


# ─── Transactions Embed hashtag_ids ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_transactions_have_hashtag_ids(client, test_data):
    r = await client.get("/v1/sync", params={"sync_token": "*"}, headers=HEADERS)
    body = r.json()

    tx = next(t for t in body["transactions"] if t["id"] == test_data.transaction_id)
    assert "hashtag_ids" in tx
    assert isinstance(tx["hashtag_ids"], list)
    assert test_data.hashtag_id in tx["hashtag_ids"]


@pytest.mark.asyncio
async def test_transaction_without_hashtags_has_empty_array(client, test_data):
    bare_tx_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO expense_transactions
                (id, user_id, title, amount_cents, amount_home_cents, transaction_type,
                 date, account_id, category_id, exchange_rate, cleared,
                 created_at, updated_at)
               VALUES ($1, $2, 'No Hashtags', 1000, 1000, 1,
                 now(), $3, $4, 1.0, false, now(), now())""",
            bare_tx_id, test_data.user_id, test_data.account_id, test_data.category_id,
        )

    try:
        r = await client.get("/v1/sync", params={"sync_token": "*"}, headers=HEADERS)
        tx = next(t for t in r.json()["transactions"] if t["id"] == bare_tx_id)
        assert tx["hashtag_ids"] == []
    finally:
        async with db.pool.acquire() as conn:
            await conn.execute("DELETE FROM expense_transactions WHERE id = $1", bare_tx_id)


# ─── Empty Delta ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_delta(client):
    headers = {"X-Client-Id": str(uuid.uuid4())}

    r1 = await client.get("/v1/sync", params={"sync_token": "*"}, headers=headers)
    token = r1.json()["sync_token"]

    r2 = await client.get("/v1/sync", params={"sync_token": token}, headers=headers)
    body = r2.json()

    for key in ["accounts", "categories", "hashtags", "inbox", "transactions", "reconciliations"]:
        assert body[key] == [], f"Expected empty {key}, got {len(body[key])} rows"
    assert body["settings"] is None


# ─── Mutation Delta ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mutation_delta(client, test_data):
    headers = {"X-Client-Id": str(uuid.uuid4())}

    r1 = await client.get("/v1/sync", params={"sync_token": "*"}, headers=headers)
    token = r1.json()["sync_token"]

    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE expense_transactions SET title='Mutated', updated_at=now(), version=version+1 WHERE id=$1",
            test_data.transaction_id,
        )

    r2 = await client.get("/v1/sync", params={"sync_token": token}, headers=headers)
    body = r2.json()

    assert len(body["transactions"]) == 1
    assert body["transactions"][0]["id"] == test_data.transaction_id
    assert body["transactions"][0]["title"] == "Mutated"

    for key in ["accounts", "categories", "hashtags", "inbox", "reconciliations"]:
        assert body[key] == []

    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE expense_transactions SET title='Test Tx', updated_at=now(), version=version+1 WHERE id=$1",
            test_data.transaction_id,
        )


# ─── Tombstone Delta ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tombstone_delta(client, test_data):
    headers = {"X-Client-Id": str(uuid.uuid4())}

    r1 = await client.get("/v1/sync", params={"sync_token": "*"}, headers=headers)
    token = r1.json()["sync_token"]

    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE expense_transactions SET deleted_at=now(), updated_at=now(), version=version+1 WHERE id=$1",
            test_data.transaction_id,
        )

    r2 = await client.get("/v1/sync", params={"sync_token": token}, headers=headers)
    body = r2.json()

    assert len(body["transactions"]) == 1
    assert body["transactions"][0]["id"] == test_data.transaction_id
    assert body["transactions"][0]["deleted_at"] is not None

    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE expense_transactions SET deleted_at=NULL, updated_at=now(), version=version+1 WHERE id=$1",
            test_data.transaction_id,
        )


# ─── Settings Delta ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_settings_delta(client, test_data):
    headers = {"X-Client-Id": str(uuid.uuid4())}

    r1 = await client.get("/v1/sync", params={"sync_token": "*"}, headers=headers)
    token = r1.json()["sync_token"]

    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_settings SET sidebar_show_people=false, version=version+1, updated_at=now() WHERE user_id=$1",
            test_data.user_id,
        )

    r2 = await client.get("/v1/sync", params={"sync_token": token}, headers=headers)
    body = r2.json()
    assert body["settings"] is not None
    assert body["settings"]["sidebar_show_people"] is False

    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_settings SET sidebar_show_people=true, version=version+1, updated_at=now() WHERE user_id=$1",
            test_data.user_id,
        )


# ─── Cross-Client Isolation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_client_isolation(client, test_data):
    client_a = {"X-Client-Id": str(uuid.uuid4())}
    client_b = {"X-Client-Id": str(uuid.uuid4())}

    # Client A syncs
    r_a = await client.get("/v1/sync", params={"sync_token": "*"}, headers=client_a)
    token_a = r_a.json()["sync_token"]

    # Client B syncs (should not invalidate A's token)
    await client.get("/v1/sync", params={"sync_token": "*"}, headers=client_b)

    # Client A's token still works
    r_a2 = await client.get("/v1/sync", params={"sync_token": token_a}, headers=client_a)
    assert r_a2.status_code == 200


# ─── Token Rotation ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_old_token_invalid_after_rotation(client, test_data):
    headers = {"X-Client-Id": str(uuid.uuid4())}

    r1 = await client.get("/v1/sync", params={"sync_token": "*"}, headers=headers)
    token1 = r1.json()["sync_token"]

    r2 = await client.get("/v1/sync", params={"sync_token": token1}, headers=headers)
    assert r2.status_code == 200

    # token1 has been rotated out — should now be invalid
    r3 = await client.get("/v1/sync", params={"sync_token": token1}, headers=headers)
    assert r3.status_code == 422
    assert "sync_token" in r3.json()["error"]["fields"]


# ─── Response Shape Invariants ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_account_has_null_home_balance(client, test_data):
    r = await client.get("/v1/sync", params={"sync_token": "*"}, headers=HEADERS)
    acct = next(a for a in r.json()["accounts"] if a["id"] == test_data.account_id)
    assert acct["current_balance_home_cents"] is None


@pytest.mark.asyncio
async def test_settings_has_version(client, test_data):
    r = await client.get("/v1/sync", params={"sync_token": "*"}, headers=HEADERS)
    s = r.json()["settings"]
    assert "version" in s
    assert isinstance(s["version"], int)
    assert s["version"] >= 1


@pytest.mark.asyncio
async def test_all_rows_have_version_and_updated_at(client, test_data):
    r = await client.get("/v1/sync", params={"sync_token": "*"}, headers=HEADERS)
    body = r.json()

    for key in ["accounts", "categories", "hashtags", "inbox", "transactions", "reconciliations"]:
        for row in body[key]:
            assert "version" in row, f"{key} row missing version"
            assert "updated_at" in row, f"{key} row missing updated_at"
            assert "deleted_at" in row, f"{key} row missing deleted_at"
