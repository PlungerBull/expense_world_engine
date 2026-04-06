# API Lessons — TickTick

> Lessons inferred from TickTick's API endpoint design and JSON response shapes. These informed several database schema decisions. These are inferences from their public API — not confirmed implementation details — but instructive enough to lock in.

---

## 1. Relational Core with Document-Like Flexibility

TickTick's API implies a relational database at its core with a clear hierarchy: users → folders → projects → tasks. Each level carries a foreign key to its parent. The relational model enforces integrity, supports complex queries, and works naturally with Postgres.

**Our decision:** Relational model throughout. `users → expense_bank_accounts → expense_transactions`, with `expense_categories` as a separate dimension. No JSONB blobs for core financial data — everything is a proper row with typed columns.

---

## 2. UUIDs Over Auto-Incrementing Integers

TickTick uses alphanumeric string IDs rather than simple integers. Auto-incrementing integers are dangerous in a multi-client, sync-enabled system — they can collide when records are created offline or across environments. UUIDs are generated client-side before the server confirms creation, which is essential for idempotency and offline support.

**Our decision:** All primary keys are UUIDs (`uuid_generate_v4()`), generated client-side. The client always has the ID before the server responds.

---

## 3. Enums as Smallints

Fields like `priority` (0, 1, 3, 5) and `status` (0 = open, 2 = completed) are stored as integers rather than strings. This saves storage and speeds up indexing — a meaningful optimisation on tables like `expense_transactions` that grow indefinitely.

**Our decision:** All enum-like fields stored as `smallint` with documented mappings. Never raw strings in the database.

| Field | Table | Mapping |
|---|---|---|
| `category_type` | `expense_categories` | 1 = expense, 2 = income |
| `status` | `expense_reconciliations` | 1 = draft, 2 = completed |
| `transaction_source` | `expense_transaction_hashtags` | 1 = inbox, 2 = ledger |
| `action` | `activity_log` | 1 = created, 2 = updated, 3 = deleted, 4 = restored |

---

## 4. Timestamps: Always UTC, Always with Timezone

TickTick stores all dates in ISO 8601 format with UTC timezone. All columns are `TIMESTAMP WITH TIME ZONE`. Storing local time is a reliability failure for any multi-region or multi-timezone app.

**Our decision:** Every timestamp is `TIMESTAMPTZ` in Postgres, stored in UTC. Display conversion to the user's local timezone (e.g. `America/Lima`) happens at the presentation layer only. `display_timezone` in `user_settings` is the IANA string used for all client-side date boundary calculations.

---

## 5. Splits: Parent ID Pattern Over Embedded Arrays

TickTick handles subtasks via a `parentTaskId` foreign key — subtasks are full rows pointing to a parent, not embedded arrays inside the parent record. This keeps queries clean and consistent with the rest of the schema.

**Our decision:** `parent_transaction_id` nullable FK on `expense_transactions`. Split transactions are full rows. Each child points to its parent. This keeps reporting accurate and the schema consistent with soft-delete and activity log requirements.

---

## 6. Read-Heavy Optimisation: Rich Single Endpoints

TickTick's `/project/{id}/data` endpoint returns an entire project tree — metadata, tasks, subtasks — in a single response. Cascading requests add latency and complexity.

**Our decision:** Dashboard and summary endpoints return transactions, running balances, and category breakdowns in one call. Endpoints are designed around what the client needs to render a complete view, not around what is convenient for the database to return.
