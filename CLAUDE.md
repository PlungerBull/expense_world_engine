# expense_world_engine — CLAUDE.md

## What this repo is

The Brain. A Python (FastAPI) backend, backed by Postgres. It is the single source of truth for all business logic, validation, and data. The iOS app, CLI, and web dashboard are all equal clients — none of them implement logic. If it isn't in the engine, it doesn't exist.

**Deployment (since 2026-07-30): local profile** — the engine runs on the owner's Mac (launchd, `127.0.0.1:8000`) against Homebrew Postgres; the Render/Supabase cloud profile is mothballed until a second client needs it. Profiles live in `deploy/` (local = active, cloud = reactivation checklist); rationale in `expense_world_CLI/docs/decisions.md` ("Local-first deployment, 2026-07-30"). This changes where the engine runs, not what it is — every convention below holds unchanged.

## Key documentation

| Doc | What it contains |
|---|---|
| `docs/engine-spec.md` | Every endpoint, every business logic rule, every validation. The rulebook. |
| `docs/schema-reference.md` | Full database schema. |
| `docs/open-bugs.md` | **Known defects, by severity.** A work queue, not a document — delete a row when it is fixed rather than annotating it done. Read before assuming something is broken by accident. |
| `docs/design-philosophy.md` | UX philosophy and product vision. |
| `docs/client-breaking-changes.md` | Engine changes that require work in a client repo. Append here whenever a change breaks the CLI/iOS/web contract. |
| `docs/currency-model-decision.md` | How multi-currency works: native storage, conversion at read time, what `@Transfer ≠ 0` means. Read before touching exchange rates, `amount_home_cents`, or transfer home values. |
| `docs/rework/` | **Transient (2026-08-04).** The deletion program: work packages WP1–WP7, one per agent, executing the audit of 2026-08-04. Start at its `README.md`. **Read it before changing transfers, balances, currency, `/sync`, or reconciliations** — several conventions below are scheduled to change and WP7 rewrites them. Delete the directory and this row when WP7 lands. |

## Who this is for

**One user — the owner (since 2026-08-01).** The earlier 1000+ public-users target is retired; if scaling happens it gets a dedicated, professionally-staffed plan. Build what makes daily use work well, not what a hypothetical user base would need. Ledger correctness is never a scale trade-off — the conventions below hold at any size.

**What is single-user-shaped** (safe today, revisit only if a second user appears — none of these is a known defect):

| Constraint | At scale |
|---|---|
| One Mac, launchd, one Homebrew Postgres, no pooler | `deploy/cloud/` is the reactivation checklist |
| Pool `min=5 / max=20`, sized for direct connections | ~50 behind a transaction-mode pooler; `statement_cache_size=0` already set |
| **RLS inert — engine-side `user_id` scoping is the only guard** | Non-owner role, or `FORCE ROW LEVEL SECURITY`. Do it *before* user two |
| `sql/015` locks currencies to USD/PEN; no non-USD↔non-USD cross-rate | Lift the CHECK, implement cross-rate math, widen the FX job |
| `GET /sync` returns the whole delta, no cursor | Cursoring or `has_more` |
| No rate limiting at all | Required before any public exposure |
| No worker or queue — everything is request-synchronous | Needed by any import/report job |
| Nightly `pg_dump` to Drive, manual restore drill | Managed PITR |
| No PAT list endpoint, no `last_used_at` (deliberate — avoids a write per request) | Ship with a management UI |

A decision belongs in that table if **a bug report from a second user would be about it**. If one user could hit it and get a wrong number, it is business logic and belongs in `engine-spec.md`.

## The engine comes first (2026-08-01)

**The engine's job is to be correct and coherent, not convenient for its clients.**
We are not live. Prefer breaking a client and fixing the root cause over patching
around a design flaw — client repos absorb their own churn, that is their job.
Never let CLI/iOS/web convenience justify keeping a bad shape in the engine.

Corollaries:

- **Fix at the root, not at the call site.** A guard added to stop one symptom is a
  smell — ask what design let the symptom exist. (Example: the transfer-leg field
  guard was a deny-list that forgot `category_id`; the fix was inverting it to an
  allow-list, not adding one more field.)
- **Fail closed.** Enumerate what is *permitted*, never what is forbidden. New
  fields must default to blocked, unknown input must 422 rather than be silently
  dropped, and missing data must surface as `null` + a flag rather than a
  convenient substitute value.
- **Breaking a client is a documented cost, not a blocker.** Record it in
  `docs/client-breaking-changes.md` and proceed.

Do not weigh "this would be less work for the CLI" against engine correctness. It
is not a factor.

## Tech stack

- **Language:** Python
- **Framework:** FastAPI
- **Database:** Postgres (local profile: Homebrew 17 on the owner's Mac; cloud profile: Supabase). RLS policies ship in the schema (`auth.uid() = user_id`); live protection in the cloud profile, inert under the local owner connection.
- **Tests:** `pytest` (no flags, no env) against a dedicated `expense_world_test` database — never the ledger. Create it with `deploy/local/create-test-db.sh`, re-run with `--force` after a schema change. `tests/conftest.py` fails closed if pointed anywhere else.
- **Auth:** Bearer tokens — engine-issued PATs (local profile uses these exclusively) or Supabase Auth JWTs (cloud profile). Engine validates, extracts `user_id`, never stores passwords.
- **Hosting:** per deployment profile (`deploy/`) — local launchd service now; Render on cloud reactivation. Stateless either way — all state lives in Postgres.

## Non-negotiable conventions

These apply everywhere, no exceptions:

**Sign convention**
- Requests: `amount_cents` is signed. Negative = outflow. Positive = inflow. The engine infers `transaction_type` from the sign — callers never set it manually. Transfers are identified by the presence of a `transfer` field, not by sign.
- Storage: `amount_cents` is always stored as a positive integer. `transaction_type` (1 = outflow, 2 = inflow) encodes direction, on **every** row, never null. Enforced by the database — `CHECK (transaction_type IN (1, 2))` and `CHECK (amount_cents > 0)` (`sql/020`).
- Responses: `amount_cents` is always positive. `transaction_type` tells direction. The `?debit_as_negative=true` flag is a caller-side preference, not a schema property.
- **A transfer is two ordinary rows paired by `transfer_transaction_id`.** That FK is the *only* discriminator — there is no transfer transaction type, and a transfer leg is an ordinary outflow or inflow. "Who the counterparty was" is a fact about the pairing; "which way the money moved" is a fact about the row. Keeping them in one column is what forced direction into a second column in the first place (`transaction_type = 3` + `transfer_direction`, deleted by `sql/020`).
- **No column's sign means anything, anywhere.** Direction is always a separate typed column. `infer_transaction_type` (`app/schemas/transactions.py`) is the **single** place a sign is read — adding a second is the bug, not the fix. Its former twin `infer_transfer_direction` was byte-identical, which is what the duplication cost.
- **One rendering of the sign matrix, and it is now in SQL only.** `helpers/home_currency.signed_expr` is the single implementation; `monthly_report.py`, `dashboard.py` and `account_balance.py` call it rather than carrying their own copies — four literal copies drifting is audit finding WP9.1. The Python twin (`helpers/balance._delta_for_apply`) went with the stored balance in `sql/022`, and it had *already* sprouted a third copy inline in `transactions.create_batch` that no grep for `apply_balance` surfaced and no test covered. Writes read no sign matrix at all now: recording a row is the whole of the write.
- **This holds on the inbox identically.** The inbox is a draft ledger row: looser about *which fields are null*, never about *how a field encodes its meaning*. `transfer_amount_cents` was the one exception — signed until `sql/019` (2026-08-03), which is what let a same-sign transfer draft promote with a leg silently flipped (audit WP7.2). The lesson: a table that mirrors another copies the whole encoding or none of it — a half-copied convention makes the missing half load-bearing without anyone deciding it should be.
- ⚠️ **When a CHECK enforces a closed enum, spell out `IS NOT NULL`.** A `CHECK` rejects a row only when it evaluates to `FALSE`; `NULL` passes. `transaction_type IN (1, 2)` is `NULL` — not `FALSE` — on a null column, so a coherence constraint written that way silently admits the row it was added to forbid. `sql/020`'s `inbox_transfer_fields_coherent` carries the explicit guard and a test pins it.

**Home currency**
**The engine is the only thing that does currency conversion. Clients never compute it.** That part is absolute.

Home-currency values appear on **figures the user compares or sums across currencies** — never on individual ledger records:

| Surface | Home-currency field |
|---|---|
| Monthly report and `/dashboard` (they share `compute_month_flow`) | `spent_home_cents` per category and per hashtag combination |
| Month totals | `inflow_home_cents`, `outflow_home_cents`, `net_home_cents` |
| Account balances | `current_balance_home_cents` — the only view showing all your money at once |
| Individual transactions, inbox items, reconciliations | **none** — native currency only |

Those report figures are the **only** cross-account aggregates, and they have no native counterpart: `GROUP BY category_id` has no currency partition, so a category holding $15 and S/25 would report `4000` — a number in no currency. Never emit a total that sums across currencies without converting.

Values are **computed at read time**, never stored (`sql/021`; see `docs/currency-model-decision.md`). Conversion is a lookup of the rate for that row's date in the user's `display_timezone`, carried forward from the most recent rate on or before it. **The write path performs no rate lookup at all** — recording what happened is never blocked by a stale FX table.

⚠️ **A per-row `null` is not enough — every home-currency `SUM` is paired with an `unconverted_count`.** `SUM` skips nulls, and `SUM(CASE WHEN x > 0 THEN x ELSE 0 END)` scores a null row as **zero**, so an unflagged aggregate understates in silence. A non-zero count makes the figure `null` rather than a partial total. `app/helpers/home_currency.py` is the single implementation; `UNCONVERTIBLE_FLAG_EXPR` must be projected inside the CTE and summed by name outside it.

*Amended 2026-08-02, then 2026-08-05 (`docs/rework/WP2`). This convention previously read "every response that contains an amount must include a home-currency version", which was written for the stored-column model and made every transaction carry a PEN value no client rendered.*

**Null over omission**
Optional fields with no value are always returned as `null`, never omitted. Response shape never changes based on data presence.

**Soft delete everywhere**
All mutable *domain* tables carry `deleted_at` (nullable timestamptz). Hard deletion is never performed on financial records. Deleted records are excluded from active queries but remain in the DB. `user_settings` is the deliberate exception — a settings row lives and dies with its user and is never soft-deleted; its `deleted_at` sat permanently null on the wire until `sql/024` dropped it (WP5).

**Activity log on every mutation**
Every write to any mutable table produces an immutable `activity_log` row: resource type, resource ID, action (created/updated/deleted/restored), full before/after JSON snapshots, timestamp, actor. No exceptions. This is how "why does my balance look wrong?" gets answered.

**Idempotency keys on all writes**
`POST`, `PUT`, `DELETE` operations accept `X-Idempotency-Key: <uuid>`. The engine checks `idempotency_keys` before processing. Duplicates return the stored response verbatim. TTL: 24 hours. Critical for financial writes where duplicates corrupt balances.

**Auth on every route — PAT only**
Every request requires `Authorization: Bearer ewe_pat_…`. No public endpoints except `/health`. Unauthenticated requests return `401`.

The JWT branch was **deleted 2026-08-03** (audit 2.1), not disabled: it picked its verification key from the algorithm named in the token's own header, and the HS256 key was `local-unused` — a string committed in `.env.example`. Anyone who could reach the port forged a token for any `user_id`. Do not reintroduce a signing path without an expiry requirement, a pinned algorithm, and a secret that startup refuses to accept as a placeholder. `tests/test_auth_over_the_wire.py` is the only module that exercises auth without the `conftest` override — the absence of such a test is why this survived from the day it shipped.

**IDs-only in responses**
Responses reference related resources by ID only. No `category_name` beside `category_id`, no `account_name`, ever. Clients resolve display names from their own replica. A hydrated name is a second copy of a mutable value that goes stale the moment the row is renamed.

**Collection ordering**
User-ordered collections use a per-scope `sort_order integer NOT NULL DEFAULT 0`, listed ASC. New rows append (`max+1` within the scope). Soft-deleted rows keep their slot and reclaim it on restore. Cross-scope values are meaningless and never compared. `sort_order` is writable via the normal `PUT` **except** where reordering cascades to other rows — those expose `PUT /{parent}/{id}/{children}/order` with `{"ordered_ids": [...]}`, reject `sort_order` in the plain `PUT` with `422`, and renumber inside one transaction. Bulk reorder accepts any subset: the submitted rows' existing slots are reused in the new order, everything else is untouched.

**Tenant isolation is engine-side, not RLS**
RLS policies (`auth.uid() = user_id`) exist on all 15 tables and `rowsecurity` is on, but they are **inert** — the engine connects as the table owner and `FORCE ROW LEVEL SECURITY` is not set, so Postgres bypasses them. The `user_id` predicate in every query is the *only* thing isolating data. Treat a missing `user_id` filter as a security defect, not a tidiness one. Before a second user exists, either connect as a non-owner role or issue `FORCE ROW LEVEL SECURITY`.

**UUID-first**
All resources are identified by a UUID generated client-side before server confirmation. The frontend always has the ID before making a write. Resources are never looked up by name or any mutable attribute.

**Balances are computed at read time, never stored**
An account's `current_balance_cents` is the signed sum of its non-deleted transactions, including its `@Opening` seed, in the account's own currency. `app/helpers/account_balance.py` is the single implementation, and it reuses `home_currency.SIGNED_CENTS_EXPR` rather than rendering the sign matrix again. The field's wire shape is unchanged; only its source is.

- **No write path touches an account balance.** Creating, editing, soft-deleting or restoring a transaction *is* the balance change. There is no second write to keep in step, so no path can forget one — which is the entire point (`sql/022`).
- **`@Opening` is included in balances and excluded from flow reports.** Two rules that live one function apart. The report's `NOT EXISTS (… system_key = 'opening_balance')` filter belongs to `monthly_report.py` alone and must never be copied into the balance sum; an opening balance is not money that moved, but it is money you have.
- **Zero is a balance, not a missing value.** An account with no transactions reports `0` — never `null`, never an absent row. `fetch_balances` seeds every id it was asked about, so callers index with `balances[id]` and a forgotten account raises rather than silently reading as empty.
- **The balance is native-only.** Home-currency conversion is applied on top, per the home-currency rules above.
- ⚠️ **Splits will break a naive `SUM`.** `parent_transaction_id` is reserved and always null, so the sum is correct today. The documented rule is that a parent row is a display container that does not move the balance and only its children do — which a `SUM` has no way to honour. The predicate needed the day splits ship is written out in `account_balance.py` and `sql/022`.

*Replaced the "Balance updates are atomic" convention, 2026-08-06 (`docs/rework/WP3`). That rule existed only to protect a stored derived value; deleting the value leaves it nothing to protect. The sentence above is deliberately the same one the currency model uses — the two are one idea.*

**Batch = all or nothing**
Any batch endpoint wraps all operations in a single DB transaction. All succeed or all fail. Partial success is never acceptable for financial data.

**Reuse before writing**
Before writing a new helper, utility, or service function, check if one already exists in the codebase that does the same thing. Duplicate logic is a bug waiting to happen.

## Build phases (current status)

| Step | Scope | Status |
|---|---|---|
| 0–3 | Setup, Schema, Engine skeleton, Auth | ✅ Done |
| 4 | Accounts, Categories, Hashtags | ✅ Done |
| 5 | Inbox + Promote | ✅ Done |
| 6 | Transactions (Ledger) | ✅ Done |
| 7 | Transfers | ✅ Done |
| — | **Phase 1 complete. Deployed to Render.** | ✅ Done |
| 8 | Reconciliations | ✅ Done |
| 9 | Sync, Dashboard, Reports, Activity reads, Exchange rates | ✅ Done |
| 9.1 | ~~Home Currency Recalculation~~ | ⛔ Retired 2026-08-01 — home currency locked to PEN (`sql/018`); helper deleted |
| 9.2 | Personal Access Tokens (CLI auth) | ✅ Done |
| 9.3 | Profile mutation (`PUT /auth/profile`) | ✅ Done |
| 9.4 | Opening balances (`@Opening` + report exclusion) | ✅ Done |
| — | **Engine feature-complete. All endpoints shipped + tested.** | ✅ Done |
| 10 | CLI | Shipped (separate repo — flat CLI + TUI complete; see `expense_world_CLI/docs/roadmap.md`) |
| 11 | Local deployment (engine + Postgres on the owner's Mac; cloud mothballed) | ✅ Done (2026-07-30) |

## Error format

All errors use this exact shape — no deviations:
```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Human-readable description.",
    "fields": { "amount_cents": "Must not be zero." }
  }
}
```

## Custom skills (in `.claude/skills/`)

| Skill | What it does |
|---|---|
| `audit-business-logic` | Scans the codebase and checks every endpoint/service against `engine-spec.md` |
| `audit-coding-patterns` | Checks cross-cutting concerns (error format, null-over-omission, auth, idempotency, etc.) against the conventions in this file |
| `audit-bloat` | Finds dead code, unused imports, redundant logic, and unused dependencies |
| `audit-doc-drift` | Compares `engine-spec.md` against the implementation in both directions — planned gaps, undocumented behavior, divergences |
| `audit-schema-drift` | Compares `schema-reference.md` against the SQL migrations in `sql/` — undocumented tables/columns, type and constraint mismatches |
| `tech-consultant` | Second opinion on a proposal or plan from another AI agent — skeptical by default, checks it against this project's design principles |
