# CR3 — Write paths stop resolving rates; migration drops the columns

**Prerequisites:** CR1 + CR2 merged. Read
[`../currency-model-decision.md`](../currency-model-decision.md) first.
**Blocks:** CR4, CR5, CR6. **Blocked by:** CR2 — reads must already be computing,
or dropping the columns breaks every response.

---

## Goal

Remove all rate resolution from write paths, drop `exchange_rate` from the API
contract, and land `sql/019` dropping the three columns. This is the contract half
of the expand/contract migration.

**This package closes audit findings 1.2, 1.3, 1.4 and 1.5 — by deletion, not
repair.**

---

## Why each finding closes

| Finding | Today | After |
|---|---|---|
| **1.3** 🔴 | Transfer dominant-side rule tests the caller override *before* the currency-match rule (violating §547), and `raise RuntimeError` at `transfers.py:166` is reachable — **every USD→USD transfer 500s** for a PEN-home user | No dominant-side rule exists. Each leg stores its own `amount_cents`. Unrepresentable. |
| **1.4** 🔴 | Inbox `exchange_rate` defaults to `1.0`; update re-rates only on `date` change; promote uses the stored value verbatim → a USD expense promotes at rate 1.0 | No stored rate, no `1.0` default. Unrepresentable. |
| **1.5** 🔴 | `PUT /transactions` changing `account_id` never re-rates, so a PEN→USD move keeps the PEN conversion | Nothing stored to fall out of sync. Unrepresentable. |
| **1.2** 🟡 | The surviving dominant-side implementation is the buggy one | Deleted with the rule. |

---

## Files and changes

### `app/helpers/transfers.py` — delete the dominant-side block

Lines **119-177**, roughly 60 lines. Remove in full:

- the `user_settings` fetch at `:145-148`
- all four branches at `:150-171` — caller override, primary-matches-home,
  sibling-matches-home, and the `else: raise RuntimeError`
- the forced `sibling_home = primary_home` assignment
- `primary_home` / `sibling_home` / `sibling_exchange_rate` entirely

Both `INSERT`s (`:182-237`) stop writing `amount_home_cents` and `exchange_rate`.
Each leg writes its own `primary_abs` / `sibling_abs` and nothing else.

Drop the now-unused `primary_exchange_rate` parameter from the signature
(`:30`) and from all call sites.

⚠️ Keep the `primary_id == sibling_id` collision check (`:173-177`) — unrelated,
still needed. Audit WP10.2 notes it runs late and outside the accumulate-errors
pattern; **leave that for WP10.2**, don't refactor here.

### `app/helpers/inbox.py`

- `:94` — drop the `lookup_exchange_rate` call on create
- `:104` — remove `COALESCE($10, 1.0)` and the `exchange_rate` column from the INSERT
- `:200-207` — delete the date-triggered re-rate block entirely
- `:447` — remove `primary_exchange_rate=` from the `create_transfer_pair` call
- `:460-461` — promote stops reading a stored rate and computing
  `amount_home_cents`; the INSERT stops writing those columns

### `app/helpers/transactions.py`

- `:319` — remove the create-path rate lookup
- `:523-526` — delete the date-triggered re-rate
- `:528-531` — delete the `amount_home_cents` recompute
- `:1216` — remove the batch-create rate lookup
- Both INSERT paths stop writing the two columns

### `app/helpers/exchange_rate.py` — delete `lookup_exchange_rate`

Lines **171-220**. All five call sites are removed above. Its raise-on-missing
behaviour (`RATE_UNAVAILABLE` → 422) **is the write-blocking policy being
retired** — writes must no longer fail because a rate is absent.

Keep `get_rate`, `batch_get_rates`, `clear_rate_cache` — still used by account
balances, reconciliations, `GET /exchange-rates`, and CR1's parity test.

Check whether `rate_unavailable` in `app/errors.py` still has callers. If not,
remove it too.

### Schemas — drop `exchange_rate` from the wire (decision D-a)

- `app/schemas/transactions.py:25,38` — request models
- `app/schemas/transactions.py` — `TransactionResponse` and `transaction_from_row`
- `app/schemas/inbox.py` — request and response models

ℹ️ **`amount_home_cents` is already gone from these responses — CR2 removed it**
(owner decision, 2026-08-02: individual transactions and inbox items carry no PEN
value). This package only has `exchange_rate` left to remove. If you still find
`amount_home_cents` in a transaction or inbox schema, CR2 is incomplete — stop and
finish it rather than absorbing the work here.

**Account balances keep `current_balance_home_cents`.** Do not remove it for
consistency; it is a compare-across-currencies figure and is out of scope.

**Both directions.** A rate belongs to a (currency, date) pair, not a transaction;
`GET /exchange-rates` already serves it. Keeping it as a read-only computed field
would preserve the mental model that caused these three findings.

The field is **removed**, not nulled — a deliberate, documented exception to
null-over-omission, same treatment as the `recalculation` field in the WP1.1
change. Note it in CR5's spec pass.

### Missing-rate warning (decision D-b)

A write whose date has no resolvable rate **succeeds**. The response carries a
warning naming the pair and date, e.g.:

> `No USD→PEN rate on or before 2023-05-01; this transaction is excluded from
> home-currency totals until one exists.`

A `warnings` key already exists on transaction delete/restore
(`transactions.py:778, 1085`). Audit **WP10.2** flags it as inconsistently present
— stabilise it across create/update/get while here, closing that item.

### `sql/019_drop_stored_home_currency.sql`

```
expense_transactions.amount_home_cents
expense_transactions.exchange_rate
expense_transaction_inbox.exchange_rate
```

Header comment must follow `sql/018`'s house style: what is dropped, why, what
depends on it, and the restoration path if a future author wants stored home
values back. Reference `docs/currency-model-decision.md`.

**Both tables hold 0 rows (verified 2026-08-01)** — no data migration.

### Idempotency-key cutover

`helpers/idempotency.py:70-80` replays a stored `response_snapshot` **verbatim** for
24 hours. So a write issued before this rework and retried after it returns the
**old response shape** — carrying `amount_home_cents`, `exchange_rate`, and any
other field CR2/CR3 removed — from a code path that no longer produces those fields.

Not a correctness bug (the replay is doing its job), but it is a 24-hour window in
which the contract is not what the docs say. Either purge `idempotency_keys` when
`sql/019` is applied, or state the window explicitly in the release note. Purging is
simpler and the table is disposable by design (24h TTL).

`exchange_rates`, `app/jobs/fetch_exchange_rates.py` and
`app/jobs/backfill_exchange_rates.py` are **kept and become load-bearing** —
every report now reads them at query time.

Afterwards: `deploy/local/create-test-db.sh --force`.

---

## Tests

**The 1.3 repro is the important one.** No existing transfer test seeds a
non-home-currency account, which is exactly why the USD→USD 500 shipped.

- **USD→USD transfer with PEN home** → 201, both legs stored natively, report nets
  to 0 after conversion. Previously a guaranteed 500.
- **Cross-currency promote after an `account_id`-only PUT** (1.4 repro) — create an
  inbox item with a date but no account, PUT only the `account_id` to a USD
  account, promote. Must not land at rate 1.0.
- **Account-move re-rate** (1.5 repro) — move a transaction from a PEN account to a
  USD account; the home value must reflect USD.
- **Missing rate → write succeeds and warns** — a transaction dated before
  2024-03-02 is accepted, with the warning present.
- **`exchange_rate` rejected on write** — sending it now fails (CR4 adds
  `extra="forbid"`; until then it is silently ignored, so assert on the response
  not containing it).
- **`exchange_rate` absent from every response** — transactions, inbox, sync.

---

## Done when

- [ ] `grep -rn "amount_home_cents\|exchange_rate" app/helpers/transfers.py` →
      nothing
- [ ] `lookup_exchange_rate` deleted; no references remain
- [ ] `exchange_rate` absent from all request **and** response models
- [ ] `sql/019` applied to the dev DB; `create-test-db.sh --force` re-run
- [ ] `grep -rn "amount_home_cents" sql/` → only the `sql/019` drop statement
- [ ] Writes with an unresolvable rate return 2xx **with** a warning
- [ ] `pytest` green (expect a lower total — deletions removed tests)
- [ ] Manual: `POST` a USD→USD transfer against the local engine → **201, not 500**

---

## Do not

- Touch field guards, `extra="forbid"`, or the system-category lock — CR4
- Update `engine-spec.md` / `schema-reference.md` / `client-breaking-changes.md`
  — CR5
- Touch the CLI — CR6, separate repo
- Delete `get_rate`, `batch_get_rates`, `exchange_rates`, or the FX jobs — all
  still required
- Add an `@FX` category — deferred, D-d in [README.md](README.md)
