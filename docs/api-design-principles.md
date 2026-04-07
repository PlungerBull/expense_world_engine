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

All resources are identified by a UUID generated client-side before server confirmation. No resource is identified by its name, slug, or any mutable attribute. The frontend always has the ID before making a write — it never needs to "find" a resource by querying its content first. See `lessons-todoist.md §1`.

### 2. Headless Architecture — The Engine Is the Product

The Python engine is the product. The iOS app and CLI are equal clients of the same API surface. No endpoint is designed for a specific screen. No business logic lives outside the engine. The spec is the rulebook; the CLI and iOS app are ATMs that can only offer what the vault supports.

If a feature cannot be expressed as an API operation, it is not well-designed. Features are verified via the OpenAPI/Swagger UI before any client code is written. See `lessons-todoist.md §2`.

### 3. Sync Token Pattern

Every mutable table carries a `version` integer, incremented on every update. The `sync_checkpoints` table tracks each client's last known sync position. Delta requests return only records with a `version` higher than the client's last checkpoint, plus tombstones for deleted records. Full re-fetch uses a wildcard token. See `lessons-todoist.md §3`.

### 4. Idempotency Keys

Clients generate a unique key per intended write operation and include it in the request. The engine checks `idempotency_keys` before processing — duplicates return the stored result of the original operation. TTL is 24 hours. Critical for transaction creation where duplicates corrupt balances. See `lessons-todoist.md §4`.

### 5. Soft Delete Everywhere

All mutable tables carry `deleted_at` (nullable timestamptz). Hard deletion is never performed on financial records. Deleted records are excluded from active queries but remain in the database and participate in historical calculations. Delta sync responses include tombstones — deletions are communicated explicitly, never inferred from absence. See `lessons-todoist.md §5`.

### 6. Activity Log as a Correctness Requirement

Every mutation to any mutable table produces an immutable row in `activity_log` capturing: resource type, resource ID, action (created/updated/deleted/restored), full before/after JSON snapshots, timestamp, and actor. For a financial application this is not optional — it is the mechanism for answering "why does my balance look wrong?" Designed from day one; retrofitting loses historical record. See `lessons-todoist.md §6`.

### 7. Command Batching for Multi-Record Workflows

Batch endpoints exist for operations that are inherently multi-record: CSV import, split transactions, account initialisation. The server wraps each batch in a database transaction — all succeed or all fail. Partial success is never an acceptable outcome for financial data. See `lessons-todoist.md §7`.

### 8. to_base Consistency Across All API Layers

Every API response containing an amount — at any level of aggregation — includes a home-currency version alongside the native amount. This applies to individual transactions (`amount_home_cents`), category totals, account balances, monthly summaries, and any future budget or P&L views. The engine is exclusively responsible for currency conversion. The frontend never performs it. See `lessons-lunchmoney.md §2`.

### 9. debit_as_negative as a Caller-Side Flag

Sign conventions are not baked into the schema. The engine accepts a `debit_as_negative` flag on relevant read endpoints. When true, negative = expense/outflow, positive = income/inflow. This allows the CLI and iOS app to work in whatever sign convention is natural for them without the schema forcing an interpretation. See `lessons-lunchmoney.md §6`.

### 10. Null Over Omission

Optional fields with no value are returned as `null` in API responses, never omitted. The response shape is always identical regardless of data presence. Clients never check for key existence before reading — they always find the key, potentially null. See `lessons-ynab.md §7`.

### 11. IDs-Only as the Future Direction

Phase 1 responses may include hydrated names alongside IDs (e.g. `category_name` next to `category_id`) for development convenience. The intended direction as the system matures is IDs-only, with clients maintaining local caches of lookup objects. No client code should treat hydrated names as guaranteed — always use the ID as the authoritative value. See `lessons-lunchmoney.md §7`.

### 12. Directional Invariant on Transfers

Any paired transfer (two `expense_transactions` linked via `transfer_transaction_id`) must be directionally opposite: one side negative (outflow), the other positive (inflow). The engine validates this before committing — returns `422` if both have the same sign. **No magnitude equality check** is performed, even when both accounts share the same currency. Amounts may differ because accounts use different currencies, or because the user intentionally records a different value (e.g., fees absorbed during transfer). See `lessons-splitwise.md §8`.

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
Python (FastAPI) + Supabase. Single source of truth. Handles all database connections, all business logic (categorisation, balance updates, sync processing), and exposes the API that all clients consume. Hosted on Koyeb. Never replaced — only evolved.

**`expense_world_cli` — The Hands**
Python (Typer). Developer-facing terminal interface. Talks to the engine via the API. Logs expenses, checks sync state, runs bulk imports. The primary tool for verifying backend behaviour during development. Built before the web dashboard.

**`expense_world_web` — The Eyes**
Next.js on Vercel. Lightweight read-only dashboard. Calls the engine API and displays balances, monthly category totals, and recent transactions. No business logic, no entry, no editing — read-only first. Expanded incrementally as real needs emerge. May eventually replace the need for an iOS app.

**`expense_world_ios` — The Face (maybe)**
Swift / SwiftUI. Minimalist mobile interface. Has no knowledge of the database or business logic — it only knows how to ask the engine for data and display it. Built last, only if the web dashboard proves insufficient for mobile use.

**Why polyrepo:** Each repo has a single unambiguous purpose. AI-assisted development across repos requires only the OpenAPI spec as shared context — no cross-repo code knowledge needed.

---

## Infrastructure Stack

**Database:** Supabase (managed Postgres). Single source of truth for all persistent data. Row-Level Security (RLS) enabled on all tables: `auth.uid() = user_id`. Even if someone bypasses the engine, they can only access their own rows.

**Engine:** Python FastAPI on Koyeb. Stateless — all state lives in Supabase. Deployment, restarts, and scaling are straightforward.

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

*Last updated: April 2026*
