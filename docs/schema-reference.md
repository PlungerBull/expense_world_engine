# Expense Tracker — Schema Reference

> Single source of truth for all database tables.
> Architectural decisions: `api-design-principles.md`
> Lesson sources: `lessons-todoist.md`, `lessons-ticktick.md`, `lessons-ynab.md`, `lessons-lunchmoney.md`, `lessons-splitwise.md`

---

## Schema Conventions

These rules apply to all mutable tables unless explicitly noted as an exception.

- **Amounts in cents:** All monetary values stored as `bigint` in cents (e.g. $30.50 = 3050). Never floating point.
- **Amounts always positive:** `amount_cents` is always stored as a positive integer representing magnitude. The direction (inflow vs outflow) is determined by `transaction_type`, not by the sign of the amount. The API may expose negative numbers to clients via the `debit_as_negative` convention — this is a display concern only, never a storage concern.
- **Soft deletes:** All mutable tables have `deleted_at` (nullable timestamptz). `NULL` = active. Timestamp = soft-deleted. Hard deletion is never performed on financial records.
- **Sync version:** Every mutable table has `version` (integer, default 1), incremented on every update. Used by the sync token mechanism — each client tracks the max version it has seen and requests only rows with a higher version on the next sync.
- **UUIDs:** All primary keys are UUID (`uuid_generate_v4()`), generated client-side before server confirmation.
- **Timestamps:** `created_at` and `updated_at` on every mutable table, both `timestamptz`, defaulting to `now()`. Always stored in UTC.
- **snake_case:** All column and table names.
- **Smallints for enums:** Enum-like fields stored as `smallint`. Never raw strings. Mappings documented below.

### Smallint Enum Mappings

| Field | Table | Mapping |
|---|---|---|
| `transaction_type` | `expense_transactions` | 1 = expense, 2 = income, 3 = transfer |
| `transfer_direction` | `expense_transactions` | 1 = debit (balance decreases), 2 = credit (balance increases) |
| `status` | `expense_transaction_inbox` | 1 = pending, 2 = promoted, 3 = dismissed |
| `status` | `expense_reconciliations` | 1 = draft, 2 = completed |
| `transaction_source` | `expense_transaction_hashtags` | 1 = inbox, 2 = ledger |
| `action` | `activity_log` | 1 = created, 2 = updated, 3 = deleted, 4 = restored |

### Exceptions (no version / no deleted_at)

- `global_currencies` — static lookup, predefined rows, never user-edited
- `exchange_rates` — append-only reference, never edited or deleted by clients
- `users` — managed by Supabase Auth
- `activity_log` — immutable append-only audit trail. No soft delete, no version, no updated_at.
- `idempotency_keys` — expire via TTL; hard-deleted by a cleanup job after expiry.

---

## Infrastructure Tables

### users

Supabase Auth mirror. One row per authenticated user, created by the engine on first login contact. The `id` mirrors `auth.users.id` — the bridge between Supabase Auth and all application tables.

```
users
  - id              UUID, primary key              — mirrors auth.users.id
  - email           text
  - display_name    text, nullable
  - last_login_at   timestamptz, nullable           — updated on every successful authentication
  - created_at      timestamptz, default now()
  - updated_at      timestamptz, default now()
```

**Active user:** Derived at query time — not stored. A user is considered active if `last_login_at > now() - interval '30 days'`.

---

### user_settings

App preferences. One row per user, created alongside the `users` row on first login.

```
user_settings
  - user_id                        UUID, primary key, FK → users
  - theme                          smallint, NOT NULL, default 1
                                   — 1=system, 2=light, 3=dark
  - start_of_week                  smallint, NOT NULL, default 0
                                   — 0=Sunday, 1=Monday
  - main_currency                  text, NOT NULL, default 'PEN', FK → global_currencies.code
  - transaction_sort_preference    smallint, NOT NULL, default 1
                                   — 1=date, 2=created_at
  - display_timezone               text, NOT NULL, default 'UTC'
                                   — IANA string e.g. 'America/Lima'. Used for all client-side date boundaries.
  - sidebar_show_bank_accounts     boolean, NOT NULL, default true
  - sidebar_show_people            boolean, NOT NULL, default true
  - sidebar_show_categories        boolean, NOT NULL, default true
  - created_at                     timestamptz, default now()
  - updated_at                     timestamptz, default now()
```

**Timezone architecture:** All timestamps stored in UTC. `display_timezone` is the IANA string used for all "today" calculations, date boundaries, and overdue detection. Set to device timezone on first launch. Conversion to local time always happens at the presentation layer.

---

### global_currencies

Static lookup table. Predefined rows, never user-edited. No soft delete, no version.

```
global_currencies
  - code    text, primary key     — e.g. 'USD', 'PEN'
  - name    text, NOT NULL        — e.g. 'US Dollar'
  - symbol  text, NOT NULL        — e.g. '$'
```

---

### exchange_rates

Append-only reference table. Populated by a scheduled job (external API). Never edited or deleted by clients. One row per currency pair per day. No soft delete, no version.

```
exchange_rates
  - id               UUID, primary key, default uuid_generate_v4()
  - base_currency    text, NOT NULL, FK → global_currencies.code    — always 'USD'
  - target_currency  text, NOT NULL, FK → global_currencies.code
  - rate             numeric, NOT NULL
                     — units of target_currency per 1 USD (e.g. 3.75 = 1 USD = 3.75 PEN)
  - rate_date        date, NOT NULL
  - created_at       timestamptz, default now()
  - UNIQUE (base_currency, target_currency, rate_date)
```

**Rate source:** Frankfurter.app — free, no API key required, uses ECB rates. Endpoint: `https://api.frankfurter.app/latest?from=USD&to=PEN`. Sufficient for Phase 1.

**Fetch schedule:** A daily background job fetches the previous day's closing rate every morning and inserts one row per currency pair. Clients never write to this table.

**Missing rate fallback:** If no rate exists for a given date (e.g. the job hasn't run yet, or it's a weekend), the engine queries: `WHERE rate_date <= target_date ORDER BY rate_date DESC LIMIT 1` — the most recent available rate.

---

### sync_checkpoints

Tracks each client's position in the sync history. One row per user per client device. Used to compute delta responses.

A client that loses its `client_id` (e.g. app reinstall) generates a new UUID and starts a fresh sync from version 0 — re-downloading all data from the server. The server is always the source of truth.

```
sync_checkpoints
  - id               UUID, primary key, default uuid_generate_v4()
  - user_id          UUID, NOT NULL, FK → users
  - client_id        text, NOT NULL
                     — stable device or session identifier, assigned by the client on first launch
  - last_sync_token  text, NOT NULL
                     — opaque token returned by the last sync response
  - last_sync_at     timestamptz, NOT NULL
  - created_at       timestamptz, default now()
  - updated_at       timestamptz, default now()
  - UNIQUE (user_id, client_id)
```

---

### idempotency_keys

Deduplicates write operations. Clients send a unique key per intended write. If the server has already processed that key, it returns the stored response instead of creating a duplicate. Entries expire after 24 hours and are cleaned up by a background job.

**Why this matters:** A CLI or app sends "create $50 Food expense" → network timeout → client retries → without idempotency, two $50 expenses are created and the balance is corrupted. With idempotency, the retry gets the original response and no duplicate is created.

```
idempotency_keys
  - id                 UUID, primary key, default uuid_generate_v4()
  - key                text, NOT NULL
  - user_id            UUID, NOT NULL, FK → users
  - processed_at       timestamptz, NOT NULL
  - response_snapshot  jsonb, NOT NULL
                       — stored response returned verbatim on duplicate requests
  - expires_at         timestamptz, NOT NULL
                       — processed_at + 24 hours
  - created_at         timestamptz, default now()
  - UNIQUE (user_id, key)
```

---

### activity_log

Immutable append-only audit trail. Every mutation to any mutable table produces a row here. No soft delete, no version, no updated_at — rows are never modified after creation.

This table is both a correctness requirement (answers "why does my balance look wrong?") and the foundation for an Activity Feed UI feature — a stylised, human-readable log of all account changes surfaced to the user.

```
activity_log
  - id               UUID, primary key, default uuid_generate_v4()
  - user_id          UUID, NOT NULL, FK → users
  - resource_type    text, NOT NULL
                     — e.g. 'expense_transaction', 'expense_bank_account', 'expense_category'
  - resource_id      UUID, NOT NULL
  - action           smallint, NOT NULL
                     — 1=created, 2=updated, 3=deleted, 4=restored
  - before_snapshot  jsonb, nullable
                     — full row state before the change. null on creates.
  - after_snapshot   jsonb, nullable
                     — full row state after the change. null on deletes.
  - changed_by       UUID, NOT NULL, FK → users
  - created_at       timestamptz, default now()
```

---

## Expense Tables

### expense_bank_accounts

Real bank accounts and person virtual accounts (`is_person = true`). One currency per account. A real-world multi-currency card is modelled as separate accounts, one per currency. The same rule applies to person virtual accounts — if someone shares expenses in both PEN and USD, they have two rows.

`current_balance_cents` is a cached field on the account row itself — not stored on transactions. Reading an account's balance is a single row lookup, never an aggregation. The engine updates this field atomically on every transaction write, edit, and soft-delete. If a transaction is soft-deleted, the engine reverses its balance contribution in the same database operation.

Historical balance (e.g. "what was my balance on March 1?") is always computed on demand: `SUM(amount_cents WHERE transaction_date <= target_date)`.

```
expense_bank_accounts
  - id                     UUID, primary key, default uuid_generate_v4()
  - user_id                UUID, NOT NULL, FK → users
  - name                   text, NOT NULL
  - currency_code          text, NOT NULL, default 'PEN', FK → global_currencies.code
                           — immutable after creation
  - is_person              boolean, NOT NULL, default false
                           — true for virtual accounts representing people (debt tracking)
                           — person accounts appear in the People sidebar section, not Accounts
  - color                  text, NOT NULL, default '#3b82f6'
  - current_balance_cents  bigint, NOT NULL, default 0
                           — cached running balance. Updated atomically on every transaction write.
                           — never recalculate as SUM() at read time; always read this cached value.
                           — soft-deleting a transaction reverses its balance contribution atomically.
  - is_archived            boolean, NOT NULL, default false
                           — hides from pickers and entry flows but preserves all historical records
                           — accounts with transactions can be archived; they cannot be hard-deleted
  - sort_order             integer, NOT NULL, default 0
  - created_at             timestamptz, default now()
  - updated_at             timestamptz, default now()
  - version                integer, NOT NULL, default 1
  - deleted_at             timestamptz, nullable
  - UNIQUE (user_id, name, currency_code)
```

---

### expense_categories

Flat category list. No hierarchy. No type restriction — any category can be used on any transaction type (expense, income, or transfer). System categories are auto-created, non-deletable, and non-renamable.

```
expense_categories
  - id          UUID, primary key, default uuid_generate_v4()
  - user_id     UUID, NOT NULL, FK → users
  - name        text, NOT NULL
  - color       text, NOT NULL, default '#6b7280'
  - is_system   boolean, NOT NULL, default false
                — true for @Transfer and @Debt. System categories cannot be deleted or renamed.
  - sort_order  integer, NOT NULL, default 0
  - created_at  timestamptz, default now()
  - updated_at  timestamptz, default now()
  - version     integer, NOT NULL, default 1
  - deleted_at  timestamptz, nullable
  - UNIQUE (user_id, name)
```

**System categories (auto-created on first use, `is_system = true`):**
- `@Transfer` — auto-assigned to both legs of a transfer between the user's own real accounts. Cannot be manually assigned to other transactions.
- `@Debt` — auto-assigned to transactions on person accounts (both the receivable entry and the settlement). Represents money owed to or from people.

**Category on transfers:** `category_id` is NOT NULL on all transactions, including transfers. Own-account transfers auto-receive `@Transfer`. Person-account transactions auto-receive `@Debt`. This enforces completeness without requiring the user to choose a category manually for these flows.

**Refunds:** Use the same category as the original expense. Tag the refund as `transaction_type = 2 (income)`. The category accumulates both directions — net spend in that category across the month reflects the true cost.

---

### expense_transaction_inbox

Incomplete transactions waiting to be promoted to the ledger. Fields are nullable — the inbox exists precisely because the user doesn't have all the information yet.

```
expense_transaction_inbox
  - id            UUID, primary key, default uuid_generate_v4()
  - user_id       UUID, NOT NULL, FK → users
  - title         text, nullable
  - description   text, nullable
  - amount_cents  bigint, nullable             — always positive when set
  - date          timestamptz, nullable
  - account_id    UUID, nullable, FK → expense_bank_accounts
  - category_id   UUID, nullable, FK → expense_categories
  - exchange_rate numeric, default 1.0
                  — converts account currency → user's main_currency for display
                  — auto-filled from exchange_rates table, always user-overridable
                  — 1.0 when account currency = main_currency
  - status        smallint, NOT NULL, default 1
                  — 1=pending (active in inbox)
                  — 2=promoted (moved to ledger; row is soft-deleted)
                  — 3=dismissed (rejected without promoting; row is soft-deleted)
                  — status distinguishes why a row was soft-deleted
  - created_at    timestamptz, default now()
  - updated_at    timestamptz, default now()
  - version       integer, NOT NULL, default 1
  - deleted_at    timestamptz, nullable
```

**Promotion flow:** User-initiated. When `title`, `amount_cents`, `date`, `account_id`, and `category_id` are all present and `date ≤ now()`, the item is eligible. Promoting atomically:
1. Creates a new row in `expense_transactions` with all validated data.
2. Sets `inbox_id` on the new transaction row to link back to this item.
3. Sets `status = 2` (promoted) on this inbox row.
4. Sets `deleted_at` on this inbox row (soft delete).
5. Updates `current_balance_cents` on the account.

`exchange_rate` is never a blocking field — it auto-populates from the reference table and does not prevent promotion.

**Deferred features:** Recurring expenses (`is_recurring`), CSV import (`source_text`), and receipt capture (`receipt_photo_url`) are not in Phase 1. These fields will be added when those phases begin.

---

### expense_transactions

Confirmed transactions — the clean, reliable ledger.

**Balance update rule:** The engine updates `current_balance_cents` on the account for every transaction write. One exception: **parent transactions in a split do not update the balance**. Only child rows (where `parent_transaction_id IS NOT NULL`) and standalone rows (no parent, no children) update the balance. The parent is a display container only. Splits must be created atomically in a single API call.

```
expense_transactions
  - id                        UUID, primary key, default uuid_generate_v4()
  - user_id                   UUID, NOT NULL, FK → users
  - title                     text, NOT NULL
  - description               text, nullable
  - amount_cents              bigint, NOT NULL
                              — always positive. Represents magnitude only.
                              — direction is determined by transaction_type (and transfer_direction for transfers).
                              — immutable once the transaction is part of a completed reconciliation.
  - amount_home_cents         bigint, nullable
                              — cached: amount_cents converted to main_currency via exchange_rate.
                              — always positive.
                              — not the source of truth; derivable from amount_cents × exchange_rate.
                              — recalculated by the engine when:
                                  (a) the transaction date changes (engine fetches historical rate for new date)
                                  (b) the user's main_currency changes (engine recalculates all transactions)
  - transaction_type          smallint, NOT NULL
                              — 1=expense (subtracts from account balance)
                              — 2=income (adds to account balance)
                              — 3=transfer (direction determined by transfer_direction)
  - transfer_direction        smallint, nullable
                              — only set when transaction_type = 3
                              — 1=debit (balance decreases on this account — the outgoing leg)
                              — 2=credit (balance increases on this account — the incoming leg)
  - date                      timestamptz, NOT NULL, default now()
  - account_id                UUID, NOT NULL, FK → expense_bank_accounts
  - category_id               UUID, NOT NULL, FK → expense_categories
  - exchange_rate             numeric, NOT NULL, default 1.0
                              — rate at time of entry (or at transaction date for imports).
                              — locked at entry. Only recalculated when date changes.
  - cleared                   boolean, NOT NULL, default false
                              — true when the transaction has been confirmed on a bank statement.
                              — drives the reconciliation flow.
  - transfer_transaction_id   UUID, nullable, FK → expense_transactions (self-referencing)
                              — each row in a paired transfer points to the other row
                              — the engine validates that paired transfers net to zero
  - parent_transaction_id     UUID, nullable, FK → expense_transactions (self-referencing)
                              — for split transactions: child rows point to their parent
                              — parent rows do not update account balance (see balance rule above)
  - inbox_id                  UUID, nullable, FK → expense_transaction_inbox
                              — lineage back to the inbox item this was promoted from
  - reconciliation_id         UUID, nullable, FK → expense_reconciliations
  - created_at                timestamptz, default now()
  - updated_at                timestamptz, default now()
  - version                   integer, NOT NULL, default 1
  - deleted_at                timestamptz, nullable
```

**Field locking on reconciliation:** When `reconciliation_id` is set and the referenced reconciliation has `status = 2` (completed), these four fields are read-only: `amount_cents`, `account_id`, `title`, `date`. All other fields remain editable. Un-reconciling (reverting status to 1) unlocks all fields.

**Deferred features:** Receipt capture (`receipt_photo_url`), raw import text (`source_text`), and bank-import approval flow (`approved`) are not in Phase 1.

---

### expense_hashtags

Registry of all hashtag names per user. Used for autocomplete and filtering. Hashtags are cross-cutting — they cut across categories. A `#vacation` tag can appear on a Food expense, a Transport expense, and an Accommodation expense. Querying by hashtag returns everything regardless of category.

`@Other` is a pre-seeded default hashtag (not a system category). It appears in hashtag-based views when a transaction has no hashtag assigned. It is a display convention — not enforced by the schema.

```
expense_hashtags
  - id          UUID, primary key, default uuid_generate_v4()
  - user_id     UUID, NOT NULL, FK → users
  - name        text, NOT NULL
  - sort_order  integer, NOT NULL, default 0
  - created_at  timestamptz, default now()
  - updated_at  timestamptz, default now()
  - version     integer, NOT NULL, default 1
  - deleted_at  timestamptz, nullable
  - UNIQUE (user_id, name)
```

---

### expense_transaction_hashtags

Junction table. Links hashtags to transactions in either the inbox or the ledger. `transaction_source` distinguishes which table `transaction_id` refers to (no formal FK, but always valid).

A transaction with 3 hashtags produces 3 rows in this table — same `transaction_id`, three different `hashtag_id` values.

```
expense_transaction_hashtags
  - id                  UUID, primary key, default uuid_generate_v4()
  - transaction_id      UUID, NOT NULL
                        — references expense_transactions OR expense_transaction_inbox
  - transaction_source  smallint, NOT NULL
                        — 1=inbox, 2=ledger
  - hashtag_id          UUID, NOT NULL, FK → expense_hashtags
  - user_id             UUID, NOT NULL, FK → users
  - created_at          timestamptz, default now()
  - updated_at          timestamptz, default now()
  - version             integer, NOT NULL, default 1
  - deleted_at          timestamptz, nullable
  - UNIQUE (transaction_id, hashtag_id)
```

---

### expense_reconciliations

Batch reconciliation records. Each batch belongs to one account and covers a date range. The user opens a reconciliation, marks transactions as `cleared`, and completes it when the cleared balance matches the bank statement.

```
expense_reconciliations
  - id                       UUID, primary key, default uuid_generate_v4()
  - user_id                  UUID, NOT NULL, FK → users
  - account_id               UUID, NOT NULL, FK → expense_bank_accounts
  - name                     text, NOT NULL
  - date_start               timestamptz, nullable
  - date_end                 timestamptz, nullable
  - status                   smallint, NOT NULL, default 1
                             — 1=draft, 2=completed
  - beginning_balance_cents  bigint, NOT NULL, default 0
                             — pre-filled from the previous reconciliation's ending_balance_cents.
                             — if no prior reconciliation exists, defaults to 0.
                             — always user-editable in case of discrepancy.
  - ending_balance_cents     bigint, NOT NULL, default 0
                             — user-entered from the bank statement. Always editable.
  - created_at               timestamptz, default now()
  - updated_at               timestamptz, default now()
  - version                  integer, NOT NULL, default 1
  - deleted_at               timestamptz, nullable
```

**Field locking on completion:** When `status = 2` (completed), four fields lock on every transaction in the batch: `amount_cents`, `account_id`, `title`, `date`. Un-reconciling (reverting to `status = 1`) unlocks all fields.

---

## Deferred Tables (Later Phases)

### expense_budgets *(Phase 3+)*

Monthly per-category budget targets. Deferred to the budgeting phase. No schema defined yet.

### transaction_shares *(Phase 4+)*

Cross-user shared expenses. Deferred to the people and sharing phase. When implemented, follow the Splitwise patterns documented in `lessons-splitwise.md`: separate `paid_share_cents` and `owed_share_cents`, pre-computed balance cache, settlements as standard transactions.

---

## Multi-Currency Model

### The core problem

When a user has accounts in PEN and USD, every report — total spend by category, monthly summary, net worth — needs a single unified number. You can't sum PEN and USD directly. The solution is `amount_home_cents`: every transaction carries a pre-converted home-currency equivalent, computed at entry time. Reports always SUM `amount_home_cents`, never `amount_cents`.

### Two amount fields, two purposes

Every transaction has exactly two amount fields:

| Field | Purpose | Currency | Mutable? |
|---|---|---|---|
| `amount_cents` | Accounting — what the bank sees | Account's native currency | Immutable once reconciled |
| `amount_home_cents` | Reporting — unified dashboard total | User's home currency (PEN) | Recalculated when date or home currency changes |

These two fields serve completely different systems and never interfere with each other.

### How dashboard totals work

```sql
-- Total Food spend this month, across all accounts and currencies
SELECT SUM(amount_home_cents)
FROM expense_transactions
WHERE category = 'Food'
  AND transaction_date >= start_of_month
  AND transaction_type = 1  -- expenses only
  AND deleted_at IS NULL
```

No currency conversion at query time. Every transaction already carries its home-currency value. Example:

| Transaction | Native | Rate locked at entry | amount_home_cents |
|---|---|---|---|
| Netflix | $15.00 USD | 3.75 | S/ 56.25 |
| Lunch | S/ 45.00 PEN | 1.00 | S/ 45.00 |
| Spotify | $5.00 USD | 3.72 | S/ 18.60 |
| **Total Food** | | | **S/ 119.85** |

Each rate was locked when the transaction was entered. The total reflects what you actually spent at the rates that applied at the time — not what those amounts would be worth today.

### Exchange rate lifecycle

1. Daily job fetches the closing rate from Frankfurter.app and inserts a row into `exchange_rates`.
2. When a transaction is created, the engine looks up the rate for that transaction's date. If no rate exists for that exact date, it falls back to the most recent available rate.
3. The rate is written to `exchange_rate` on the transaction and `amount_home_cents` is computed and cached.
4. The rate is now **locked**. It never changes unless the transaction date changes.
5. If the transaction date is edited, the engine fetches the historical rate for the new date and recalculates `amount_home_cents`.

### User-overridable rate

The `exchange_rate` field is always user-overridable. If you withdraw USD at an ATM and the actual rate was 3.65 (after fees), not the official 3.75, you enter 3.65. The `amount_home_cents` is then computed from your actual rate. Reconciliation against the bank statement still uses `amount_cents` in native currency — the override only affects home-currency reporting.

### Reconciliation and exchange rates

Reconciliation is **always done in the account's native currency**. You reconcile a USD account against a USD bank statement by matching `amount_cents` values. Exchange rates are completely irrelevant to reconciliation. The two systems never interfere.

### Dashboard net worth

Each account's `current_balance_cents` is stored in its native currency. To show a combined net worth, the dashboard converts each account balance using today's most recent rate. This value is approximate by nature and is labeled as such: "~S/ 4,250 equivalent." No one expects a cross-currency net worth to be exact to the cent.

### Home currency change

If the user changes `main_currency` (rare), the engine triggers a background job that recalculates `amount_home_cents` on every transaction using the historical rates already stored in `exchange_rates`. `amount_cents` is never touched.

### Decision summary

| Question | Decision |
|---|---|
| Store amounts | Native currency, always positive cents |
| Rate source | Frankfurter.app, fetched daily |
| Missing rate | Most recent available rate |
| Rate locked | At entry time, per transaction |
| Rate overridable | Yes — user enters actual rate received |
| Recalculate when | Transaction date changes, or home currency changes |
| Dashboard totals | SUM(amount_home_cents) — no conversion at query time |
| Net worth display | Today's rate × account balance, labeled approximate |
| Reconciliation | Always in native currency — rates irrelevant |

---

## People Model

People are bank accounts with `is_person = true`. There is no separate people table. If someone shares expenses in multiple currencies, they have multiple accounts — one per currency — both shown in the People sidebar section.

**Debt tracking model:** A person account's balance represents the financial position with that person. Positive balance = they owe you money. Negative balance = you owe them money. Transactions on person accounts use the `@Debt` system category automatically.

**Full debt cycle example (you pay $100 lunch, split $50 with John):**

| Step | Transaction | Account | Category | Balance effect |
|---|---|---|---|---|
| 1. Pay lunch | $100 expense | Checking | Food | Checking −100 |
| 2. Register John's share | $50 income | John (person) | @Debt | John +50 (he owes you) |
| 3. John pays you back | $50 expense | John (person) | @Debt | John −50 = 0 |
| 4. Receive John's payment | $50 income | Checking | Food | Checking +50 |

End state: Checking −50 (your true out of pocket), Food −50 (your true food spend), John 0 (debt cleared).

**Shortcut API flows:** Creating a split expense with a person and settling a debt are exposed as dedicated API endpoints that create both transactions atomically. See `engine-spec.md` for `/transactions` split field and `/accounts/{id}/settle`.

**System category assignment:**

| Scenario | Primary leg category | Paired leg category |
|---|---|---|
| Real account → Real account | `@Transfer` (automatic) | `@Transfer` (automatic) |
| Real account + Person account | User's chosen category | `@Debt` (automatic) |
| Person account only | `@Debt` (automatic) | N/A |

---

## Split Transactions

Split transactions are modelled using `parent_transaction_id` (self-reference on `expense_transactions`).

**Structure:** One parent row holds the original full amount. Child rows hold the split portions. `parent_transaction_id` on each child points to the parent's `id`.

**Balance rule:** The parent row **never** updates `current_balance_cents`. Only child rows update the balance. The parent is a display and grouping container — its amount equals the sum of its children's amounts.

**Creation:** Splits must be created atomically in a single API call (parent + all children together). Creating a parent alone and adding children later is not supported — it would cause an incorrect transient balance state.

**Example:** $100 grocery receipt split into $60 Food and $40 Household:
- Parent: $100, no category required, account=Checking — no balance update
- Child 1: $60, Food, account=Checking, parent_id=[parent] — Checking −60
- Child 2: $40, Household, account=Checking, parent_id=[parent] — Checking −40
- Net: Checking −100 ✓

---

## Recurrence *(Phase 5 — Fully Deferred)*

Recurring expenses are not part of Phase 1 through 4. No recurring-related columns exist in the current schema. Full recurrence architecture (patterns, anchor modes, generation logic) will be designed and added as a schema migration when Phase 5 begins.
