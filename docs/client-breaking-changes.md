# Client Breaking Changes

Engine changes that require work in a client repo (`expense_world_CLI`, and any
future iOS / web client). Newest first.

Only entries that **break a client** belong here. Additive changes — a new
endpoint, a new nullable response field — do not. If a client can ignore the
change and keep working, it is not a breaking change.

Each entry states what changed, what breaks, and what the client must do.

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
> next one.** The currency rework (decisions D-e, D-g, D-h, D-i in
> [`currency-rework/README.md`](currency-rework/README.md)) removes
> `amount_home_cents` from transactions and inbox items,
> `current_balance_home_cents` from accounts, the reconciliation home balances, the
> native report aggregates, and the dashboard's archived category/hashtag panels.
> **Response shapes change substantially.** No *values* change and nothing needs
> re-syncing — the removed figures were derived, never stored facts — but a client
> that reads those keys will find them absent. CR5 writes the full entry when the
> code lands; this pointer exists so nobody plans against the sentence above in the
> meantime.

### Engine references

- `sql/018_lock_home_currency_to_pen.sql` — the constraint and the restoration path
- `docs/engine-spec.md` §`PUT /auth/settings`
- `docs/audit-2026-08-01-remediation-plan.md` WP1.1
