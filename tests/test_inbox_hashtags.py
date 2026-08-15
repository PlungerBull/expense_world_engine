"""Inbox hashtags — the draft carries tags, and they survive promotion.

Owner decision 2026-08-07, shipped 2026-08-14 with `sql/033`. Before this the
inbox schemas had no `hashtag_ids` field at all: a user who captured through
the inbox could not tag, and the only signal was a `422` on the unknown field.

What the tests below hold in place, roughly in the order a draft lives:

  * `hashtag_ids` is on every inbox representation — create, get, list, put,
    delete — `[]` when empty, never null, never omitted (§3a).
  * Every id must reference an active, caller-owned hashtag. This is the one
    reference rule the inbox does NOT relax: a draft may point at a dead
    category and be refused at promote, but a dead hashtag is refused on the
    spot, because its junction row would be invisible the moment it was
    written.
  * PUT replaces the set; `[]` clears it; omission leaves it alone; a
    tags-only edit still bumps `version`, because the draft's wire shape
    changed.
  * Dismiss closes the tags with the draft (one-way — there is no inbox
    restore), and the DELETED activity entry keeps what they were.
  * Promote MOVES the set to the ledger row: source flips 2 → 1 against the
    new id, the draft's copy closes, and the returned transaction shows them.
  * `DELETE /hashtags/{id}` reaches drafts too, bumping their `version`.
  * None of it leaks into a report — those aggregate ledger rows only.

Run: .venv/bin/pytest tests/test_inbox_hashtags.py -v
"""
import json
import uuid

import pytest

from app import db
from app.constants import TransactionSource


def _idem() -> dict:
    return {"X-Idempotency-Key": str(uuid.uuid4())}


async def _junction_rows(parent_id: str) -> list[tuple]:
    """(source, hashtag_id, deleted) for one parent, both sources."""
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT transaction_source, hashtag_id, deleted_at
            FROM expense_transaction_hashtags
            WHERE transaction_id = $1
            ORDER BY transaction_source, hashtag_id
            """,
            parent_id,
        )
    return [
        (r["transaction_source"], str(r["hashtag_id"]), r["deleted_at"] is not None)
        for r in rows
    ]


async def _activity(resource_id: str) -> list[dict]:
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT action, before_snapshot, after_snapshot
            FROM activity_log WHERE resource_id = $1 ORDER BY created_at, action
            """,
            resource_id,
        )
    return [dict(r) for r in rows]


async def _cleanup(*ids: str) -> None:
    async with db.pool.acquire() as conn:
        id_list = list(ids)
        await conn.execute(
            "DELETE FROM expense_transaction_hashtags WHERE transaction_id = ANY($1::uuid[])",
            id_list,
        )
        await conn.execute(
            "DELETE FROM activity_log WHERE resource_id = ANY($1::uuid[])", id_list
        )
        await conn.execute(
            "DELETE FROM expense_transactions WHERE id = ANY($1::uuid[])", id_list
        )
        await conn.execute(
            "DELETE FROM expense_transaction_inbox WHERE id = ANY($1::uuid[])", id_list
        )


async def _create_draft(client, test_data, **overrides) -> tuple[str, dict]:
    inbox_id = overrides.pop("id", str(uuid.uuid4()))
    body = {
        "id": inbox_id,
        "title": f"draft-{uuid.uuid4().hex[:8]}",
        "amount_cents": -1500,
        "date": "2026-06-09T12:00:00Z",
        "account_id": test_data.account_id,
        "category_id": test_data.category_id,
    }
    body.update(overrides)
    r = await client.post("/v1/inbox", json=body, headers=_idem())
    assert r.status_code == 201, r.text
    return inbox_id, r.json()


# ---------------------------------------------------------------------------
# Wire shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_draft_can_be_tagged_at_creation(client, test_data):
    inbox_id, body = await _create_draft(
        client, test_data, hashtag_ids=[test_data.hashtag_id, test_data.hashtag2_id]
    )
    try:
        assert body["hashtag_ids"] == sorted(
            [test_data.hashtag_id, test_data.hashtag2_id]
        ), "sorted ascending by uuid, same convention as the ledger"

        assert await _junction_rows(inbox_id) == sorted(
            [
                (int(TransactionSource.INBOX), test_data.hashtag_id, False),
                (int(TransactionSource.INBOX), test_data.hashtag2_id, False),
            ],
            key=lambda t: t[1],
        )
    finally:
        await _cleanup(inbox_id)


@pytest.mark.asyncio
async def test_every_inbox_read_surface_carries_the_array(client, test_data):
    """`[]` on an untagged draft, the ids on a tagged one — on GET, list and
    the mutation responses alike. Never null, never absent."""
    plain_id, plain = await _create_draft(client, test_data)
    tagged_id, tagged = await _create_draft(
        client, test_data, hashtag_ids=[test_data.hashtag_id]
    )
    try:
        assert plain["hashtag_ids"] == []
        assert tagged["hashtag_ids"] == [test_data.hashtag_id]

        for item_id, expected in ((plain_id, []), (tagged_id, [test_data.hashtag_id])):
            got = await client.get(f"/v1/inbox/{item_id}")
            assert got.status_code == 200, got.text
            assert got.json()["hashtag_ids"] == expected

        listed = await client.get("/v1/inbox?limit=200")
        assert listed.status_code == 200, listed.text
        by_id = {item["id"]: item for item in listed.json()["items"]}
        assert by_id[plain_id]["hashtag_ids"] == []
        assert by_id[tagged_id]["hashtag_ids"] == [test_data.hashtag_id]
    finally:
        await _cleanup(plain_id, tagged_id)


# ---------------------------------------------------------------------------
# Validation — the one reference rule the inbox does not relax
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_hashtag_is_refused_and_no_draft_is_written(
    client, test_data
):
    """422 and nothing stored — the create and the tag write share one
    transaction, so a rejected tag rolls the draft back with it."""
    inbox_id = str(uuid.uuid4())
    ghost = str(uuid.uuid4())
    r = await client.post(
        "/v1/inbox",
        json={
            "id": inbox_id,
            "title": f"bad-tag-{uuid.uuid4().hex[:8]}",
            "hashtag_ids": [ghost],
        },
        headers=_idem(),
    )
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert ghost in error["fields"]["hashtag_ids"]

    got = await client.get(f"/v1/inbox/{inbox_id}")
    assert got.status_code == 404, "the draft must not survive its own rejection"


@pytest.mark.asyncio
async def test_a_soft_deleted_hashtag_cannot_be_attached_to_a_draft(
    client, test_data
):
    """A dead hashtag is refused at the door rather than at promote.

    Deliberately unlike `category_id`, which a draft may hold while dead. A
    category is a field on the row and promote re-checks it; a tag is a
    junction row, and `delete_hashtag` has already dropped every one of them —
    writing another would store a row no reader can see.
    """
    hashtag_id = str(uuid.uuid4())
    created = await client.post(
        "/v1/hashtags",
        json={"id": hashtag_id, "name": f"#doomed-{uuid.uuid4().hex[:6]}"},
        headers=_idem(),
    )
    assert created.status_code == 201, created.text
    deleted = await client.delete(f"/v1/hashtags/{hashtag_id}", headers=_idem())
    assert deleted.status_code == 200, deleted.text

    inbox_id = str(uuid.uuid4())
    try:
        r = await client.post(
            "/v1/inbox",
            json={
                "id": inbox_id,
                "title": f"dead-tag-{uuid.uuid4().hex[:8]}",
                "hashtag_ids": [hashtag_id],
            },
            headers=_idem(),
        )
        assert r.status_code == 422, r.text
        assert hashtag_id in r.json()["error"]["fields"]["hashtag_ids"]
    finally:
        await _cleanup(inbox_id)
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1", hashtag_id
            )
            await conn.execute("DELETE FROM expense_hashtags WHERE id = $1", hashtag_id)


@pytest.mark.asyncio
async def test_explicit_null_is_still_refused(client, test_data):
    """Null is not a verb on the inbox — `[]` is the clear operation. Same
    rule every other inbox field follows."""
    inbox_id, _ = await _create_draft(
        client, test_data, hashtag_ids=[test_data.hashtag_id]
    )
    try:
        r = await client.put(
            f"/v1/inbox/{inbox_id}", json={"hashtag_ids": None}, headers=_idem()
        )
        assert r.status_code == 422, r.text
        assert r.json()["error"]["fields"]["hashtag_ids"] == "Must not be null."
    finally:
        await _cleanup(inbox_id)


# ---------------------------------------------------------------------------
# PUT — replacement semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_replaces_clears_and_leaves_alone(client, test_data):
    inbox_id, created = await _create_draft(
        client, test_data, hashtag_ids=[test_data.hashtag_id]
    )
    try:
        version = created["version"]

        replaced = await client.put(
            f"/v1/inbox/{inbox_id}",
            json={"hashtag_ids": [test_data.hashtag2_id]},
            headers=_idem(),
        )
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["hashtag_ids"] == [test_data.hashtag2_id]
        assert replaced.json()["version"] > version, (
            "a tags-only edit changes the draft as clients see it, so version moves"
        )
        version = replaced.json()["version"]

        # An unrelated edit leaves the set alone and still reports it.
        renamed = await client.put(
            f"/v1/inbox/{inbox_id}", json={"title": "renamed"}, headers=_idem()
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["hashtag_ids"] == [test_data.hashtag2_id]

        cleared = await client.put(
            f"/v1/inbox/{inbox_id}", json={"hashtag_ids": []}, headers=_idem()
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["hashtag_ids"] == []

        # Re-attaching the original reuses the junction row rather than piling
        # up a second one — the ON CONFLICT upsert's stable-id property.
        reattached = await client.put(
            f"/v1/inbox/{inbox_id}",
            json={"hashtag_ids": [test_data.hashtag_id]},
            headers=_idem(),
        )
        assert reattached.status_code == 200, reattached.text
        assert reattached.json()["hashtag_ids"] == [test_data.hashtag_id]
        assert len(await _junction_rows(inbox_id)) == 2, (
            "one row per (draft, hashtag) pair, live or not — no phantom rows"
        )
    finally:
        await _cleanup(inbox_id)


@pytest.mark.asyncio
async def test_the_update_snapshots_show_the_tag_change(client, test_data):
    """before/after must bracket the edit — the junction table writes no
    activity rows of its own (§6 aggregate exception #1)."""
    inbox_id, _ = await _create_draft(
        client, test_data, hashtag_ids=[test_data.hashtag_id]
    )
    try:
        r = await client.put(
            f"/v1/inbox/{inbox_id}",
            json={"hashtag_ids": [test_data.hashtag2_id]},
            headers=_idem(),
        )
        assert r.status_code == 200, r.text

        entries = await _activity(inbox_id)
        updated = [e for e in entries if e["action"] == 2]
        assert len(updated) == 1
        before = json.loads(updated[0]["before_snapshot"])
        after = json.loads(updated[0]["after_snapshot"])
        assert before["hashtag_ids"] == [test_data.hashtag_id]
        assert after["hashtag_ids"] == [test_data.hashtag2_id]
    finally:
        await _cleanup(inbox_id)


# ---------------------------------------------------------------------------
# Dismiss — one-way, tags included
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dismissing_a_draft_closes_its_tags(client, test_data):
    inbox_id, _ = await _create_draft(
        client, test_data, hashtag_ids=[test_data.hashtag_id]
    )
    try:
        r = await client.delete(f"/v1/inbox/{inbox_id}", headers=_idem())
        assert r.status_code == 200, r.text
        assert r.json()["hashtag_ids"] == [], "post-delete wire state"

        assert await _junction_rows(inbox_id) == [
            (int(TransactionSource.INBOX), test_data.hashtag_id, True)
        ], "soft-deleted, not erased"

        listed = await client.get("/v1/inbox?include_deleted=true&limit=200")
        dismissed = next(
            item for item in listed.json()["items"] if item["id"] == inbox_id
        )
        assert dismissed["hashtag_ids"] == []
    finally:
        await _cleanup(inbox_id)


@pytest.mark.asyncio
async def test_the_delete_snapshot_remembers_what_was_tagged(client, test_data):
    """With no restore route, the activity log is the only surviving record of
    a dismissed draft's tags — so the before-snapshot must be captured ahead of
    the cascade."""
    inbox_id, _ = await _create_draft(
        client, test_data, hashtag_ids=[test_data.hashtag_id]
    )
    try:
        await client.delete(f"/v1/inbox/{inbox_id}", headers=_idem())

        deleted = [e for e in await _activity(inbox_id) if e["action"] == 3]
        assert len(deleted) == 1
        assert json.loads(deleted[0]["before_snapshot"])["hashtag_ids"] == [
            test_data.hashtag_id
        ]
        assert json.loads(deleted[0]["after_snapshot"])["hashtag_ids"] == []
    finally:
        await _cleanup(inbox_id)


# ---------------------------------------------------------------------------
# Promote — the tags move
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_carries_the_tags_into_the_ledger(client, test_data):
    inbox_id, _ = await _create_draft(
        client,
        test_data,
        hashtag_ids=[test_data.hashtag_id, test_data.hashtag2_id],
    )
    txn_id = str(uuid.uuid4())
    try:
        r = await client.post(
            f"/v1/inbox/{inbox_id}/promote", json={"id": txn_id}, headers=_idem()
        )
        assert r.status_code == 200, r.text
        expected = sorted([test_data.hashtag_id, test_data.hashtag2_id])
        assert r.json()["hashtag_ids"] == expected

        # Ledger rows live, draft's copies closed — one live set, not two.
        assert await _junction_rows(txn_id) == [
            (int(TransactionSource.LEDGER), h, False) for h in expected
        ]
        assert await _junction_rows(inbox_id) == [
            (int(TransactionSource.INBOX), h, True) for h in expected
        ]

        # And the ledger read surface agrees.
        got = await client.get(f"/v1/transactions/{txn_id}")
        assert got.json()["hashtag_ids"] == expected
    finally:
        await _cleanup(inbox_id, txn_id)


@pytest.mark.asyncio
async def test_promoting_onto_the_drafts_own_uuid_still_tags_the_ledger_row(
    client, test_data
):
    """The case the old two-column UNIQUE key broke (sql/033).

    Nothing forbids a client reusing the draft's uuid for the ledger row — the
    two tables have separate id spaces. Under the old key the ledger-side
    upsert arbitrated against the *inbox* junction row, found it active, did
    nothing, and produced an untagged ledger row with no error at all.
    """
    inbox_id, _ = await _create_draft(
        client, test_data, hashtag_ids=[test_data.hashtag_id]
    )
    try:
        r = await client.post(
            f"/v1/inbox/{inbox_id}/promote", json={"id": inbox_id}, headers=_idem()
        )
        assert r.status_code == 200, r.text
        assert r.json()["id"] == inbox_id
        assert r.json()["hashtag_ids"] == [test_data.hashtag_id], (
            "the promoted row must carry the tag it inherited"
        )

        assert await _junction_rows(inbox_id) == [
            (int(TransactionSource.LEDGER), test_data.hashtag_id, False),
            (int(TransactionSource.INBOX), test_data.hashtag_id, True),
        ]
    finally:
        await _cleanup(inbox_id)


@pytest.mark.asyncio
async def test_an_untagged_draft_promotes_unchanged(client, test_data):
    """Tags are not part of readiness."""
    inbox_id, _ = await _create_draft(client, test_data)
    txn_id = str(uuid.uuid4())
    try:
        r = await client.post(
            f"/v1/inbox/{inbox_id}/promote", json={"id": txn_id}, headers=_idem()
        )
        assert r.status_code == 200, r.text
        assert r.json()["hashtag_ids"] == []
        assert await _junction_rows(txn_id) == []
    finally:
        await _cleanup(inbox_id, txn_id)


# ---------------------------------------------------------------------------
# Hashtag deletion reaches drafts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleting_a_hashtag_untags_drafts_and_bumps_their_version(
    client, test_data
):
    """The bug the pre-2026-08-14 cascade would have had: it bumped
    `expense_transactions` by id array only, so an inbox parent's ids matched
    nothing and a draft's `hashtag_ids` changed on the wire behind a stale
    `version`."""
    hashtag_id = str(uuid.uuid4())
    created = await client.post(
        "/v1/hashtags",
        json={"id": hashtag_id, "name": f"#cascade-{uuid.uuid4().hex[:6]}"},
        headers=_idem(),
    )
    assert created.status_code == 201, created.text

    inbox_id, draft = await _create_draft(
        client, test_data, hashtag_ids=[hashtag_id]
    )
    try:
        before_version = draft["version"]

        r = await client.delete(f"/v1/hashtags/{hashtag_id}", headers=_idem())
        assert r.status_code == 200, r.text

        got = await client.get(f"/v1/inbox/{inbox_id}")
        assert got.json()["hashtag_ids"] == []
        assert got.json()["version"] > before_version, (
            "the draft's wire shape changed, so its version must have moved"
        )
    finally:
        await _cleanup(inbox_id)
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM activity_log WHERE resource_id = $1", hashtag_id
            )
            await conn.execute("DELETE FROM expense_hashtags WHERE id = $1", hashtag_id)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_another_tenants_hashtag_is_not_attachable(client, test_data):
    """`sync_hashtags` scopes its validation by user_id — a foreign id is
    indistinguishable from a nonexistent one, which is the correct answer."""
    other_user = str(uuid.uuid4())
    other_hashtag = str(uuid.uuid4())
    async with db.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, display_name, created_at, updated_at) "
            "VALUES ($1, $2, now(), now())",
            other_user, f"other-{uuid.uuid4().hex[:8]}",
        )
        await conn.execute(
            """INSERT INTO expense_hashtags
                (id, user_id, name, sort_order, created_at, updated_at)
               VALUES ($1, $2, '#theirs', 1, now(), now())""",
            other_hashtag, other_user,
        )

    inbox_id = str(uuid.uuid4())
    try:
        r = await client.post(
            "/v1/inbox",
            json={
                "id": inbox_id,
                "title": f"cross-tenant-{uuid.uuid4().hex[:8]}",
                "hashtag_ids": [other_hashtag],
            },
            headers=_idem(),
        )
        assert r.status_code == 422, r.text
        assert other_hashtag in r.json()["error"]["fields"]["hashtag_ids"]
    finally:
        await _cleanup(inbox_id)
        async with db.pool.acquire() as conn:
            await conn.execute("DELETE FROM expense_hashtags WHERE user_id = $1", other_user)
            await conn.execute("DELETE FROM users WHERE id = $1", other_user)
