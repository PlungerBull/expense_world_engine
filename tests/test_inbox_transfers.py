"""Inbox transfer drafts: direction is stored, not inferred from a sign.

Before `sql/019` the inbox had no direction column at all, so the sign of
`transfer_amount_cents` was the only direction signal on the row. The primary
leg's own sign was discarded by `abs()` on write and re-derived at promote time
as the *negation* of the sibling's, which made `create_transfer_pair`'s
opposite-sign guard unreachable from this path — two outflows promoted cleanly
with one leg silently flipped (WP7.2).

`sql/019` fixed that with a dedicated `transfer_direction` column. WP1
(`sql/020`) then removed that column, not by reverting the lesson but by
finishing it: direction now lives in `transaction_type` on **every** row —
1 = outflow, 2 = inflow — and a transfer is identified by its transfer columns
rather than by a third type value. So the assertions below read
`transaction_type` where they used to read `transfer_direction`; the contract
they pin is otherwise unchanged.

These tests pin that contract end to end:

  * signed in the request, absolute in storage beside `transaction_type`,
    absolute in the response beside `transaction_type`
  * a contradictory pair is a 422, not a silent coercion
  * the transfer columns survive an `amount_cents` edit in the same request
  * `transfer: null` un-marks a draft and preserves its direction
  * `?debit_as_negative=true` flips both legs, in opposite directions
  * `?ready=true` and promote agree on what is promotable
  * the half-transfer row is impossible at the database level
"""

import uuid

import asyncpg
import pytest

from app import db

PAST_DATE = "2026-04-12T12:00:00Z"


# ---------------------------------------------------------------------------
# Fixtures — inline accounts. conftest seeds only one, and mutating it would
# race the rest of the suite under xdist.
# ---------------------------------------------------------------------------

async def _make_account(user_id: str, name: str, balance_cents: int = 100000) -> str:
    account_id = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO expense_bank_accounts
                (id, user_id, name, currency_code, is_person, color,
                 current_balance_cents, is_archived, sort_order,
                 created_at, updated_at)
            VALUES ($1, $2, $3, 'PEN', false, '#00FF00', $4, false, 2, now(), now())
            """,
            account_id, user_id, f"{name}-{uuid.uuid4().hex[:8]}", balance_cents,
        )
    return account_id


async def _cleanup_account(account_id: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE expense_bank_accounts SET deleted_at = now() WHERE id = $1",
            account_id,
        )


async def _archive_account(account_id: str) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE expense_bank_accounts SET is_archived = true WHERE id = $1",
            account_id,
        )


async def _balance(account_id: str) -> int:
    async with db.pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT current_balance_cents FROM expense_bank_accounts WHERE id = $1",
            account_id,
        )


async def _inbox_row(inbox_id: str) -> asyncpg.Record:
    async with db.pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM expense_transaction_inbox WHERE id = $1", inbox_id
        )


async def _post_inbox(client, **payload):
    body = {"id": payload.pop("id", str(uuid.uuid4()))}
    body.update(payload)
    r = await client.post(
        "/v1/inbox", json=body, headers={"X-Idempotency-Key": str(uuid.uuid4())}
    )
    return r, body["id"]


async def _put_inbox(client, inbox_id: str, payload: dict):
    return await client.put(
        f"/v1/inbox/{inbox_id}",
        json=payload,
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )


# ---------------------------------------------------------------------------
# Storage shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transfer_draft_stores_absolute_amount_and_direction(client, test_data):
    """A signed request becomes absolute storage plus an explicit direction."""
    sibling = await _make_account(test_data.user_id, "outflow-target")
    try:
        r, inbox_id = await _post_inbox(
            client,
            title=f"inbox-transfer-{uuid.uuid4()}",
            amount_cents=-5000,  # money leaves the primary account
            date=PAST_DATE,
            account_id=test_data.account_id,
            transfer={"account_id": sibling, "amount_cents": 5000},
        )
        assert r.status_code == 201, r.text
        body = r.json()

        assert body["amount_cents"] == 5000
        assert body["transfer_amount_cents"] == 5000  # positive, was signed before
        assert body["transaction_type"] == 1  # OUTFLOW — the primary pays
        assert "transfer_direction" not in body

        row = await _inbox_row(inbox_id)
        assert row["transfer_amount_cents"] == 5000
        assert row["transaction_type"] == 1
        assert row["transfer_account_id"] is not None
    finally:
        await _cleanup_account(sibling)


@pytest.mark.asyncio
async def test_incoming_transfer_draft_stores_credit_direction(client, test_data):
    """The mirror case: money arriving at the primary account."""
    sibling = await _make_account(test_data.user_id, "inflow-source")
    try:
        r, _ = await _post_inbox(
            client,
            title=f"inbox-transfer-in-{uuid.uuid4()}",
            amount_cents=5000,  # money arrives
            date=PAST_DATE,
            account_id=test_data.account_id,
            transfer={"account_id": sibling, "amount_cents": -5000},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["transfer_amount_cents"] == 5000
        assert body["transaction_type"] == 2  # INFLOW — the primary receives
    finally:
        await _cleanup_account(sibling)


@pytest.mark.asyncio
async def test_transfer_id_is_not_accepted_on_inbox(client, test_data):
    """The inbox has its own transfer model without the discarded `id`.

    TransferField.id is the sibling *ledger row's* UUID; no ledger rows exist
    at draft time and the value was never read. Requiring it made the
    documented request shape 422 (WP7.2).
    """
    sibling = await _make_account(test_data.user_id, "no-id-needed")
    try:
        r, _ = await _post_inbox(
            client,
            title=f"inbox-no-transfer-id-{uuid.uuid4()}",
            amount_cents=-1000,
            date=PAST_DATE,
            account_id=test_data.account_id,
            transfer={"account_id": sibling, "amount_cents": 1000},  # no "id"
        )
        assert r.status_code == 201, r.text
    finally:
        await _cleanup_account(sibling)


# ---------------------------------------------------------------------------
# The WP7.2 corruption case
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_sign_transfer_draft_is_rejected(client, test_data):
    """Two outflows are a contradiction, not a transfer.

    This is the regression that motivated the change: the old encoding accepted
    it, listed it as ready, and promoted it with the primary silently flipped to
    +6000. Spec §546 requires 422.
    """
    sibling = await _make_account(test_data.user_id, "same-sign")
    try:
        r, _ = await _post_inbox(
            client,
            title=f"inbox-same-sign-{uuid.uuid4()}",
            amount_cents=-6000,
            date=PAST_DATE,
            account_id=test_data.account_id,
            transfer={"account_id": sibling, "amount_cents": -1500},
        )
        assert r.status_code == 422, r.text
        assert "transfer.amount_cents" in r.json()["error"]["fields"]
    finally:
        await _cleanup_account(sibling)


@pytest.mark.asyncio
async def test_same_sign_transfer_rejected_on_update(client, test_data):
    """The same guard applies to PUT, against the merged row state."""
    sibling = await _make_account(test_data.user_id, "same-sign-put")
    try:
        r, inbox_id = await _post_inbox(
            client,
            title=f"inbox-put-same-sign-{uuid.uuid4()}",
            date=PAST_DATE,
            account_id=test_data.account_id,
        )
        assert r.status_code == 201, r.text

        r = await _put_inbox(
            client,
            inbox_id,
            {
                "amount_cents": -6000,
                "transfer": {"account_id": sibling, "amount_cents": -1500},
            },
        )
        assert r.status_code == 422, r.text
        assert "transfer.amount_cents" in r.json()["error"]["fields"]
    finally:
        await _cleanup_account(sibling)


# ---------------------------------------------------------------------------
# Sparse drafts — the legitimate inbox looseness
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transfer_draft_without_primary_amount_keeps_direction(client, test_data):
    """A draft with no primary amount yet still knows which way it points."""
    sibling = await _make_account(test_data.user_id, "sparse")
    try:
        r, _ = await _post_inbox(
            client,
            title=f"inbox-sparse-{uuid.uuid4()}",
            account_id=test_data.account_id,
            transfer={"account_id": sibling, "amount_cents": 2500},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["amount_cents"] is None
        assert body["transfer_amount_cents"] == 2500
        # Sibling receives, so the primary is the one paying. This is the case
        # that needs the sibling's sign: with no primary amount, the sibling is
        # the only thing that can say which way the draft points.
        assert body["transaction_type"] == 1  # OUTFLOW
        assert body["transfer_account_id"] is not None
    finally:
        await _cleanup_account(sibling)


# ---------------------------------------------------------------------------
# The PUT ordering hazard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_put_with_amount_and_transfer_keeps_transfer_columns(client, test_data):
    """`amount_cents` in the same request must not clobber the transfer columns.

    The amount block used to run after the transfer block and overwrite
    transaction_type, leaving transfer columns set on a row whose type
    disagreed with them — promote treated it as a transfer, the read path did
    not. Both are now derived in one place from the merged state.
    """
    sibling = await _make_account(test_data.user_id, "put-both")
    try:
        r, inbox_id = await _post_inbox(
            client,
            title=f"inbox-put-both-{uuid.uuid4()}",
            date=PAST_DATE,
            account_id=test_data.account_id,
        )
        assert r.status_code == 201, r.text

        r = await _put_inbox(
            client,
            inbox_id,
            {
                "amount_cents": -3000,
                "transfer": {"account_id": sibling, "amount_cents": 3000},
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["transaction_type"] == 1  # OUTFLOW, from the primary's sign
        assert body["transfer_account_id"] == sibling
        assert body["amount_cents"] == 3000
        assert body["transfer_amount_cents"] == 3000
    finally:
        await _cleanup_account(sibling)


@pytest.mark.asyncio
async def test_put_amount_alone_flips_an_existing_transfer(client, test_data):
    """Restating the primary's sign flips the draft, keeping it a transfer."""
    sibling = await _make_account(test_data.user_id, "put-amount-only")
    try:
        r, inbox_id = await _post_inbox(
            client,
            title=f"inbox-flip-{uuid.uuid4()}",
            amount_cents=-4000,
            date=PAST_DATE,
            account_id=test_data.account_id,
            transfer={"account_id": sibling, "amount_cents": 4000},
        )
        assert r.status_code == 201, r.text
        assert r.json()["transaction_type"] == 1  # OUTFLOW

        r = await _put_inbox(client, inbox_id, {"amount_cents": 4000})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["transaction_type"] == 2  # INFLOW — now the primary receives
        assert body["transfer_account_id"] == sibling  # still a transfer
        assert body["transfer_amount_cents"] == 4000  # still absolute
    finally:
        await _cleanup_account(sibling)


# ---------------------------------------------------------------------------
# Clearing a transfer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transfer_null_reverts_draft_to_expense(client, test_data):
    """`transfer: null` un-marks a draft; it was previously a one-way door."""
    sibling = await _make_account(test_data.user_id, "clearable")
    try:
        r, inbox_id = await _post_inbox(
            client,
            title=f"inbox-clear-{uuid.uuid4()}",
            amount_cents=-7000,
            date=PAST_DATE,
            account_id=test_data.account_id,
            category_id=test_data.category_id,
            transfer={"account_id": sibling, "amount_cents": 7000},
        )
        assert r.status_code == 201, r.text
        assert r.json()["transaction_type"] == 1  # OUTFLOW

        r = await _put_inbox(client, inbox_id, {"transfer": None})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["transfer_account_id"] is None
        assert body["transfer_amount_cents"] is None
        # Direction survives the clear untouched. Dropping the counterparty
        # does not change which way the money moved, and transaction_type has
        # held that fact all along — it no longer has to be recovered from a
        # separate column on the way out.
        assert body["transaction_type"] == 1  # still OUTFLOW
        assert body["amount_cents"] == 7000  # untouched
    finally:
        await _cleanup_account(sibling)


@pytest.mark.asyncio
async def test_transfer_null_on_incoming_draft_becomes_income(client, test_data):
    """The mirror case: an inflow draft stays an inflow when un-marked."""
    sibling = await _make_account(test_data.user_id, "clearable-in")
    try:
        r, inbox_id = await _post_inbox(
            client,
            title=f"inbox-clear-in-{uuid.uuid4()}",
            amount_cents=7000,
            date=PAST_DATE,
            account_id=test_data.account_id,
            transfer={"account_id": sibling, "amount_cents": -7000},
        )
        assert r.status_code == 201, r.text

        r = await _put_inbox(client, inbox_id, {"transfer": None})
        assert r.status_code == 200, r.text
        assert r.json()["transaction_type"] == 2  # INFLOW
    finally:
        await _cleanup_account(sibling)


@pytest.mark.asyncio
async def test_explicit_null_still_rejected_on_other_fields(client, test_data):
    """`transfer` is the only field that opted into nullability."""
    r, inbox_id = await _post_inbox(
        client,
        title=f"inbox-null-guard-{uuid.uuid4()}",
        amount_cents=-100,
        date=PAST_DATE,
        account_id=test_data.account_id,
    )
    assert r.status_code == 201, r.text

    r = await _put_inbox(client, inbox_id, {"title": None})
    assert r.status_code == 422, r.text
    assert "title" in r.json()["error"]["fields"]


# ---------------------------------------------------------------------------
# debit_as_negative
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_debit_as_negative_flips_both_legs_opposite_ways(client, test_data):
    """The sibling used to be emitted as-stored beside a flipped primary (WP10.2)."""
    sibling = await _make_account(test_data.user_id, "flip-both")
    try:
        r, inbox_id = await _post_inbox(
            client,
            title=f"inbox-flip-both-{uuid.uuid4()}",
            amount_cents=-5000,
            date=PAST_DATE,
            account_id=test_data.account_id,
            transfer={"account_id": sibling, "amount_cents": 5000},
        )
        assert r.status_code == 201, r.text

        plain = await client.get(f"/v1/inbox/{inbox_id}")
        assert plain.status_code == 200, plain.text
        assert plain.json()["amount_cents"] == 5000
        assert plain.json()["transfer_amount_cents"] == 5000

        flipped = await client.get(f"/v1/inbox/{inbox_id}?debit_as_negative=true")
        assert flipped.status_code == 200, flipped.text
        body = flipped.json()
        assert body["amount_cents"] == -5000  # primary pays
        assert body["transfer_amount_cents"] == 5000  # sibling receives

        # And the mirror: a credit primary flips the other leg instead.
        r = await _put_inbox(client, inbox_id, {"amount_cents": 5000})
        assert r.status_code == 200, r.text
        flipped = await client.get(f"/v1/inbox/{inbox_id}?debit_as_negative=true")
        body = flipped.json()
        assert body["amount_cents"] == 5000
        assert body["transfer_amount_cents"] == -5000
    finally:
        await _cleanup_account(sibling)


# ---------------------------------------------------------------------------
# Promote
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_promote_outgoing_transfer_moves_both_balances(client, test_data):
    source = await _make_account(test_data.user_id, "promote-src")
    target = await _make_account(test_data.user_id, "promote-dst")
    try:
        source_before = await _balance(source)
        target_before = await _balance(target)

        r, inbox_id = await _post_inbox(
            client,
            title=f"inbox-promote-out-{uuid.uuid4()}",
            amount_cents=-5000,
            date=PAST_DATE,
            account_id=source,
            transfer={"account_id": target, "amount_cents": 5000},
        )
        assert r.status_code == 201, r.text

        primary_id = str(uuid.uuid4())
        sibling_id = str(uuid.uuid4())
        r = await client.post(
            f"/v1/inbox/{inbox_id}/promote",
            json={"id": primary_id, "transfer_id": sibling_id},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 200, r.text
        primary = r.json()
        assert primary["transaction_type"] == 1  # OUTFLOW
        assert primary["transfer_transaction_id"] == sibling_id
        assert primary["amount_cents"] == 5000

        async with db.pool.acquire() as conn:
            sibling_row = await conn.fetchrow(
                "SELECT * FROM expense_transactions WHERE id = $1", sibling_id
            )
        assert sibling_row["transaction_type"] == 2  # INFLOW
        assert sibling_row["amount_cents"] == 5000

        assert await _balance(source) == source_before - 5000
        assert await _balance(target) == target_before + 5000
        # Same-currency pair nets to exactly zero.
        assert (await _balance(source) - source_before) + (
            await _balance(target) - target_before
        ) == 0
    finally:
        await _cleanup_account(source)
        await _cleanup_account(target)


@pytest.mark.asyncio
async def test_promote_incoming_transfer_moves_balances_the_other_way(client, test_data):
    """The direction the old encoding could only reach through the sibling."""
    primary_account = await _make_account(test_data.user_id, "promote-in-primary")
    other = await _make_account(test_data.user_id, "promote-in-other")
    try:
        primary_before = await _balance(primary_account)
        other_before = await _balance(other)

        r, inbox_id = await _post_inbox(
            client,
            title=f"inbox-promote-in-{uuid.uuid4()}",
            amount_cents=3000,  # arriving
            date=PAST_DATE,
            account_id=primary_account,
            transfer={"account_id": other, "amount_cents": -3000},
        )
        assert r.status_code == 201, r.text

        r = await client.post(
            f"/v1/inbox/{inbox_id}/promote",
            json={"id": str(uuid.uuid4()), "transfer_id": str(uuid.uuid4())},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 200, r.text
        assert r.json()["transaction_type"] == 2  # INFLOW

        assert await _balance(primary_account) == primary_before + 3000
        assert await _balance(other) == other_before - 3000
    finally:
        await _cleanup_account(primary_account)
        await _cleanup_account(other)


@pytest.mark.asyncio
async def test_promote_transfer_without_transfer_id_is_rejected(client, test_data):
    sibling = await _make_account(test_data.user_id, "promote-no-tid")
    try:
        r, inbox_id = await _post_inbox(
            client,
            title=f"inbox-no-tid-{uuid.uuid4()}",
            amount_cents=-1000,
            date=PAST_DATE,
            account_id=test_data.account_id,
            transfer={"account_id": sibling, "amount_cents": 1000},
        )
        assert r.status_code == 201, r.text

        r = await client.post(
            f"/v1/inbox/{inbox_id}/promote",
            json={"id": str(uuid.uuid4())},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 422, r.text
        assert "transfer_id" in r.json()["error"]["fields"]
    finally:
        await _cleanup_account(sibling)


@pytest.mark.asyncio
async def test_promote_non_transfer_with_transfer_id_is_rejected(client, test_data):
    """Spec §383 mandates 422; the value used to be silently discarded."""
    r, inbox_id = await _post_inbox(
        client,
        title=f"inbox-stray-tid-{uuid.uuid4()}",
        amount_cents=-1000,
        date=PAST_DATE,
        account_id=test_data.account_id,
        category_id=test_data.category_id,
    )
    assert r.status_code == 201, r.text

    r = await client.post(
        f"/v1/inbox/{inbox_id}/promote",
        json={"id": str(uuid.uuid4()), "transfer_id": str(uuid.uuid4())},
        headers={"X-Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 422, r.text
    assert "transfer_id" in r.json()["error"]["fields"]


@pytest.mark.asyncio
async def test_promote_rejects_archived_sibling_account(client, test_data):
    """The sibling gets the same account check the primary does."""
    sibling = await _make_account(test_data.user_id, "promote-archived")
    try:
        r, inbox_id = await _post_inbox(
            client,
            title=f"inbox-archived-sibling-{uuid.uuid4()}",
            amount_cents=-1000,
            date=PAST_DATE,
            account_id=test_data.account_id,
            transfer={"account_id": sibling, "amount_cents": 1000},
        )
        assert r.status_code == 201, r.text

        await _archive_account(sibling)

        r = await client.post(
            f"/v1/inbox/{inbox_id}/promote",
            json={"id": str(uuid.uuid4()), "transfer_id": str(uuid.uuid4())},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 422, r.text
        assert "transfer.account_id" in r.json()["error"]["fields"]
    finally:
        await _cleanup_account(sibling)


# ---------------------------------------------------------------------------
# ?ready=true agrees with promote
# ---------------------------------------------------------------------------

async def _ready_ids(client) -> set:
    r = await client.get("/v1/inbox?ready=true&limit=200")
    assert r.status_code == 200, r.text
    return {item["id"] for item in r.json()["items"]}


@pytest.mark.asyncio
async def test_ready_filter_includes_categoryless_transfer(client, test_data):
    """Transfers never carry a category; requiring one hid every promotable item."""
    sibling = await _make_account(test_data.user_id, "ready-transfer")
    try:
        r, inbox_id = await _post_inbox(
            client,
            title=f"inbox-ready-transfer-{uuid.uuid4()}",
            amount_cents=-1200,
            date=PAST_DATE,
            account_id=test_data.account_id,
            transfer={"account_id": sibling, "amount_cents": 1200},
        )
        assert r.status_code == 201, r.text

        assert inbox_id in await _ready_ids(client)

        # ...and what it reports as ready actually promotes.
        r = await client.post(
            f"/v1/inbox/{inbox_id}/promote",
            json={"id": str(uuid.uuid4()), "transfer_id": str(uuid.uuid4())},
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert r.status_code == 200, r.text
    finally:
        await _cleanup_account(sibling)


@pytest.mark.asyncio
async def test_ready_filter_excludes_archived_sibling_account(client, test_data):
    """A row that would 422 on promote must not be reported ready."""
    sibling = await _make_account(test_data.user_id, "ready-archived")
    try:
        r, inbox_id = await _post_inbox(
            client,
            title=f"inbox-ready-archived-{uuid.uuid4()}",
            amount_cents=-1300,
            date=PAST_DATE,
            account_id=test_data.account_id,
            transfer={"account_id": sibling, "amount_cents": 1300},
        )
        assert r.status_code == 201, r.text
        assert inbox_id in await _ready_ids(client)

        await _archive_account(sibling)
        assert inbox_id not in await _ready_ids(client)
    finally:
        await _cleanup_account(sibling)


@pytest.mark.asyncio
async def test_ready_filter_still_requires_category_for_non_transfers(client, test_data):
    r, inbox_id = await _post_inbox(
        client,
        title=f"inbox-ready-nocat-{uuid.uuid4()}",
        amount_cents=-1400,
        date=PAST_DATE,
        account_id=test_data.account_id,
        # no category_id, no transfer
    )
    assert r.status_code == 201, r.text
    assert inbox_id not in await _ready_ids(client)


# ---------------------------------------------------------------------------
# The database refuses a half-transfer row
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_half_transfer_row_violates_check_constraint(test_data):
    """Fail closed: the transfer columns are all-or-nothing.

    The Python guards above make this unreachable through the API; the
    constraint makes it unreachable full stop. sql/020 rewrote this CHECK
    without `transfer_direction` — the direction requirement did not go away,
    it moved onto `transaction_type IN (1, 2)`, which is asserted below.
    """
    async with db.pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO expense_transaction_inbox
                    (id, user_id, title, amount_cents, transaction_type,
                     transfer_account_id, transfer_amount_cents,
                     created_at, updated_at)
                VALUES ($1, $2, 'half-transfer', 1000, 1, $3, NULL, now(), now())
                """,
                str(uuid.uuid4()), test_data.user_id, test_data.account_id,
            )


@pytest.mark.asyncio
async def test_directionless_transfer_draft_violates_check_constraint(test_data):
    """A populated transfer triple with no direction is still impossible.

    This is the invariant `sql/019` added and `sql/020` carried over: before
    019 the inbox could hold a transfer whose direction nothing recorded. The
    column enforcing it changed; the guarantee did not.
    """
    async with db.pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO expense_transaction_inbox
                    (id, user_id, title, amount_cents, transaction_type,
                     transfer_account_id, transfer_amount_cents,
                     created_at, updated_at)
                VALUES ($1, $2, 'no-direction', 1000, NULL, $3, 1000, now(), now())
                """,
                str(uuid.uuid4()), test_data.user_id, test_data.account_id,
            )


@pytest.mark.asyncio
async def test_negative_transfer_amount_violates_check_constraint(test_data):
    async with db.pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO expense_transaction_inbox
                    (id, user_id, title, amount_cents, transaction_type,
                     transfer_account_id, transfer_amount_cents,
                     created_at, updated_at)
                VALUES ($1, $2, 'negative-sibling', 1000, 1, $3, -1000, now(), now())
                """,
                str(uuid.uuid4()), test_data.user_id, test_data.account_id,
            )
