"""The transfer feature is removed (owner decision 2026-08-10) — fail-closed pins.

The auto-paired transfer — one request carrying a ``transfer`` object, the
engine registering the opposite leg — is gone. These tests pin the removal
the fail-closed way: every retired field 422s as unknown input via
``StrictModel`` (never silently dropped), responses carry no transfer keys,
and ``category_id`` is unconditionally required now that the transfer path's
waiver is gone. A move between accounts is two ordinary rows; see
engine-spec's "Moves between accounts" convention.
"""

import uuid

import pytest

PAST_DATE = "2026-04-12T12:00:00Z"


def _idem() -> dict:
    return {"X-Idempotency-Key": str(uuid.uuid4())}


def _txn_body(test_data, **overrides) -> dict:
    body = {
        "id": str(uuid.uuid4()),
        "title": f"no-transfer-{uuid.uuid4().hex[:8]}",
        "amount_cents": -1500,
        "date": PAST_DATE,
        "account_id": test_data.account_id,
        "category_id": test_data.category_id,
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# The `transfer` field 422s as unknown input everywhere it used to exist
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transfer_field_rejected_on_transaction_create(client, test_data):
    body = _txn_body(
        test_data,
        transfer={
            "id": str(uuid.uuid4()),
            "account_id": test_data.account_id,
            "amount_cents": 1500,
        },
    )
    r = await client.post("/v1/transactions", json=body, headers=_idem())
    assert r.status_code == 422, r.text
    assert "transfer" in r.json()["error"]["fields"]


@pytest.mark.asyncio
async def test_transfer_field_rejected_on_batch_item(client, test_data):
    body = {
        "transactions": [
            _txn_body(test_data, transfer={"account_id": test_data.account_id}),
        ]
    }
    r = await client.post("/v1/transactions/batch", json=body, headers=_idem())
    assert r.status_code == 422, r.text
    fields = r.json()["error"]["fields"]
    assert any("transfer" in key for key in fields), fields


@pytest.mark.asyncio
async def test_transfer_field_rejected_on_inbox_create_and_update(client, test_data):
    r = await client.post(
        "/v1/inbox",
        json={
            "id": str(uuid.uuid4()),
            "title": "draft",
            "transfer": {"account_id": test_data.account_id, "amount_cents": 100},
        },
        headers=_idem(),
    )
    assert r.status_code == 422, r.text
    assert "transfer" in r.json()["error"]["fields"]

    draft = await client.post(
        "/v1/inbox",
        json={"id": str(uuid.uuid4()), "title": "plain draft"},
        headers=_idem(),
    )
    assert draft.status_code == 201, draft.text
    inbox_id = draft.json()["id"]

    # Both a value and the former explicit-null clearing gesture are unknown now.
    for payload in ({"transfer": {"account_id": test_data.account_id}}, {"transfer": None}):
        r = await client.put(f"/v1/inbox/{inbox_id}", json=payload, headers=_idem())
        assert r.status_code == 422, r.text
        assert "transfer" in r.json()["error"]["fields"]

    await client.delete(f"/v1/inbox/{inbox_id}", headers=_idem())


@pytest.mark.asyncio
async def test_transfer_id_rejected_on_promote(client, test_data):
    draft = await client.post(
        "/v1/inbox",
        json={
            "id": str(uuid.uuid4()),
            "title": f"promote-{uuid.uuid4().hex[:8]}",
            "amount_cents": -900,
            "date": PAST_DATE,
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
        },
        headers=_idem(),
    )
    assert draft.status_code == 201, draft.text
    inbox_id = draft.json()["id"]

    r = await client.post(
        f"/v1/inbox/{inbox_id}/promote",
        json={"id": str(uuid.uuid4()), "transfer_id": str(uuid.uuid4())},
        headers=_idem(),
    )
    assert r.status_code == 422, r.text
    assert "transfer_id" in r.json()["error"]["fields"]

    await client.delete(f"/v1/inbox/{inbox_id}", headers=_idem())


# ---------------------------------------------------------------------------
# category_id is unconditionally required — schema boundary, plain missing field
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_category_id_422s_on_create(client, test_data):
    body = _txn_body(test_data)
    del body["category_id"]
    r = await client.post("/v1/transactions", json=body, headers=_idem())
    assert r.status_code == 422, r.text
    assert "category_id" in r.json()["error"]["fields"]


@pytest.mark.asyncio
async def test_missing_category_id_422s_on_batch_item(client, test_data):
    item = _txn_body(test_data)
    del item["category_id"]
    r = await client.post(
        "/v1/transactions/batch", json={"transactions": [item]}, headers=_idem()
    )
    assert r.status_code == 422, r.text
    fields = r.json()["error"]["fields"]
    assert any("category_id" in key for key in fields), fields


# ---------------------------------------------------------------------------
# Responses carry no transfer keys — deleted, not nulled (like exchange_rate)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transaction_response_has_no_transfer_key(client, test_data):
    r = await client.post(
        "/v1/transactions", json=_txn_body(test_data), headers=_idem()
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert "transfer_transaction_id" not in body

    detail = await client.get(f"/v1/transactions/{body['id']}")
    assert detail.status_code == 200
    assert "transfer_transaction_id" not in detail.json()

    await client.delete(f"/v1/transactions/{body['id']}", headers=_idem())


@pytest.mark.asyncio
async def test_inbox_response_has_no_transfer_keys(client, test_data):
    r = await client.post(
        "/v1/inbox",
        json={
            "id": str(uuid.uuid4()),
            "title": "shape check",
            "amount_cents": -700,
            "account_id": test_data.account_id,
        },
        headers=_idem(),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert "transfer_account_id" not in body
    assert "transfer_amount_cents" not in body

    await client.delete(f"/v1/inbox/{body['id']}", headers=_idem())
