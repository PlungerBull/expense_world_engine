"""Datetime input contract — the engine accepts only RFC 3339 with offset.

Naive ISO 8601 strings (no `Z`, no `+HH:MM`) used to reach the
``body.date > now()`` guard in helpers/transactions.py and raise
``TypeError: can't compare offset-naive and offset-aware datetimes``,
which bubbled to the catch-all handler as 500 INTERNAL_ERROR.

The fix uses ``pydantic.AwareDatetime`` on the request schemas so naive
input is rejected at the boundary with a 422 VALIDATION_ERROR carrying
``fields.date``. These tests pin that behaviour for every documented
naive shape and confirm aware input still works.
"""
import uuid

import pytest


_NAIVE_INPUTS = [
    "2026-04-25T16:30:00",       # naive, T separator, with seconds
    "2026-04-25 16:30:00",       # naive, space separator
    "2026-04-25T16:30",          # naive, T separator, no seconds
    "2026-04-25",                # date-only
]

_AWARE_INPUTS = [
    "2020-04-25T16:30:00Z",
    "2020-04-25T16:30:00+00:00",
    "2020-04-25T11:30:00-05:00",
]


def _tx_payload(test_data, date_value: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "title": "datetime-contract",
        "amount_cents": -500,
        "date": date_value,
        "account_id": test_data.account_id,
        "category_id": test_data.category_id,
    }


def _inbox_payload(test_data, date_value: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "title": "datetime-contract",
        "amount_cents": -500,
        "date": date_value,
        "account_id": test_data.account_id,
        "category_id": test_data.category_id,
    }


# ---------------------------------------------------------------------------
# POST /v1/transactions — naive rejected, aware accepted
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("naive", _NAIVE_INPUTS)
async def test_transactions_reject_naive_datetime(client, test_data, naive):
    r = await client.post(
        "/v1/transactions",
        json=_tx_payload(test_data, naive),
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 422, r.text
    body = r.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert "date" in (body.get("fields") or {})


@pytest.mark.asyncio
@pytest.mark.parametrize("aware", _AWARE_INPUTS)
async def test_transactions_accept_aware_datetime(client, test_data, aware):
    r = await client.post(
        "/v1/transactions",
        json=_tx_payload(test_data, aware),
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# POST /v1/inbox — naive rejected, aware accepted
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("naive", _NAIVE_INPUTS)
async def test_inbox_reject_naive_datetime(client, test_data, naive):
    r = await client.post(
        "/v1/inbox",
        json=_inbox_payload(test_data, naive),
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 422, r.text
    body = r.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert "date" in (body.get("fields") or {})


@pytest.mark.asyncio
@pytest.mark.parametrize("aware", _AWARE_INPUTS)
async def test_inbox_accept_aware_datetime(client, test_data, aware):
    r = await client.post(
        "/v1/inbox",
        json=_inbox_payload(test_data, aware),
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Garbage and well-formed-but-invalid still rejected as 422 (existing behaviour)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["not-a-date", "2026-04-25T25:99:00Z"])
async def test_transactions_reject_unparseable_datetime(client, test_data, bad):
    r = await client.post(
        "/v1/transactions",
        json=_tx_payload(test_data, bad),
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 422, r.text
    assert "date" in (r.json()["error"].get("fields") or {})
