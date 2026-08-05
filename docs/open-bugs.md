# Open bugs

Work queue, not documentation. Findings from the 2026-08-01 audits (business logic,
coding patterns, bloat/DRY, doc+schema drift — all ~60 non-test files, the spec, the
schema doc, every migration). **Compressed 2026-08-03** from 361 lines to this: every
closed, void and superseded entry was deleted, since git history holds them and a
resolved bug sitting in a bug list is noise.

Severity: 🔴 corrupts stored data, bypasses auth, or loses writes · 🟠 high · 🟡 medium · ⚪ low.

**Delete a row when it is fixed. Do not annotate it as done.**

---

## 🔴 Critical

### 1.3 — Every USD→USD transfer returns 500
`helpers/transfers.py` — the dominant-side block tests the caller's rate override
*before* the currency-match rule (violating spec §Transfers point 7), and its final
`raise RuntimeError` is reachable whenever neither leg matches `main_currency`.
**Reproduced 2026-08-03:** `POST /transactions` USD→USD with PEN home →
`RuntimeError: neither leg (USD, USD) matches main_currency ('PEN')`, uncaught, 500.
**Fix:** deleted, not repaired — the dominant-side rule goes when home values stop
being stored. See `docs/rework/WP2`.

### 1.4 — Inbox items promote at exchange rate 1.0
`helpers/inbox.py` — `exchange_rate` defaults to `1.0` via `COALESCE`; the PUT
re-rate fires only on a `date` change, never on `account_id`; promote uses the stored
value verbatim. **Reproduced 2026-08-03:** a USD inbox item created without a date
stores rate `1.0`, so $100 is worth 100 PEN cents.
⚠️ Note the asymmetry — with a date present and no rate available the engine
correctly returns `422 RATE_UNAVAILABLE`. It fails closed when it looks and finds
nothing, and **fails open when it does not look at all.**
**Fix:** stop storing a rate at all. `docs/rework/WP2`.

### 1.5 — `PUT /transactions/{id}` changing `account_id` never re-rates
`helpers/transactions.py` — the re-rate trigger checks `date` only, but the account
determines the source currency. **Reproduced 2026-08-03:** a PEN transaction moved to
a USD account keeps `home=10000 rate=1.0`, unchanged.
**Fix:** nothing stored, nothing to go stale. `docs/rework/WP2`.

### 3.1 — Delta sync can permanently drop committed writes
`helpers/sync.py` — the checkpoint stores `now()` at transaction *start*; writers
stamp `updated_at` at *their* start; the delta reads `WHERE updated_at > $2`. A writer
that begins before a sync and commits after it is never delivered, and the next delta
starts past its timestamp.
**Validate:** two connections, manual `BEGIN`/sleep/`COMMIT` around a sync.
**Fix:** persist `now() - interval '5 seconds'` as the checkpoint (payloads are
full-row upserts, so re-delivery is idempotent), or take
`COALESCE(min(xact_start), now())` from `pg_stat_activity`. Correct the docstring too.
⚠️ The `REPEATABLE READ` wrapper is correct — the bug is only the boundary value.

### 4.1 — Expired idempotency keys duplicate financial writes
`helpers/idempotency.py` — `_claim` correctly ignores rows past `expires_at`, so the
write re-executes; but `_store`'s `ON CONFLICT DO NOTHING` then hits the surviving
UNIQUE row and writes nothing, leaving the stale row in place forever. Any retry of
the same request after 24 h writes a second ledger row.
**Validate:** insert a key with `expires_at` in the past, replay twice, count rows.
**Fix:** `ON CONFLICT ... DO UPDATE ... WHERE idempotency_keys.expires_at <= now()`.
Add a purge job — the table grows unbounded today.

---

## 🟠 High

- **6.1 `extra="forbid"` sweep** — request schemas silently drop unknown fields. Fail closed: unknown input must `422`.
- **6.2 UUID path/query params typed `str`** — malformed input reaches SQL and 500s instead of `422`.
- **6.3 CHECK constraints for closed enums** — *partially shipped:* `sql/019` covers `expense_transaction_inbox`. Still open for `expense_transactions` (`transaction_type IN (1,2,3)`; `transfer_direction` present exactly when type = 3), `transaction_source`, reconciliation `status`, `activity_log.actor_type`, `exchange_rates.rate > 0`, `user_settings` enums.
- **7.4 Reserved system-category names can permanently 500 transfers** — nothing stops a user claiming `@Debt`/`@Transfer`/`@Opening`; `ensure_system_category`'s `ON CONFLICT (user_id, system_key)` arbiter does not cover the `(user_id, LOWER(name))` index, so the `UniqueViolationError` escapes uncaught. Reject reserved names at the boundary *and* wrap the seeding INSERT.

---

## 🟡 Medium

- **1.2** The surviving dominant-side implementation is the buggy one. Closes with 1.3.
- **1.7** Rate hygiene — provider-rate validation, negative-lookup cache TTL, archived-account currencies missing from the fetch target list, `Decimal`/`ROUND_HALF_UP` instead of float.
- **2.3** `resolve_home_rates` reads an account with no `user_id` filter. Closes when the currency work deletes that helper; **until then it is a live cross-tenant read** — RLS is inert, so query scoping is the only guard.
- **2.4** PAT plaintext sits 24 h in `idempotency_keys.response_snapshot`, cancelling "only the hash is stored". Exempt `POST /auth/pat` from snapshot storage; return `409` on replay.
- **5.3** `sort_order` in a `PUT` body: dead guard, silent `200`.
- **5.5** Reconciliation state-machine gaps.
- **6.4** Settings validation.
- **7.1** `POST`/`PUT /inbox` do no referential or ownership validation — a bad FK 500s, and another user's `account_id` is stored and only rejected at promote. Use the existing `validate_active_account` / `validate_active_category`.
- **8.2** CREATE snapshots record `hashtag_ids: []` on the batch and transfer paths.
- **10.1** 57 of 61 routes declare no `response_model`, so `openapi.json` documents no response shapes.
- **10.2** Error/shape nits: null-valued keys in `/reports/monthly` `fields`; `warnings` present on delete/restore but absent elsewhere; `system_key` missing from category responses; `GET /exchange-rates` 404 vs write paths' 422 for the same condition; transfer sibling gets no `inbox_id` on promote; `primary_id == sibling_id` checked late and outside the accumulate-errors pattern.

---

## ⚪ Low

`/health` 500s when the DB is down (it is a readiness check — document or reshape) ·
asyncpg pool has no `command_timeout` · `X-Client-Id` not case-normalised before
checkpoint lookup · `?search=` is unescaped `ILIKE` (escape `%`/`_`, and fix the
"full-text" claim) · `compute_month_flow` hashtag aggregation missing
`transaction_source = 1`.

---

## Decisions taken — kept because they record *why*, not *what*

| # | Decision |
|---|---|
| **D2** | Strict IDs-only, no carve-out for aggregates. A second class of endpoint is how a standing rule rots. |
| **D3** | Retire reconciliation chaining entirely — explicit values, one-time prefill on POST, read-time `continuity_gap_cents`. Deletes findings 5.1/5.2/5.4. Tracked in `TODO.md`. |
| **D4** | Balance writes get a documented activity-log exception; reconciliation side-effects get real entries. Derived balances were measured (19.3 ms for 8 accounts at 200k rows) and declined — a large refactor against a bug that does not exist. |
| **D5** | Add `PROMOTED` action code (5) — promotion is a distinct user action, not an inbox edit. |
| **D6** | Exempt `POST /auth/pat` from idempotency snapshot storage. |
| **D7** | Person accounts uncreatable = parked feature gap, not a defect. Correct the spec's People API claims. |
| **D8** | `parent_transaction_id` stays reserved and `null`. The docs are already truthful. |

---

## Closed since the audit

`1.1` recalculation deleted · `1.6` void (repro needed a currency switch) ·
`2.1` **JWT forgery — auth branch deleted 2026-08-03**, `tests/test_auth_over_the_wire.py`
pins it · `2.2` JWKS fetch — `jwks.py` deleted with the branch ·
`5.1`/`5.2`/`5.4` superseded by D3 · `7.2`/`7.3` inbox transfer direction, `sql/019` ·
`10.2` inbox `debit_as_negative` flip.

Details are in git history — `git log -- docs/open-bugs.md`.
