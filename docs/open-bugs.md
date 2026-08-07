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

- **6.5 The transfer-leg edit guard is a deny-list, and it forgets `category_id`** — `helpers/transactions.update_transaction` blocks `{amount_cents, account_id, date}` on a row with a `transfer_transaction_id`, so a `PUT` can still move ONE leg out of `@Transfer`, stranding the other with nothing to cancel against — indistinguishable from a loan to a person, which is the other thing a non-zero `@Transfer` means. Invert it: enumerate what a transfer leg may change. (`CLAUDE.md`'s "fix at the root" corollary and `engine-spec.md`'s transfer-edit-guard section both cite this bug as the standing example since WP7 corrected them — they no longer claim the inversion already happened.)
- **6.2 UUID path/query params typed `str`** — malformed input reaches SQL and 500s instead of `422`.
- **6.3 CHECK constraints for closed enums** — *partially shipped:* `sql/019` covers `expense_transaction_inbox`, `sql/020` covers `expense_transactions` (`transaction_type IN (1,2)` plus `amount_cents > 0`), `sql/025` covers reconciliation `status` (WP6). Still open for `transaction_source` and `exchange_rates.rate > 0` (`actor_type` and the `user_settings` enums left the list with their columns, `sql/024`). ⚠️ Write these as `col IS NOT NULL AND col IN (…)` on any nullable column — a `CHECK` passes on `NULL`, so the bare `IN` admits exactly the row it was added to forbid (found while writing `sql/020`).
- **7.4 Reserved system-category names can permanently 500 transfers** — nothing stops a user claiming `@Debt`/`@Transfer`/`@Opening`; `ensure_system_category`'s `ON CONFLICT (user_id, system_key)` arbiter does not cover the `(user_id, LOWER(name))` index, so the `UniqueViolationError` escapes uncaught. Reject reserved names at the boundary *and* wrap the seeding INSERT.

---

## 🟡 Medium

- **1.7** Rate hygiene — provider-rate validation, negative-lookup cache TTL, archived-account currencies missing from the fetch target list, `Decimal`/`ROUND_HALF_UP` instead of float. ⚠️ **Higher stakes since `sql/021`:** `exchange_rates` is now the only source of every home-currency figure, so a bad provider row misprices reports rather than one write. The float/rounding half is why `tests/test_home_currency_parity.py` compares rates and not cents — SQL keeps full `numeric` and rounds half-away-from-zero, while `_fetch_rate_from_db` truncates to binary float and Python rounds half-to-even.
- **2.4** PAT plaintext sits 24 h in `idempotency_keys.response_snapshot`, cancelling "only the hash is stored". Exempt `POST /auth/pat` from snapshot storage; return `409` on replay.
- **5.5** Reconciliation state-machine gaps — all four live in `helpers/transactions.py` interactions, so they survived WP6's deletion of the chaining machinery. (Re-elaborated 2026-08-06 from the pre-compression remediation plan; the one-liner had lost the content.)
  - Unassigning a transaction from a COMPLETED reconciliation via `PUT /transactions/{id}` (`reconciliation_id: null`) is silent and unguarded — assignment *to* a completed one is refused, and DELETE at least warns. Sharper since WP6: unassignment now visibly changes a completed reconciliation's `difference_cents`. Emit the same warning, or block symmetrically with assignment.
  - `PUT` changing `reconciliation_id` alongside other fields bumps the transaction's `version` twice, breaking read-modify-write conflict detection. Single bump.
  - Deleting one transfer leg omits the sibling's stale-reconciliation warning; restore gets it right. Mirror it.
  - `restore()` returning `None` (delete/restore race) flows into `reconciliation_from_row(None)` → TypeError (`helpers/reconciliations.py`, restore path; same pattern in categories/hashtags). Guard.
- **7.1** `POST`/`PUT /inbox` do no referential or ownership validation — a bad FK 500s, and another user's `account_id` is stored and only rejected at promote. Use the existing `validate_active_account` / `validate_active_category`.
- **8.2** CREATE snapshots record `hashtag_ids: []` on the batch and transfer paths.
- **10.1** 51 of 55 routes declare no `response_model` (only the four in `routers/auth.py` do), so `openapi.json` documents no response shapes.
- **10.2** Error/shape nits: null-valued keys in `/reports/monthly` `fields`; `warnings` present on delete/restore but absent elsewhere; `system_key` missing from category responses; `GET /exchange-rates` 404 vs write paths' 422 for the same condition; transfer sibling gets no `inbox_id` on promote; `primary_id == sibling_id` checked late and outside the accumulate-errors pattern.

---

## ⚪ Low

`/health` 500s when the DB is down (it is a readiness check — document or reshape) ·
asyncpg pool has no `command_timeout` · `?search=` is unescaped `ILIKE` (escape
`%`/`_`, and fix the "full-text" claim) · `compute_month_flow` hashtag aggregation
missing `transaction_source = 1`.

---

## Decisions taken — kept because they record *why*, not *what*

| # | Decision |
|---|---|
| **D2** | Strict IDs-only, no carve-out for aggregates. A second class of endpoint is how a standing rule rots. |
| **D3** | Retire reconciliation chaining entirely — explicit values, one-time prefill on POST, read-time `continuity_gap_cents`. Deletes findings 5.1/5.2/5.4. **Executed by WP6 (`sql/025`, 2026-08-06) with two amendments by owner decision:** no prefill — `beginning_balance_cents` is required on POST and always typed — and no `continuity_gap_cents`; the read-time figure that shipped is `difference_cents`, the add-up check against the assigned transactions. |
| **D4** | Balance writes get a documented activity-log exception; reconciliation side-effects get real entries. Derived balances were measured (19.3 ms for 8 accounts at 200k rows) and declined — a large refactor against a bug that does not exist. |
| **D5** | Add `PROMOTED` action code (5) — promotion is a distinct user action, not an inbox edit. |
| **D6** | Exempt `POST /auth/pat` from idempotency snapshot storage. |
| **D7** | Person accounts uncreatable = parked feature gap, not a defect. Correct the spec's People API claims. |
| **D8** | ~~`parent_transaction_id` stays reserved and `null`.~~ **Superseded by the 2026-08-04 audit:** the column was a placeholder, not a foundation — dropped in `sql/024` (WP5). Splits get designed fresh if they ever ship; the parent-exclusion predicate they will need is preserved in `sql/022`'s header. |

Closed entries are deleted, not listed — `git log -- docs/open-bugs.md` holds them.
