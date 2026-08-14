"""Integration tests for the People API — `POST /people` and the person axis.

A person is an `expense_bank_accounts` row with `is_person = true`, not a
separate resource: the balance you record against them IS the debt. So there is
exactly one people route (creation, the only moment `is_person` is settable) and
everything afterwards is account behaviour on `/accounts/{id}`.

Four owner decisions from 2026-08-13 are pinned here, each of which could
otherwise be "simplified" back out by a later reader:

  1. one create route, not a namespace   → test_person_uses_the_account_routes
  2. `sort_order` scoped to `is_person`  → test_sort_order_counters_are_independent
  3. people are archivable, own panel    → test_archived_person_leaves_every_active_surface
  4. name uniqueness NOT scoped          → test_name_uniqueness_is_shared_with_real_accounts

plus the rule that outlasts all four: a settled person stays visible at 0
(test_settled_person_stays_visible_with_zero_balance). Hiding people at a zero
balance was proposed and rejected — it would make "settled" and "never recorded"
identical on screen.

Run: .venv/bin/pytest tests/test_people_api.py -v
"""
import uuid

import pytest

from app import db

PAST_DATE = "2026-04-12T12:00:00Z"


def _idem() -> dict:
    return {"X-Idempotency-Key": str(uuid.uuid4())}


def _body(name: str, currency: str = "PEN", **overrides) -> dict:
    body = {"id": str(uuid.uuid4()), "name": name, "currency_code": currency}
    body.update(overrides)
    return body


async def _cleanup(user_id: str, *account_ids: str) -> None:
    async with db.pool.acquire() as conn:
        for account_id in account_ids:
            # Transactions first — the account row is their FK target.
            await conn.execute(
                "DELETE FROM expense_transactions WHERE account_id = $1 AND user_id = $2",
                account_id, user_id,
            )
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1 AND user_id = $2",
                account_id, user_id,
            )
            await conn.execute(
                "DELETE FROM expense_bank_accounts WHERE id = $1 AND user_id = $2",
                account_id, user_id,
            )


async def _activity_actions(resource_id: str, user_id: str) -> list[int]:
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT action FROM activity_log
            WHERE resource_id = $1 AND user_id = $2
            ORDER BY created_at ASC
            """,
            resource_id, user_id,
        )
    return [r["action"] for r in rows]


async def _post_txn(client, account_id: str, category_id: str, amount_cents: int):
    return await client.post(
        "/v1/transactions",
        json={
            "id": str(uuid.uuid4()),
            "title": f"people-txn-{uuid.uuid4().hex[:8]}",
            "amount_cents": amount_cents,
            "date": PAST_DATE,
            "account_id": account_id,
            "category_id": category_id,
        },
        headers=_idem(),
    )


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_person(client, test_data):
    """The response IS an account response, with `is_person` true and a zero
    balance — zero because nothing has been recorded, not because it is unknown.
    """
    body = _body(f"Person-Create-{uuid.uuid4()}")
    r = await client.post("/v1/people", json=body, headers=_idem())
    try:
        assert r.status_code == 201, r.text
        person = r.json()
        assert person["is_person"] is True
        assert person["id"] == body["id"]
        assert person["current_balance_cents"] == 0
        assert person["is_archived"] is False
        assert person["deleted_at"] is None
        # A person is an account — one CREATED entry under resource_type
        # "account", not a second resource type.
        assert await _activity_actions(body["id"], test_data.user_id) == [1]
        async with db.pool.acquire() as conn:
            resource_type = await conn.fetchval(
                "SELECT resource_type FROM activity_log WHERE resource_id = $1",
                body["id"],
            )
        assert resource_type == "account"
    finally:
        await _cleanup(test_data.user_id, body["id"])


@pytest.mark.asyncio
async def test_accounts_endpoint_still_refuses_is_person(client):
    """`POST /accounts` must never mint a person.

    Explicit creation is the whole design — a person is never conjured as a
    side effect of another write. The rejection is inherited from StrictModel,
    which is exactly why it needs a test: nothing in `accounts.py` spells it out
    in code, so a future `extra="allow"` would silently open this door.
    """
    r = await client.post(
        "/v1/accounts",
        json=_body(f"Sneaky-Person-{uuid.uuid4()}", is_person=True),
        headers=_idem(),
    )
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "is_person" in (error.get("fields") or {}), error


@pytest.mark.asyncio
async def test_people_endpoint_rejects_is_person_and_unknown_fields(client):
    """On `/people`, `is_person` is implied — sending it is still a 422.

    Fail closed: the endpoint enumerates what it accepts, and a caller trying to
    set the flag explicitly is a caller with a wrong model of the API.
    """
    r = await client.post(
        "/v1/people", json=_body(f"Redundant-{uuid.uuid4()}", is_person=True), headers=_idem()
    )
    assert r.status_code == 422, r.text
    assert "is_person" in (r.json()["error"].get("fields") or {})

    r = await client.post(
        "/v1/people", json=_body(f"Bogus-{uuid.uuid4()}", bogus=1), headers=_idem()
    )
    assert r.status_code == 422, r.text
    assert "bogus" in (r.json()["error"].get("fields") or {})


@pytest.mark.asyncio
async def test_people_creation_shares_account_validation(client, test_data):
    """Same currency/name/colour rules as `POST /accounts` — one implementation."""
    r = await client.post("/v1/people", json=_body("   "), headers=_idem())
    assert r.status_code == 422, r.text
    assert r.json()["error"]["fields"]["name"] == "Must not be empty."

    r = await client.post(
        "/v1/people", json=_body(f"Bad-Currency-{uuid.uuid4()}", currency="XYZ"), headers=_idem()
    )
    assert r.status_code == 422, r.text
    assert "currency_code" in r.json()["error"]["fields"]

    r = await client.post(
        "/v1/people", json=_body(f"Bad-Color-{uuid.uuid4()}", color="banana"), headers=_idem()
    )
    assert r.status_code == 422, r.text
    assert "color" in r.json()["error"]["fields"]

    duplicate = _body(f"Dup-Id-{uuid.uuid4()}")
    try:
        r = await client.post("/v1/people", json=duplicate, headers=_idem())
        assert r.status_code == 201, r.text
        clash = dict(duplicate, name=f"Different-Name-{uuid.uuid4()}")
        r = await client.post("/v1/people", json=clash, headers=_idem())
        assert r.status_code == 409, r.text
    finally:
        await _cleanup(test_data.user_id, duplicate["id"])


@pytest.mark.asyncio
async def test_create_person_replay_returns_stored_response(client, test_data):
    """Idempotent like every other write — the replay is the stored 201."""
    body = _body(f"Replay-Person-{uuid.uuid4()}")
    key = {"X-Idempotency-Key": str(uuid.uuid4())}
    try:
        first = await client.post("/v1/people", json=body, headers=key)
        assert first.status_code == 201, first.text

        second = await client.post("/v1/people", json=body, headers=key)
        assert second.status_code == 201, second.text
        assert second.json() == first.json()

        # One row, one CREATED entry — the replay wrote nothing.
        assert await _activity_actions(body["id"], test_data.user_id) == [1]
    finally:
        await _cleanup(test_data.user_id, body["id"])


# --------------------------------------------------------------------------
# Decision 4 — name uniqueness is shared, not scoped by is_person
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_name_uniqueness_is_shared_with_real_accounts(client, test_data):
    """One "Eliana" per currency, whichever section she renders in.

    The `sql/028` index is `(user_id, LOWER(name), currency_code)` with no
    `is_person` term, and that is deliberate (owner decision 2026-08-13):
    two things called Eliana in one currency are confusing regardless of which
    list they live in. Splitting the scopes later means reworking that index,
    which is why it was decided before the first person existed.
    """
    name = f"Eliana-{uuid.uuid4()}"
    account = _body(name)
    person_same_currency = _body(name)
    person_other_currency = _body(name, currency="USD")
    try:
        r = await client.post("/v1/accounts", json=account, headers=_idem())
        assert r.status_code == 201, r.text

        # Person colliding with a REAL account, same currency → 409.
        r = await client.post("/v1/people", json=person_same_currency, headers=_idem())
        assert r.status_code == 409, r.text
        assert name in r.json()["error"]["message"]

        # Different currency is a different scope → allowed, same as accounts.
        r = await client.post("/v1/people", json=person_other_currency, headers=_idem())
        assert r.status_code == 201, r.text

        # And the collision is symmetric: a real account cannot take a
        # person's name either.
        r = await client.post("/v1/accounts", json=_body(name, currency="USD"), headers=_idem())
        assert r.status_code == 409, r.text
    finally:
        await _cleanup(
            test_data.user_id, account["id"], person_other_currency["id"]
        )


# --------------------------------------------------------------------------
# Decision 2 — sort_order is scoped to is_person
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_person_starts_at_zero(client, test_data):
    """People number from 0 in their own section, however many accounts exist.

    The shared fixture account sits at `sort_order = 1`, so a person appending
    at 0 proves the counters do not share a MAX.
    """
    async with db.pool.acquire() as conn:
        # Other files never create people; this only clears strays from earlier
        # tests in THIS file (pytest.ini pins --dist loadfile, so they ran here
        # and ran serially).
        await conn.execute(
            "DELETE FROM expense_bank_accounts WHERE user_id = $1 AND is_person = true",
            test_data.user_id,
        )
        real_max = await conn.fetchval(
            """
            SELECT MAX(sort_order) FROM expense_bank_accounts
            WHERE user_id = $1 AND is_person = false
            """,
            test_data.user_id,
        )
    assert real_max is not None and real_max >= 1, (
        "fixture account should exist at sort_order >= 1 for this test to mean anything"
    )

    person = _body(f"First-Person-{uuid.uuid4()}")
    try:
        r = await client.post("/v1/people", json=person, headers=_idem())
        assert r.status_code == 201, r.text
        assert r.json()["sort_order"] == 0, (
            f"first person must append at 0 in its own scope, not after the "
            f"real accounts (whose max is {real_max})"
        )
    finally:
        await _cleanup(test_data.user_id, person["id"])


@pytest.mark.asyncio
async def test_sort_order_counters_are_independent(client, test_data):
    """Creating one kind never advances the other's counter.

    Interleaved on purpose: if a single MAX were shared, the second person
    would land after the real account instead of directly after the first
    person. Asserted relationally so it holds whatever rows already exist.
    """
    real_one = _body(f"Scope-Acct-A-{uuid.uuid4()}")
    person_one = _body(f"Scope-Person-A-{uuid.uuid4()}")
    real_two = _body(f"Scope-Acct-B-{uuid.uuid4()}")
    person_two = _body(f"Scope-Person-B-{uuid.uuid4()}")
    try:
        r = await client.post("/v1/accounts", json=real_one, headers=_idem())
        assert r.status_code == 201, r.text
        real_one_slot = r.json()["sort_order"]

        r = await client.post("/v1/people", json=person_one, headers=_idem())
        assert r.status_code == 201, r.text
        person_one_slot = r.json()["sort_order"]

        r = await client.post("/v1/accounts", json=real_two, headers=_idem())
        assert r.status_code == 201, r.text
        assert r.json()["sort_order"] == real_one_slot + 1, (
            "creating a person must not advance the real-account counter"
        )

        r = await client.post("/v1/people", json=person_two, headers=_idem())
        assert r.status_code == 201, r.text
        assert r.json()["sort_order"] == person_one_slot + 1, (
            "creating a real account must not advance the person counter"
        )
    finally:
        await _cleanup(
            test_data.user_id,
            real_one["id"], person_one["id"], real_two["id"], person_two["id"],
        )


@pytest.mark.asyncio
async def test_explicit_sort_order_is_honoured_including_zero(client, test_data):
    """Scoping the append does not change the explicit-value rule."""
    person = _body(f"Explicit-Slot-{uuid.uuid4()}", sort_order=0)
    try:
        r = await client.post("/v1/people", json=person, headers=_idem())
        assert r.status_code == 201, r.text
        assert r.json()["sort_order"] == 0
    finally:
        await _cleanup(test_data.user_id, person["id"])


# --------------------------------------------------------------------------
# Decision 1 — the account routes serve people after creation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_person_uses_the_account_routes(client, test_data):
    """Rename, fetch, delete and restore all work on a person via /accounts.

    This is why `POST /people` is the only people route: nothing else about a
    person differs from an account, so a parallel namespace would be a second
    copy of routes that already work.
    """
    person = _body(f"Route-Reuse-{uuid.uuid4()}")
    try:
        r = await client.post("/v1/people", json=person, headers=_idem())
        assert r.status_code == 201, r.text
        person_id = person["id"]

        r = await client.get(f"/v1/accounts/{person_id}")
        assert r.status_code == 200, r.text
        assert r.json()["is_person"] is True

        new_name = f"Renamed-{uuid.uuid4()}"
        r = await client.put(
            f"/v1/accounts/{person_id}", json={"name": new_name}, headers=_idem()
        )
        assert r.status_code == 200, r.text
        assert r.json()["name"] == new_name
        # The flag survives a PUT and is not settable through one.
        assert r.json()["is_person"] is True

        r = await client.put(
            f"/v1/accounts/{person_id}", json={"is_person": False}, headers=_idem()
        )
        assert r.status_code == 422, r.text
        assert "is_person" in (r.json()["error"].get("fields") or {})

        r = await client.delete(f"/v1/accounts/{person_id}", headers=_idem())
        assert r.status_code == 200, r.text
        assert r.json()["deleted_at"] is not None

        r = await client.post(f"/v1/accounts/{person_id}/restore", headers=_idem())
        assert r.status_code == 200, r.text
        assert r.json()["deleted_at"] is None
        assert r.json()["is_person"] is True
    finally:
        await _cleanup(test_data.user_id, person["id"])


@pytest.mark.asyncio
async def test_person_cannot_carry_an_opening_balance(client, test_data):
    """A debt is built from recorded rows, never seeded.

    The guard already had coverage against a hand-inserted person row
    (`test_opening_balance.py`); this is the same rule reached through the
    creation path that now exists.
    """
    person = _body(f"No-Opening-{uuid.uuid4()}")
    try:
        r = await client.post("/v1/people", json=person, headers=_idem())
        assert r.status_code == 201, r.text

        r = await client.post(
            f"/v1/accounts/{person['id']}/opening-balance",
            json={
                "transaction_id": str(uuid.uuid4()),
                "amount_cents": 5000,
                "date": PAST_DATE,
            },
            headers=_idem(),
        )
        assert r.status_code == 422, r.text
        assert "account_id" in r.json()["error"]["fields"]
    finally:
        await _cleanup(test_data.user_id, person["id"])


# --------------------------------------------------------------------------
# Decision 3 — archiving, and its own dashboard panel
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archived_person_leaves_every_active_surface(client, test_data):
    """Archive moves a person from `people` to `archived_people`, consistently.

    Before 2026-08-14 the dashboard's person slice had no archive filter at all
    while `GET /accounts?include_people=true` did, so an archived person would
    have vanished from one surface and persisted on the other. Never observed —
    no person could exist until `POST /people` shipped in the same change — but
    this is the test that keeps the two in step.
    """
    person = _body(f"Archivable-{uuid.uuid4()}")
    person_id = person["id"]
    try:
        r = await client.post("/v1/people", json=person, headers=_idem())
        assert r.status_code == 201, r.text

        r = await client.get("/v1/dashboard")
        assert person_id in {p["id"] for p in r.json()["people"]}

        r = await client.post(f"/v1/accounts/{person_id}/archive", headers=_idem())
        assert r.status_code == 200, r.text
        assert r.json()["is_archived"] is True

        r = await client.get("/v1/dashboard?include_archived=true")
        assert r.status_code == 200, r.text
        body = r.json()
        assert person_id not in {p["id"] for p in body["people"]}
        assert person_id in {p["id"] for p in body["archived_people"]}
        # And she does not leak into the bank-account archive.
        assert person_id not in {a["id"] for a in body["archived_accounts"]}

        # The accounts list agrees.
        r = await client.get("/v1/accounts?include_people=true&limit=200")
        assert person_id not in {a["id"] for a in r.json()["items"]}
        r = await client.get(
            "/v1/accounts?include_people=true&include_archived=true&limit=200"
        )
        assert person_id in {a["id"] for a in r.json()["items"]}

        # Unarchive puts her back on all of them.
        r = await client.post(f"/v1/accounts/{person_id}/unarchive", headers=_idem())
        assert r.status_code == 200, r.text
        body = (await client.get("/v1/dashboard?include_archived=true")).json()
        assert person_id in {p["id"] for p in body["people"]}
        assert person_id not in {p["id"] for p in body["archived_people"]}
    finally:
        await _cleanup(test_data.user_id, person_id)


@pytest.mark.asyncio
async def test_cannot_record_against_an_archived_person(client, test_data):
    """The point of archiving a person: no accidental writes.

    `active_account_row` refuses archived accounts and never checked
    `is_person`, so this protection applied to people the day they existed.
    """
    person = _body(f"Archived-Write-{uuid.uuid4()}")
    try:
        r = await client.post("/v1/people", json=person, headers=_idem())
        assert r.status_code == 201, r.text

        r = await _post_txn(client, person["id"], test_data.category_id, -2500)
        assert r.status_code == 201, r.text

        r = await client.post(f"/v1/accounts/{person['id']}/archive", headers=_idem())
        assert r.status_code == 200, r.text

        r = await _post_txn(client, person["id"], test_data.category_id, -2500)
        assert r.status_code == 422, r.text
        assert "account_id" in r.json()["error"]["fields"]
    finally:
        await _cleanup(test_data.user_id, person["id"])


@pytest.mark.asyncio
async def test_dashboard_default_omits_archived_people(client):
    """Null-over-omission: the key is always on the wire, null without the flag."""
    r = await client.get("/v1/dashboard")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "archived_people" in body
    assert body["archived_people"] is None
    assert body["archived_accounts"] is None


# --------------------------------------------------------------------------
# The rule that outlasts the four decisions
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settled_person_stays_visible_with_zero_balance(client, test_data):
    """A cleared debt reports 0 and stays in the list. It is NOT hidden.

    Hiding people at a zero balance was proposed and rejected (owner,
    2026-08-13): it would make "she paid me back" and "I never recorded the
    loan" identical on screen, flicker a person in and out of the list as rows
    land, and misread a coincidental net zero (lent 200, borrowed 200 — two
    live debts) as nothing to show. Zero is a balance, not a missing value;
    archiving is the deliberate way to retire someone. Decluttering a long
    People list is a client display choice.
    """
    person = _body(f"Settled-{uuid.uuid4()}")
    person_id = person["id"]
    try:
        r = await client.post("/v1/people", json=person, headers=_idem())
        assert r.status_code == 201, r.text

        # She owes you 200, then pays it back.
        r = await _post_txn(client, person_id, test_data.category_id, 20000)
        assert r.status_code == 201, r.text
        r = await client.get(f"/v1/accounts/{person_id}")
        assert r.json()["current_balance_cents"] == 20000

        r = await _post_txn(client, person_id, test_data.category_id, -20000)
        assert r.status_code == 201, r.text

        r = await client.get(f"/v1/accounts/{person_id}")
        assert r.status_code == 200, r.text
        assert r.json()["current_balance_cents"] == 0

        body = (await client.get("/v1/dashboard")).json()
        settled = [p for p in body["people"] if p["id"] == person_id]
        assert settled, "a settled person must remain in the `people` panel"
        assert settled[0]["current_balance_cents"] == 0
    finally:
        await _cleanup(test_data.user_id, person_id)
