"""Every request model fails closed: unknown fields 422, never dropped.

Bloat audit 2026-08-06, Correctness §2: `extra="forbid"` was copy-pasted onto
11 request models and missing from 10. All request models now inherit
schemas.StrictModel; these tests pin the previously-leaky shapes.

The nested-fragment pins left with the transfer removal (2026-08-10): the only
nested request fragments were TransferField / InboxTransferField, where
Pydantic's non-propagating model_config silently re-opened the hole inside
`transfer: {...}` even though the parent forbade extras. No nested fragment
exists to pin today — the next one must bring its own pin.

Pattern per test_wp5_schema_slimming.py: 422, VALIDATION_ERROR, junk key named
in `fields` (nested keys as dotted paths, per errors.py's loc join).
"""
import uuid

import pytest


def _idem():
    return {"X-Idempotency-Key": str(uuid.uuid4())}


def _assert_rejects(r, field: str):
    assert r.status_code == 422, r.text
    body = r.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert field in (body.get("fields") or {}), body


@pytest.mark.asyncio
async def test_account_update_rejects_unknown_field(client, test_data):
    r = await client.put(
        f"/v1/accounts/{test_data.account_id}", json={"bogus": 1}, headers=_idem()
    )
    _assert_rejects(r, "bogus")


@pytest.mark.asyncio
async def test_category_create_and_update_reject_unknown_field(client, test_data):
    r = await client.post(
        "/v1/categories",
        json={"id": str(uuid.uuid4()), "name": "strict-cat", "color": "#123456", "bogus": 1},
        headers=_idem(),
    )
    _assert_rejects(r, "bogus")
    r = await client.put(
        f"/v1/categories/{test_data.category_id}", json={"bogus": 1}, headers=_idem()
    )
    _assert_rejects(r, "bogus")


@pytest.mark.asyncio
async def test_hashtag_create_and_update_reject_unknown_field(client, test_data):
    r = await client.post(
        "/v1/hashtags",
        json={"id": str(uuid.uuid4()), "name": "#strict", "bogus": 1},
        headers=_idem(),
    )
    _assert_rejects(r, "bogus")
    r = await client.put(
        f"/v1/hashtags/{test_data.hashtag_id}", json={"bogus": 1}, headers=_idem()
    )
    _assert_rejects(r, "bogus")


@pytest.mark.asyncio
async def test_pat_create_rejects_unknown_field(client):
    r = await client.post(
        "/v1/auth/pat", json={"name": "cli", "bogus": 1}, headers=_idem()
    )
    _assert_rejects(r, "bogus")


@pytest.mark.asyncio
async def test_inbox_promote_rejects_unknown_field(client, test_data):
    r = await client.post(
        f"/v1/inbox/{test_data.inbox_id}/promote",
        json={"id": str(uuid.uuid4()), "bogus": 1},
        headers=_idem(),
    )
    _assert_rejects(r, "bogus")


@pytest.mark.asyncio
async def test_batch_rejects_unknown_fields_top_level_and_per_item(client, test_data):
    r = await client.post(
        "/v1/transactions/batch",
        json={"transactions": [], "bogus": 1},
        headers=_idem(),
    )
    _assert_rejects(r, "bogus")
    r = await client.post(
        "/v1/transactions/batch",
        json={
            "transactions": [
                {
                    "id": str(uuid.uuid4()),
                    "title": "strict",
                    "amount_cents": -100,
                    "date": "2026-08-01T12:00:00Z",
                    "account_id": test_data.account_id,
                    "category_id": test_data.category_id,
                    "bogus": 1,
                }
            ]
        },
        headers=_idem(),
    )
    _assert_rejects(r, "transactions.0.bogus")
