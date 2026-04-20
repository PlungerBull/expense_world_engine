# API Design Principles — Warm Productivity Expense Tracker

> This document captures the architectural decisions for the expense tracker engine. Raw observations from third-party APIs live in the lesson files below. This document contains only locked decisions and the reasoning behind them.

---

## Source Lessons

Observations drawn from studying production financial and productivity APIs:

| File | What it covers |
|---|---|
| `lessons-todoist.md` | UUID-first design, headless architecture, sync token, idempotency, soft deletes, activity log, command batching |
| `lessons-ticktick.md` | Relational schema, UUIDs, smallint enums, UTC timestamps, splits, rich read endpoints |
| `lessons-ynab.md` | cleared/approved separation, transfer modeling, tombstones, null over omission |
| `lessons-splitwise.md` | Multi-party splits, settlements as transactions, zero-sum invariant (deferred phase) |
| `lessons-lunchmoney.md` | to_base consistency, historical rates, debit_as_negative convention, dehydration direction |

---

## Architectural Principles

### 1. UUID-First Resource Identification

All resources are identified by a UUID generated client-side before server confirmation. Every `POST` that creates a resource requires an `id: UUID` field in the request body — the engine never picks the id. This enables offline-first clients to reference a resource (render it, attach hashtags, link it to other local state) before the round trip completes, and makes idempotent retries trivial: a second POST with the same `id` returns `409 CONFLICT`, not a duplicate row. No resource is identified by its name, slug, or any mutable attribute. The frontend always has the ID before making a write — it never needs to "find" a resource by querying its content first. Internally-seeded system resources (system categories, activity_log rows, idempotency keys, sync checkpoints) are the only exception and use server-generated UUIDs because no client request triggers them. See `lessons-todoist.md §1`.

### 2. Headless Architecture — The Engine Is the Product

The Python engine is the product. The iOS app and CLI are equal clients of the same API surface. No endpoint is designed for a specific screen. No business logic lives outside the engine. The spec is the rulebook; the CLI and iOS app are ATMs that can only offer what the vault supports.

If a feature cannot be expressed as an API operation, it is not well-designed. Features are verified via the OpenAPI/Swagger UI before any client code is written. See `lessons-todoist.md §2`.

### 3. Sync Token Pattern

Every mutable table carries a `version` integer and `updated_at` timestamp, both bumped on every update. The `sync_checkpoints` table tracks each client's last known sync position via `(user_id, client_id)` — clients send a stable `X-Client-Id` UUID on every `/v1/sync` call so each device gets its own independent checkpoint.

**Token mechanics.** The `sync_token` is an opaque UUID. Server-side, the `sync_checkpoints` row stores the token alongside a `last_sync_at` timestamp captured at sync time. Delta queries are `WHERE updated_at > last_sync_at` against every synced table. Wildcard `sync_token=*` does a full fetch (active rows only, no tombstones, fresh checkpoint). Tokens are opaque to clients — they store the value and send it back on the next sync, never parse it.

**Snapshot isolation.** Every sync read and the checkpoint write happen inside one Postgres `REPEATABLE READ` transaction so all tables are read at the same MVCC snapshot. A concurrent mutation either lands entirely in this sync or entirely in the next, never split across them. Without this, two queries against different tables could observe partially-applied multi-table transactions.

**Parent-bump rule for junction edits.** When a junction row is mutated, the same DB transaction must also bump `version` + `updated_at` on the parent row whose embedded array changes. Concrete instance: editing `expense_transaction_hashtags` for a transaction also bumps the parent `expense_transactions.updated_at`. Without this, a hashtag-only edit would leave the parent stale and `/sync` would miss the change. See `lessons-todoist.md §3`.

### 3a. Junction Tables Are Storage, Not Wire Format

Many-to-many relationships (transaction ↔ hashtag) live in junction tables in the database — the canonical storage with FK integrity, per-link `version`, per-link `deleted_at`, and per-link activity-log entries. **On the wire, the relationship is flattened to an embedded array of IDs on the parent object.** Clients see `transaction.hashtag_ids = [uuid, ...]` and never see junction rows. This matches Todoist's `task.labels` and Lunch Money's `transaction.tags`. The price of denormalizing on the wire while normalizing in storage is the parent-bump rule above (§3): junction-row mutations must bump the parent so delta sync notices.

### 3b. Client Local Replica Standard

All interactive clients — iOS, web, CLI, and any future client — hold a disposable local read replica of the user's data, built from `GET /sync`. The replica is **not a second source of truth.** It is a performance optimization and an offline affordance. The engine remains the only authoritative store. This matches Todoist's client architecture, where every official app (mobile, desktop, web, CLI) maintains a sync-backed local cache and all business logic stays server-side.

**Replica mechanics.** On first launch, a client generates a stable `X-Client-Id` UUID (OS keychain on iOS, localStorage on web, config file on CLI) and calls `GET /sync?sync_token=*` to fetch the full state for that user. Subsequent commands call `GET /sync?sync_token=<stored>` and apply deltas — inserts, updates, and tombstones — to the local store. Reads (list, search, balance lookups, category pickers) hit the local replica directly and are instant. Writes go to the engine over HTTPS with `X-Idempotency-Key`; on success, the affected rows are patched locally or refreshed via a follow-up delta sync.

**Disposable by design.** The replica is always rebuildable. A corrupt cache, a new device, a schema migration, or a user wiping local state is not a recovery scenario — it is a cold start. The client calls `sync_token=*`, rebuilds from scratch, and continues. No client code ever attempts to repair the replica from partial local state; when in doubt, full-sync.

**Offline writes are out of scope by default.** Writes require network connectivity. Individual clients may add a local write queue if their product calls for it — likely iOS, probably never CLI — but this is an explicit per-client decision, not the default. A client that queues writes locally takes on the full burden of conflict resolution, retry, reconciliation, and UUID pre-generation discipline. This is a significant design commitment, not a convenience, and must be documented in that client's own repo.

**Home-currency amounts in the replica are point-in-time.** The `amount_home_cents` and related fields returned by `/sync` reflect FX rates at sync time and drift as rates move. Clients must treat any balance-sensitive display (dashboard totals, report summaries, net-worth views) as requiring a fresh engine call, not a local recomputation. `/dashboard` is the canonical place for derived home-currency values, and clients should prefer it over computing aggregates from cached transaction rows.

**Stateless escape hatch (CLI).** The CLI additionally supports an explicit stateless mode via a `--no-cache` flag or `EXPENSE_STATELESS=1` environment variable that bypasses the replica entirely for a given invocation. In this mode, reads go straight to the engine, no cache file is opened, and no sync is performed. This mode exists for CSV imports, CI pipelines, cron-triggered scripts, and ad-hoc automation piped into tools like `jq` — contexts where per-invocation freshness matters more than speed, where ephemeral environments (CI containers) gain nothing from a cache, and where concurrent invocations would otherwise contend on the local SQLite file. Other clients may expose an equivalent mode if their product calls for it, but it is not required.

### 4. Idempotency Keys

Clients generate a unique key per intended write operation and include it in the request. The engine stores `(user_id, key) → (response_body, response_status)` in `idempotency_keys` so replays reconstruct the **full response envelope, including the original HTTP status code** — no per-route re-derivation. TTL is 24 hours.

**Concurrency.** Every write handler acquires a transaction-scoped Postgres advisory lock (`pg_advisory_xact_lock`) derived from `(user_id, key)` as the first statement inside the write transaction. Two concurrent requests with the same key serialize at the DB: the second blocks until the first commits, then reads the stored snapshot and returns it verbatim. No double writes are possible, no race window between check and store. This is implemented once in `helpers/idempotency.py::run_idempotent` and every write handler delegates to it — changing idempotency behavior is a one-file edit, not a 23-file scavenger hunt.

Critical for transaction creation where duplicates corrupt balances. See `lessons-todoist.md §4`.

### 5. Soft Delete Everywhere

All mutable tables carry `deleted_at` (nullable timestamptz). Hard deletion is never performed on financial records. Deleted records are excluded from active queries but remain in the database and participate in historical calculations. Delta sync responses include tombstones — deletions are communicated explicitly, never inferred from absence. Every resource with a delete endpoint also exposes `POST /{resource}/{id}/restore`, which clears `deleted_at`, writes a `RESTORED` activity log entry, and enforces any collision rules (e.g., restoring a category whose name now collides with an active one returns `409`). See `lessons-todoist.md §5`.

### 6. Activity Log as a Correctness Requirement

Every mutation to any mutable table produces an immutable row in `activity_log` capturing: resource type, resource ID, action (1=CREATED, 2=UPDATED, 3=DELETED, 4=RESTORED), full before/after JSON snapshots, timestamp, and actor. For a financial application this is not optional — it is the mechanism for answering "why does my balance look wrong?" Designed from day one; retrofitting loses historical record. See `lessons-todoist.md §6`.

**Deliberate aggregate exceptions.** Three mutation paths skip per-row entries in favor of a single aggregate entry. Each is a conscious trade, not an oversight:

1. **Junction-row mutations on `expense_transaction_hashtags`** — the parent transaction's `UPDATED` entry carries the full new `hashtag_ids` list, so the change is captured at parent granularity. Per-link entries would multiply audit row count by the average hashtags per transaction without enabling new audit questions.
2. **`recalculate_home_currency` bulk UPDATEs** — a single `UPDATED` entry on `user_settings` carries a `recalculation` summary block (rows touched per pass). A full recalc on a busy user can rewrite tens of thousands of rows in one request; per-row entries would inflate `activity_log` by orders of magnitude.
3. **`users.last_login_at` bumps on repeat bootstrap** — not logged at all. Login bumps are operational metadata, not user actions. If session-level audit becomes a requirement, a dedicated `auth_sessions` table is the right home, not inflated `activity_log`.

### 7. Command Batching for Multi-Record Workflows

Batch endpoints exist for operations that are inherently multi-record: CSV import, split transactions, account initialisation. The server wraps each batch in a database transaction — all succeed or all fail. Partial success is never an acceptable outcome for financial data. See `lessons-todoist.md §7`.

### 8. to_base Consistency Across All API Layers

Every API response containing an amount — at any level of aggregation — includes a home-currency version alongside the native amount. This applies to individual transactions (`amount_home_cents`), category totals, account balances, monthly summaries, and any future budget or P&L views. The engine is exclusively responsible for currency conversion. The frontend never performs it. See `lessons-lunchmoney.md §2`.

### 9. debit_as_negative as a Caller-Side Flag

Sign conventions are not baked into the schema. The engine accepts a `debit_as_negative` flag on relevant read endpoints. When true, negative = expense/outflow, positive = income/inflow. This allows the CLI and iOS app to work in whatever sign convention is natural for them without the schema forcing an interpretation. See `lessons-lunchmoney.md §6`.

### 10. Null Over Omission

Optional fields with no value are returned as `null` in API responses, never omitted. The response shape is always identical regardless of data presence. Clients never check for key existence before reading — they always find the key, potentially null. See `lessons-ynab.md §7`.

**`VALIDATION_ERROR.fields` is an exception** — on `VALIDATION_ERROR` responses specifically, `fields` is always an object (possibly empty), never `null`, so clients can uniformly iterate `Object.keys(error.fields)` without a null check. Two other precondition-unmet codes also carry field-scoped payloads so clients can branch on the specific bootstrap/remediation step: `SETTINGS_MISSING` with `fields: {"user_settings": ...}` and `RATE_UNAVAILABLE` with `fields: {"exchange_rate": ...}`, both returned as `422`. Non-validation errors (`UNAUTHORIZED`, `NOT_FOUND`, `FORBIDDEN`, `CONFLICT`, `INTERNAL_ERROR`) keep `fields: null` since they aren't field-scoped.

### 11. IDs-Only as the Future Direction

Phase 1 responses may include hydrated names alongside IDs (e.g. `category_name` next to `category_id`) for development convenience. The intended direction as the system matures is IDs-only, with clients maintaining local caches of lookup objects. No client code should treat hydrated names as guaranteed — always use the ID as the authoritative value. See `lessons-lunchmoney.md §7`.

### 12. Directional Invariant on Transfers

Any paired transfer (two `expense_transactions` linked via `transfer_transaction_id`) must be directionally opposite: one side negative (outflow), the other positive (inflow). The engine validates this before committing — returns `422` if both have the same sign. **No native-currency magnitude equality check** is performed, even when both accounts share the same currency. Native amounts may differ because accounts use different currencies, or because the user intentionally records a different value (e.g., fees absorbed during transfer). See `lessons-splitwise.md §8`.

**Home-currency zero-sum is enforced, though.** While native amounts are free, the derived `amount_home_cents` values on the two legs must net to zero. The engine achieves this via the **dominant-side rule**: the side whose currency matches `main_currency` is dominant (its home value equals its native amount at rate `1.0`); the other side's `amount_home_cents` is forced by direct assignment to equal the dominant side's, and its `exchange_rate` is derived from that. Phase 1 supports only USD and PEN (`sql/015` CHECK), so `main_currency` always matches one of the two legs and the dominant-side rule is always decisive. The engine also still contains a 3-currency fallback branch (debit side uses a market-rate lookup, credit side forced to match) that is dead code under the current currency policy — it would be revisited if a third currency is ever introduced. This guarantees that cross-currency transfers do not leak phantom home-currency balances into dashboard or report totals, and it matches how production fintech systems (Stripe, Wise, Xero, QuickBooks Online) treat the execution rate as the historical spot rate for the transaction. No per-transaction FX gain/loss is recognised — that's a period-end remeasurement concern, out of scope for Phase 1.

---

## Project Vision

The goal is a high-performance, minimalist expense tracker with a headless architecture. The philosophy is to build the logic before the aesthetics — the Brain before the Face — ensuring the system is automation-ready from day one.

**Build one app at a time.** The expense tracker is the first app. The `expense_world_engine` repo is the backend today; it grows into the Warm Productivity platform over time as real needs emerge. Not a rewrite — a natural expansion.

**De-risk with CLI-first.** A fully functional, automated expense tracker exists the moment the backend is finished — before a single line of Swift is written. The CLI is the fastest path to a working system and the best tool for verifying the engine behaves correctly.

**The machine-readable contract.** The engine exposes an OpenAPI spec and an `llms.txt` file. An AI agent can hand the spec to any client developer — or to itself — and produce correct matching code with no guesswork about route names, parameter types, or response shapes.

---

## Repository Structure (Polyrepo)

Four repositories, one managed database. Each has a single, clear role.

**`expense_world_engine` — The Brain**
Python (FastAPI) + Supabase. Single source of truth. Handles all database connections, all business logic (categorisation, balance updates, sync processing), and exposes the API that all clients consume. Hosted on Render. Never replaced — only evolved.

**`expense_world_cli` — The Hands**
Python (Typer). Developer-facing terminal interface. Talks to the engine via the API. Holds a local SQLite replica built from `GET /sync` (see §3b) so interactive reads are instant and work offline; writes go directly to the engine over HTTPS with idempotency keys. Supports an explicit stateless mode (`--no-cache` / `EXPENSE_STATELESS=1`) that bypasses the replica for scripting, CSV imports, and CI contexts. The primary tool for verifying backend behaviour during development. Built before the web dashboard.

**`expense_world_web` — The Eyes**
Next.js on Vercel. Lightweight read-only dashboard. Calls the engine API and displays balances, monthly category totals, and recent transactions. No business logic, no entry, no editing — read-only first. Expanded incrementally as real needs emerge. May eventually replace the need for an iOS app.

**`expense_world_ios` — The Face (maybe)**
Swift / SwiftUI. Minimalist mobile interface. Has no knowledge of the database or business logic — it only knows how to ask the engine for data and display it. Built last, only if the web dashboard proves insufficient for mobile use.

**Why polyrepo:** Each repo has a single unambiguous purpose. AI-assisted development across repos requires only the OpenAPI spec as shared context — no cross-repo code knowledge needed.

---

## Infrastructure Stack

**Database:** Supabase (managed Postgres). Single source of truth for all persistent data. Row-Level Security (RLS) enabled on all tables: `auth.uid() = user_id`. Even if someone bypasses the engine, they can only access their own rows.

**Engine:** Python FastAPI on Render. Stateless — all state lives in Supabase. Deployment, restarts, and scaling are straightforward.

**Configuration:** All credentials via environment variables. No hardcoded secrets. Same codebase points to local, staging, or production by swapping one variable.

**Edge performance:** If latency from Lima is a concern, Fly.io allows deploying in Santiago or São Paulo for sub-50ms response times to Peruvian clients.

---

## OpenAPI as the System Contract

FastAPI generates an OpenAPI spec automatically from the engine's route definitions. This spec is the formal definition of everything the system can do.

**The contract rule:** No feature can exist in the CLI or iOS app unless it is first defined in the engine. If an endpoint is not in the spec, it does not exist for any client. This prevents logic leakage and ensures all clients work from identical capabilities.

**Verification before UI:** The Swagger UI is the primary testing interface during backend development. Every feature is verified by calling it directly through the spec before any CLI or iOS code is written.

**AI advantage:** A clean OpenAPI spec is a high-resolution map of the entire system. An AI agent handed `openapi.json` can write correct matching code for any client with no guesswork.

---

## Authentication Strategy

Authentication is fully delegated to Supabase Auth. The engine never sees, stores, or manages passwords. It only verifies tokens.

**Token validation:** Every request must carry a JWT as `Authorization: Bearer <token>`. The engine verifies the signature using the Supabase JWT secret, rejects expired or tampered tokens (401), and extracts `user_id` from the verified payload. That `user_id` is passed into all downstream functions.

**iOS:** Sign in with Apple and Sign in with Google, configured via Supabase Auth. Deep-link redirect URLs registered so the OAuth flow returns the user to the app with a valid session.

**CLI:** Personal Access Token (PAT) — a long-lived token generated once from the web dashboard, stored in `~/.expense-config`. From the engine's perspective, a PAT and an iOS JWT are identical — both validated the same way.

**RLS as the final failsafe:** Supabase Row-Level Security (`auth.uid() = user_id`) is the last line of defence. No application-level bug can expose another user's financial data. Enforced at the database level automatically.

---

*Last updated: April 2026 (Sprints 1–4 aligned)*
