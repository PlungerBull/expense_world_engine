# CR6 — CLI handoff

> **⚠️ This package is executed in a DIFFERENT REPOSITORY: `expense_world_CLI`.**
> It is filed here because the engine change is what forces it. An agent doing
> this work should be started in the CLI repo, with this file and
> [`../client-breaking-changes.md`](../client-breaking-changes.md) as input.

**Prerequisites:** engine CR3 merged and deployed locally. **Blocked by:** CR3.
**Blocks:** nothing.

---

## Goal

Stop the CLI sending `exchange_rate`, and stop it expecting one back.

---

## Why

The engine no longer accepts or returns a per-transaction exchange rate. Amounts
are stored in the account's own currency; the PEN value is computed at read time
from the rate table using the rate on the transaction's date.

Per the engine repo's `CLAUDE.md` → "The engine comes first": **this breakage is a
documented cost, not a reason to have kept the field.** The rate belongs to a
(currency, date) pair — `GET /v1/exchange-rates` serves it — not to a transaction.

Full model: [`../currency-model-decision.md`](../currency-model-decision.md).

---

## Call sites

| File | Line | What | Change |
|---|---|---|---|
| `expense/commands/log_cmd.py` | 34, 94-95 | `--exchange-rate` option; adds to payload | **Remove both** |
| `expense/commands/inbox_cmd.py` | 226, 254-255 | `--exchange-rate` on inbox add | **Remove** |
| `expense/commands/inbox_cmd.py` | 278, 297 | `--exchange-rate` on inbox update | **Remove** |
| `expense/commands/transactions_cmd.py` | 305, 328 | `--exchange-rate` on transaction update | **Remove** |
| `expense/commands/transactions_cmd.py` | 52 | help text: *"amount_cents/account_id/date/exchange_rate are read-only on transfer legs"* | **Rewrite** — see below |
| `expense/commands/accounts_cmd.py` | 248-250 | `--exchange-rate` on opening balance, help *"Override engine auto-fetch."* | **Remove** |
| `expense/import_/parse.py` | 206-213 | `usd-no-rate` / `bad-rate` skip reasons | **Delete the skip** — see below |
| `expense/import_/parse.py` | 29, 47, 223, 236 | `exchange_rate: Decimal \| None` on row models | **Remove the field** |
| `expense/import_/apply.py` | 198-202, 213-216 | adds `exchange_rate` to the payload | **Remove** |
| `tests/unit/test_resource.py` | 133 | asserts `format_field_value("exchange_rate", 1.0)` | Retarget to another field |
| `tests/unit/test_cmd_transactions.py` | 31 | `"exchange_rate": 1.0` in a fixture | Remove |
| `tests/unit/test_import_parse.py` | 101, 113 | asserts parsed `exchange_rate` | Remove |
| `tests/unit/test_cmd_import.py` | 34, 52 | `exchange_rate=None` in fixtures | Remove |

Also grep for any display of `exchange_rate` in TUI screens or table renderers.

---

## The CSV importer — the interesting one

`parse.py:206-213` currently **rejects any USD row that has no rate**:

```python
if currency == "USD":
    rate = parse_rate(cell("rate"))
    if rate is None:
        return SkippedRow(raw.line, "usd-no-rate", str(cell("rate")))
```

**Delete this entirely.** A USD row imports to a USD account as a USD amount — no
rate is required or wanted. The engine converts for reporting at query time using
the historical rate for that row's date.

Decide what to do with an existing `rate` column in user CSVs:

- **Recommended: ignore it silently.** Rejecting files that carry a now-meaningless
  column is hostile, and the historical rates are already backfilled (2024-03-02 →
  today, 881 daily USD→PEN rows), so the engine's own numbers are better than
  whatever the spreadsheet recorded.
- Optionally emit one informational line per import: *"`rate` column ignored —
  the engine converts using historical rates."*

Do **not** try to preserve the spreadsheet's rates by any other route. That is the
mental model the engine just removed.

**Rows dated before 2024-03-02** will import fine but won't contribute to PEN
totals until a rate exists for their date. The engine returns a warning on those
writes — surface it rather than swallowing it.

---

## Transfer-leg help text

`transactions_cmd.py:52` lists which fields are read-only on a transfer leg. That
list is now both wrong and inverted — engine CR4 replaced the deny-list with an
allow-list. The accurate statement:

> On a transfer leg, only `title`, `description`, `cleared` and `reconciliation_id`
> may be edited. Everything else — including `category_id` — returns 422. Transfers
> are changed by delete + recreate.

Note `category_id` explicitly: it used to be silently accepted and is now rejected.

---

## Other engine changes to absorb

Beyond `exchange_rate`:

0. **`amount_home_cents` is absent from transaction and inbox responses**, and from
   `/sync` payloads for those two resources. Absent, not null.

   ✅ **Verified 2026-08-02: the CLI does not use this field anywhere** —
   `grep -rn "amount_home_cents" expense/` returns nothing. No work expected. It is
   listed only so the absence is a confirmed fact rather than an assumption.

   ⚠️ **`current_balance_home_cents` on accounts is UNCHANGED** and the CLI does use
   it (`tui/screens/home.py:105`, `outstanding.py:54`). So are
   `spent_home_cents` / `net_home_cents` / `outflow_home_cents` on reports and the
   dashboard (`reports_cmd.py:42,112,126`, `home.py:110`). **Do not remove home-currency
   handling wholesale** — only the per-transaction field went away, and the CLI never
   read it.

1. **`exchange_rate` is absent from responses** — absent, not null. Any
   `.get("exchange_rate")` is dead code; any direct `[...]` access will `KeyError`.
2. **`warnings` may appear on create/update responses** — surface them. Previously
   only on delete/restore.
3. **`spent_home_cents` may be `null`**, with `unconverted_count` alongside, when a
   month contains a row whose date has no rate. Render "—" or similar; **do not
   render 0** — that is a different fact.

   `unconverted_count` arrives at **two levels**: on each category / hashtag row,
   and once per report (per month in the multi-month range form). Surface both — the
   report-level one as a visible warning line, since a blank cell is easy to skim
   past. `lifetime_spent_home_cents` on the archived panels follows the same rule.

   ⚠️ `format_cents` (`reports_cmd.py:42,53`) is already fed `.get(...)`, so it
   receives `None` today when a key is missing — confirm it renders a dash rather
   than `0` or crashing, because `None` stops being an edge case and becomes a
   documented state.
4. **`@Transfer` may show a non-zero total.** Two legitimate causes: an FX spread
   on a cross-currency transfer, or a loan/repayment with a person. Any TUI logic
   assuming it cancels to zero is now wrong.
5. **System categories are rejected** — `@Transfer`, `@Debt`, `@Opening` return 422
   if sent as `category_id`. Filter them out of any category picker.

---

## Done when

- [ ] `grep -rn "exchange_rate" expense/` returns nothing
- [ ] `--exchange-rate` gone from all five commands; `--help` mentions it nowhere
- [ ] CSV import accepts a USD row with no rate; a `rate` column is ignored, not fatal
- [ ] `warnings` on create/update surfaced to the user
- [ ] `null` `spent_home_cents` renders distinctly from zero
- [ ] Category pickers exclude system categories
- [ ] Transfer-leg help text matches the allow-list
- [ ] CLI test suite green
- [ ] Manual: `expense log` a USD expense, run a monthly report, confirm the PEN
      figure is right with no rate ever supplied
- [ ] `expense_world_CLI/docs/` updated — `cli-spec.md`, and `decisions.md` if the
      CSV `rate` column choice deserves recording

---

## Do not

- Reintroduce a rate field by another name or route
- Compute conversions client-side. The engine is the only thing that converts —
  that convention is unchanged and non-negotiable.
- Change engine code from this repo
