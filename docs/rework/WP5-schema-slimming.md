# WP5 — Delete the columns and routes nothing reads

**Read [`README.md`](README.md) first.** Independent of every other package — run it whenever.

This is the low-risk package. Fifteen columns and four routes, none of which any engine
logic branches on. Each deletion is small, but the *reasoning* for each differs, and the
reasoning is why they were approved. Read it before assuming they're interchangeable.

---

## Group 1 — Six echo-only settings columns

`user_settings.theme`, `.start_of_week`, `.transaction_sort_preference`,
`.sidebar_show_bank_accounts`, `.sidebar_show_people`, `.sidebar_show_categories`

The engine stores these, returns them, and **branches on none of them**. They are client
state living in the engine's database. Their entire justification is propagating
preferences between devices — and there is one device.

Verified end-to-end on 2026-08-04, in both repos:

- Nothing in `app/` reads any of them to make a decision.
- The CLI (`../expense_world_CLI/expense/commands/auth_cmd.py`) exposes all six as
  `--flag` options and **writes** them, but grepping for any *branch* on them returns
  nothing.
- The TUI's theme system (`../expense_world_CLI/expense/tui/theme.py`) is entirely local —
  Textual `Theme` objects with their own `ctrl+p` picker. **It never reads
  `user_settings.theme`.**
- Nothing in the engine computes a week, so `start_of_week` has nothing to configure.
- `GET /transactions` ordering comes from its own `sort` / `order` query params, not from
  `transaction_sort_preference`.

**After this, `PUT /auth/settings` mutates exactly one meaningful field:
`display_timezone`.** (`main_currency` is locked to `'PEN'` by `sql/018` and 422s on any
change attempt — it stays, deliberately, as a chokepoint rather than a literal scattered
across ~10 call sites.)

The CLI must drop six options. That is a breaking change; record it.

## Group 2 — `is_archived` on categories and hashtags

`expense_categories.is_archived`, `expense_hashtags.is_archived`, plus four routes:
`POST /categories/{id}/archive`, `/unarchive`, and the hashtag equivalents.

The argument is not merely "unused" — it is **redundant with a feature you already have**.
Soft delete (`deleted_at`) already hides a row from pickers while leaving past transactions
that reference it fully intact. That is what archiving was for.

Its only remaining reader is the dashboard's `archived_categories` / `archived_hashtags`
panels, which WP2 removes.

> **`is_archived` on `expense_bank_accounts` stays.** An archived *account* still holds
> real money; an archived *category* holds only history. That asymmetry is the whole
> reason one survives and two don't. Do not "tidy" the account one for consistency.

Note also: `compute_month_flow` deliberately has **no** `is_archived` filter — archived
categories must stay in reports so the visible rows sum to the total. Removing the column
must not change any report figure. If it does, something was filtering that shouldn't have
been.

## Group 3 — Dead columns with no reader at all

| Column | Why it's free |
|---|---|
| `global_currencies.name` | Zero reads in `app/`. Only `code` is ever selected. |
| `global_currencies.symbol` | Zero reads. Clients hardcode `S/` and `$`. |
| `activity_log.actor_type` | Added to distinguish `user` / `system` / `admin`. **No caller has ever passed a non-default value** — every `write_activity_log` call takes the `"user"` default. It encodes a multi-actor future that does not exist at one user with no worker. |
| `users.email` | Populated from the JWT claim; that claim source was deleted 2026-08-03 and nothing replaced it. `deps.py` returns `email=None` unconditionally. The existing row keeps a legacy value only because bootstrap writes email on the *insert* branch alone — any new user would get `NULL`. Still advertised on the wire, which makes it a field that lies. |
| `user_settings.deleted_at` | Added to satisfy the every-mutable-table-has-`deleted_at` convention. A settings row is never soft-deleted; no code sets it and `_fetch_user_settings` does not filter on it. |
| `idempotency_keys.processed_at` | Written on every claim, read by nothing. `expires_at` is the only temporal guard. |
| `expense_transaction_hashtags.version` | **Is written** — the re-tag upsert bumps it via `ON CONFLICT DO UPDATE` — but read by nothing. Junction rows never take part in optimistic concurrency. The deletion therefore also touches that upsert clause; it is not purely a `DROP COLUMN`. |
| `expense_transactions.parent_transaction_id` | Never written by any code path. A self-FK for an unbuilt split-transaction feature, appearing on the wire as a permanent `null`. The *column* is a placeholder, not a foundation — deleting it does not make splits harder to build later, because you would design that fresh anyway. |

`global_currencies` keeps its `code` column and its FK role. Do **not** drop the table —
the foreign keys from four currency columns are doing real referential work, and the
`CHECK (code IN ('USD','PEN'))` from `sql/015` is what makes adding a third currency an
explicit, reviewable migration.

Also remove `transactions.fetch_hashtag_ids_map` if it is still there — an orphan function
with zero references in `app/` **and** zero in `tests/`.

## Group 4 — One small fix, not a deletion

**Validate `display_timezone` on write.** It is unvalidated user input settable via
`PUT /auth/settings` (`sql/002`), and Python and SQL disagree about a bad value:
`compute_month_bounds` catches an invalid zone and silently falls back to UTC, while
`AT TIME ZONE` raises and would 500. After WP2 that expression is on the read path for
every report.

Validating on write is the root fix — `CLAUDE.md`'s "fix at the root, not the call site".
It must still reach SQL as a bind parameter, never interpolated.

## What you must work out

- **Whether `TransferDirection` or any other constant is left orphaned** by the other
  packages by the time you run. Check `app/constants.py` for enums with no callers.
- **Whether the six settings fields appear in more places than the schemas** — request
  model, response model, bootstrap defaults, sync serialisation (until WP4), tests.
- **What `PUT /auth/settings` should do with an unknown field.** `CLAUDE.md` says unknown
  input must 422 rather than be silently dropped. Confirm the request model is
  `extra="forbid"`; if it is not, that is a fail-closed defect worth fixing here, because a
  CLI still sending `--theme` should get a clear error rather than silence.
- **Whether `users.email` should be dropped or repopulated.** Dropping is the audit's
  recommendation, since PAT auth has no email source. Either is defensible — but do one.
  A permanently-null field on the wire is the option that is definitely wrong.

## Where to look

```bash
grep -rn "theme\|start_of_week\|transaction_sort_preference\|sidebar_show" app/ tests/
grep -rn "is_archived" app/ tests/          # keep the expense_bank_accounts hits
grep -rn "actor_type\|processed_at\|parent_transaction_id\|fetch_hashtag_ids_map" app/ tests/
```

| File | Role |
|---|---|
| `app/schemas/auth.py`, `app/routers/auth.py`, `app/helpers/auth.py` | Settings request/response shapes and bootstrap defaults |
| `app/routers/categories.py`, `app/routers/hashtags.py` | The four archive routes |
| `app/helpers/activity_log.py` | `write_activity_log`'s `actor_type` default |
| `app/helpers/transactions.py` | The junction upsert that bumps `version` |
| `../expense_world_CLI/expense/commands/auth_cmd.py` | The six CLI options that must go |

## Invariants that must survive

- **Null over omission.** Optional fields with no value are returned as `null`, never
  omitted. Response shape never changes based on data presence. Deleting a field means
  deleting it from the model, not conditionally omitting it.
- Soft delete stays on every table that legitimately has it.
- Every mutation still writes an activity-log row with before/after snapshots.
- Archived *accounts* still work, still hide from default lists, still participate in
  history and still hold a balance.
- Report figures do not change. This package should be numerically invisible.

## Definition of done

- [ ] All 15 columns dropped by migration; greps for each return nothing in `app/`.
- [ ] The four archive/unarchive routes gone; route count reduced by 4.
- [ ] The junction's `ON CONFLICT DO UPDATE` no longer references `version`.
- [ ] `display_timezone` is validated on write, as a bind parameter, with a 422 on an
      invalid zone.
- [ ] `PUT /auth/settings` rejects unknown fields with 422 rather than dropping them.
- [ ] A test proves report totals are unchanged by the `is_archived` removal.
- [ ] `pytest` green.
- [ ] Entry appended to `docs/client-breaking-changes.md`: six settings fields, four
      routes, `is_archived` on two resources, `parent_transaction_id` and `email` gone from
      responses. Flag the six CLI options that must be removed from `auth_cmd.py`.

## Out of scope

- `is_person` and `transaction_source` — both are open **product** questions, documented in
  [`README.md`](README.md). They look like dead columns and are not; do not sweep them up.
- `is_archived` on `expense_bank_accounts`.
- `main_currency`, even though it has one legal value.
- The `users` table itself, and `user_id` anywhere. Only the `email` column is in scope.
