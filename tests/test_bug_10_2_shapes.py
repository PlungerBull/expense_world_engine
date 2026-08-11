"""Bug 10.2 — error/shape consistency fixes (2026-08-07).

Pins five behaviors:

  * `/reports/monthly` validation errors never carry null-valued keys in
    `fields` — a key is present iff it has a message.
  * Category responses expose `system_key` (null for user categories) — the
    identity the rename-safety guarantee keys off.
  * `GET /exchange-rates` treats an unsupported currency as a 422 field error
    (same as the write paths), reserving 404 for a supported pair with no rate
    row on/before the date.
  * The transfer id-collision check (`transfer.id == id`) is accumulated with
    the other field errors — one 422, all failing fields — on both
    `POST /transactions` and the promote path.
  * Promoting a transfer draft links BOTH ledger legs back to the inbox row
    via `inbox_id` (amended 2026-08-07; previously primary only).
"""

import uuid

import pytest

from app import db

PAST_DATE = "2026-04-12T12:00:00Z"


async def _make_account(user_id: str, name: str) -> str:
    account_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, is_person, color,
                 is_archived, sort_order, created_at, updated_at)
            VALUES ($1, $2, $3, 'PEN', false, '#00FF00', false, 2, now(), now())
            """,
            account_id, user_id, f"{name}-{uuid.uuid4().hex[:8]}",
        )
    return account_id


async def _cleanup_account(account_id: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE expense_bank_accounts SET deleted_at = now() WHERE id = $1",
            account_id,
        )


def _idem() -> dict:
    return {"X-Idempotency-Key": str(uuid.uuid4())}


# ---------------------------------------------------------------------------
# /reports/monthly — fields never carries null values
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_report_single_month_incomplete_no_null_fields(client):
    r = await client.get("/v1/reports/monthly", params={"year": 2026})
    assert r.status_code == 422, r.text
    fields = r.json()["error"]["fields"]
    assert fields == {"month": "required"}


@pytest.mark.asyncio
async def test_report_mutually_exclusive_no_null_fields(client):
    r = await client.get(
        "/v1/reports/monthly", params={"year": 2026, "from_year": 2026}
    )
    assert r.status_code == 422, r.text
    fields = r.json()["error"]["fields"]
    assert fields == {"year": "mutually exclusive with range form"}


@pytest.mark.asyncio
async def test_report_range_incomplete_no_null_fields(client):
    r = await client.get("/v1/reports/monthly", params={"from_year": 2026})
    assert r.status_code == 422, r.text
    fields = r.json()["error"]["fields"]
    assert fields == {
        "from_month": "required",
        "to_year": "required",
        "to_month": "required",
    }
    assert None not in fields.values()


# ---------------------------------------------------------------------------
# Category responses expose system_key
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_user_category_has_null_system_key(client):
    r = await client.post(
        "/v1/categories",
        json={"id": str(uuid.uuid4()), "name": f"cat-{uuid.uuid4().hex[:8]}",
              "color": "#123456"},
        headers=_idem(),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert "system_key" in body
    assert body["system_key"] is None

    detail = await client.get(f"/v1/categories/{body['id']}")
    assert detail.status_code == 200
    assert detail.json()["system_key"] is None

    # cleanup
    await client.delete(f"/v1/categories/{body['id']}", headers=_idem())


@pytest.mark.asyncio
async def test_system_category_exposes_its_key(client, test_data):
    """Creating a transfer auto-creates @Transfer; its key must be on the wire."""
    sibling = await _make_account(test_data.user_id, "syskey-sibling")
    try:
        r = await client.post(
            "/v1/transactions",
            json={
                "id": str(uuid.uuid4()),
                "title": f"syskey-{uuid.uuid4().hex[:8]}",
                "amount_cents": -1200,
                "date": PAST_DATE,
                "account_id": test_data.account_id,
                "transfer": {
                    "id": str(uuid.uuid4()),
                    "account_id": sibling,
                    "amount_cents": 1200,
                },
            },
            headers=_idem(),
        )
        assert r.status_code == 201, r.text

        cats = await client.get("/v1/categories", params={"limit": 200})
        keys = {c["system_key"] for c in cats.json()["items"] if c["is_system"]}
        assert "transfer" in keys
    finally:
        await _cleanup_account(sibling)


# ---------------------------------------------------------------------------
# GET /exchange-rates — 422 for unsupported currency, 404 only for no data
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exchange_rate_unsupported_pair_is_422_both_fields(client):
    r = await client.get(
        "/v1/exchange-rates", params={"base": "EUR", "target": "JPY"}
    )
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert set(error["fields"].keys()) == {"base", "target"}


@pytest.mark.asyncio
async def test_exchange_rate_unsupported_target_only_is_422(client):
    r = await client.get("/v1/exchange-rates", params={"target": "XXX"})
    assert r.status_code == 422, r.text
    assert set(r.json()["error"]["fields"].keys()) == {"target"}


@pytest.mark.asyncio
async def test_exchange_rate_supported_pair_without_data_is_404(client):
    # Seeded rate sits at CURRENT_DATE; nothing exists on/before 1990.
    r = await client.get(
        "/v1/exchange-rates", params={"target": "PEN", "date": "1990-01-01"}
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Multi-field accumulation — one 422 carries every failing field
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_accumulates_all_failing_fields(client, test_data):
    """POST /transactions reports every bad field in one response.

    Whitespace title, zero amount and a future date are three independent
    failures of the create accumulator (`helpers/transactions.py`); a valid
    account and category keep the reference checks out of the way. Pinned
    against the accumulator degrading into first-failure-wins.
    """
    r = await client.post(
        "/v1/transactions",
        json={
            "id": str(uuid.uuid4()),
            "title": "   ",
            "amount_cents": 0,
            "date": "2030-01-01T00:00:00Z",
            "account_id": test_data.account_id,
            "category_id": test_data.category_id,
        },
        headers=_idem(),
    )
    assert r.status_code == 422, r.text
    fields = r.json()["error"]["fields"]
    assert set(fields.keys()) == {"title", "amount_cents", "date"}


# ---------------------------------------------------------------------------
# Inbox rejects explicit null field values
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inbox_put_rejects_explicit_null_title(client, test_data):
    """`PUT /inbox/{id}` 422s on `{"title": null}` — null is never "clear"."""
    draft = await client.post(
        "/v1/inbox",
        json={
            "id": str(uuid.uuid4()),
            "title": f"null-guard-{uuid.uuid4().hex[:8]}",
            "amount_cents": -100,
            "date": PAST_DATE,
            "account_id": test_data.account_id,
        },
        headers=_idem(),
    )
    assert draft.status_code == 201, draft.text
    inbox_id = draft.json()["id"]

    r = await client.put(
        f"/v1/inbox/{inbox_id}", json={"title": None}, headers=_idem()
    )
    assert r.status_code == 422, r.text
    assert "title" in r.json()["error"]["fields"]

    await client.delete(f"/v1/inbox/{inbox_id}", headers=_idem())


# ---------------------------------------------------------------------------
# transfer.id == id — accumulated, not a second round trip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transfer_id_collision_accumulates_with_other_errors(client, test_data):
    sibling = await _make_account(test_data.user_id, "collision-sibling")
    shared_id = str(uuid.uuid4())
    try:
        r = await client.post(
            "/v1/transactions",
            json={
                "id": shared_id,
                "title": "collision",
                "amount_cents": -5000,
                "date": PAST_DATE,
                "account_id": test_data.account_id,
                "transfer": {
                    "id": shared_id,       # collision
                    "account_id": sibling,
                    "amount_cents": 0,      # second, independent failure
                },
            },
            headers=_idem(),
        )
        assert r.status_code == 422, r.text
        fields = r.json()["error"]["fields"]
        # Both failures in ONE response — the collision check no longer fires
        # on its own after the accumulator has already raised.
        assert "transfer.id" in fields
        assert "transfer.amount_cents" in fields
    finally:
        await _cleanup_account(sibling)


@pytest.mark.asyncio
async def test_promote_transfer_id_collision_accumulated(client, test_data):
    sibling = await _make_account(test_data.user_id, "promote-collision")
    try:
        draft = await client.post(
            "/v1/inbox",
            json={
                "id": str(uuid.uuid4()),
                "title": f"promote-collision-{uuid.uuid4().hex[:8]}",
                "amount_cents": -3000,
                "date": PAST_DATE,
                "account_id": test_data.account_id,
                "transfer": {"account_id": sibling, "amount_cents": 3000},
            },
            headers=_idem(),
        )
        assert draft.status_code == 201, draft.text
        inbox_id = draft.json()["id"]

        shared_id = str(uuid.uuid4())
        r = await client.post(
            f"/v1/inbox/{inbox_id}/promote",
            json={"id": shared_id, "transfer_id": shared_id},
            headers=_idem(),
        )
        assert r.status_code == 422, r.text
        assert "transfer_id" in r.json()["error"]["fields"]

        await client.delete(f"/v1/inbox/{inbox_id}", headers=_idem())
    finally:
        await _cleanup_account(sibling)


# ---------------------------------------------------------------------------
# Promote links BOTH legs to the inbox row
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_promote_sets_inbox_id_on_both_legs(client, test_data):
    sibling = await _make_account(test_data.user_id, "backlink-sibling")
    try:
        draft = await client.post(
            "/v1/inbox",
            json={
                "id": str(uuid.uuid4()),
                "title": f"backlink-{uuid.uuid4().hex[:8]}",
                "amount_cents": -4000,
                "date": PAST_DATE,
                "account_id": test_data.account_id,
                "transfer": {"account_id": sibling, "amount_cents": 4000},
            },
            headers=_idem(),
        )
        assert draft.status_code == 201, draft.text
        inbox_id = draft.json()["id"]

        primary_id, sibling_id = str(uuid.uuid4()), str(uuid.uuid4())
        r = await client.post(
            f"/v1/inbox/{inbox_id}/promote",
            json={"id": primary_id, "transfer_id": sibling_id},
            headers=_idem(),
        )
        assert r.status_code == 200, r.text

        async with db.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, inbox_id FROM expense_transactions WHERE id = ANY($1::uuid[])",
                [primary_id, sibling_id],
            )
        assert len(rows) == 2
        for row in rows:
            assert str(row["inbox_id"]) == inbox_id, (
                f"leg {row['id']} lost its inbox backlink"
            )
    finally:
        await _cleanup_account(sibling)
