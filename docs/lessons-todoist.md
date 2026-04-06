# API Lessons — Todoist

> Observations from studying the Todoist REST and Sync API. These lessons directly informed the core architectural decisions of the expense tracker engine. See `api-design-principles.md` for the decisions themselves.

---

## 1. UUID-First Resource Identification

Every resource in Todoist — tasks, projects, sections, labels, comments — is identified by a globally unique UUID. All operations reference this UUID directly in the URL path or request body. There is no reliance on names, slugs, or composite keys. The Sync API even introduced `temp_id` so resources can be referenced client-side before the server confirms their creation.

**Our decision:** UUID primary keys on all tables, generated client-side before server confirmation. A resource's name, amount, or category can change — its identity cannot. The frontend never needs to "find" a resource by querying its attributes; it always has the handle.

---

## 2. Headless Architecture

Todoist exposes its full product functionality through a public API completely decoupled from its frontends. The web app, mobile apps, and third-party integrations are all equal consumers of the same API surface. No feature is baked into the UI — everything flows through the API layer. Official SDKs in Python and JavaScript wrap the same endpoints their own apps use.

**Our decision:** The Python engine is the product. The iOS app and CLI are clients. No endpoint is designed for a specific screen. No business logic lives in the client. If a feature cannot be expressed as an API operation, it is not well-designed.

---

## 3. Sync Token Pattern

Todoist's Sync API is built around a `sync_token`. The first request sends `"*"` to fetch the full account state. Every subsequent request sends the last received token and gets back only what changed — new records, updates, and deletions communicated as tombstones. The client merges the delta into its local state.

**Our decision:** `sync_checkpoints` table tracking each client's position in the sync history. Every mutable table has a `version` column incremented on every update. Deletions are communicated explicitly as tombstones — never inferred from absence.

---

## 4. Idempotency Keys

Todoist supports an optional `X-Request-Id` HTTP header on write operations. A request carrying a given ID that has already been processed is silently discarded — the server returns the result of the original operation.

**Our decision:** `idempotency_keys` table. Clients generate a unique key per intended write operation and send it with the request. The engine stores processed keys with a 24-hour TTL and deduplicates on receipt. Critical for transaction creation — a duplicate corrupts balances and reports.

---

## 5. Soft Delete / Archive Pattern

Todoist distinguishes between archiving and deleting. Archived items are removed from active views but remain accessible. Even "deleted" items are communicated as tombstones in the Sync API — the server tells the client "this item is gone," rather than simply omitting it. Historical state is a first-class concern.

**Our decision:** Every mutable table has `deleted_at` (nullable timestamptz). Deleted records are excluded from active queries by default but remain in the database and participate in historical calculations. Hard deletion is never performed on financial records. Archiving (distinct from deletion) applies to bank accounts — an archived account is hidden from pickers but all its transactions remain intact.

---

## 6. Activity Log as First-Class

Todoist exposes a dedicated `/activity/get` endpoint logging every action on any resource — what changed, which resource was affected, when, and by whom. This is part of the core API surface, not an admin afterthought.

**Our decision:** `activity_log` table capturing every mutation: resource type, resource ID, action (created/updated/deleted/restored), full before/after JSON snapshots, timestamp, and actor. Designed from day one — retrofitting it later means the historical record is incomplete. For a financial application this is a correctness requirement, not a nice-to-have.

---

## 7. Command Batching

Todoist's Sync API allows multiple operations to be bundled into a single HTTP request via a `commands` array. Each command is independent with its own type, UUID, and arguments. The server processes them in order and returns a consolidated response.

**Our decision:** Batch endpoints for inherently multi-record workflows: CSV import, split transactions across multiple categories, account initialisation with opening balance. Either everything succeeds or nothing does — mapped directly to database transactions on the server side.
