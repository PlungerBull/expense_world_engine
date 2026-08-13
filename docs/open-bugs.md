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
---

## ⚪ Low

- **7.4-r** `restore_category` (`helpers/categories.py` ~327) re-checks only "an active category already uses this name", not `RESERVED_CATEGORY_NAMES` — a user category that held `@Opening` (the only reserved name since the transfer removal shrank the set, 2026-08-11) before the 7.4 guard shipped and was soft-deleted can be restored straight back into the squat. Blast radius bounded (the opening-balance seeding INSERT answers 409, not 500), but 7.4 sealed only 2 of the 3 write paths; the restore path is the third.
- **account-color** `create_account` (`helpers/accounts.py` ~85) binds `color or "#3b82f6"` — truthiness, so an explicitly-sent `color: ""` silently becomes the blue default instead of being stored or rejected. The exact class of collapse the adjacent comment says was already fixed for `sort_order` (`or 0` ate explicit zeros); the `or` on `color` survived that fix. (Found closing bloat-audit Magic Values, 2026-08-08.) Needs a decision before fixing: an empty-string color is junk, so the likely right shape is reject (`clean_name`-style 422) rather than store-verbatim — `is not None` alone would just move the bug from "silently defaulted" to "silently stored junk". Whatever ships must keep omitted-`color` falling to the default (now owned by `sql/003`'s column DEFAULT on the categories seed path; accounts still bind the literal because the INSERT's fixed column list can't express "omit").
- **inbox-title** Inbox titles are stored verbatim — `create_inbox_item` (`helpers/inbox.py` ~170) binds `body.title` unstripped and the update path does the same, and a whitespace-only title is truthy, so it passes both the `?ready=true` SQL (`i.title IS NOT NULL`) and promote's readiness check and lands in the ledger as whitespace — bypassing the trim+reject rule every direct ledger write enforces via `clean_name`/`normalize_name`. (Found closing bloat-audit Tier 2 §10, 2026-08-08 — which also corrected §5's strike note falsely claiming inbox titles are "normalized at inbox-write time".) Fix at inbox write time (`clean_name` on create/update, whitespace-only → the same 422 the ledger gives, or → NULL as "not yet filled in"); the paired readiness SQL in `routers/inbox.py` needs the matching arm so the two definitions of "ready" stay in step.
- **hashtag-filter** `?hashtag_id=` on `GET /transactions` (`routers/transactions.py` ~100) queries `expense_transaction_hashtags` without `transaction_source = 1` — the same class of gap `compute_month_flow` just had fixed. Inert while `sql/027` pins the column to `1`; becomes a live divergence the day the inbox hashtag writer widens the CHECK to `IN (1, 2)` (which is the plan — see TODO.md). Fix alongside that feature or now, whichever comes first. (`helpers/hashtags.py` ~181's unfiltered cascade soft-delete is correct as-is — it should hit all sources.)
---

## Decisions taken — kept because they record *why*, not *what*

| # | Decision |
|---|---|
| **D2** | Strict IDs-only, no carve-out for aggregates. A second class of endpoint is how a standing rule rots. |
| **D3** | Retire reconciliation chaining entirely — explicit values, one-time prefill on POST, read-time `continuity_gap_cents`. Deletes findings 5.1/5.2/5.4. **Executed by WP6 (`sql/025`, 2026-08-06) with two amendments by owner decision:** no prefill — `beginning_balance_cents` is required on POST and always typed — and no `continuity_gap_cents`; the read-time figure that shipped is `difference_cents`, the add-up check against the assigned transactions. |
| **D4** | Balance writes get a documented activity-log exception; reconciliation side-effects get real entries. Derived balances were measured (19.3 ms for 8 accounts at 200k rows) and declined — a large refactor against a bug that does not exist. |
| **D5** | Add `PROMOTED` action code (5) — promotion is a distinct user action, not an inbox edit. |
| **D6** | Exempt `POST /auth/pat` from idempotency snapshot storage. |
| **D7** | Person accounts uncreatable = parked feature gap, not a defect. Correct the spec's People API claims. **Resolved 2026-08-10:** keep the feature, build `POST /people` (explicit creation only) — scheduled in TODO.md after the bug burn-down. The transfer removal (2026-08-10/11) deleted the `@Debt` auto-branch; a person's balance is now built from ordinary rows, which makes the explicit-creation rule the whole design. |
| **D8** | ~~`parent_transaction_id` stays reserved and `null`.~~ **Superseded by the 2026-08-04 audit:** the column was a placeholder, not a foundation — dropped in `sql/024` (WP5). Splits get designed fresh if they ever ship; the parent-exclusion predicate they will need is preserved in `sql/022`'s header. |
| **D9** | `warnings` stays **scoped** to the endpoints that can actually produce one — closing 10.2, a uniform `warnings: []` on every mutation was proposed and declined (2026-08-07). Uniformity is for *representation* rules (IDs-only, sign convention), where a carve-out rots; `warnings` is endpoint-specific *content*, and an always-empty key on ~30 endpoints is structure without meaning. The rule is stated in engine-spec's conventions ("Warnings channel"), so scoped reads as designed, not accidental. *Scope shrank 2026-08-11 (bug 5.5): transaction DELETE's only warning became a 409 block, so its `warnings` key left with it — the same principle applied again — leaving restore the sole member.* |
| **D10** | Deliberate keeps a bloat audit must not re-flag (owner, 2026-08-07): the prose tombstone blocks (`errors.py`, `exchange_rate.py`, `config.py`, `helpers/transactions.py`) are decision records, not dead code; the "recording the row IS the balance change" point stays stated in full at each domain module (~5 copies), no reduction to pointers — repetition is the feature, a reader landing in any one module gets the invariant. Likewise `clear_rate_cache` stays despite test-only callers (the module-global cache needs a reset hook). The 2026-08-06 bloat audit itself is fully executed and deleted; git history (`docs/bloat-audit-2026-08-06.md`) holds its census, corrections, and per-section rulings — every surviving carve-out is commented at its point of use. |

Closed entries are deleted, not listed — `git log -- docs/open-bugs.md` holds them.
