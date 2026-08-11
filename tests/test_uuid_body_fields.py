"""Malformed UUID *body* fields return 422, never 500 (open-bugs 6.6).

The path/query half landed with bug 6.2 (tests/test_uuid_params.py); this is
the same hazard one layer deeper. FK fields in request bodies are typed
``uuid.UUID`` like the ``id`` PKs beside them, so garbage 422s at the schema
boundary with the standard envelope instead of reaching SQL as a bind param
and 500ing. Field keys follow the handler's dotted-loc convention
(``transactions.0.category_id``).
"""
import pytest

# Fixed literal, not uuid4(): parametrize ids must collect identically on
# every xdist worker. The value never has to exist — validation 422s first.
VALID = "b0a7f00d-0000-4000-8000-000000000001"
DATE = "2026-01-15T12:00:00Z"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, url, body, field",
    [
        (
            "POST", "/v1/transactions",
            {"id": VALID, "title": "t", "amount_cents": -100, "date": DATE,
             "account_id": "not-a-uuid", "category_id": VALID},
            "account_id",
        ),
        (
            "PUT", f"/v1/transactions/{VALID}",
            {"reconciliation_id": "not-a-uuid"},
            "reconciliation_id",
        ),
        (
            "PUT", f"/v1/transactions/{VALID}",
            {"hashtag_ids": ["not-a-uuid"]},
            "hashtag_ids.0",
        ),
        (
            "POST", "/v1/transactions/batch",
            {"transactions": [
                {"id": VALID, "title": "t", "amount_cents": -100, "date": DATE,
                 "account_id": VALID, "category_id": "not-a-uuid"},
            ]},
            "transactions.0.category_id",
        ),
        (
            "POST", "/v1/inbox",
            {"id": VALID, "account_id": "not-a-uuid"},
            "account_id",
        ),
        (
            "PUT", f"/v1/inbox/{VALID}",
            {"category_id": "not-a-uuid"},
            "category_id",
        ),
        (
            "POST", "/v1/reconciliations",
            {"id": VALID, "account_id": "not-a-uuid", "name": "January",
             "beginning_balance_cents": 0},
            "account_id",
        ),
    ],
)
async def test_malformed_uuid_body_field_returns_422(client, method, url, body, field):
    r = await client.request(method, url, json=body)
    assert r.status_code == 422, (url, r.text)
    payload = r.json()["error"]
    assert payload["code"] == "VALIDATION_ERROR"
    assert field in (payload.get("fields") or {}), (url, payload)
