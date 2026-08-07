---
name: audit-business-logic
description: Audits the expense_world_engine codebase for spec compliance — checking that every endpoint and service correctly implements the rules defined in engine-spec.md. Use this skill whenever asked to audit business logic, check spec compliance, find missing validations, or verify that the engine implementation matches the spec. Covers sign conventions, atomicity, promotion logic, field locking, transfer pairing, reconciliation state machine, balance updates, and more.
---

# Business Logic Audit

This skill cross-references the engine's actual code against `docs/engine-spec.md` to find places where the implementation diverges from the spec — missing validations, wrong behavior, incomplete atomicity, or gaps in the rules. The output is a detailed report grouped by severity.

## How it works

The audit runs in three phases:

**Phase 1 — Recon:** Map the codebase. Identify every source file, measure its size, and decide how to distribute the work across a team of agents. The goal is to give each agent a focused, bounded slice — enough context to be thorough, not so much that details get lost.

**Phase 2 — Domain agents (parallel):** Each agent receives a specific file assignment and a list of spec sections to check against. Agents run in parallel and return structured findings.

**Phase 3 — Assembly:** A single assembler agent receives all findings and produces the final report.

---

## Phase 1: Recon

Before spawning any agents, scan the entire codebase:

1. List all non-test `.py` files outside of `.venv/`. Note their paths and line counts.
2. Group files by their logical domain (routes, services, models, utils, dependencies, etc.). A route file and its corresponding service file belong together — they form a logical unit because compliance often spans the boundary between the two.
3. Apply the **budget ceiling**: the total number of domain agents must not exceed 9 (leaving 1 slot for the assembler). This is a hard limit — agents handed too much context lose the thoroughness the audit depends on.
4. Rank domains by complexity (line count + number of endpoints/functions). The most complex domains each get their own agent. Simpler domains are grouped together to stay within the budget.
5. If the codebase is very small (early build phase with only a handful of files), a single domain agent may be sufficient. Don't spawn agents for empty or near-empty modules.

Produce an assignment plan before proceeding:

```
Assignment plan:
- Agent 1: routes/auth.py + services/auth.py (auth bootstrap, JWT, settings)
- Agent 2: routes/accounts.py + services/accounts.py (balance, archive, currency)
- Agent 3: routes/inbox.py + services/inbox.py (promotion logic — complex)
- Agent 4: routes/transactions.py + services/transactions.py (field locking, batch)
- Agent 5: routes/categories.py + services/categories.py + routes/hashtags.py + services/hashtags.py (simple CRUD, grouped)
- ...
- Assembler: receives all findings
```

---

## Phase 2: Domain agents

Spawn all domain agents in parallel. Each agent receives:
- The list of files it is responsible for
- The specific spec sections it should check (see checklist below)
- The output format to use

### What each agent checks

Every agent should check these universal rules first, then the domain-specific rules below:

**Universal (applies to every domain):**
- Every write endpoint (POST, PUT, DELETE) checks for an idempotency key before processing
- Every route requires PAT authentication (`Authorization: Bearer ewe_pat_…`) — the JWT branch was deleted 2026-08-03; the only unauthenticated route is `GET /health`
- Every mutation writes to `activity_log` with before/after JSON snapshots
- Every delete is a soft delete (`deleted_at = now()`) — no hard deletes on financial records
- All error responses use the standard shape: `{error: {code, message, fields}}`

**Auth domain:**
- `POST /auth/bootstrap` is idempotent — it skips row creation if rows already exist, and always returns current state regardless
- `PUT /auth/settings` rejects `main_currency` with `422` — the home currency is locked to PEN (`sql/018`) and there is no recalculation pass (conversion is read-time, `sql/021`); the only updatable field is `display_timezone` (IANA-validated)

**Accounts domain:**
- `POST /accounts`: rejects `is_person` — person accounts are never auto-created (decision D7); the explicit People API that would create them is planned but unbuilt, so no row can currently have `is_person = true`
- `PUT /accounts/{id}`: returns `422` if `currency_code` is included (it's immutable after creation)
- `DELETE /accounts/{id}`: returns `409` if any non-deleted transactions exist — must archive instead
- `POST /accounts/{id}/archive`: sets `is_archived = true`, does not delete
- Balances are **computed, never written** (`sql/022`). `current_balance_cents` is not a column — it is the signed sum of the account's non-deleted transactions, produced by `app/helpers/account_balance.py`. ⚠️ **Flag any code that writes a balance, not code that fails to.** An account with no transactions must report `0`, and the `@Opening` seed must be INCLUDED (it is excluded from flow reports only).

**Categories domain:**
- System categories (`is_system = true`) cannot be renamed or deleted — returns `403`
- `DELETE /categories/{id}`: returns `409` if referenced by any non-deleted transaction
- `@Debt` and `@Transfer` are auto-created by the engine on first use, never via this endpoint

**Hashtags domain:**
- `DELETE /hashtags/{id}`: removes all `expense_transaction_hashtags` rows for this hashtag atomically in the same operation

**Inbox domain:**
- `POST /inbox` and `PUT /inbox/{id}`: auto-populate `exchange_rate` when both `date` and `account_id` are present; fall back to most recent available rate if no exact date match
- **The inbox is a draft ledger row.** Its looseness is confined to *which fields may be null*; the *encoding* of every field matches `expense_transactions` exactly. `amount_cents` and `transfer_amount_cents` are stored positive, and `transaction_type` alone carries direction (`transfer_direction` was deleted by `sql/020`). A diff that reintroduces a signed stored amount, or a second way of expressing direction, is wrong — that was audit finding WP7.2, fixed in `sql/019`.
- `POST /inbox` / `PUT /inbox/{id}` with a `transfer` object: takes `account_id` + `amount_cents` and **no `id`** (unlike `POST /transactions`). `transfer.amount_cents` must be signed opposite to the item's own `amount_cents` — same sign is `422`. `transaction_type` must be assigned exactly once per request; if an `amount_cents` in the same body can overwrite `TRANSFER`, that is the WP7.2 ordering bug regressing. `"transfer": null` clears the transfer and is the only explicit null the inbox accepts.
- `POST /inbox/{id}/promote`: enforces all seven promotion conditions before proceeding, accumulating **all** failures into one response:
  1. `title` is present and not `'UNTITLED'`
  2. `amount_cents` is present and not zero
  3. `date` is present and `≤ now()`
  4. `account_id` references an active, non-archived account
  5. `category_id` references an active category — **non-transfer items only**; transfers auto-assign the system category
  6. `transfer_account_id` references an active, non-archived account — transfer items only
  7. `transfer_id` is present for a transfer item and absent for a non-transfer one
  - Returns `422` with the specific failing fields if any condition fails
- `GET /inbox?ready=true` must be the exact complement of that list — every row it returns promotes, every row that promotes appears in it. The two are written separately (SQL in the router, Python in the helper), which is how they drifted into WP7.3; check them against each other, not just against the spec.
- Promotion is atomic — all five steps happen in one DB transaction:
  1. Creates `expense_transactions` row with `inbox_id` pointing back
  2. Sets `status = 2` (promoted) on the inbox row
  3. Sets `deleted_at` on the inbox row
  4. Writes `activity_log` entry for the new transaction (action=1 created)
  5. Writes `activity_log` entry for the inbox item (action=3 deleted)

  There is no balance step — writing the ledger row in step 1 is the balance change.
- `status = 2` (promoted) vs `status = 3` (dismissed) must be distinguishable

**Transactions domain:**
- `PUT /transactions/{id}` field locking: if `reconciliation_id` is set and reconciliation `status = 2`, these four fields are read-only: `amount_cents`, `account_id`, `title`, `date`. Attempts to update them return `422`.
- `PUT /transactions/{id}` date change: no re-rating. `amount_home_cents` and `exchange_rate` were deleted by `sql/021`; conversion is a read-time lookup.
- `PUT /transactions/{id}` amount/account change: no balance write. Changing the row changes what it contributes; changing `account_id` moves that contribution between accounts, in the same `UPDATE`.
- `DELETE /transactions/{id}`: no balance write — the sum excludes soft-deleted rows. If `transfer_transaction_id` is set, soft-deletes both the transaction and its paired sibling atomically
- `DELETE /transactions/{id}` on completed reconciliation: allows deletion but includes a warning in response body (reconciliation totals become stale — engine does not auto-adjust)
- `POST /transactions/batch`: all operations wrapped in a single DB transaction — all succeed or all fail

**Transfers domain (via `POST /transactions` or `POST /inbox` with a `transfer` field):**
- Creates both the primary and paired transaction atomically
- Links both via `transfer_transaction_id` (each row points to the other)
- Auto-assigns categories correctly: person account side gets `@Debt`, real account transfer side gets `@Transfer`. These override any `category_id` passed in the request.
- Auto-creates `@Debt` or `@Transfer` system categories if they don't exist yet
- Zero-sum validation: enforces that the two transactions are directionally opposite (one negative, one positive). Returns `422` if both are the same sign. Does NOT enforce equal raw amounts (different currencies allowed).
- Both account balances move by construction — each leg is an ordinary row on its own account and the balance is a sum over rows. There is no balance step to check for.
- Writes `activity_log` entries for both transactions

**Reconciliations domain:**
- `POST /reconciliations/{id}/complete`: returns `422` if no transactions are assigned; sets field locks on all assigned transactions
- `POST /reconciliations/{id}/revert`: sets status back to draft, unlocks all assigned transaction fields
- `DELETE /reconciliations/{id}`: only allowed if `status = 1` (draft); returns `409` if completed — must revert first

**Sync/Dashboard domain:**
- `GET /sync` with `sync_token=*`: full fetch, returns all active records, creates new checkpoint
- `GET /sync` with `sync_token=<token>`: delta fetch, returns only records with `version` higher than checkpoint
- Deleted records included as tombstones (`deleted_at` set) — deletions are never inferred from absence
- `/dashboard` and `/reports/monthly`: every amount-bearing field includes both native and `_home_cents` versions

### Agent output format

Each agent returns a findings block in this structure:

```
## [Domain Name] — Business Logic Audit

### Files reviewed
- path/to/file.py (N lines)

### Findings

#### [CRITICAL] Title of issue
**Spec ref:** engine-spec.md §Section name
**Expected:** What the spec says should happen
**Actual:** What the code actually does (or that the check is missing entirely)
**Risk:** Why this matters (e.g., "balance corruption possible", "promotion bypass possible")

#### [WARNING] Title of issue
**Spec ref:** ...
**Expected:** ...
**Actual:** ...
**Risk:** ...

#### [PASS] Area that is correctly implemented
Brief note confirming compliance.

### Summary
X critical · X warnings · X passing
```

Use CRITICAL for violations that could corrupt data, bypass validation, or produce wrong financial results. Use WARNING for gaps that reduce reliability or create inconsistency. Use PASS to confirm areas that are correctly implemented — a fully green domain is useful signal too.

If a file is empty or not yet written, note that and move on. Don't fabricate findings for code that doesn't exist yet.

---

## Phase 3: Assembly

Once all domain agents have returned their findings, the assembler agent:

1. Reads all findings blocks
2. Produces the final report in this structure:

```
# Business Logic Audit — expense_world_engine
**Date:** [today]
**Files reviewed:** [N files, N total lines]
**Agents deployed:** [N]

## Executive Summary
[2-3 sentences: overall health, most critical areas, general pattern of issues if any]

## Critical Issues — Fix Before Shipping
[Each CRITICAL finding from all domains, with domain label, full detail, and spec reference]

## Warnings — Should Fix
[Each WARNING finding, same format]

## Clean Areas
[Domains or specific areas that passed cleanly — brief]

## Domain Breakdown
[One section per domain: files covered, finding counts, brief narrative]
```

The assembler does not re-read source files — it works only from the findings blocks it receives. Its job is to synthesize, not to re-audit.
