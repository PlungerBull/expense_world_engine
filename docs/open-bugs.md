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

*(none open)*

---

## ⚪ Low

- **fx-store-float** `jobs/fetch_exchange_rates` parses provider JSON with `json.loads` (floats) and binds `float(rates[target])` into `exchange_rates.rate`, which is `numeric` — so every stored rate carries binary-float expansion: today's USD→PEN reads back as `3.3751531400000001070793587132357060909271240234375`, not the provider's `3.37515314`. Harmless today at ~1e-16 relative (far below a cent on any balance) and **parity-neutral**, since SQL and Python both read the same stored row — found while closing 1.7-round, which is why it is worth writing down rather than assuming the column holds what the provider sent. Fix is `json.loads(..., parse_float=Decimal)` in `_fetch_currency_api` plus `Decimal` through `_upsert_rate`'s signature; the plausibility guard's ratio arithmetic works on `Decimal` unchanged.
- **1.7-archived** `_fetch_target_currencies` excludes archived accounts, so archiving an account drops its currency from the daily fetch list. **Inert under `sql/015`** — USD is the base (never a target) and PEN is always on the list via `user_settings.main_currency`, so the only rate that exists is fetched regardless of what is archived. Becomes live the day a third currency is admitted; fix it in the same change that lifts the CHECK, not before.
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
