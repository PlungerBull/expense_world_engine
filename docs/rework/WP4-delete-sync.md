# WP4 — Delete `/sync` and the checkpoint table

**Read [`README.md`](README.md) first. Requires WP3 to have landed.**

> **Why the dependency is real.** Every domain table's only non-unique index is
> `(user_id, updated_at)`, which exists to serve this endpoint. Deleting it before WP3 has
> added the replacement indexes leaves `expense_transactions` with nothing but its primary
> key, and every query in the engine becomes a sequential scan. **Confirm WP3's indexes
> exist before you drop anything.**

---

## The case for deleting it

`GET /sync` hands a client the whole delta since a token it presents, rotates the token,
and records the position in `sync_checkpoints`. It was built to serve an offline-capable
iOS app that keeps a local replica.

**`sync_checkpoints` holds zero rows. No client has ever completed a sync against this
database.** The CLI — the only client — talks to `127.0.0.1` over HTTP and uses the direct
REST endpoints. A local-first *engine* is not the same thing as an offline-first *client*.

What it costs to keep, as of 2026-08-04:

| Item | Size |
|---|---|
| `app/helpers/sync.py` | 221 lines |
| `app/routers/sync.py` | 103 lines |
| `tests/test_sync.py` | 335 lines |
| `sync_checkpoints` | 1 table, 7 columns |
| `(user_id, updated_at)` indexes | 7, across every mutable table |
| Open bug **3.1** | 🔴 critical — *can permanently drop committed writes* |

That last row matters. 3.1 is a dropped-writes bug, which is the most serious class of
defect a ledger can have, and it is critical work for as long as the endpoint exists.
**Deleting the endpoint deletes the bug.**

## What is decided

- Delete `GET /sync`, `sync_checkpoints`, `app/helpers/sync.py`, `app/routers/sync.py`,
  the sync schemas, `tests/test_sync.py`, and the `X-Client-Id` header handling.
- Delete the seven `idx_*_user_updated` indexes.
- **Keep `version` and `updated_at` on every mutable table.** They are load-bearing for
  optimistic concurrency and for ordinary auditing, entirely independently of sync. This is
  the distinction that makes the deletion reversible.
- Keep soft-delete tombstones. They are how history works, not a sync artifact.

## Why this is safe to reverse

This package deletes an **implementation**, not a **substrate**. Everything sync was built
on stays: `version`, `updated_at`, `deleted_at` tombstones on every table, and
client-generated UUIDs so a client can create rows offline without coordination.

Rebuilding sync later means writing delta queries against a schema that already supports
them — roughly 1–2 days, purely additive, no data migration and no risk to existing rows.
That is why this deletion was approved while `users` / `user_id` was not: one is a feature
on a foundation, the other is the foundation.

**Do not remove any of that substrate as "now unused".** If you find yourself deleting
`version` or `updated_at`, stop — you are outside this package.

## What you must work out

- **Whether anything else reads the sync helpers.** `helpers/sync.py` may contain
  serialisation shared with other routers. Check before deleting wholesale.
- **What `X-Client-Id` touches.** It may be validated or logged in shared middleware rather
  than only in the sync router.
- **Whether `resolve_home_rates` was reachable from here.** The audit noted it is called
  from both `/reconciliations` and `/sync`, and that it selects accounts **with no
  `user_id` filter** — a tenancy defect under `CLAUDE.md`'s rules. WP2 may already have
  removed it. If it still exists after your deletion, the missing filter is now a
  single-entry-point bug and must be fixed or handed to WP6, not left.
- **Which indexes are genuinely sync-only.** WP3 added a replacement set; confirm no
  remaining query depends on `(user_id, updated_at)` before dropping the seven. If one
  does, keep that single index and say why.
- **Whether the CLI references `/sync` at all.** The audit found it does not
  (`../expense_world_CLI`), but verify rather than assume.

## Where to look

```bash
grep -rn "sync" app/ tests/ --include="*.py" | grep -vi "async"
grep -rn "X-Client-Id\|client_id\|sync_token" app/
```

```sql
SELECT indexname FROM pg_indexes
WHERE schemaname = 'public' AND indexdef LIKE '%updated_at%';
```

| File | Role |
|---|---|
| `app/helpers/sync.py` | Delta reads, token rotation, `REPEATABLE READ` wrapper, tombstone semantics |
| `app/routers/sync.py` | The single route |
| `app/schemas/sync.py` | Wire shapes |
| `tests/test_sync.py` | 335 lines, all deleted |
| `docs/engine-spec.md` | The endpoint's specification — note what you invalidated for WP7 |

## Invariants that must survive

- **`version` and `updated_at` remain on every mutable table and remain maintained.**
  Optimistic concurrency does not change.
- Soft deletes remain soft. No tombstone is hard-deleted as part of this.
- Route count goes from 61 to 60 (before WP5's four archive routes). Every remaining route
  still requires a PAT; `/health` remains the only public endpoint.

## Definition of done

- [ ] `GET /sync` returns 404. No sync module remains in `app/`.
- [ ] `sync_checkpoints` dropped by migration.
- [ ] The seven `(user_id, updated_at)` indexes dropped — **and WP3's replacements verified
      present first**.
- [ ] `version` and `updated_at` untouched on all mutable tables; a test still proves
      optimistic concurrency works.
- [ ] `pytest` green with `test_sync.py` deleted, not skipped.
- [ ] Open bug **3.1** deleted from `docs/open-bugs.md`.
- [ ] Entry appended to `docs/client-breaking-changes.md` — the endpoint is gone. Note that
      the CLI never called it, so the practical impact is nil.
- [ ] Note in your summary that `engine-spec.md` still documents the endpoint, for WP7.

## Out of scope

- Removing `version` / `updated_at` / `deleted_at`. **Explicitly forbidden here.**
- Bug **4.1** (expired idempotency keys duplicate writes). It is in `idempotency_keys`,
  which is deliberately kept — retries are the one failure mode whose consequence is
  silently wrong money, and loopback does not eliminate them. Unrelated to sync, and it
  survives this program.
- Any judgement about whether an iOS app will ever exist. That question was already
  answered: if one appears, sync gets rebuilt on the substrate that stays.
