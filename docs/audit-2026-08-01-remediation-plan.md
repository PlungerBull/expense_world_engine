# Audit Remediation Plan — 2026-08-01

Source: four parallel audits (business logic vs `engine-spec.md`, coding patterns vs
`api-design-principles.md`, bloat/DRY, doc+schema drift). Full coverage: all ~60
non-test `.py` files under `app/`, every router/helper/schema, the spec (950 lines),
`schema-reference.md` (722 lines), and all 17 migrations.

**How to use this document.** Each Work Package (WP) is a self-contained assignment:
one coder per WP, no two WPs edit the same functions (exceptions flagged in
"Conflicts"). Every task lists the finding, where it lives, how to **validate** it is
real (do this first — findings are agent-reported and should be independently
confirmed), and the recommended fix. Severity: 🔴 critical (can corrupt stored data,
bypass auth, or lose writes), 🟠 high, 🟡 medium, ⚪ low.

**Recommended order:** WP1 → (WP2, WP3, WP4, WP5, WP7 in parallel) → WP6 → WP8 →
WP9 → WP10 → WP11. WP9 (refactors) must wait until the logic fixes land — refactoring
code that is about to change guarantees conflicts. WP11 (docs) goes last because
several doc entries depend on decisions recorded in WP0.

---

## WP0 — Owner decisions required (no code until decided)

These are genuine design forks the audits surfaced. Each blocks one or more tasks
below; decide and record here (and later in the spec / `scaling-boundaries.md`).

| # | Decision | Blocks |
|---|---|---|
| D1 | ~~Missing-rate policy for full-ledger recalculation~~ — **VOID 2026-08-01: the recalculation no longer exists.** Home currency is locked to PEN (`sql/018`) and the helper was deleted, so there is no missing-rate policy to decide. Original decision, kept for the record: *abort.* If any row lacks a rate, the whole `main_currency` switch rolls back and the request returns `422 RATE_UNAVAILABLE`; `main_currency` stays unchanged. No partial switch, no `missing_rate_rows` skip counter, no 1.0 fallback. Matches §127. | WP1.1 |
| D2 | ~~§11 "IDs-only" vs hydrated names on aggregates~~ — **DECIDED: strict IDs-only, no carve-out.** Drop `name` from `routers/dashboard.py:86,156,203` and `helpers/monthly_report.py:179`; the CLI resolves names from its replica exactly as it already does for hashtags (`hashtag_label(ids, name_map)`). One rule, zero exceptions. **The carve-out was rejected deliberately** — a second class of endpoint is how a standing rule rots. *Soundness confirmed, not assumed:* nothing holding transactions is deletable, only archivable — `delete_category` and `delete_account` both 409 while active transactions reference them, and transaction restore revalidates the account/category is still active. Archived rows keep `deleted_at IS NULL` and so ship in a full-refresh `/sync`. Therefore every ID a report emits is resolvable by a fresh client, and no sync change is needed. | WP10.3, WP11 |
| D3 | ~~Reconciliation cascade vs the §646 completed-row lock~~ — **DECIDED: neither. Retire chaining entirely.** Explicit stored values, a one-time prefill suggestion on POST, and a read-time `continuity_gap_cents` that surfaces where the chain breaks instead of silently repairing it. Deletes `_cascade_chained_recalc` and with it findings **5.1, 5.2 and 5.4**. Scheduled as the open item in [TODO.md](../TODO.md) — do it now, both tables hold 0 rows so the freeze-existing-rows migration is a no-op. | WP5.2 (removed), TODO |
| D4 | ~~Missing `activity_log` rows on balance writes and reconciliation side-effects~~ — **DECIDED: documented exception for balances, real entries for the reconciliation side-effects** (delete-cascade unassign, insert-at-position `sort_order` shift). The balance is derived and only ever moves because a transaction moved — that transaction's own entry already answers the audit question; logging both doubles volume for nothing. *Derived-balance alternative measured and declined:* on PG17 with 200k transactions / 8 accounts, one account's balance sums in **3.9 ms** and all eight in **19.3 ms** — ~30 years of use, so speed is not the obstacle. Declined because the stored balance's only real failure mode is drift, and the audit found none (see "Verified clean"); it is a large refactor against a bug that does not exist. Record the numbers and the escape hatch in `scaling-boundaries.md`. | WP8.1 |
| D5 | ~~Promotion logged as `DELETED`, identical to dismissal~~ — **DECIDED: add `PROMOTED` action code (5).** Promotion is a distinct user action and the feed must say so; `UPDATED` would blend it into ordinary inbox edits. Include the new value in the `activity_log.action` CHECK constraint landing in WP6.3. | WP8.3 |
| D6 | ~~PAT plaintext persisting 24 h in `idempotency_keys.response_snapshot`~~ — **DECIDED: exempt `POST /auth/pat` from snapshot storage.** "The engine stores only the SHA-256 hash" (§173) is the security property; a 24-hour plaintext window cancels it. Store the key with a stub so a retry returns **409 CONFLICT** ("a token was already created with this key; mint a new one") rather than storing nothing and silently minting a second token. Update `tests/test_pat.py:83-104`, which currently asserts the plaintext replay. | WP2.4 |
| D7 | ~~Person accounts uncreatable~~ — **DECIDED: park.** A feature gap, not a defect: nothing is broken, a capability is absent. Doc-only follow-through — correct spec §252 (and §135), which claim a People API that does not exist, and annotate `transfers.py:99-108` as unreachable-until-`POST /people` so it is not later "cleaned up" as dead code. `POST /people` is the unblocking work whenever it is scheduled. | WP11 |
| D8 | ~~`parent_transaction_id` never written~~ — **DECIDED: keep, no action.** Same class as D7 — the Phase 5 split-transaction feature is unbuilt — but unlike D7 the docs are already truthful: §414 documents it as reserved and always `null`, and instructs clients not to build on it. Retiring it would cost a migration plus a `schema-reference.md` rewrite to reclaim one nullable column and one null key. Revisit only if splits are cancelled outright. Parked as a tracked entry in [TODO.md](../TODO.md) so the reserved field has a home and the next audit doesn't rediscover it as a mystery. | TODO |

---

## WP1 — 🔴 FX / home-currency correctness

**The priority package.** Five bugs, one theme: exchange-rate resolution is
inconsistent across create / promote / transfer / update / recalc, and these are the
only findings that silently write wrong values into the ledger. Cross-confirmed by
three of the four audits. One coder, sequential — the tasks share files.

Files: `app/helpers/transfers.py`, `app/helpers/inbox.py`,
`app/helpers/transactions.py`, `app/helpers/exchange_rate.py`.
*(`app/helpers/recalculate_home_currency.py` was deleted 2026-08-01 — see 1.1.)*

### 1.1 ✅ DONE 2026-08-01 — resolved by deletion, not by fix
Home currency is now locked to PEN, so the code path is unreachable and was removed rather than repaired.

**Shipped:** `app/helpers/recalculate_home_currency.py` (222 lines) and `tests/test_home_currency_recalc.py` (294 lines) deleted; the orphan-leg test in `tests/test_phase_fixes.py` deleted; `RecalculationSummary` and the `recalculation` response field dropped from `app/schemas/auth.py`; `PUT /auth/settings` now 422s on `main_currency` (`app/helpers/auth.py`); `sql/018_lock_home_currency_to_pen.sql` adds `CHECK (main_currency = 'PEN')`; `tests/test_home_currency_locked.py` pins both layers. 583 lines net removed, 165 tests pass.

**Deliberately kept:** `main_currency` stays declared on `SettingsUpdateRequest`. Removing it would let Pydantic's `extra="ignore"` silently discard the field, returning `200` for a switch that never happened — a client would then cache a currency the engine never adopted. Explicit field, explicit rejection.

**Client impact:** breaking for the CLI — see [client-breaking-changes.md](client-breaking-changes.md).

### 1.2 🟡 PARTIALLY RESOLVED 2026-08-01 — duplication gone, survivor still wrong
- **Was:** the rule was implemented twice and the copies disagreed — `app/helpers/transfers.py:142-171` vs `app/helpers/recalculate_home_currency.py:134-160`.
- **Now:** the second copy was deleted with WP1.1, so the DRY violation is closed and no `resolve_transfer_home_values(...)` extraction is needed — there is one copy and nothing to share it with.
- **Still open:** the *surviving* copy is the one with the bugs. It tests the caller-rate override before the currency-match rule (violating §547) and has no branch for "neither leg matches home". That is 1.3, and it is no longer merely a consistency issue — it is the only implementation.
- ⚠️ **Before touching `transfers.py`, recover the deleted fallback:** `git show HEAD~1:app/helpers/recalculate_home_currency.py` lines 142-159 hold the working no-dominant-leg logic (debit leg dominant at market rate, sibling forced equal). It is the reference implementation for 1.3's missing branch.

### 1.3 🔴 Transfer branch order: caller rate overrides the currency-match rule; same-currency non-home transfer → 500
- **Where:** `app/helpers/transfers.py:150-171`. Branch (a) caller-rate-override precedes (b)/(c) currency match, violating §547 (the leg matching `main_currency` must have `amount_home_cents == amount_cents`). Branch (d) `raise RuntimeError` is reachable with shipped data: currencies are locked to {USD, PEN} (`sql/015`), `main_currency` defaults to `'PEN'` (`sql/002:20`), so **any USD→USD transfer for a PEN-home user 500s every time**.
- **Validate:** (i) POST a PEN→USD transfer with an explicit `exchange_rate` while `main_currency = USD`; the USD leg's stored rate ≠ 1.0. (ii) POST a USD→USD transfer with `main_currency = PEN`; expect 500 INTERNAL_ERROR. Note all existing transfer tests seed PEN accounts, which is why the suite misses this.
- **Fix:** test currency match before the caller override; for the neither-leg-matches case adopt the recalc helper's fallback (debit leg dominant at market rate, sibling forced to equal home value). Raise `settings_missing()` (422) instead of `RuntimeError` when settings are absent. Convert the remaining internal raise to `AppError` so no bare `RuntimeError` remains in a write flow.

### 1.4 🔴 Inbox promote never re-resolves the rate; transfer promote always bypasses the dominant-side rule
- **Where:** `app/helpers/inbox.py:460-461` uses the stored `exchange_rate` verbatim; the column defaults to 1.0 via `COALESCE($10, 1.0)` at `:104`; `update_inbox_item` re-rates only `if "date" in fields` (`:200`) — setting/changing `account_id` alone never re-rates. Transfer branch: `:447` passes `primary_exchange_rate` unconditionally (never `None`), so every inbox-originated transfer takes the caller-override branch in `create_transfer_pair` — the currency-match branches are dead for that path.
- **Validate:** POST /inbox with `date` but no `account_id` → PUT `{account_id: <USD acct>}` → promote. The ledger row lands with `amount_home_cents == amount_cents` (rate 1.0) for a non-home-currency account.
- **Fix:** widen the PUT re-rate guard to `("date" in fields or "account_id" in fields) and "exchange_rate" not in fields`; in `promote_inbox_item` re-resolve via `lookup_exchange_rate` when the stored rate was never user-supplied; pass `primary_exchange_rate=None` for promoted transfers unless genuinely user-set (may need a "rate was user-supplied" signal, e.g. nullable `exchange_rate_source`).
- Violates spec §127, §351, §547.

### 1.5 🔴 `PUT /transactions/{id}` changing `account_id` never re-rates
- **Where:** `app/helpers/transactions.py:522-537` — re-rate trigger is `if "date" in fields...` (`:523`); recompute trigger at `:529` checks only `amount_cents`/`exchange_rate`. The account determines the source currency, so it is a rate-determining input exactly like `date`.
- **Validate:** move a transaction from a PEN account to a USD account; balances correct atomically, `amount_home_cents` keeps the PEN conversion.
- **Fix:** `if ("date" in fields or "account_id" in fields) and "exchange_rate" not in fields:` with `effective_date = fields.get("date", before_row["date"])`; add `or "account_id" in fields` to the `:529` guard. Transfer legs already block `account_id` at `:497-506`, so this touches non-transfer rows only. Spec §468 must gain the rule too (WP11).

### 1.6 ❌ VOID 2026-08-01 — repro required a currency switch
The repro was delete → switch `main_currency` → restore. With the switch removed there is no path that makes a soft-deleted row's stored rate stale, so a restored row still carries the rate it was created with. No action.

### 1.7 🟡 Rate hygiene (validation + jobs + cache + precision)
- `exchange_rate` request field unvalidated: `app/schemas/transactions.py:25,38` → add `Field(None, gt=0)` (DB CHECK lands in WP6.3).
- FX jobs insert unvalidated provider rates: `jobs/fetch_exchange_rates.py:182`, `jobs/backfill_exchange_rates.py:208` → guard `math.isfinite(rate) and rate > 0`; `ON CONFLICT DO NOTHING` makes a bad rate permanent, and a zero rate writes `amount_home_cents = 0` silently everywhere that day.
- Negative lookups cached 3600 s: `exchange_rate.py:124-137` — engine keeps 422ing up to an hour after the fetch job fills the rate (separate process, cache not invalidated). Short TTL for `None`, and never cache `None` for `as_of == today`.
- Fetch target list excludes archived accounts (`jobs/fetch_exchange_rates.py:85`) although archived balances are still converted by list/dashboard/recalc → drop the `is_archived = false` filter or union in currencies with transaction history.
- Float64 + banker's rounding for money: `exchange_rate.py:80,96` cast asyncpg's `Decimal` to `float`; `round(cents * rate)` open-coded ~10 places → keep `Decimal`, centralize in one helper with explicit `ROUND_HALF_UP` (do together with 1.2).

### 1.8 Tests to add with this WP
- Cross-currency promote after `account_id`-only PUT (1.4 repro).
- USD→USD transfer with PEN home (1.3 repro) — requires seeding non-home accounts, which no current transfer test does.
- Currency switch with a missing-rate row (1.1) asserting the D1-chosen behavior.
- Account-move re-rate (1.5); delete→switch→restore (1.6).

---

## WP2 — 🔴 Auth & security hardening

Files: `app/deps.py`, `app/helpers/jwks.py`, `app/config.py`, `.env.example`,
`app/helpers/reconciliations.py` (one line), `app/routers/pat.py` / `app/helpers/idempotency.py` (D6).

### 2.1 🔴 HS256 JWT forgery with the committed placeholder secret
- **Where:** `app/deps.py:63-66, 77-83`. Tokens with header `alg: HS256` are verified against `settings.supabase_jwt_secret` = the literal `local-unused`, committed in `.env.example:14` and present in `.env:11`. No `exp` required, no issuer check → forge `jwt.encode({"sub": "<uuid>"}, "local-unused", "HS256")` and get full read/write.
- **Scope:** engine binds `127.0.0.1:8000`, so exposure is local processes / anything proxying to the port — still a total bypass with a public key, and the local profile is documented PAT-only, so the JWT branch is unnecessary there.
- **Fix:** gate the JWT branch behind an explicit profile flag defaulting **off** (local profile 401s anything not `ewe_pat_`-prefixed); refuse startup when the flag is on and the secret is a placeholder or < 32 chars (mirror the `_LOCAL_DB_HOSTS` fail-closed guard in `config.py`); blank the value in `.env.example`; add `"require_exp": True` to decode options.
- **Test gap to close:** `tests/conftest.py:226` overrides `get_current_user` for the whole suite — no test exercises real auth over HTTP, so this class of bug is invisible today. Add a small non-overridden auth test module.

### 2.2 🟡 JWKS fetch: blocking, unguarded, wrong failure code
- **Where:** `app/helpers/jwks.py:34-54`, `deps.py:68-73`. Unknown `kid` triggers a blocking `urllib.request.urlopen` on the event loop; any fetch failure surfaces as 500 instead of 401.
- **Fix:** wrap in try/except → `unauthorized(...)`; add a negative cache / refetch cooldown. (Becomes near-moot if 2.1 disables the JWT branch locally, but the cloud profile needs it.)

### 2.3 🟡 Unscoped account read in `resolve_home_rates`
- **Where:** `app/helpers/reconciliations.py:59-62` (reached from `routers/sync.py:76`) — `SELECT id, currency_code FROM expense_bank_accounts WHERE id = ANY($1)` with **no `AND user_id`**. The only unscoped read in the sync/dashboard/reports/activity routers; RLS is inert under the local owner connection so nothing else catches it.
- **Fix:** add the predicate — `user_id` is already a function parameter. One line.
- ~~Related (⚪, same theme): `recalculate_home_currency.py:81,165,177,211` bulk UPDATEs omit `AND user_id`~~ — **void**, file deleted 2026-08-01 (1.1).

### 2.4 🟡 PAT plaintext in the idempotency snapshot (per D6)
- **Where:** `app/routers/pat.py:32-37,71` → `app/helpers/idempotency.py:99-111,141-142`; `tests/test_pat.py:83-104` asserts the replay. Contradicts spec §173.
- **Fix (recommended):** exempt `POST /auth/pat` from response-snapshot storage (store a replay-safe stub or nothing and document non-replayability for this one route); update the test; note in spec.

---

## WP3 — 🔴 Sync watermark

File: `app/helpers/sync.py` (+ docstring, + spec §716 in WP11).

### 3.1 🔴 Delta sync can permanently drop committed writes
- **Where:** `sync.py:129` stores `snapshot_at = SELECT now()` (transaction-START time) as the checkpoint; writers stamp `updated_at = now()` = their own start time; delta reads `WHERE updated_at > $2` (`:48`). Writer begins t=100, sync begins t=101 (checkpoint 101, writer invisible/uncommitted), writer commits with `updated_at=100` → next delta queries `> 101`, rows never delivered.
- **Note:** the `REPEATABLE READ` wrapper (`routers/sync.py:60`) is correct — the defect is purely the checkpoint boundary value. Don't touch the transaction handling.
- **Validate:** hard to repro by hand; validate by code-reading the three-timestamp interleave above, or with two concurrent connections and manual `BEGIN`/sleep/`COMMIT`.
- **Fix options (increasing rigor):** (a) one-liner — persist `now() - interval '5 seconds'` (payloads are full-row upserts, re-delivery is idempotent); (b) `COALESCE(min(xact_start), now()) FROM pg_stat_activity WHERE datname = current_database() AND state <> 'idle'` inside the sync transaction; (c) move the delta predicate off timestamps onto `pg_snapshot_xmin(pg_current_snapshot())`. Take (a) now, note (c) as the long-term model.
- Correct the docstring at `sync.py:124-127` alongside.

---

## WP4 — 🔴 Idempotency TTL

File: `app/helpers/idempotency.py` (+ a small purge job in `app/jobs/`).

### 4.1 🔴 Expired keys are never reclaimed → duplicated financial writes on >24 h retries
- **Where:** `_claim` filters `AND expires_at > now()` (`:68-75`) so stale rows are ignored and the write re-executes — correct. But `_store`'s `ON CONFLICT (user_id, key) DO NOTHING` (`:99-111`) hits the surviving UNIQUE row and writes nothing, so the row keeps its expired snapshot forever and **every** subsequent retry with that key re-executes the write. Any offline client queue that persists a key across a >24 h window duplicates transactions.
- **Validate:** insert a key row with `expires_at` in the past, replay the same request twice, observe two ledger rows.
- **Fix:** `ON CONFLICT ... DO UPDATE SET response_snapshot = ..., status_code = ..., expires_at = ... WHERE idempotency_keys.expires_at <= now()`. Add a periodic purge (the table currently grows unbounded — no purge job exists).

---

## WP5 — 🟠 Reconciliation correctness

File: `app/helpers/reconciliations.py`, `app/schemas/reconciliations.py`,
`app/helpers/transactions.py` (two guards), `tests/test_reconciliation_ordering.py`.
**Conflicts:** WP6 also touches `schemas/reconciliations.py` (`extra="forbid"`) — coordinate.

> **Scope reduced by D3 (2026-08-01).** Chaining is being retired outright (see the open
> item in [TODO.md](../TODO.md)), which deletes `_cascade_chained_recalc` and with it
> **5.1, 5.2 and 5.4** — all three are cascade defects, and there will be no cascade.
> Do the chaining retirement *first*; what remains of this WP is **5.3 and 5.5**, neither
> of which touches the cascade. The three struck tasks are kept below for the reviewer's
> record — do not implement them.

### 5.1 ~~🟠 Cascade early-stop is unsound for reorder and restore~~ — SUPERSEDED by D3
- **Where:** `_cascade_chained_recalc` returns at the first row whose recomputed value equals its stored value (`reconciliations.py:272-276`), reached from reorder (`:1035-1037`) and restore (`:897-899`). Sound only when upstream pairing is unchanged (PUT / soft-delete); false when rows change position.
- **Validate (concrete repro):** chained rows on one account A@1 (0→100), B@2 (100→100), C@3 (100→500), D@4 (500→600). Reorder to [A,C,B,D]: C@2 recomputes 100 == stored → walk stops; B and D keep stale beginnings; `recalculated_count = 0`.
- **Fix:** add `stop_early: bool = True` param; pass `False` from reorder, restore, and the insert-at-position call (`:467`). Walk the full chain, skipping UPDATEs where values already match. Row counts are small; the optimization isn't worth the hole. Amend spec §607 (WP11).

### 5.2 ~~🟡 Cascade rewrites COMPLETED reconciliations~~ — SUPERSEDED by D3 (this finding *is* what D3 decided)
- **Where:** the walk has no status predicate (`:220-231`), so an edit to an upstream draft mutates a completed batch's locked `beginning_balance_cents`. Spec conflict: §646 lock vs §605-607 cascade.
- **Fix (per D3):** either `AND status = 1` on the walk, treating completed rows' stored ending balances as fixed upstream (mirroring manual-source handling at `:321-327`), or amend §646.

### 5.3 🟡 `sort_order` in PUT body: dead guard, silent 200
- **Where:** `schemas/reconciliations.py:37-51` has no `extra="forbid"` and doesn't declare `sort_order`, so Pydantic drops the key and the explicit guard at `reconciliations.py:524-528` is unreachable. The schema comment at `:51` claims a mechanism that doesn't exist; `tests/test_reconciliation_ordering.py:462-468` asserts the wrong 200.
- **Fix:** declare `sort_order: Optional[int] = None` (and add `extra="forbid"` — coordinate with WP6.1) so the guard fires with §652's exact message; retighten the test.

### 5.4 ~~🟡 Reorder response omits cascade-affected rows~~ — SUPERSEDED by D3 (reorder becomes a pure `sort_order` write; keep only the ⚪ duplicate-SELECT perf note)
- **Where:** `:1049-1064` filters the response to submitted ids only; `recalculated_count` can be > 0 with none of those rows present. Spec §677 requires every affected row.
- **Fix:** have the cascade return the ids it rewrote; union into the response query. (Pairs with the ⚪ perf note: the identical SELECT runs twice at `:1011-1019` and `:1049-1057` — guard the second with `if recalculated:`.)

### 5.5 🟡 Assorted state-machine gaps
- Unassigning from a COMPLETED reconciliation via PUT is silent/unguarded (`transactions.py:448-461, 600-612`) — DELETE warns in the analogous case; PUT doesn't. Emit the same warning (or block).
- PUT changing `reconciliation_id` alongside other fields bumps `version` twice (`:577-592` then `:601-612`), breaking read-modify-write conflict detection (§48). Single bump.
- Delete of one transfer leg omits the sibling's stale-reconciliation warning (`:766-777`); restore gets it right at `:1082-1083`. Mirror it.
- `restore()` returning `None` (race) then `*_from_row(None)` → TypeError at `reconciliations.py:885-886` (same pattern in categories/hashtags — see WP7.4). Guard.

---

## WP6 — 🟠 Input-validation hardening (mechanical sweep + migration 018)

Three codebase-wide sweeps + one migration. Mechanical, low-risk, closes ~15 findings.
**Conflicts:** touches most schema files — land after WP1/WP5/WP7 or rebase carefully.

### 6.1 🟠 `extra="forbid"` sweep
- Only `app/schemas/accounts.py:11,24` set it today. Consequences found by three audits independently: silent `sort_order` no-op (WP5.3), `is_person: true` silently dropped on account update (spec §207 mandates rejection), `is_system`/`system_key` silently dropped on category/hashtag PUT, field-name typos (`curency_code`) return 200 and bypass the currency-immutability guard, `amount_home_cents` documented as a lockable transfer field (§466) can never trigger its 422.
- **Fix:** add `extra="forbid"` to every request model (`model_config = ConfigDict(extra="forbid")`); generalize the rule in Base Conventions (WP11). Check each update-schema's declared fields against its spec section while there.

### 6.2 🟠 UUID path/query params typed `str` → 500 on malformed input
- **Where (systemic):** `pat.py:40-51`; `reconciliations.py:33,137,206,229,250,268,287`; `accounts.py:135,243`; `categories.py:75`; `hashtags.py:75`. asyncpg raises `DataError` → catch-all 500. `/activity` does it right (`Optional[UUID]` → 422).
- **Fix:** type them all as `UUID`. Also constrain `routers/activity.py:41` `resource_type` to a `Literal` of known types.

### 6.3 🟠 Migration 018 — CHECK constraints for closed enums
- Currently ONE check exists repo-wide (`global_currencies_phase1_only`). Add: `transaction_type IN (1,2,3)`, `transfer_direction IN (1,2)`, `transaction_source IN (1,2)`, inbox `status IN (1,2)`, reconciliation `status IN (1,2)`, `beginning_balance_source IN (1,2,3)`, `activity_log.actor_type` closed set, `exchange_rates.rate > 0`, `user_settings` enum columns. (The doc never claimed these exist — this is unenforced-invariant hardening, not drift repair.)
- Include here: **accounts unique index** — `expense_bank_accounts` missed the `sql/012` treatment: `UNIQUE (user_id, name, currency_code)` is unconditional + case-sensitive, so a soft-deleted account permanently locks its name and **renaming onto it → uncaught `UniqueViolationError` → 500** (`helpers/accounts.py:239-247` pre-checks live rows only; `query_builder.py:40` has no handler). Migrate to a partial case-insensitive unique index mirroring sql/012; translate `UniqueViolationError` → 409 in `update_account`, branching on `constraint_name` so the 409 message no longer misattributes name collisions as ID collisions; run names through the existing `normalize_name`.

### 6.4 🟡 Settings validation
- `schemas/auth.py:51-59`: `theme` / `start_of_week` / `transaction_sort_preference` are bare `Optional[int]` → `{"theme": 99}` stores silently, > smallint 500s. Use `Literal`.
- `display_timezone` accepted as any string; `monthly_report.py:60-63` does `except Exception: tz = UTC`, so a typo silently shifts every month boundary to UTC. Validate against `zoneinfo.available_timezones()` on write; narrow the except to `ZoneInfoNotFoundError` and log.

---

## WP7 — 🟠 Inbox correctness

File: `app/helpers/inbox.py`, `app/schemas/inbox.py`, `app/routers/inbox.py`,
`app/helpers/categories.py`.
**Conflicts:** WP1.4 also edits `inbox.py` (promote rate) — land WP1 first.

### 7.1 🟡 POST/PUT /inbox: no referential or ownership validation
- **Where:** `inbox.py:96-121, 209`. Bad FK → `ForeignKeyViolationError` → 500; another user's `account_id` passes the FK and is stored (promote rejects it later); same bad input yields 404 vs 500 depending on whether `date` was sent.
- **Fix:** use the existing `validate_active_account`/`validate_active_category` (accumulating-errors pattern) → 422.

### 7.2 🟡 Transfer inbox items: schema demands an `id` the code discards; same-sign coercion
- `schemas/inbox.py:19,30` reuse `TransferField` whose `id: UUID` is required — the documented request shape 422s and the value is never read (sibling id arrives at promote as `transfer_id`). Give inbox its own transfer model without `id`.
- Same-sign transfers silently coerced: create discards the primary's sign via `abs()` (`inbox.py:75-89`); promote reconstructs direction as the opposite of the sibling (`:434-435`), making `create_transfer_pair`'s zero-sum guard unreachable — two outflows become a valid-looking transfer moving money the wrong way. Spec §546 requires 422. Validate the sign relationship at POST/PUT while the signed value is still available; persist the direction.
- `transfer_id` on a NON-transfer promote silently discarded; spec §385 mandates 422 (`inbox.py:455`).

### 7.3 🟡 `?ready=true` hides every promotable transfer item
- **Where:** `routers/inbox.py:56,66-70` require `category_id` unconditionally; transfer rows never have one (promote skips category validation for them, `inbox.py:403`).
- **Fix:** qualify the category conditions with `transfer_account_id IS NOT NULL OR (...)`.

### 7.4 🟡 Reserved system-category names can permanently 500 transfers
- **Where:** nothing rejects `@Debt`/`@Transfer`/`@Opening` on POST/PUT /categories (`categories.py:100-110, 174-188`). If a user claims the name before the system row is seeded, `ensure_system_category`'s `ON CONFLICT (user_id, system_key)` arbiter doesn't cover the `(user_id, LOWER(name))` index (sql/012) → uncaught `UniqueViolationError` through `transfers.py:99` / `accounts.py:180` → permanent 500 until a manual rename.
- **Fix:** reject reserved names at the public boundary AND wrap the seeding INSERT in a handler. Also: check-then-act races on category/hashtag update+restore degrade 409 → 500 (`categories.py:177-190, 385-403`; `hashtags.py:113-126, 329-347`) — catch `UniqueViolationError` at those sites too.

---

## WP8 — 🟡 Activity-log completeness

Files: `app/helpers/transactions.py`, `app/helpers/transfers.py`,
`app/helpers/reconciliations.py`, `app/helpers/inbox.py`, `app/helpers/categories.py`.
**Conflicts:** overlaps WP1/WP5 files — land after both.

### 8.1 Per D4 — undocumented log gaps
- Balance writes (`balance.py:83-93, 115-125`) — recommend documented exception.
- Reconciliation delete cascade nulling `reconciliation_id` on N transactions (`reconciliations.py:828-836`) — recommend real per-row entries ("why did this transaction leave its batch?" is exactly what the log is for).
- Insert-at-position `sort_order` shift (`:169-187`) — reorder logs the identical change per-row; insert doesn't. Log it.
- complete/revert version bumps on assigned transactions (`:699-707, :770-778`) — documented exception or entries, per D4.
- Bootstrap `last_login_at` bump is a deliberate but unrecorded exception (`auth.py:60-76`) — record it in §6.

### 8.2 🟡 CREATE snapshots record `hashtag_ids: []` on batch and transfer paths
- **Where:** `transactions.py:1259-1267` logs inside the loop, `attach_hashtag_ids` runs after at `:1283-1284`; `transfers.py:281-294` logs before `_sync_hashtags` runs (`transactions.py:294-299`). Junction rows are deliberately unlogged (§922), so the parent snapshot is the ONLY record — lost for every batch/transfer create. Single-create does it correctly (`:355-366`).
- **Fix:** attach hashtags before writing the log on both paths.

### 8.3 Per D5 — promotion logged as `DELETED`
- `inbox.py:522-526` writes the same action as dismissal (`:251-255`). Add `PROMOTED` (5) or use `UPDATED`.

### 8.4 ⚪ Smaller
- `ensure_system_category` seeds with no CREATED entry (`categories.py:55-69`) — write one with `actor_type="system"` (the parameter exists, nothing uses it; spec §908's cron example currently describes nothing).
- Asymmetric before-snapshot in the chained cascade: `reconciliations.py:279-283` hardcodes `chained_from_reconciliation_id=None` while the after-side resolves it (`:305-309`) → phantom `null → uuid` diff. Use `_serialize_with_neighbor` for both sides.
- Bulk reorder account entry passes `after_snapshot` only (`:1042-1045`); §6 requires both. Also the key name is `reconciliations_reordered` vs documented `ordered_ids`.
- `GET /activity` has no stable tiebreaker (`routers/activity.py:72`) — rows in one transaction share byte-identical `created_at`; add `, id DESC`.

---

## WP9 — 🟢 DRY / extraction refactors (AFTER logic fixes)

Behavior-preserving. One coder. Run the full suite between each task.

| # | Task | Where | Fix |
|---|---|---|---|
| 9.1 🟠 | Signed-amount SQL CASE matrix ×3 (feeds `/dashboard` + `/reports/monthly`; drift = the two endpoints disagree on the same month) | `routers/dashboard.py:102-120`, `helpers/monthly_report.py:111-124, 190-203` | Shared public constants in `monthly_report.py` (or `helpers/sql_fragments.py`); interpolate enum members, not bare 1/2/3 |
| 9.2 🟠 | archive/unarchive ×4 (~160 dup lines); parameterized version already exists | `categories.py:266-359`, `hashtags.py:211-299` vs `accounts.py:419-460` | Generic `set_archive_flag(...)` in `query_builder.py`; guard param for the system-category check |
| 9.3 🟡 | Owned-row lookup ×36 | helpers + routers, e.g. `categories.py:156…`, `reconciliations.py:533…` | `fetch_owned(conn, table, id, user_id, *, deleted=False, lock=False)` in `query_builder.py` (lock covers the FOR UPDATE variants) |
| 9.4 🟡 | Active-reference predicate ×10 | `transactions.py:880-921`, `inbox.py:391-414`, `transfers.py:68-86` | Non-raising `check_active_*` siblings in `validation.py`; `validate_active_*` become raise-on-None wrappers |
| 9.5 🟡 | Name-uniqueness query ×6 | categories/hashtags create/update/restore | `assert_name_available(...)` in `validation.py` |
| 9.6 🟡 | Dynamic-UPDATE loop rewritten ×2 | `auth.py:161-170, 215-223` | Generalize `query_builder.dynamic_update` (`where`, `bump_version` params) |
| 9.7 🟡 | Transfer sibling-leg mirroring (~95 lines) | `transactions.py:665-750, 958-1066` | Per-leg `_soft_delete_leg` / `_restore_leg` helpers (the module's declared "no-split zones" do NOT cover these) |
| 9.8 🟡 | List-endpoint pagination scaffold ×8 (two conflicting param-numbering idioms) | 8 routers | `build_paginated_query(...)` in `helpers/pagination.py`; start with the near-identical categories/hashtags pair |
| 9.9 🟡 | Magic ints where the enum is imported | `transactions.py:454,484,772`; `reconciliations.py:437,688,762`; `inbox.py:357,513`; `transfers.py:189,219`; `routers/reconciliations.py:87`; `schemas/transactions.py:127` | Use the enum members / interpolate `int(Enum.X)` |
| 9.10 🟡 | `get_home_balance` double-fetches settings per mutation (4 queries/write) | `accounts.py:26-53` + call sites `:279-450` | Optional `main_currency` param; hoist the fetch |
| 9.11 ⚪ | Dead code: `app/schemas/sync.py` (whole module — or wire as `response_model`, see WP10.1), `SOURCE_INT_BY_LABEL`, `ReconciliationReorderResponse`, `MAX_LIMIT`/`DEFAULT_LIMIT` (use in `Query(...)` instead — pairs with 9.8), `_reset_cache_for_tests`; unused imports `WILDCARD_TOKEN` (`routers/sync.py:14`), `TransactionUpdateRequest` (`transactions.py:50`) | as listed | Delete or wire |
| 9.12 ⚪ | UUID validator ×2 (+ only function-body import in codebase) | `routers/sync.py:31-39` vs `helpers/sync.py:22-27` | Publicize `is_uuid`, delete the copy |
| 9.13 ⚪ | Private cross-module access + partial reimplementation | `routers/reconciliations.py:84-100, 88, 176` | Public `serialize_many_with_neighbors(...)` |
| 9.14 ⚪ | `tzdata` undeclared (silent-UTC failure on the cloud profile); pip-freeze requirements with drift (`click` pin ≠ installed) | `monthly_report.py:61-63`, `requirements.txt` | Declare `tzdata`; consider direct-deps-only or a lockfile |
| 9.15 ⚪ | `/reports/monthly` range form: up to 72 serial queries; identical categories query re-run per month | `helpers/monthly_report.py` loop | Hoist the categories query; consider gathering months |

---

## WP10 — 🟡 API contract

### 10.1 🟡 `response_model` on all routes
- 57 of 61 routes declare none — `openapi.json` (the "system contract" per the design principles) documents no response shapes. The 4 that do (`auth.py`) are bypassed anyway because `run_idempotent` returns `JSONResponse`.
- **Fix:** add `response_model` to read routes (they return dicts) and `responses={200: {"model": X}}` documentation on idempotent writes; wire the currently-dead `SyncResponse` into `GET /sync` (validate first — sync account rows intentionally null `current_balance_home_cents`); wire `ReconciliationReorderResponse`.

### 10.2 🟡 Error/shape consistency nits
- `VALIDATION_ERROR.fields` with null-valued keys in `/reports/monthly` (`routers/reports.py:82-114`) — clients iterating keys hit fields not in error; build with a comprehension omitting nulls. Also `errors.py:52-57` silently overwrites when Pydantic reports two errors on one field.
- `warnings` key present on transaction delete/restore, absent on create/update/get (`transactions.py:778, 1085`) — stabilize the shape.
- `debit_as_negative` doesn't flip inbox transfer-leg amounts (`formatting.py:53-57`) — flip or document.
- `system_key` missing from category responses (`schemas/categories.py:21-32`) — clients see `is_system` but not which; re-introduces the name-coupling §287 says was fixed. Add it.
- `GET /exchange-rates` 404 vs write paths' 422 RATE_UNAVAILABLE for the same condition — unify or document.
- Transfer-pair nits: sibling gets no `inbox_id` on promote (`transfers.py:212-235`, contradicts §397 and the local docstring); primary returned at `version=2` vs sibling 1; `primary_id == sibling_id` check runs late and outside the accumulate-errors pattern (`:173-177`).
- Misc ⚪: `/health` 500s when the DB is down (it's a readiness check — document or reshape); asyncpg pool lacks `command_timeout` (`db.py:11-22`); `X-Client-Id` not case-normalized before checkpoint lookup; `?search=` is unescaped ILIKE — escape `%`/`_` (and fix the "full-text" doc claim); archived dashboard panels miss the `@Opening` exclusion (`routers/dashboard.py:133-152, 175-199` vs `monthly_report.py:139-143`); `compute_month_flow` hashtag aggregation missing `transaction_source = 1` (`monthly_report.py:125-133`, plus two other junction queries — inert today, real once source=2 exists).

---

## WP11 — 📝 Documentation pass (last; needs WP0 decisions)

**Fix the doc, never the code:**
- `transaction_source` mapping is INVERTED in `schema-reference.md:31, 500-501` — code universally uses 1 = ledger. Correct the doc; the CHECK lands in WP6.3.
- Batch: spec §509's partial-success sentence contradicts §503 and the code (all-or-nothing is correct). Also envelope is `{"transactions": [...]}` not a bare array, and the per-item error path is `fields.items[k].fields.*` + `.index`. Rewrite the section.
- Dashboard/report JSON examples show positive `spent_cents` for expenses, contradicting the spec's own rule at §823 and the code. Fix examples.
- §252 "People API" claims listing/management run through a dedicated API — they run through `/accounts`. Align with D7.
- `GET /health` describes liveness; it's a readiness check (D/WP10 misc).
- "Daily FX fetch" wording stale — actually login + every 6 h per `deploy/local/README.md:21`; backfill job undocumented.

**Document what exists:**
- `GET /categories/{id}`, `GET /hashtags/{id}` (implemented, no spec heading); `GET /exchange-rates` full contract incl. USD-pivot-only limitation (`exchange_rate.py:98-103`); transfer participation in dashboard/report totals (both legs, signed, cancel in net — keep per the standing rule); explicit-null-on-PUT → 422 as a Base Convention (with the side effect that optional fields are currently unclearable — flag for a future decision); future-date rejection as a general rule; empty-PUT no-op semantics; complete/revert idempotent-200 no-ops (including skipped empty-batch check); create-with-sort_order triggers the cascade (§607 trigger list); reorder response semantics after WP5.4; RLS coverage (complete on all 15 tables, doc lists 1; `FORCE ROW LEVEL SECURITY` deliberately not issued — inert for table owner); `sql/015` USD/PEN currency lock; `sync_checkpoints` and `personal_access_tokens` added to the schema doc's conventions-exception lists; PAT §5 exceptions (revoked_at naming, no restore endpoint — security-sound, record it) per D6; §6 exception list updated per D4/D5; §11 updated per D2; §646/§607 updated per D3; §716 updated per WP3; §468 gains the account_id re-rate rule per WP1.5.
- Retire or ticket: `parent_transaction_id` (D8); sql/006 auth trigger targets `auth.users`, absent under the local profile — annotate as cloud-profile-only.
- Bare spec headings with no body: `GET /accounts/{id}`, `GET /inbox/{id}`, `GET /transactions/{id}`, `GET /categories/{id}`.

---

## Verified clean (no action — context for reviewers)

Multiple audits independently confirmed these hold everywhere; regressions here are
what tests should guard:
- Sign convention end-to-end (single inference point; `transaction_type`/`transfer_direction` absent from all request schemas; storage positive; responses positive; `debit_as_negative` presentation-only on a copy).
- Balance atomicity: all math through `balance.py`'s exact-inverse pair; `run_idempotent` holds the ONLY `pool.acquire()` in any write path — one real transaction everywhere; FOR UPDATE before mutation; account-change reverses old then applies new.
- Idempotency core (advisory lock first statement; key row commits/rolls back with the effect; body AND status replayed verbatim) — apart from WP4.
- Batch all-or-nothing; promote's six steps in one transaction with accumulate-all-errors; transaction restore's §484-489 matrix exact.
- Soft delete universal — zero hard `DELETE FROM` in the codebase; `include_deleted` on all six list endpoints.
- Auth coverage: every `/v1` route takes `CurrentUser`; `/health` the only public route — apart from WP2.1.
- UUID-first, no name-based lookups; system categories resolve by `system_key`.
- Error envelope uniform via four registered handlers; nothing escapes unshaped.
- Pagination envelope + real `count(*)` on all 9 list endpoints.
- Transfers correctly INCLUDED in dashboard/report totals (standing rule holds); @Opening exclusion keys off `system_key`; dashboard and monthly report share `compute_month_flow`; hashtag_breakdown sums to parent by construction.
- Schema docs: all 15 tables match migrations 100 % at column level (names/types/nullability/defaults/PK/FK); RLS present on all 15; deferred tables correctly absent.
- Dependencies: zero unused packages (python-jose's cryptography chain is runtime-required for ES256/RS256).

**Known test-suite blind spots** (add coverage alongside the relevant WPs):
`conftest.py:226` bypasses real auth for the entire suite (WP2); all transfer tests
seed home-currency accounts only (WP1.3); `test_reconciliation_ordering.py:462-468`
asserts the wrong sort_order behavior (WP5.3); `test_pat.py:83-104` asserts plaintext
replay (WP2.4).
