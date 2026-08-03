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

> ⚠️ **Do not work from this list alone.** Two alignment audits (2026-08-01,
> 2026-08-02) each found sites an earlier enumeration had missed — enumeration is
> the wrong tool for a sweep. **Start by grepping**, then use the list below as a
> checklist of known-hard cases, not as the scope:
>
> ```
> grep -n "amount_home_cents\|exchange_rate\|dominant\|RATE_UNAVAILABLE" docs/engine-spec.md
> ```
>
> Every hit is guilty until proven innocent.
>
> ⚠️ **Widen the grep for D-g / D-h / D-i** (2026-08-02). Three further decisions
> landed mid-CR2 and each deletes a family of fields:
>
> ```
> grep -rn "lifetime_spent\|spent_cents\|inflow_cents\|outflow_cents\|net_cents" docs/
> grep -rn "current_balance_home_cents\|balance_home_cents" docs/
> ```
>
> ⚠️ **And grep `app/`, not just `docs/`.** This package has only ever swept
> documentation, so wrong *code comments* survive every pass — including a
> **user-facing** one. See "Stale comments in `app/`" below.

### Remove — `amount_home_cents` from individual-record contracts (D-e)

These document a field that no longer exists on transactions or inbox items.
Line numbers as of 2026-08-02:

| Line | What it says | Action |
|---|---|---|
| `:352-353` | inbox response carries `amount_home_cents` and `transfer_amount_home_cents`, *"computed as `amount_cents × exchange_rate` at read time"* | **Delete both bullets.** Neither field nor rate survives. |
| `:355` | `?debit_as_negative=true` negates `amount_home_cents` | **Correct** — it negates `amount_cents` only; CR2 deleted the home-value branch in `formatting.py` |
| `:395` | promote *"Computes `amount_home_cents` from `amount_cents × exchange_rate`"* | **Delete the sentence** — promote copies the native amount and nothing else |
| `:447` | `POST /transactions` *"Auto-populates `exchange_rate` and computes `amount_home_cents` same as inbox"* | **Delete** |
| `:466` | date change *"re-fetches the historical exchange rate and recalculates `amount_home_cents`"* | **Delete the rule.** Nothing is recalculated because nothing is stored. This is the §468 note below, at its real line. |
| `:730` | `/sync` row shapes — *"`inbox` rows include `amount_home_cents` / `transfer_amount_home_cents` computed from the stored `exchange_rate`"* | **Delete.** ⚠️ The same paragraph says `accounts` rows return `current_balance_home_cents: null` in sync — **that is now false too** (D-i removed the field; there is nothing left to null). The surviving true claim is that `/dashboard` is canonical for derived values and clients must not recompute — keep only that sentence. |

⚠️ **`:125` — the exchange-rate precondition block** is the highest-risk single
paragraph in the file. It states that five named write endpoints return
`422 RATE_UNAVAILABLE` when no rate exists, and tells clients to *"bypass the lookup
by supplying an explicit `exchange_rate` on the request."* After CR3 **both halves
are false**: writes succeed and warn (D-b), and no endpoint accepts an
`exchange_rate`. Rewrite, don't patch.

Keep the *"No silent `1.0` fallback"* principle in whatever replaces it — that
invariant survives and is the reason any of this happened.

### Remove — the rest

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
  under WP1.5. That finding closed by deletion; there is nothing to re-rate. The
  *existing* date-change re-rate rule at `:466` must also go — see the table above.
- **§545/§547 point 7** (`:545`) — the dominant-side rule, *"the other side's
  `amount_home_cents` is forced to equal the dominant side's"*, ending with *"No
  separate FX gain/loss is recognized at transaction time."* Both are reversed: legs
  store native amounts only, and the FX difference now surfaces as a non-zero
  `@Transfer`. This is the same claim as `api-design-principles.md:113` — fix both,
  and make them say the same thing.

### Add

- **The currency model**, or a pointer to `currency-model-decision.md` — native
  storage, read-time conversion, rate on the transaction's date.
- **Where currency appears** (decisions D-e, D-h, D-i). State it as the three-level
  rule, not as an endpoint list, so it stays true as endpoints are added:

  | Level | Native | PEN |
  |---|---|---|
  | Individual records — transactions, inbox | **only** | none |
  | Per-account figures — balances, reconciliations | **only** | none |
  | Cross-account aggregates — category, hashtag, month totals | none | **only** |

  In practice that means the monthly report and its month totals carry a PEN figure
  and **nothing else does**. Say so plainly, and say that **no net-worth total
  exists**, so its absence reads as a decision rather than a gap.
- **`amount_home_cents`, `current_balance_home_cents`, the reconciliation balances
  and the native aggregates are removed, not nulled** — documented exceptions to
  null-over-omission alongside `exchange_rate`. Absent, not `null`.
- **`unconverted_count` appears at two levels** (decision D-f): per category /
  hashtag row, and once per report — per month in the multi-month range form. Note
  that the report-level figure counts **distinct transactions**, so it is not the
  sum of the per-row counts.
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
2b. **`amount_home_cents` absent from transaction and inbox responses** (D-e), and
   from their `/sync` payloads. Absent, not null.
2c. **`current_balance_home_cents` absent from every account response and `/sync`;
   `beginning_/ending_balance_home_cents` absent from every reconciliation
   response** (D-i). ⚠️ **State that no cross-account total replaces them** — the
   engine reports no net worth, deliberately, and a client must not synthesise one
   by converting (clients never convert).
2d. **`spent_cents`, `inflow_cents`, `outflow_cents`, `net_cents` absent from the
   dashboard and monthly report** (D-h), because a category spanning two accounts
   summed dollars into soles.
2e. **`archived_categories` and `archived_hashtags` absent from `/dashboard`**
   (D-g). `archived_accounts` survives.

   ⚠️ **State plainly what is kept**, since the risk is a client stripping
   home-currency handling wholesale — after D-e/D-g/D-h/D-i the surviving list is
   short: `spent_home_cents` per category and per hashtag row, and
   `inflow_/outflow_/net_home_cents` on month totals. **That is the entire set.**
3. **New `warnings` on create/update** when a rate is unavailable.
4. **New `unconverted_count`** at two levels (D-f) on dashboard and monthly-report
   responses; `spent_home_cents` may now be `null`.
5. **`@Transfer` may be non-zero** — clients must not assume it cancels. Explain
   both causes.
6. **System `category_id` → 422.**
7. **Transfer-leg `category_id` → 422.**
8. **`?debit_as_negative=true` no longer negates any home value** — there is none
   left on a record to negate.

⚠️ **Correct the existing entry too.** `client-breaking-changes.md:79` claims *"No
amounts, balances, or transaction shapes changed. Nothing needs re-syncing."* That
was true on 2026-08-01 and is false after this rework. Amend it in place with a
dated pointer to the new entry rather than deleting it.

Include the per-call-site CLI table from [CR6](CR6-cli-handoff.md) so the CLI
maintainer has one place to work from.

---

## `docs/api-design-principles.md` — §12, the biggest single miss

⚠️ **This file was omitted from an earlier draft of this package.** It carries the
most detailed description of the deleted design anywhere in the repo.

**`:113`** documents the dominant-side rule in full — *"the side whose currency
matches `main_currency` is dominant … the other side's `amount_home_cents` is
forced by direct assignment"* — plus the dead 3-currency fallback branch. All of it
describes code CR3 deleted.

The same paragraph ends with a claim the new model **reverses**:

> *"No per-transaction FX gain/loss is recognised — that's a period-end
> remeasurement concern, out of scope for Phase 1."*

Under the new model the FX difference **is** surfaced, as a non-zero `@Transfer`.
Rewrite the paragraph to state: native amounts are stored per leg; the executed
rate is recoverable as `sibling.amount_cents ÷ primary.amount_cents`; home values
are computed at read time; and a cross-currency transfer therefore shows its spread
rather than being forced to zero. Keep the industry framing but correct the
conclusion — production systems recognise the difference, they do not hide it.

Check the other four hits in this file for the same assumption. **Two are already
known stale** — name them rather than leaving them to the grep:

- **`:91`** — *"Every API response containing an amount — at any level of
  aggregation — includes a home-currency version alongside the native amount. This
  applies to individual transactions (`amount_home_cents`), category totals, account
  balances, monthly summaries…"* Reversed by D-e, D-h and D-i in three separate
  places. Replace with the three-level rule from [README.md](README.md). The
  sentence *"The engine is exclusively responsible for currency conversion. The
  frontend never performs it"* is **still true and more load-bearing than before** —
  keep it verbatim.
- **`:59`** — *"The `amount_home_cents` and related fields returned by `/sync`
  reflect FX rates at sync time and drift as rates move."* No such field is returned
  by `/sync` any more. The paragraph's conclusion — that `/dashboard` is canonical
  for derived values and clients must not recompute — survives; its premise does not.

---

## D-g / D-h / D-i — three further families of removals (2026-08-02)

Landed mid-CR2, after this package was last revised. Each deletes fields the spec
documents at length.

**D-g — archived lifetime panels deleted.** `engine-spec.md:823-845` — the whole
`?include_archived=true` block. Rewrite it: the response gains **`archived_accounts`
only**. Delete the `archived_categories` / `archived_hashtags` JSON examples and the
`lifetime_spent_cents` / `lifetime_spent_home_cents` semantics paragraph at `:845`.

**D-h — native aggregates deleted.** Every `spent_cents` / `inflow_cents` /
`outflow_cents` / `net_cents` occurrence in the dashboard and monthly-report
contracts, including the JSON examples at `:779-794` and `:861-865`. ⚠️ **Two
invariants are worded against deleted fields and must be restated, not dropped:**

| Where | Now reads | Must read |
|---|---|---|
| `engine-spec.md:815` | breakdown rows sum to the category's **`spent_cents`** | …to its **`spent_home_cents`**; a `null` parent has `null` rows |
| `scaling-boundaries.md:28` | *"visible rows sum exactly to **`net_cents`**"* | …to **`net_home_cents`** |

`scaling-boundaries.md:28` sits under **business logic — scale-invariant**. The
invariant is unchanged and must stay there; only the field name moves. It is also
the reason archived categories remain in the monthly report — see CR2's
"Archived categories stay" section, and say so in the spec so the missing
`is_archived` filter is documented as deliberate.

**D-i — per-account PEN deleted.** Every `current_balance_home_cents` occurrence
(accounts, dashboard panels, `/sync` — including the `:730` paragraph that says sync
rows null it, which now has no field to null) and the reconciliation
`beginning_/ending_balance_home_cents`. State plainly that **the engine reports no
net-worth total**, so its absence reads as a decision rather than a gap.

---

## Stale comments in `app/` — not just docs

Grep the code, not only `docs/`. Known wrong after CR2/CR3:

| Site | Problem |
|---|---|
| **`app/errors.py:102-106`** | **User-facing.** The `RATE_UNAVAILABLE` message tells the caller to *"supply an explicit `exchange_rate`"* — a request field CR3 deletes. Fix or delete with the error |
| `app/jobs/backfill_exchange_rates.py:28` | *"which is the whole reason `amount_home_cents` is cached per transaction"* — no longer true |
| `app/helpers/exchange_rate.py:180` | describes `lookup_exchange_rate`'s write-blocking role, retired by D-b |
| `app/helpers/transfers.py:120-141` | the dominant-side rationale block, deleted with the rule |
| `app/helpers/transactions.py:388` | references the stored home value |

---

## `docs/scaling-boundaries.md` — four stale rows

⚠️ **Also omitted from an earlier draft.**

- **`:16`** — table row *"Home-currency amount alongside every amount; the engine is
  the only converter"*, filed under **business logic — scale-invariant**. The first
  clause is exactly what D-e/D-h/D-i reverse. The second clause (*"the engine is the
  only converter"*) is still true and load-bearing — keep it, and replace the first
  with the three-level rule from [README.md](README.md).
- **`:28`** — the `net_cents` → `net_home_cents` restatement above.

- **`:24`** — table row *"Transfer pairing + cross-currency zero-sum via the
  dominant-side rule"*. The rule is gone; transfer pairing remains. Rewrite the row
  and drop the `api-design-principles.md §12` reference if that section no longer
  describes it.
- **`:29`** — *"a genuine miss is `422 RATE_UNAVAILABLE`, never a silent `1.0`"*.
  **False after CR3.** Writes now succeed and warn (decision D-b); the miss surfaces
  at read time as `null` + `unconverted_count`. The "never a silent 1.0" half is
  still true and worth keeping — it is the invariant that survived.
- **`:35`** — *"the dominant-side rule keeping cross-currency transfer pairs
  zero-sum — survives in `transfers.py` and is unaffected"*. **False after CR3.**
  Written on 2026-08-01 when WP1.1 deleted the recalc helper; the currency rework
  deleted the rule too. Correct it rather than deleting the paragraph — the
  distinction it draws (retired capability vs. genuine business logic) is still
  useful, the example just moved.

---

## Cross-cutting: `RATE_UNAVAILABLE` no longer blocks writes

Decision **D-b** changed a behaviour that several docs assert independently. Grep
for `RATE_UNAVAILABLE` across `docs/` and check every hit. Known stale ones:

- `scaling-boundaries.md:29` (above)
- **`TODO.md:94`** — *"cross-currency writes for dates without rates fail with
  `422 RATE_UNAVAILABLE` by design"*, in the backfill entry. The advice it supports
  ("run the backfill before importing historical data") is still good, but the
  reason changed: rows now import fine and simply don't contribute to PEN totals
  until a rate exists.

The error code itself may survive for `GET /exchange-rates`; confirm against the
code before deleting any definition.

---

## Other files

- **`TODO.md`** — reconciliation-chaining retirement is next (decision D-c). Note
  the currency rework as done with its date. Also correct **`:9`**, which asserts
  *"The dominant-side zero-sum logic does survive, in `app/helpers/transfers.py`"* —
  true when written on 2026-08-01, false after CR3. And **`:94`**, per the
  `RATE_UNAVAILABLE` note above. Per the repo's absolute-dates rule, append a dated
  correction rather than rewriting the original sentence.
- **`docs/roadmap.md`** — if it describes stored home amounts or the dominant-side
  rule, correct it. Per the repo's absolute-dates rule, historical dated entries
  stay as written; add a dated note rather than rewriting history.
- **`docs/lessons-lunchmoney.md`** — two stale claims *about our own design*, not
  just notes on theirs. **`:11`**: *"Our `amount_cents` (native) + `exchange_rate` +
  `amount_home_cents` (home currency cache) follows this exact pattern. The design
  is correct."* **`:27`**: *"Our locked-rate-at-entry model is correct"*, plus a
  description of date-change recalculation that CR3 deleted. Append a dated note
  that the design changed on 2026-08-01 and why, rather than rewriting the original
  lesson — the observation about Lunch Money may still be accurate; the conclusion
  we drew from it is what changed. Check the other `lessons-*.md` files for the same
  pattern while here.
- **`CLAUDE.md`** — ✅ **already amended 2026-08-02**, no action expected. The
  **Home currency** convention was rewritten from *"every response that contains an
  amount must include a home-currency version"* (written for the stored-column
  model) to a per-surface table implementing D-e, plus the read-time and
  `null` + `unconverted_count` rules. **Verify it matches what actually shipped**
  and correct any drift; do not rewrite it from scratch.
- **`docs/currency-rework/`** — this directory is transient. Once this package
  lands, either delete it or add a "superseded, see `currency-model-decision.md`"
  banner to `README.md` — **and remove its row from the `CLAUDE.md` Key
  documentation table** either way.

---

## Done when

- [ ] **Ran the greps first**, not just the checklists — `amount_home_cents`,
      `exchange_rate`, `dominant`, `RATE_UNAVAILABLE` across `docs/`, `CLAUDE.md`,
      `TODO.md`. Two prior audits each caught sites an enumeration had missed.
- [ ] No doc claims transactions store a rate or a home amount
- [ ] No doc shows `amount_home_cents` on a **transaction or inbox** response
      shape. Known sites: `engine-spec.md:352,353,355,395,447,466,730`
- [ ] `engine-spec.md:125` no longer says writes 422 on a missing rate, nor that a
      client may supply `exchange_rate` to bypass the lookup
- [ ] Docs still show home values on **reports, month totals, archived lifetime
      panels and account balances** — the sweep must not over-delete. If
      `current_balance_home_cents` vanished from the spec, you went too far.
- [ ] `grep -rn "dominant" docs/ CLAUDE.md TODO.md` returns only dated historical
      entries — **no doc still describes the rule as live**. Known sites:
      `api-design-principles.md:113`, `scaling-boundaries.md:24,35`, `TODO.md:9`
- [ ] `grep -rn "RATE_UNAVAILABLE" docs/ TODO.md` — every hit reflects D-b
      (writes succeed and warn), or is scoped to `GET /exchange-rates`
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
