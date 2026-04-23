# expense_world_engine — CLAUDE.md

## What this repo is

The Brain. A Python (FastAPI) backend hosted on Render, backed by Supabase (Postgres). It is the single source of truth for all business logic, validation, and data. The iOS app, CLI, and web dashboard are all equal clients — none of them implement logic. If it isn't in the engine, it doesn't exist.

## Key documentation

| Doc | What it contains |
|---|---|
| `docs/engine-spec.md` | Every endpoint, every business logic rule, every validation. The rulebook. |
| `docs/api-design-principles.md` | Architectural decisions and the reasoning behind them. |
| `docs/schema-reference.md` | Full database schema. |
| `docs/design-philosophy.md` | UX philosophy and product vision. |

## Tech stack

- **Language:** Python
- **Framework:** FastAPI
- **Database:** Supabase (managed Postgres). RLS enabled on all tables: `auth.uid() = user_id`.
- **Auth:** Supabase Auth. Engine validates JWT, extracts `user_id`, never stores passwords.
- **Hosting:** Render (stateless — all state lives in Supabase).

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
| — | **Engine feature-complete. All endpoints shipped + tested.** | ✅ Done |
| 9.5 | Web Dashboard (read-only) | Pending (separate repo) |
| 10 | CLI | Pending (separate repo) |

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
