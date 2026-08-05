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

- **6.1 `extra="forbid"` sweep** — request schemas silently drop unknown fields. Fail closed: unknown input must `422`. *Partially shipped:* the four schemas that lost `exchange_rate` in `sql/021` carry it (`TransactionCreateRequest`, `TransactionUpdateRequest`, `InboxCreateRequest`, `InboxUpdateRequest`), as does `OpeningBalanceRequest`. Every other request schema is still open.
- **6.5 The transfer-leg edit guard is a deny-list, and it forgets `category_id`** — `helpers/transactions.update_transaction` blocks `{amount_cents, account_id, date}` on a row with a `transfer_transaction_id`, so a `PUT` can still move ONE leg out of `@Transfer`, stranding the other with nothing to cancel against — indistinguishable from a loan to a person, which is the other thing a non-zero `@Transfer` means. `CLAUDE.md`'s "fix at the root" corollary already describes this guard as having been inverted to an allow-list; it has not been. Invert it: enumerate what a transfer leg may change.
- **6.2 UUID path/query params typed `str`** — malformed input reaches SQL and 500s instead of `422`.
- **6.3 CHECK constraints for closed enums** — *partially shipped:* `sql/019` covers `expense_transaction_inbox`, `sql/020` covers `expense_transactions` (`transaction_type IN (1,2)` plus `amount_cents > 0`). Still open for `transaction_source`, reconciliation `status`, `activity_log.actor_type`, `exchange_rates.rate > 0`, `user_settings` enums. ⚠️ Write these as `col IS NOT NULL AND col IN (…)` on any nullable column — a `CHECK` passes on `NULL`, so the bare `IN` admits exactly the row it was added to forbid (found while writing `sql/020`).
- **7.4 Reserved system-category names can permanently 500 transfers** — nothing stops a user claiming `@Debt`/`@Transfer`/`@Opening`; `ensure_system_category`'s `ON CONFLICT (user_id, system_key)` arbiter does not cover the `(user_id, LOWER(name))` index, so the `UniqueViolationError` escapes uncaught. Reject reserved names at the boundary *and* wrap the seeding INSERT.

---

## 🟡 Medium

- **1.7** Rate hygiene — provider-rate validation, negative-lookup cache TTL, archived-account currencies missing from the fetch target list, `Decimal`/`ROUND_HALF_UP` instead of float. ⚠️ **Higher stakes since `sql/021`:** `exchange_rates` is now the only source of every home-currency figure, so a bad provider row misprices reports rather than one write. The float/rounding half is why `tests/test_home_currency_parity.py` compares rates and not cents — SQL keeps full `numeric` and rounds half-away-from-zero, while `_fetch_rate_from_db` truncates to binary float and Python rounds half-to-even.
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
`10.2` inbox `debit_as_negative` flip · `1.3`/`1.2` transfer collapse, `sql/020` ·
**`1.4`/`1.5`/`2.3` read-time currency, `sql/021`** — all three by deletion rather than
repair: with no stored conversion there is no rate to default to `1.0`, nothing to go
stale when an account changes, and no `resolve_home_rates` to read an account without
a `user_id` filter.

Details are in git history — `git log -- docs/open-bugs.md`.
