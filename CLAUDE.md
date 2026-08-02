# expense_world_engine — CLAUDE.md

## What this repo is

The Brain. A Python (FastAPI) backend, backed by Postgres. It is the single source of truth for all business logic, validation, and data. The iOS app, CLI, and web dashboard are all equal clients — none of them implement logic. If it isn't in the engine, it doesn't exist.

**Deployment (since 2026-07-30): local profile** — the engine runs on the owner's Mac (launchd, `127.0.0.1:8000`) against Homebrew Postgres; the Render/Supabase cloud profile is mothballed until a second client needs it. Profiles live in `deploy/` (local = active, cloud = reactivation checklist); rationale in `expense_world_CLI/docs/decisions.md` ("Local-first deployment, 2026-07-30"). This changes where the engine runs, not what it is — every convention below holds unchanged.

## Key documentation

| Doc | What it contains |
|---|---|
| `docs/engine-spec.md` | Every endpoint, every business logic rule, every validation. The rulebook. |
| `docs/api-design-principles.md` | Architectural decisions and the reasoning behind them. |
| `docs/schema-reference.md` | Full database schema. |
| `docs/design-philosophy.md` | UX philosophy and product vision. |
| `docs/scaling-boundaries.md` | What is business logic (scale-invariant) vs. a scaling constraint (single-user-shaped). Read before arguing that something should be built or deferred "for scale". |

## Who this is for

**One user — the owner (since 2026-08-01).** The earlier 1000+ public-users target is retired; if scaling happens it gets a dedicated, professionally-staffed plan. Build what makes daily use work well, not what a hypothetical user base would need. The one obligation this leaves: when a decision is single-user-shaped, record it in `docs/scaling-boundaries.md` instead of leaving it implicit. Ledger correctness is never a scale trade-off — the conventions below hold at any size.

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
- Requests: `amount_cents` is signed. Negative = expense/outflow. Positive = income/inflow. The engine infers `transaction_type` from the sign — callers never set it manually. Transfers are identified by the presence of a `transfer` field, not by sign.
- Storage: `amount_cents` is always stored as a positive integer. `transaction_type` (1=expense, 2=income, 3=transfer) and `transfer_direction` (1=debit, 2=credit) encode direction.
- Responses: `amount_cents` is always positive. `transaction_type` tells direction. The `?debit_as_negative=true` flag is a caller-side preference, not a schema property.

**Home currency**
Every response that contains an amount must include a home-currency version alongside it (`amount_home_cents`, `spent_home_cents`, `current_balance_home_cents`, etc.). The engine is the only thing that does currency conversion. Clients never compute it.

**Null over omission**
Optional fields with no value are always returned as `null`, never omitted. Response shape never changes based on data presence.

**Soft delete everywhere**
All mutable tables carry `deleted_at` (nullable timestamptz). Hard deletion is never performed on financial records. Deleted records are excluded from active queries but remain in the DB.

**Activity log on every mutation**
Every write to any mutable table produces an immutable `activity_log` row: resource type, resource ID, action (created/updated/deleted/restored), full before/after JSON snapshots, timestamp, actor. No exceptions. This is how "why does my balance look wrong?" gets answered.

**Idempotency keys on all writes**
`POST`, `PUT`, `DELETE` operations accept `X-Idempotency-Key: <uuid>`. The engine checks `idempotency_keys` before processing. Duplicates return the stored response verbatim. TTL: 24 hours. Critical for financial writes where duplicates corrupt balances.

**JWT on every route**
Every request requires `Authorization: Bearer <token>`. No public endpoints. Unauthenticated requests return `401`.

**UUID-first**
All resources are identified by a UUID generated client-side before server confirmation. The frontend always has the ID before making a write. Resources are never looked up by name or any mutable attribute.

**Balance updates are atomic**
Whenever a transaction is created, updated, or deleted, `current_balance_cents` on the affected account(s) is updated in the same database transaction. Balance and transaction state are never out of sync.

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
| 9.1 | Home Currency Recalculation | ✅ Done |
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
| `audit-coding-patterns` | Checks cross-cutting concerns (error format, null-over-omission, auth, idempotency, etc.) against `api-design-principles.md` |
| `audit-bloat` | Finds dead code, unused imports, redundant logic, and unused dependencies |
| `audit-doc-drift` | Compares `engine-spec.md` against the implementation in both directions — planned gaps, undocumented behavior, divergences |
| `audit-schema-drift` | Compares `schema-reference.md` against the SQL migrations in `sql/` — undocumented tables/columns, type and constraint mismatches |
| `tech-consultant` | Second opinion on a proposal or plan from another AI agent — skeptical by default, checks it against this project's design principles |
