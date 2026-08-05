# Client Breaking Changes

Engine changes that require work in a client repo (`expense_world_CLI`, and any
future iOS / web client). Newest first.

Only entries that **break a client** belong here. Additive changes — a new
endpoint, a new nullable response field — do not. If a client can ignore the
change and keep working, it is not a breaking change.

Each entry states what changed, what breaks, and what the client must do.

---

## 2026-08-03 — Inbox transfers carry `transfer_direction`; `transfer_amount_cents` is now positive

**Engine change.** The inbox stored no direction column, so the *sign* of
`transfer_amount_cents` was the only record of which way a transfer draft
pointed. `sql/019` adds `transfer_direction` to `expense_transaction_inbox` and
stores both amounts positive, matching `expense_transactions` exactly. The
signed value the client sends is unchanged — only the stored and returned
encoding moves.

This closes audit findings **WP7.2**, **WP7.3** and the inbox half of **WP10.2**.
The defect that forced it: the primary leg's sign was discarded by `abs()` on
write and re-derived at promote time as the negation of the sibling's, so
`create_transfer_pair`'s opposite-sign guard was unreachable — a draft saved as
two outflows promoted cleanly with one leg silently flipped.

**Severity: breaking, but nothing in the CLI to break.** `expense/commands/inbox_cmd.py`
contains zero occurrences of "transfer" and its `promote` (`:377`) never sends
`transfer_id`, so the CLI cannot create or promote an inbox transfer today. The
work below is to *add* the feature against the corrected shape, not to repair
existing code.

### What breaks

**1. `transfer_amount_cents` on inbox responses is now always positive.**

Any client reading its sign to decide direction must read `transfer_direction`
instead — `1` = debit (the inbox row's own account pays), `2` = credit (it
receives). The sibling's direction is always the inverse. This affects
`GET /inbox`, `GET /inbox/{id}`, `GET /sync`, and the `before_snapshot` /
`after_snapshot` payloads on `GET /activity`.

**2. `transfer.id` is no longer accepted on `POST /inbox` or `PUT /inbox/{id}`.**

The inbox has its own request model without it. The field was required by the
schema and then discarded — it is the sibling *ledger row's* UUID, and no ledger
rows exist at draft time. The sibling's id is still supplied at promote time as
`transfer_id`. Note `POST /transactions` is **unchanged** and still requires
`transfer.id`; the two endpoints now take deliberately different shapes.

**3. A contradictory transfer draft now returns `422` instead of being accepted.**

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Transfer validation failed.",
    "fields": {
      "transfer.amount_cents": "Must have opposite sign to amount_cents."
    }
  }
}
```

Previously `{"amount_cents": -6000, "transfer": {"amount_cents": -1500}}` was
stored, listed as ready, and promoted with the primary silently rewritten to
`+6000`.

**4. `POST /inbox/{id}/promote` gained two `422` conditions.**

- `transfer_id` supplied on a non-transfer item — `{"transfer_id": "Must be null for non-transfer promotions."}`. Previously discarded in silence; spec §383 always required this.
- `transfer_account_id` pointing at a deleted or archived account — `{"transfer.account_id": "Must reference an active, non-archived account."}`. Previously surfaced only from deep inside the transfer engine, after the item had already passed readiness.

**5. `GET /inbox?ready=true` now returns transfer items it previously hid.**

Transfers have no `category_id` and the filter required one unconditionally, so
every promotable transfer draft was invisible. Conversely, items whose sibling
account is archived are now excluded — they would have `422`'d on promote. A
client that assumed "ready ⇒ has a category" must stop.

⚠️ **`?debit_as_negative=true` now flips the sibling too.** On a transfer the two
legs are returned with opposite signs. Previously `transfer_amount_cents` was
emitted as-stored beside a flipped primary, rendering a transfer as two amounts
pointing the same way.

### What the CLI must do

| Location | Current behaviour | Required change |
|---|---|---|
| `expense/commands/inbox_cmd.py` | No transfer support at all — no `--transfer-account-id` / `--transfer-amount` on `inbox create` / `inbox update` | **Add them.** Send `transfer: {account_id, amount_cents}` with a signed amount and **no `id`**. |
| `expense/commands/inbox_cmd.py:377` | `promote` sends `{"id": new_transaction_id}` only | **Send `transfer_id`** when the item has `transfer_account_id`. Without it a transfer item now `422`s with a clear field error rather than failing obscurely. |
| `expense/tui/screens/inbox.py:141-153` | `action_promote` calls `confirm_write` with no body; `_base.py:476-486` forwards no body argument | **Pre-existing break, unrelated to this change** — `InboxPromoteRequest.id` is required, so the TUI promote cannot have worked. Fix while adding `transfer_id`. |
| `expense/cache/db.py` / `expense/cache/sync.py` | Cached inbox table drops the transfer columns entirely | Add `transfer_account_id`, `transfer_amount_cents`, `transfer_direction` if inbox transfers are to render from cache. |
| `expense/tui/screens/inbox.py` (rendering) | n/a | When showing a draft, read `transfer_direction` for the arrow, never the amount's sign. |

### What does *not* change

- **Request signs are untouched.** `amount_cents` and `transfer.amount_cents` are
  still signed on the wire, still negative-for-outflow. Only storage and
  responses changed.
- `POST /transactions` and every ledger response — identical, including
  `transfer.id`.
- No amounts, balances or promoted transactions changed. Both tables held 0 rows
  when this shipped, so there is nothing to re-sync.

### Engine references

- `sql/019_inbox_transfer_direction.sql` — the column, backfill and constraints
- `docs/engine-spec.md` §`POST /inbox`, §`POST /inbox/{id}/promote`, §Transfers
- `docs/schema-reference.md` §`expense_transaction_inbox`
- `docs/open-bugs.md` WP7.2, WP7.3, WP10.2
- `tests/test_inbox_transfers.py` — the contract, end to end

---

## 2026-08-01 — Home currency locked to PEN; `main_currency` no longer updatable

**Engine change.** The home currency is fixed at **PEN** and cannot be changed.
`sql/018` adds `CHECK (main_currency = 'PEN')` on `user_settings`, and the
home-currency recalculation helper was deleted (it carried a silent `1.0`
exchange-rate fallback that wrote wrong home amounts — audit finding WP1.1).

**Severity: breaking.** One CLI feature stops working; one response field
disappears.

### What breaks

**1. `PUT /v1/auth/settings` with `main_currency` now returns `422`.**

Previously `200` plus a full-ledger recalculation. Now:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Home currency cannot be changed.",
    "fields": {
      "main_currency": "The home currency is locked to PEN and is not updatable."
    }
  }
}
```

⚠️ **This includes setting it to its current value.** `{"main_currency": "PEN"}`
is *also* a `422`. The field is not updatable at all — there is no
"it works if you don't actually change it" case. This is deliberate: a
sometimes-accepted field is worse to code against than a never-accepted one.

Other fields in the same request are unaffected — `{"theme": 2}` still returns
`200`. But do not send `main_currency` alongside them, or the whole request
fails.

**2. The `recalculation` response field is removed from `UserSettingsResponse`.**

It is **absent**, not `null`. This is an intentional exception to the
null-over-omission rule: the field described an operation that can no longer
occur, so leaving it permanently `null` would be dead weight in every settings
response forever.

### What the CLI must do

| Location | Current behaviour | Required change |
|---|---|---|
| `expense/tui/screens/system.py:245-253` | `ConfirmModal` reading *"This triggers a home-currency recalculation across your transactions on the engine."* | **Remove the flow.** The copy describes a deleted feature. |
| `expense/tui/screens/system.py:255` | `PromptModal("Main currency", "USD or PEN")` | Remove — nothing to choose. |
| `expense/tui/screens/system.py:280-286` | `_set_currency()` PUTs `main_currency` | Remove. Currently always errors (safely — the error path skips the config write, so `~/.expense-config` is not corrupted). |
| `expense/tui/screens/system.py:288-300` | `_currency_saved()` mirrors the value into config | Remove with the above. |
| `expense/tui/screens/system.py:111, 210` | Displays main currency | **Keep** — still a valid read-only field, always `PEN`. |
| `expense/commands/auth_cmd.py:213` | `--main-currency` option on `auth settings` | **Remove the option.** Passing it now guarantees a `422`. |
| `expense/commands/auth_cmd.py:234` | Docstring: *"main_currency change triggers engine recalc"* | Correct the text. |
| `expense/commands/auth_cmd.py:50-53` | Reads `body.get("recalculation")` | Dead code — remove. Degrades safely (`.get` → `None`), so this is cleanup, not a crash fix. |
| `expense/commands/auth_cmd.py` (`_render_recalc_summary`) | Renders the recalc summary | Dead — remove if it has no other caller. |
| `expense/config.py:40` | `main_currency: str \| None` cached locally | **Keep.** Still populated from `GET /auth/me` (`_cache_main_currency`, `auth_cmd.py:84-87`). It just never changes now. |

### What does *not* change

- `main_currency` is still returned on every settings/bootstrap response. Read it,
  cache it, display it — only writes are refused.
- Multi-currency **accounts** are unaffected. USD accounts still work exactly as
  before; USD amounts are still converted to PEN for reporting.
- No amounts, balances, or transaction shapes changed. Nothing needs re-syncing.

> ⏳ **The last bullet is scoped to this 2026-08-01 entry and does not survive the
> deletion program.** The rework in [`rework/README.md`](rework/README.md) removes
> `amount_home_cents` from transactions and inbox items,
> `current_balance_home_cents` from accounts, the reconciliation home balances, the
> native report aggregates, and the dashboard's archived category/hashtag panels —
> and separately drops `transaction_type = 3` and `transfer_direction` entirely
> (WP1), which *does* change how a client identifies a transfer.
> **Response shapes change substantially.** No *values* change and nothing needs
> re-syncing — the removed figures were derived, never stored facts — but a client
> that reads those keys will find them absent. Each work package appends its own
> entry here as it lands; this pointer exists so nobody plans against the sentence
> above in the meantime.

### Engine references

- `sql/018_lock_home_currency_to_pen.sql` — the constraint and the restoration path
- `docs/engine-spec.md` §`PUT /auth/settings`
- `docs/open-bugs.md` WP1.1
