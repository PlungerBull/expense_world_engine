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

*(none open)*

---

## 🟠 High

*(none open)*

---

## 🟡 Medium

- **1.7** Rate hygiene — provider-rate plausibility validation (positivity is now enforced — `_upsert_rate` refuses `rate <= 0` and `sql/027` backstops it; what remains is sanity against the prior day's value), negative-lookup cache TTL, archived-account currencies missing from the fetch target list, `Decimal`/`ROUND_HALF_UP` instead of float. ⚠️ **Higher stakes since `sql/021`:** `exchange_rates` is now the only source of every home-currency figure, so a bad provider row misprices reports rather than one write. The float/rounding half is why `tests/test_home_currency_parity.py` compares rates and not cents — SQL keeps full `numeric` and rounds half-away-from-zero, while `_fetch_rate_from_db` truncates to binary float and Python rounds half-to-even.
- **5.5** Reconciliation state-machine gaps — all four live in `helpers/transactions.py` interactions, so they survived WP6's deletion of the chaining machinery. (Re-elaborated 2026-08-06 from the pre-compression remediation plan; the one-liner had lost the content.)
  - Unassigning a transaction from a COMPLETED reconciliation via `PUT /transactions/{id}` (`reconciliation_id: null`) is silent and unguarded — assignment *to* a completed one is refused, and DELETE at least warns. Sharper since WP6: unassignment now visibly changes a completed reconciliation's `difference_cents`. Emit the same warning, or block symmetrically with assignment.
  - `PUT` changing `reconciliation_id` alongside other fields bumps the transaction's `version` twice, breaking read-modify-write conflict detection. Single bump.
  - Deleting one transfer leg omits the sibling's stale-reconciliation warning; restore gets it right. Mirror it.
  - `restore()` returning `None` (delete/restore race) flows into `reconciliation_from_row(None)` → TypeError (`helpers/reconciliations.py`, restore path; same pattern in categories/hashtags). Guard.
- **7.1** `POST`/`PUT /inbox` do no referential or ownership validation — a bad FK 500s, and another user's `account_id` is stored and only rejected at promote. Use the existing `validate_active_account` / `validate_active_category`.
- **8.2** CREATE snapshots record `hashtag_ids: []` on the batch and transfer paths.
- **6.6** UUID-valued *body* fields typed `str` (found closing 6.2, 2026-08-07) — `TransactionCreateRequest.account_id/category_id/hashtag_ids`, `TransactionUpdateRequest`'s same four, `TransferField.account_id`, the inbox create/update pair, `ReconciliationCreateRequest.account_id`. Same hazard one layer deeper: garbage reaches SQL as a bind param and 500s. The `id` PKs in the same models are already `UUID`, so each model is internally inconsistent. Converting requires `str()` at the two comparison sites (`helpers/transactions.py` ~507 and ~1142) and mirrors the `str(account_id)` coercion `create_opening_balance` now carries.
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
