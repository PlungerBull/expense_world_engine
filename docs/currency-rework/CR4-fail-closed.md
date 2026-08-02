# CR4 — Fail-closed sweep

**Prerequisites:** CR3 merged (§1 and §2 depend on it; **§3 does not** — see
below). Read [`../currency-model-decision.md`](../currency-model-decision.md),
section "Decided: system categories are engine-assigned only".
**Blocks:** CR5. **Blocked by:** CR3, partially.

---

## Goal

Close the three holes that let `@Transfer` mean something other than what the
decision doc says it means — and fix the design that produced them, rather than
the individual symptoms.

---

## Why — the root cause

`@Transfer ≠ 0` is supposed to mean exactly two things: an FX spread, or a
loan/repayment with a person. After CR2 it becomes a number the owner actually
reads. A signal you consult is only worth having if it cannot be polluted.

Three open paths pollute it. All three are the same design flaw:

> **The codebase has three deny-lists and zero allow-lists.** Each enumerates what
> is *forbidden*, so every new field is permitted by default.

```
transactions.py:455     locked  = {amount_cents, account_id, title, date}
transactions.py:498     blocked = {amount_cents, account_id, date,
                                   exchange_rate, amount_home_cents}
reconciliations.py:562  _LOCKED_FIELDS_WHEN_COMPLETED
```

`category_id` missing from the transfer set is not an oversight — it is what this
design produces. Two further symptoms are already in the audit: no
`extra="forbid"` anywhere (unknown fields silently dropped, WP6.1), and §466's
documented `amount_home_cents` transfer lock that **can never fire** because the
schema discards the field before the guard sees it.

Per `CLAUDE.md` → "The engine comes first": fix the design, not the instance.

### The three holes, concretely

| # | Hole | Effect |
|---|---|---|
| 1 | `POST /transactions` accepts a system `category_id` | Log groceries under `@Transfer` → dashboard reports a S/120 FX loss or loan that never happened |
| 2 | `PUT /transactions/{id}` moves an ordinary transaction *into* a system category | Same, via edit |
| 3 | `PUT /transactions/{id}` moves a transfer leg *out of* `@Transfer` | Edit one leg of a S/500 Savings→Checking transfer to "Groceries": `@Transfer` reads **+500** (a repayment that never happened) and Groceries reads **−500** (spending that never happened). Two lies from one edit, and your wealth didn't change at all. |

---

## §1 — Deny-lists become allow-lists

Declare what **is** mutable; everything else 422. New fields then default to
*blocked*, which is the whole point.

### 1a. Transfer legs — `helpers/transactions.py:497-506`

```
allow = {title, description, cleared, reconciliation_id, hashtag_ids}
```

Everything else 422. Closes hole 3 and every future field of its class.

Rationale for the allow-list: each of these is genuinely per-leg. You clear one
leg against one account's statement; you assign one leg to one reconciliation; a
typo in a title is harmless. Amount, account and date would desync the pair —
which is why the design is "transfers are edited by delete + recreate"
(`transactions.py:490-496`).

**This set is being edited anyway** — two of its five current fields
(`exchange_rate`, `amount_home_cents`) no longer exist after CR3. Inverting it is
nearly free.

### 1b. Completed-reconciliation lock — `helpers/transactions.py:455`

```
allow = {description, category_id, hashtag_ids}
```

A reconciliation signed off against a bank statement must not have its amount,
account, title or date moved. Classification is still fair game.

### 1c. `helpers/reconciliations.py:562` — `_LOCKED_FIELDS_WHEN_COMPLETED`

Same inversion, same reasoning.

**Preserve the existing error envelope and the §646 / §652 messages verbatim** —
tests and the CLI assert on them. Only the direction of the check changes.

---

## §2 — `extra="forbid"` on transaction and inbox request models

Without it, Pydantic silently drops unknown keys: a typo returns `200` and the
change vanishes. That is the same fail-open disease at the schema layer, and it is
what makes the §466 lock unfirable.

Add `model_config = ConfigDict(extra="forbid")` to transaction and inbox request
models. `app/schemas/accounts.py:11,24` already does this — **follow that
pattern**.

**Scope: only transaction and inbox schemas.** The codebase-wide sweep stays audit
WP6.1. Do not expand.

While here, check each update-schema's declared fields against its spec section —
audit WP6.1 notes several mismatches (`is_person` silently dropped on account
update, `is_system` on category/hashtag PUT). Fix only the ones in your files;
note the rest for WP6.1.

---

## §3 — System categories are engine-assigned only

**Independent of CR1–CR3.** Touches only `helpers/validation.py` (plus tests), so
it may be pulled forward and run standalone at any time.

**Owner decision, 2026-08-01:** `@Transfer`, `@Debt` and `@Opening` may be attached
to a transaction **only** by the engine flow that owns them. Clients never set
them by hand.

`validate_active_category` (`helpers/validation.py:94-114`) checks `deleted_at` and
`is_archived` but **not `is_system`**. No other guard exists.

**Fix at the public boundary**, returning 422. The internal paths must keep
working:

- `create_transfer_pair` (`helpers/transfers.py:99-108`) assigns `@Transfer` /
  `@Debt` per leg
- `create_opening_balance` (`helpers/accounts.py`) delegates to
  `create_transaction` with `@Opening`

So the check belongs where client input is validated, not inside
`create_transaction` — or `create_transaction` needs an explicit internal-caller
flag. Prefer the boundary; it is the same shape audit **WP7.4** prescribes for
reserved system-category *names*.

⚠️ **Coordinate with WP7.4.** It rejects reserved names (`@Debt`, `@Transfer`,
`@Opening`) on `POST`/`PUT /categories` at the same boundary. If WP7.4 has already
landed, extend its guard rather than adding a parallel one. Together the two make
`@Transfer` mean exactly what the decision doc says.

---

## Tests

- **Hole 3:** `PUT` a transfer leg's `category_id` → 422. Then assert
  `title` / `description` / `cleared` / `reconciliation_id` still succeed on a
  transfer leg — the allow-list must not over-block.
- **Hole 1:** `POST /transactions` with a system `category_id` → 422 (all three
  system keys).
- **Hole 2:** `PUT /transactions/{id}` moving into a system category → 422.
- **Engine paths still work:** creating a transfer still assigns `@Transfer` /
  `@Debt`; `POST /accounts/{id}/opening-balance` still assigns `@Opening`.
- **Unknown field → 422** rather than a silent `200` no-op.
- **Completed-reconciliation lock:** blocked fields still 422 with the §646
  message; allowed fields still succeed.
- **Retighten `test_reconciliation_ordering.py:462-468`**, which currently asserts
  the *wrong* 200 for `sort_order` in a PUT body (audit WP5.3 — the guard at
  `reconciliations.py:524-528` is unreachable because the schema drops the key).
  `extra="forbid"` may make this fire correctly; if the schema is in scope, declare
  `sort_order` so §652's exact message is produced.

---

## Done when

- [ ] All three deny-lists are allow-lists; no `blocked`/`locked` set enumerates
      forbidden fields
- [ ] `category_id` on a transfer leg → 422; the five allowed fields still work
- [ ] `extra="forbid"` on transaction and inbox request models
- [ ] System `category_id` rejected at create and update; engine-owned flows
      unaffected
- [ ] Error envelope and §646/§652 messages unchanged
- [ ] `pytest` green
- [ ] Manual: `@Transfer` on an ordinary expense → 422; unknown field → 422;
      transfer `category_id` edit → 422

---

## Do not

- Extend `extra="forbid"` beyond transaction/inbox schemas — WP6.1
- Change the reconciliation state machine — WP5
- Touch `helpers/transfers.py` category assignment — it is correct as written
- Update spec/schema docs — CR5
- Add an `@FX` category — deferred, D-d in [README.md](README.md)
