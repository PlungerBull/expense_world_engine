# CR5 — Documentation pass

**Prerequisites:** CR1–CR4 merged. Read
[`../currency-model-decision.md`](../currency-model-decision.md) first.
**Blocks:** nothing. **Blocked by:** CR4 — document what shipped, not what was
planned.

---

## Goal

Bring the permanent docs in line with the code that landed, and record the
breaking change for client repos.

**Read the actual diffs before writing.** `git log` CR1→CR4 and check each claim
against the code — several audit items were absorbed along the way, and this
package's job is to make the docs true, not to restate the plan.

---

## `docs/engine-spec.md` — the rulebook

### Remove

- **`exchange_rate` from every request and response contract** — transactions,
  batch create, inbox, promote. Gone from the wire in both directions (decision
  D-a). State the reason: a rate belongs to a (currency, date) pair, and
  `GET /exchange-rates` serves it.
- **§547 dominant-side rule** — deleted with `transfers.py:119-177`. Replace with
  the actual rule: each leg stores its own `amount_cents`; the executed rate is
  recoverable as `sibling.amount_cents ÷ primary.amount_cents`.
- **§466's `amount_home_cents` transfer lock** — the field no longer exists.
  (Note: it could never fire anyway, because the schema dropped the key before the
  guard ran — a symptom of the fail-open design CR4 fixed.)
- **§468** — do **not** add the `account_id` re-rate rule the audit asked for
  under WP1.5. That finding closed by deletion; there is nothing to re-rate.

### Add

- **The currency model**, or a pointer to `currency-model-decision.md` — native
  storage, read-time conversion, rate on the transaction's date.
- **`@Transfer` semantics.** Nets to zero only when both legs land in it. Non-zero
  means exactly one of: an FX spread (both legs, different home values) or a
  loan/repayment with a person (one leg, nothing to cancel). Include the four-row
  matrix from the decision doc.
- **Missing-rate policy.** Writes succeed and warn; reports show
  `spent_home_cents: null` plus `unconverted_count`; never a native-amount
  substitute.
- **`unconverted_count`** on the `/dashboard` and `/reports/monthly` contracts.
- **System categories are engine-assigned only.** `@Transfer`, `@Debt`, `@Opening`
  rejected with 422 on client-supplied `category_id`.
- **Transfer-leg mutability as an allow-list** — `{title, description, cleared,
  reconciliation_id, hashtag_ids}`; everything else 422.
- **`exchange_rate`'s removal as a documented exception to null-over-omission** —
  the field is absent, not null, same treatment as the `recalculation` field in
  the WP1.1 change. Add to the existing exception list rather than starting a new
  one.

---

## `docs/schema-reference.md`

- Remove `expense_transactions.amount_home_cents`,
  `expense_transactions.exchange_rate`,
  `expense_transaction_inbox.exchange_rate`.
- Note `sql/019` in the migration list.
- Update any prose describing stored home values — including the
  "Cross-currency transfers" section, which documents the dominant-side rule that
  no longer exists.
- ⚠️ Audit **WP11** flags that `transaction_source` is **inverted** in this file at
  `:31` and `:500-501` (code universally uses `1 = ledger`). If you are in the file
  anyway, fix it — it is a doc-only correction and cheap here.

---

## `docs/audit-2026-08-01-remediation-plan.md`

- **WP1.2, 1.3, 1.4, 1.5** → closed, resolved by deletion. Follow the format used
  for 1.1 (banner + what shipped + line counts), and say plainly that the fix was
  removing the design rather than repairing the code.
- **WP1.7** — two of five sub-items are moot (request-field validation, `gt=0`),
  since the field is gone. The other three still apply to the rate table and
  read-time math: provider-rate validation in the FX jobs, the negative-lookup
  cache TTL, archived-account currencies in the fetch target list. Note that
  `Decimal`/`ROUND_HALF_UP` was partly addressed by CR1's rounding decision —
  check what actually landed.
- **WP9.1** (signed-amount CASE matrix ×3) — absorbed by CR1's shared expressions.
- **WP10.2** — the `warnings`-key inconsistency item is closed by CR3; the inbox
  `debit_as_negative` transfer-leg flip by CR2. Verify both before marking.
- **WP5.3** — check whether CR4's `extra="forbid"` closed the `sort_order` guard;
  mark accordingly.
- **WP6.1** — note that transaction/inbox schemas are done, remainder outstanding.
- **WP7.4** — note the system-category *name* rejection still needs doing if CR4
  only covered *assignment*.

---

## `docs/client-breaking-changes.md`

**Now** — the code has landed. Newest-first, following the existing entry's shape.

Must cover:

1. **`exchange_rate` no longer accepted on any write.** `POST`/`PUT` transactions,
   batch, inbox, opening-balance.
2. **`exchange_rate` absent from every response.** Absent, not null.
3. **New `warnings` on create/update** when a rate is unavailable.
4. **New `unconverted_count`** on dashboard and monthly-report responses;
   `spent_home_cents` may now be `null`.
5. **`@Transfer` may be non-zero** — clients must not assume it cancels. Explain
   both causes.
6. **System `category_id` → 422.**
7. **Transfer-leg `category_id` → 422.**

Include the per-call-site CLI table from [CR6](CR6-cli-handoff.md) so the CLI
maintainer has one place to work from.

---

## Other files

- **`TODO.md`** — reconciliation-chaining retirement is next (decision D-c). Note
  the currency rework as done with its date.
- **`docs/roadmap.md`** — if it describes stored home amounts or the dominant-side
  rule, correct it. Per the repo's absolute-dates rule, historical dated entries
  stay as written; add a dated note rather than rewriting history.
- **`CLAUDE.md`** — already indexes `currency-model-decision.md`; verify no
  convention text contradicts the new model. In particular the **Home currency**
  convention ("Every response that contains an amount must include a
  home-currency version") still holds — the value is now computed, not stored, and
  may be `null` when unconvertible. Say so.
- **`docs/currency-rework/`** — this directory is transient. Once this package
  lands, either delete it or add a "superseded, see `currency-model-decision.md`"
  banner to `README.md` — **and remove its row from the `CLAUDE.md` Key
  documentation table** either way.

---

## Done when

- [ ] No doc claims transactions store a rate or a home amount
- [ ] `grep -rn "exchange_rate" docs/` returns only the rate-table/endpoint
      contract, `currency-model-decision.md`, and dated historical entries
- [ ] `@Transfer` semantics + the missing-rate policy documented in the spec
- [ ] `client-breaking-changes.md` has a complete entry with the CLI table
- [ ] Audit plan reflects what actually closed — verified against diffs, not the plan
- [ ] `schema-reference.md` matches `sql/019`
- [ ] `pytest` still green (docs shouldn't change behaviour — confirm)

---

## Do not

- Change code. If a doc/code mismatch appears, **fix the doc** — unless it is an
  actual bug, in which case file it in the audit plan rather than fixing it here.
- Rewrite dated historical entries — the repo uses absolute dates deliberately.
- Document `@FX` as shipped. It is specified-but-deferred (D-d); describe it as a
  future option only, in the decision doc where it already lives.
