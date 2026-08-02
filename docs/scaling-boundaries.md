# Scaling Boundaries

> **Purpose:** this repo targets **one user — the owner** (decided 2026-08-01, reversing the earlier 1000+ public-users framing). If scaling ever happens it will be a deliberate, professionally-staffed effort with its own plan. This document exists so that effort has a map: it separates what is **business logic** (true at any scale, never to be traded away) from what is a **scaling constraint** (shaped by "one user, one Mac", and the only thing a scaling project needs to touch).
>
> **The rule:** when you write or defer something *because* the system serves one user, it belongs in the second table below. Never leave a scale-conditioned decision implicit in the code — an unlabelled one is indistinguishable from a bug when someone revisits it in two years.

---

## Business logic — scale-invariant

These are the ledger's truth. They hold identically for 1 user and 1,000,000, and a scaling project must preserve every one of them unchanged. None of these is a performance trade-off; they are correctness.

| Invariant | Where |
|---|---|
| Sign convention — signed request → typed storage (`transaction_type`, `transfer_direction`) → positive response | [engine-spec.md](engine-spec.md), CLAUDE.md |
| Home-currency amount alongside every amount; the engine is the only converter | [api-design-principles.md](api-design-principles.md) |
| Null over omission — response shape never varies with data presence | [api-design-principles.md](api-design-principles.md) |
| Soft delete everywhere; financial records are never hard-deleted | all mutable tables |
| Activity log on every mutation, with before/after snapshots | [app/helpers/activity_log.py](../app/helpers/activity_log.py) |
| Idempotency keys on all writes, 24h TTL, duplicates replay verbatim | [app/helpers/idempotency.py](../app/helpers/idempotency.py) |
| UUID-first — client generates the ID before the write | all `POST` endpoints |
| Balance updates atomic with the transaction that causes them | [app/helpers/balance.py](../app/helpers/balance.py) |
| Batch = all or nothing, one DB transaction | `POST /transactions/batch` |
| Transfer pairing + cross-currency zero-sum via the dominant-side rule | [app/helpers/transfers.py](../app/helpers/transfers.py), [api-design-principles.md §12](api-design-principles.md) |
| Reconciliation state machine + transaction field locking when completed | [app/helpers/reconciliations.py](../app/helpers/reconciliations.py) |
| Inbox promotion — atomic inbox→ledger with both activity entries | [app/helpers/inbox.py](../app/helpers/inbox.py) |
| System categories resolved by `system_key`, never by display name | [app/constants.py](../app/constants.py), `sql/010` |
| `@Opening` excluded from flow reports; visible rows sum exactly to `net_cents` | [app/helpers/monthly_report.py](../app/helpers/monthly_report.py) |
| FX resolution carries the last rate forward (`rate_date <= $1 DESC LIMIT 1`); a genuine miss is `422 RATE_UNAVAILABLE`, never a silent `1.0` | [app/helpers/exchange_rate.py](../app/helpers/exchange_rate.py) |

**Home currency is fixed at PEN and is not a scaling boundary — it is a retired capability.** `sql/018` locks `user_settings.main_currency` to `'PEN'`; `PUT /auth/settings` rejects the field with `422`. The recalculation helper that rewrote `amount_home_cents` across the ledger on a switch was **deleted** on 2026-08-01, not deferred, because it carried a silent `1.0` rate fallback (audit WP1.1) that wrote wrong home amounts whenever the FX table had no row for a transaction's date.

This is *not* a "revisit at scale" row. A second user does not want a different home currency — they want their own engine, or a real multi-tenancy design in which home currency is per-user and the conversion happens at read time. Reviving the deleted helper would be the wrong move either way; the correct restoration path is documented in the `sql/018` header.

The part that *was* genuine business logic — the dominant-side rule keeping cross-currency transfer pairs zero-sum — survives in [transfers.py](../app/helpers/transfers.py) and is unaffected.

---

## Scaling constraints — single-user-shaped

Each row is safe today and would need revisiting *only* if the user count grows. Nothing here is a known defect at current scale.

| # | Constraint | Today | At scale |
|---|---|---|---|
| 1 | *(Retired 2026-08-01)* **Recalc execution model** — the helper this row described was deleted with the home-currency switch. Row number kept so rows 2-5 keep their identifiers in older references. | n/a | n/a |
| 2 | **Deployment shape** — single Mac, launchd, one Homebrew Postgres, no pooler | [deploy/local/](../deploy/local/) | [deploy/cloud/](../deploy/cloud/) is the reactivation checklist. **This is the pattern to copy for everything else in this table** — the boundary is already physical, not just documented |
| 3 | **Connection pool sizing** — `db_pool_min_size=5`, `db_pool_max_size=20` ([config.py:27-28](../app/config.py#L27-L28)) | Sized for a direct connection where each slot pins a backend | Raise to ~50 behind a transaction-mode pooler; `statement_cache_size=0` is already set for pgBouncer compatibility |
| 4 | **Tenant isolation** — RLS policies (`auth.uid() = user_id`) exist and `rowsecurity` is on for all 15 tables, but they are **inert**: the engine connects as the table owner (`ternero`) and `relforcerowsecurity` is `false`, so Postgres bypasses RLS entirely. Isolation today rests wholly on engine-level `user_id` scoping in every query (verified 2026-08-01) | Correct — every query is scoped, and there is only one user to isolate | Either connect as a non-owner role, or `ALTER TABLE ... FORCE ROW LEVEL SECURITY`. Do this *before* a second user exists, not after. Until then, engine-side scoping is the only guard and should be reviewed as load-bearing security, not defence in depth |
| 5 | **Currency scope** — `sql/015` locks currencies to USD/PEN; cross-rate (non-USD ↔ non-USD) is unsupported and `get_pair_rate` returns `None` | The owner lives PEN/USD | Lift the CHECK, implement true cross-rate math, widen the FX job's target list |
| 6 | **Sync response size** — `GET /sync` returns the entire delta in one response; no pagination, limit, or cursor | One user's delta is small | Cursoring, or a `has_more` + continuation token |
| 7 | **No rate limiting** — no middleware, no per-token quota | Single trusted caller on `127.0.0.1` | Required before any public exposure |
| 8 | **No job runtime** — no worker, no queue; every operation is request-synchronous | Nothing needs to outlive a request | Needed by #1's async path and by any future import/report job |
| 9 | **Backups** — nightly `pg_dump` to Google Drive from one machine, 30-day rotation, manual restore drill | [deploy/local/backup.sh](../deploy/local/backup.sh) | Managed PITR; the restore drill becomes automated rather than a calendar reminder |
| 10 | **PAT management** — no `GET /auth/pat` list endpoint, no `last_used_at` tracking (the latter deliberately, to avoid a write on every authenticated request) | Two tokens, both known | Both ship when a management UI exists — recorded in [engine-spec.md](engine-spec.md) §Auth |

---

## Adding to this document

When a decision is made because the system serves one user, add a row to the second table with the same three columns: what the constraint is (with a `file:line` link), why it is safe today, and what replaces it at scale. If a decision is about correctness instead, it belongs in the first table and in [engine-spec.md](engine-spec.md) — not here.

A useful test for which table something belongs in: **would a bug report from a second user be about this?** If yes, it is a scaling constraint. If a single user could hit it and get a wrong number, it is business logic.

*Created 2026-08-01, when the target narrowed from 1000+ public users to the owner alone.*
