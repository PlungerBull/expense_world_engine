"""The ``expense_transaction_hashtags`` junction table — one implementation,
two parents.

Every read and write of the junction table lives here. The table is
deliberately polymorphic: ``transaction_id`` carries no foreign key and
``transaction_source`` says which table it names — ``LEDGER`` (1) for an
``expense_transactions`` row, ``INBOX`` (2) for an
``expense_transaction_inbox`` draft. Callers pass their own source and never
see the column otherwise.

## Why this is a module and not two copies

Until 2026-08-14 this code was five private functions inside
``helpers/transactions.py`` with ``LEDGER`` hardcoded, because the ledger was
the only writer. Shipping the inbox writer meant either a source parameter or a
second copy of the upsert — and a second copy is this project's documented
failure mode (four copies of the sign matrix drifting, `helpers/balance`'s twin
sprouting a third inline). It is a module rather than a widened
``helpers/transactions`` because ``transactions.sync_hashtags`` writing inbox
rows is a misnomer a future reader would report as a bug.

## The source predicate is not optional

Every statement below is scoped by ``transaction_source`` as well as
``transaction_id``. Two rows with the same uuid in different sources are
legal — the ledger and the inbox have independent id spaces, and
``POST /inbox/{id}/promote`` lets a client hand the new ledger row the draft's
own uuid — so an unscoped statement would reach across the boundary. sql/033
widened the UNIQUE key to ``(transaction_id, transaction_source, hashtag_id)``
for the same reason; its header has the worked example of what the two-column
key silently did to a promoted draft.

``tests/test_hashtag_source_filter.py`` pins the predicate's presence in every
module that names the table.

## Activity log — deliberate aggregation exception

Junction rows are mutated here without per-row ``activity_log`` entries. The
parent's snapshot carries the resulting ``hashtag_ids`` list, so the change is
captured at parent granularity (api-design-principles.md §6 exception #1). Two
consequences callers must respect: capture the before-snapshot's tags *before*
a cascade, and the after-snapshot's *after* it.

## Parent version bumps are the caller's job

A junction mutation changes what the parent looks like on the wire, so the
parent's ``version`` and ``updated_at`` must move in the same DB transaction.
Callers that already run a ``dynamic_update``/``soft_delete`` on the parent get
this for free; ``hashtags.delete_hashtag``, which pivots on ``hashtag_id``
across many parents of both kinds, bumps them itself.
"""

from typing import Optional

import asyncpg

from app.constants import TransactionSource
from app.errors import validation_error


async def fetch_hashtag_ids_map(
    conn: asyncpg.Connection,
    parent_ids: list[str],
    source: TransactionSource,
) -> dict[str, list[str]]:
    """Resolve active hashtag IDs for a set of parents of one source.

    Returns ``{parent_id: [hashtag_id, ...]}`` with each list sorted
    ascending by UUID (one stable convention everywhere). Soft-deleted
    junction rows are excluded — when a parent is soft-deleted its junctions
    cascade-soft-delete, so deleted parents resolve to ``[]``.

    Returns an empty mapping for an empty input — never queries.
    """
    if not parent_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT transaction_id, hashtag_id
        FROM expense_transaction_hashtags
        WHERE transaction_id = ANY($1::uuid[])
          AND transaction_source = $2
          AND deleted_at IS NULL
        ORDER BY transaction_id, hashtag_id
        """,
        parent_ids,
        int(source),
    )
    result: dict[str, list[str]] = {str(pid): [] for pid in parent_ids}
    for r in rows:
        result[str(r["transaction_id"])].append(str(r["hashtag_id"]))
    return result


async def attach_hashtag_ids(
    conn: asyncpg.Connection,
    payload,
    source: TransactionSource = TransactionSource.LEDGER,
) -> None:
    """Mutate one parent dict (or a list of them) to include ``hashtag_ids``.

    Per api-design-principles.md §3a, every endpoint returning a transaction
    or an inbox item flattens the junction relationship to an embedded array.
    Call this at each response site after building the dict. One query covers
    a whole list — list endpoints pay a single round trip regardless of page
    size.

    ``source`` defaults to ``LEDGER``: the ledger has nine call sites to the
    inbox's four, and a wrong default on the inbox side is loud (a draft's
    tags read as ``[]``) rather than silent.
    """
    items = [payload] if isinstance(payload, dict) else list(payload)
    if not items:
        return
    ids = [item["id"] for item in items]
    hashtag_map = await fetch_hashtag_ids_map(conn, ids, source)
    for item in items:
        item["hashtag_ids"] = hashtag_map.get(item["id"], [])


async def cascade_delete(
    conn: asyncpg.Connection,
    user_id: str,
    parent_id: str,
    source: TransactionSource,
    *,
    keep_hashtag_ids: Optional[list[str]] = None,
) -> None:
    """Soft-delete a parent's active junction rows within one source.

    The single producer of junction ``deleted_at`` markers — used by the
    delete cascades and by ``sync_hashtags`` step 1.

    **The marker is load-bearing.** Postgres ``now()`` returns
    ``transaction_timestamp()`` — one value per DB transaction — so every
    junction row soft-deleted here carries the exact timestamp the parent row
    got in the same transaction. ``cascade_restore`` re-activates by exact
    ``deleted_at`` match against the parent's marker, which catches precisely
    the rows this cascade dropped and not soft-deleted junctions left by
    earlier ``sync_hashtags`` runs.

    ``keep_hashtag_ids`` narrows the cascade to rows *leaving* the active set
    (rows staying attached get no updated_at bump for nothing). An empty or
    omitted list makes ``<> ALL`` vacuously TRUE — everything active is
    dropped.

    Deliberate non-adopter: ``hashtags.delete_hashtag`` pivots on
    ``hashtag_id`` across all parents, spans both sources, and needs
    ``RETURNING transaction_id, transaction_source`` — a different operation,
    not another copy of this one.
    """
    await conn.execute(
        """
        UPDATE expense_transaction_hashtags
        SET deleted_at = now(), updated_at = now()
        WHERE transaction_id = $1
          AND transaction_source = $4
          AND user_id = $2
          AND deleted_at IS NULL
          AND hashtag_id <> ALL($3::uuid[])
        """,
        parent_id,
        user_id,
        keep_hashtag_ids or [],
        int(source),
    )


async def cascade_restore(
    conn: asyncpg.Connection,
    user_id: str,
    parent_id: str,
    source: TransactionSource,
    deleted_at_marker,
) -> None:
    """Re-activate the junction rows cascaded by THIS parent's delete.

    Matches ``deleted_at = $marker`` exactly, with ``$marker`` bound to the
    parent's pre-restore ``deleted_at`` — see ``cascade_delete`` for why the
    equality is precise.

    Only the ledger calls this: the inbox has no restore route (a dismissed
    draft is final, owner decision 2026-08-14), so an inbox cascade is
    one-way.
    """
    await conn.execute(
        """
        UPDATE expense_transaction_hashtags
        SET deleted_at = NULL, updated_at = now()
        WHERE transaction_id = $1 AND transaction_source = $4
          AND user_id = $2 AND deleted_at = $3
        """,
        parent_id,
        user_id,
        deleted_at_marker,
        int(source),
    )


async def sync_hashtags(
    conn: asyncpg.Connection,
    parent_id: str,
    user_id: str,
    hashtag_ids: Optional[list[str]],
    source: TransactionSource,
) -> None:
    """Make the parent's active hashtag set exactly ``hashtag_ids``.

    Replacement semantics, not delta semantics. The active set after this
    call equals ``hashtag_ids`` regardless of what was attached before.

    Every id must reference an active, caller-owned hashtag — the same rule
    on both sources. A draft may point at an inactive *category* and be
    refused only at promote (the inbox's one relaxed reference rule); tags
    get no such carve-out, because a soft-deleted hashtag's junctions are
    cascade-dropped by ``delete_hashtag`` anyway, so admitting one here would
    write a row that the next read cannot see.

    Implementation: a narrowed soft-delete drops only the rows *leaving* the
    active set, and an ``ON CONFLICT DO UPDATE`` upsert handles the rows
    joining or staying. Two key properties fall out:

      1. **Re-attach safety.** The junction table's UNIQUE key is
         unconditional — soft-deleted rows still occupy the slot. The
         previous "soft-delete-everything + plain INSERT" pattern hit a
         UNIQUE violation any time the new set overlapped with the old set
         (e.g. PUT ``[A]`` → ``[A, B]``) or re-attached a previously-deleted
         hashtag. ``ON CONFLICT DO UPDATE`` flips ``deleted_at`` back to NULL
         on the existing row instead.

      2. **Stable junction IDs.** Attach → detach → re-attach cycles keep the
         same junction row (one row per logical pair forever), instead of
         accumulating N+1 rows per cycle — a single junction lifecycle, not
         phantom rows.

    The ``DO UPDATE`` clause only fires on rows that were soft-deleted
    (``WHERE expense_transaction_hashtags.deleted_at IS NOT NULL``), so rows
    that are already active are left fully untouched — no ``updated_at``
    churn on no-op transitions.

    The conflict target names all three key columns (sql/033). Two columns
    would arbitrate across sources: promoting a tagged draft onto its own
    uuid would match the *inbox* row and leave the ledger row untagged, with
    no error. sql/033's header works the case through.
    """
    if hashtag_ids:
        valid = await conn.fetch(
            """
            SELECT id FROM expense_hashtags
            WHERE id = ANY($1::uuid[])
              AND user_id = $2
              AND deleted_at IS NULL
            """,
            hashtag_ids,
            user_id,
        )
        valid_ids = {str(r["id"]) for r in valid}
        # str() both sides: request models supply uuid.UUID since open-bugs
        # 6.6 closed; a UUID is never `in` a set[str].
        invalid = [str(h) for h in hashtag_ids if str(h) not in valid_ids]
        if invalid:
            raise validation_error(
                "Some hashtag IDs are invalid.",
                {"hashtag_ids": f"Invalid IDs: {', '.join(invalid)}"},
            )

    # Step 1: soft-delete the junctions *leaving* the active set.
    await cascade_delete(
        conn, user_id, parent_id, source, keep_hashtag_ids=hashtag_ids,
    )

    # Step 2: upsert the new set in one statement. ON CONFLICT re-activates
    # rows that exist but were soft-deleted; rows that don't exist get plain
    # INSERT semantics; rows that are already active are skipped via the
    # WHERE on DO UPDATE (no churn).
    if hashtag_ids:
        await conn.execute(
            """
            INSERT INTO expense_transaction_hashtags
                (transaction_id, transaction_source, hashtag_id, user_id, created_at, updated_at)
            SELECT $1, $4, hashtag_id, $2, now(), now()
            FROM unnest($3::uuid[]) AS hashtag_id
            ON CONFLICT (transaction_id, transaction_source, hashtag_id) DO UPDATE
            SET deleted_at = NULL,
                updated_at = now()
            WHERE expense_transaction_hashtags.deleted_at IS NOT NULL
            """,
            parent_id,
            user_id,
            hashtag_ids,
            int(source),
        )
