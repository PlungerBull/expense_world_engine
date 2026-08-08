"""Account names follow the categories/hashtags rules (sql/028).

Bloat-audit 2026-08-06 Correctness §6, owner decision 2026-08-08: account
names are trimmed (blank → 422), unique case-insensitively within
(user, currency), a soft-deleted account releases its (name, currency),
and restore blocks with 409 if an active account retook the name. Before
sql/028 the table-level UNIQUE was case-sensitive and spanned soft-deleted
rows — which also made creating over a soft-deleted account 409 with the
wrong message ("An account with id … already exists"); the last test here
is that regression.
"""
import uuid

import pytest

from app import db


def _idem() -> dict:
    return {"X-Idempotency-Key": str(uuid.uuid4())}


def _body(name: str, currency: str = "PEN") -> dict:
    return {"id": str(uuid.uuid4()), "name": name, "currency_code": currency}


async def _cleanup(user_id: str, *account_ids: str) -> None:
    async with db.pool.acquire() as conn:
        for account_id in account_ids:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
                account_id, user_id,
            )
            await conn.execute(
                "DELETE FROM expense_bank_accounts WHERE id = $1 AND user_id = $2",
                account_id, user_id,
            )


@pytest.mark.asyncio
async def test_create_trims_and_rejects_blank_names(client, test_data):
    padded = _body(f"  Trim Me {uuid.uuid4()}  ")
    try:
        r = await client.post("/v1/accounts", json=padded, headers=_idem())
        assert r.status_code == 201, r.text
        assert r.json()["name"] == padded["name"].strip()

        r = await client.post("/v1/accounts", json=_body("   "), headers=_idem())
        assert r.status_code == 422, r.text
        assert r.json()["error"]["fields"]["name"] == "Must not be empty."
    finally:
        await _cleanup(test_data.user_id, padded["id"])


@pytest.mark.asyncio
async def test_create_collision_is_case_insensitive_per_currency(client, test_data):
    name = f"Case-Rules-{uuid.uuid4()}"
    original = _body(name)
    shouty = _body(name.upper())
    other_currency = _body(name, currency="USD")
    try:
        r = await client.post("/v1/accounts", json=original, headers=_idem())
        assert r.status_code == 201, r.text

        # Same currency, different case → 409 naming the name, not the id.
        r = await client.post("/v1/accounts", json=shouty, headers=_idem())
        assert r.status_code == 409, r.text
        assert name.upper() in r.json()["error"]["message"]

        # Same name, different currency → allowed (scope includes currency).
        r = await client.post("/v1/accounts", json=other_currency, headers=_idem())
        assert r.status_code == 201, r.text
    finally:
        await _cleanup(
            test_data.user_id, original["id"], shouty["id"], other_currency["id"]
        )


@pytest.mark.asyncio
async def test_rename_collision_and_trim(client, test_data):
    first = _body(f"Rename-A-{uuid.uuid4()}")
    second = _body(f"Rename-B-{uuid.uuid4()}")
    try:
        for body in (first, second):
            r = await client.post("/v1/accounts", json=body, headers=_idem())
            assert r.status_code == 201, r.text

        # Rename onto the other's name, case-flipped and padded → 409.
        r = await client.put(
            f"/v1/accounts/{second['id']}",
            json={"name": f"  {first['name'].upper()}  "},
            headers=_idem(),
        )
        assert r.status_code == 409, r.text

        # A legitimate rename comes back trimmed.
        r = await client.put(
            f"/v1/accounts/{second['id']}",
            json={"name": f"  Renamed-{uuid.uuid4()}  "},
            headers=_idem(),
        )
        assert r.status_code == 200, r.text
        assert r.json()["name"] == r.json()["name"].strip()
    finally:
        await _cleanup(test_data.user_id, first["id"], second["id"])


@pytest.mark.asyncio
async def test_soft_delete_releases_the_name(client, test_data):
    """create → delete → create same (name, currency) succeeds — before
    sql/028 this 409ed against the old constraint with the wrong message."""
    name = f"Release-{uuid.uuid4()}"
    original = _body(name)
    successor = _body(name)
    try:
        r = await client.post("/v1/accounts", json=original, headers=_idem())
        assert r.status_code == 201, r.text
        r = await client.delete(
            f"/v1/accounts/{original['id']}", headers=_idem()
        )
        assert r.status_code == 200, r.text

        r = await client.post("/v1/accounts", json=successor, headers=_idem())
        assert r.status_code == 201, r.text

        # …and restoring the original now collides, naming the name.
        r = await client.post(
            f"/v1/accounts/{original['id']}/restore", headers=_idem()
        )
        assert r.status_code == 409, r.text
        message = r.json()["error"]["message"]
        assert name in message and "already exists" in message
    finally:
        await _cleanup(test_data.user_id, original["id"], successor["id"])
