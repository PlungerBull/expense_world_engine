# API Lessons — YNAB

> YNAB's transaction object is the most instructive financial data model available in any public API. These lessons are drawn from careful study of its field design and the reasoning behind each decision.

---

## 1. `cleared` and `approved` Are Separate Fields

YNAB distinguishes between a transaction being **cleared** (confirmed by the bank) and **approved** (accepted by the user). These are two independent states. A transaction can be imported from a bank feed and cleared but still pending user review (unapproved). A manually entered transaction can be approved but not yet cleared against a statement.

**Our decision:** Both `cleared` and `approved` are independent booleans on `expense_transactions`.
- Manually entered transactions: `approved = true`, `cleared = false` by default.
- CSV/webhook-imported transactions: `approved = false`, `cleared` depends on source.

---

## 2. `payee_id` / `payee_name` — Dual Fields for Flexibility

YNAB stores both a `payee_id` (FK to a Payees table) and a `payee_name` (denormalised string) on every transaction. When creating a transaction with a `payee_name` but no `payee_id`, the engine auto-matches or auto-creates a payee. The denormalised name avoids joins in list views.

**Our decision (deferred):** Our `title` field serves as a memo/description — what the bank printed or what the user typed — rather than a formal reusable payee entity. A formal `payees` table would enable spend-pattern analysis by payee ("you've paid this merchant 40 times") but adds schema and entry-flow complexity. Deferred to a later phase. When implemented, follow the dual-field pattern: `payee_id` (FK, nullable) + `payee_name` (denormalised, always populated).

---

## 3. `import_id` — Structured Deduplication Key

YNAB's `import_id` format for bank-imported transactions is `YNAB:[milliunit_amount]:[iso_date]:[occurrence]`. This is a deterministic, human-readable deduplication key. When the same transaction arrives twice, the engine detects it from the structured key alone — no multi-field query needed.

**Our decision (deferred):** The concept is correct but we encounter a complication: multiple transactions with the same amount, date, and account are common (e.g. two coffee purchases on the same day). A pure `amount:date` key would collide. Design of a reliable `import_id` scheme requires more thought and is deferred. Our `idempotency_keys` table handles API-call deduplication in the meantime.

---

## 4. Transfers as Paired Records

When a YNAB transfer occurs, two transaction records are created — one debit, one credit — each holding a `transfer_transaction_id` pointing to the sibling record. Neither record is a "parent"; they are peers. This is the correct relational model: atomic, balanced, and queryable from either account's perspective.

**Our decision:** Transfers create two rows in `expense_transactions`, each with `transfer_transaction_id` pointing to the other. We considered also storing `transfer_account_id` (the destination account directly on the row) but decided against it — it is derivable from the sibling row and storing it twice creates redundancy risk. `transfer_transaction_id` only.

---

## 5. `matched_transaction_id` — Reconciliation as a First-Class Field

YNAB stores a `matched_transaction_id` when an imported transaction is matched to an existing manually-entered transaction. This is the reconciliation link — the bridge between what the user typed and what the bank confirmed. Without it, a manual entry and a later bank import of the same real-world event produce a duplicate.

**Our decision (deferred):** Relevant when bank import / CSV matching is built. Add `matched_transaction_id` (nullable UUID, self-referencing FK) to `expense_transactions` at that phase.

---

## 6. `deleted` as an Explicit Field in Delta Responses

In YNAB's delta responses, deleted records are not simply absent — they are included with `deleted: true`. Clients relying on delta sync need to know not just what changed but what was removed, otherwise they accumulate stale data indefinitely.

**Our decision:** Our tombstone pattern (the `deleted_at` field) implements this. Every delta sync response includes soft-deleted records with their `deleted_at` timestamp. Clients treat any record with `deleted_at` set as a tombstone and remove it from local state.

---

## 7. `null` Over Omission for Missing Fields

YNAB made a deliberate API decision: fields with no data are returned as `null` rather than being omitted from the response. The response shape is always the same. Clients never need to check whether a key exists before reading it — deterministic parsing.

**Our decision:** All optional fields in API responses are always present, set to `null` when empty. No key omission. This is an engine-layer API convention, not a schema change.
