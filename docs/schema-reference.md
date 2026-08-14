# Expense Tracker — Schema Reference

> Single source of truth for all database tables.
> Conventions: `../CLAUDE.md`
>
> Regenerated 2026-08-06 from the live catalog (`information_schema` + `pg_indexes` +
> `pg_constraint` + `pg_policies`) after the deletion program landed (`sql/020`–`sql/025`);
> amended 2026-08-10 for the transfer removal (`sql/030` — three columns and two CHECKs dropped),
> then 2026-08-13 for the colour-format CHECKs (`sql/031` — two CHECKs added, no column changes).
> **14 tables, 123 columns.**

---

## Schema Conventions

These rules apply to all mutable tables unless explicitly noted as an exception.

- **Amounts in cents:** All monetary values stored as `bigint` in cents (e.g. $30.50 = 3050). Never floating point.
- **Amounts always positive:** `amount_cents` is always stored as a positive integer representing magnitude — enforced by CHECK constraints. Direction (outflow vs inflow) is `transaction_type`, never the sign of the amount. The API may expose negative numbers to clients via the `debit_as_negative` convention — a display concern only, never a storage concern.
- **Soft deletes:** All mutable *domain* tables have `deleted_at` (nullable timestamptz). `NULL` = active. Timestamp = soft-deleted. Hard deletion is never performed on financial records. `user_settings` is the deliberate exception — a settings row lives and dies with its user (`sql/024`).
- **Optimistic version counter:** Mutable tables carry `version` (integer, default 1), incremented by the engine on every update. Clients read it and may use it to detect concurrent edits; they never send it, and there is no `If-Match`/409 mechanism. (Its original purpose — the `/sync` delta protocol — was deleted with `/sync` in `sql/023`; the counter itself stays.)
- **UUIDs:** All primary keys are UUID (`uuid_generate_v4()`), generated client-side before server confirmation.
- **Timestamps:** `created_at` and `updated_at` on every mutable table, both `timestamptz`, defaulting to `now()`. Always stored in UTC.
- **snake_case:** All column and table names.
- **Smallints for enums:** Enum-like fields stored as `smallint`. Never raw strings. Mappings documented below. Closed enums are CHECK-enforced; a CHECK on a nullable column must spell out `IS NOT NULL` (see `CLAUDE.md`, sign convention).

### Smallint Enum Mappings

| Field | Table | Mapping |
|---|---|---|
| `transaction_type` | `expense_transactions` | 1 = outflow (expense), 2 = inflow (income). **There is no value 3** — direction is the only fact this column encodes (`sql/020`; the transfer discriminator it once pointed at was dropped by `sql/030`). CHECK-enforced. |
| `transaction_type` | `expense_transaction_inbox` | same enum, nullable (a draft may have no amount yet). CHECK-enforced with an explicit `IS NULL OR` arm. |
| `status` | `expense_transaction_inbox` | 1 = pending, 2 = promoted. **There is no value 3** — a dismissed row is `status = 1` + `deleted_at` set (the status records how far the row got, `deleted_at` records that it left the inbox). CHECK-enforced (`sql/029`). |
| `status` | `expense_reconciliations` | 1 = draft, 2 = completed. CHECK-enforced (`sql/025`). |
| `transaction_source` | `expense_transaction_hashtags` | 1 = ledger attach path — the only value ever written; see the table's section. CHECK-enforced (`sql/027`). |
| `action` | `activity_log` | 1 = created, 2 = updated, 3 = deleted, 4 = restored. CHECK-enforced (`sql/029`). |

### Exceptions (no version / no deleted_at)

- `global_currencies` — static lookup, predefined rows, never user-edited
- `exchange_rates` — append-only reference, never edited or deleted by clients
- `users` — managed alongside auth; no version, no deleted_at
- `user_settings` — has `version`, has **no** `deleted_at` (`sql/024`)
- `activity_log` — immutable append-only audit trail. No soft delete, no version, no updated_at.
- `idempotency_keys` — permanent (`sql/026`); rows are never deleted. Growth at the owner's write rate is a few MB/year.
- `expense_transaction_hashtags` — has `deleted_at`, has **no** `version` (`sql/024`); versioning lives on the parent transaction (see the table's section).

### Row-level security

Every table carries an RLS policy (`auth.uid() = user_id`; `users` uses `= id`; `global_currencies` and `exchange_rates` are authenticated-read-only) and `rowsecurity` is on. Under the local profile these are **inert** — the engine connects as the table owner — so the engine-side `user_id` predicate in every query is the only live isolation. See `CLAUDE.md`, "Tenant isolation is engine-side, not RLS".

---

## Infrastructure Tables

### users

Auth mirror. One row per authenticated user, created by the engine on first contact. The `id` mirrors `auth.uids.id` under the cloud profile — the bridge between auth and all application tables.

```
users
  - id              UUID, primary key              — mirrors auth.users.id (cloud profile)
  - display_name    text, nullable
  - last_login_at   timestamptz, nullable           — updated on every successful authentication
  - created_at      timestamptz, NOT NULL, default now()
  - updated_at      timestamptz, NOT NULL, default now()
```

`email` was dropped in `sql/024` — the engine never read it, and auth identity lives with the auth provider, not in this table.

**Active user:** Derived at query time — not stored. A user is considered active if `last_login_at > now() - interval '30 days'`.

**`handle_new_auth_user()`** — a trigger function (rewritten by `sql/024` to stop inserting `email`) that seeds `users` + `user_settings` when an `auth.users` row appears. Under the cloud profile it is attached to a trigger on `auth.users`; under the local profile the function exists but no trigger fires it — `POST /auth/bootstrap` does the seeding instead.

---

### user_settings

App preferences. One row per user, created alongside the `users` row. Never soft-deleted — the row lives and dies with its user.

```
user_settings
  - user_id            UUID, primary key, FK → users
  - main_currency      text, NOT NULL, default 'PEN', FK → global_currencies.code
                       — CHECK (main_currency = 'PEN') since sql/018: home currency is
                         locked; PUT /auth/settings 422s any attempt to change it.
  - display_timezone   text, NOT NULL, default 'UTC'
                       — IANA string e.g. 'America/Lima'. Used for all date boundaries.
                         Validated on every write path (helpers/validation.validate_timezone,
                         422 on a non-IANA zone) since sql/024.
  - created_at         timestamptz, NOT NULL, default now()
  - updated_at         timestamptz, NOT NULL, default now()
  - version            integer, NOT NULL, default 1
```

The six client-preference columns (`theme`, `start_of_week`, `transaction_sort_preference`, three `sidebar_show_*` flags) and `deleted_at` were dropped in `sql/024` — echo-only client state the engine never read.

**Timezone architecture:** All timestamps stored in UTC. `display_timezone` is the IANA string used for all "today" calculations, date boundaries, and month bounds. Conversion to local time always happens at the presentation layer.

---

### global_currencies

Static lookup table. Predefined rows, never user-edited. No soft delete, no version.

```
global_currencies
  - code    text, primary key     — 'USD', 'PEN'
  - CHECK (code IN ('USD', 'PEN'))   — sql/015: Phase-1 currency lock
```

`name` and `symbol` were dropped in `sql/024` — display strings are the clients' concern. Lifting the CHECK (plus cross-rate math) is the labelled scale boundary in `CLAUDE.md`.

---

### exchange_rates

Append-only reference table. Populated by a scheduled job (external API). Never edited or deleted by clients. One row per currency pair per day. No soft delete, no version.

```
exchange_rates
  - id               UUID, primary key, default uuid_generate_v4()
  - base_currency    text, NOT NULL, FK → global_currencies.code    — always 'USD'
  - target_currency  text, NOT NULL, FK → global_currencies.code
  - rate             numeric, NOT NULL, CHECK (rate > 0)  [sql/027]
                     — units of target_currency per 1 USD (e.g. 3.75 = 1 USD = 3.75 PEN).
                       The fetch/backfill jobs also refuse non-positive provider values
                       before inserting; the CHECK is the fail-closed backstop, because
                       since sql/021 this table is the sole source of every home-currency
                       figure — one bad row would misprice reports, not one write.
  - rate_date        date, NOT NULL
  - created_at       timestamptz, NOT NULL, default now()
  - UNIQUE (base_currency, target_currency, rate_date)
```

**Rate source:** fawazahmed0/currency-api — free, no API key, CDN-hosted, ~200 currencies, dated endpoints for backfill. Endpoint: `https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.min.json`. *(Originally Frankfurter.app; replaced 2026-07-30 — Frankfurter serves ECB reference rates only and the ECB list has no PEN.)*

**Fetch schedule:** A daily background job fetches the previous day's closing rate every morning and inserts one row per currency pair. Clients never write to this table.

**Stale-date backward scan:** The engine always queries `WHERE rate_date <= target_date ORDER BY rate_date DESC LIMIT 1`, so a weekend or one-day fetch gap transparently falls back to the most recent prior rate.

**Truly missing rate:** Conversion happens only at read time (`sql/021`; `docs/currency-model-decision.md`). A row whose date has no rate on or before it converts to `null`, and every home-currency aggregate pairs its `SUM` with an `unconverted_count` so a partial total is reported as `null` rather than an understatement. **No write path performs a rate lookup**, so a stale rate table can never block recording a transaction. (The old `422 RATE_UNAVAILABLE` write precondition died with the stored columns.)

---

### idempotency_keys

Deduplicates write operations. Clients send a unique key per intended write. If the server has already processed that key, it returns the stored response instead of creating a duplicate. Keys are **permanent** (`sql/026` dropped `expires_at`): a used key replays forever, and nothing deletes rows. Replay requires the same request — a reused key whose `request_hash` doesn't match answers `409`.

**Why this matters:** A CLI or app sends "create $50 Food expense" → network timeout → client retries → without idempotency, two $50 expenses are created. With idempotency, the retry gets the original response and no duplicate is created.

```
idempotency_keys
  - id                 UUID, primary key, default uuid_generate_v4()
  - key                text, NOT NULL
  - user_id            UUID, NOT NULL, FK → users
  - response_snapshot  jsonb, NOT NULL
                       — stored response BODY returned verbatim on duplicate requests.
                         JSON null (not SQL NULL) for POST /auth/pat: its response
                         carries the one-time plaintext token and is deliberately
                         not replayable — replays answer 409 (sql/026, bug 2.4).
  - response_status    smallint, NOT NULL, default 200
                       — HTTP status captured alongside the body so replays reconstruct
                         the full envelope verbatim. Added in sql/011.
  - request_hash       text, NOT NULL
                       — sha256 fingerprint of (method, path, query, raw body). A
                         replay with a different fingerprint 409s instead of returning
                         the unrelated snapshot. '' on rows stored before sql/026
                         (grandfathered: comparison skipped).
  - created_at         timestamptz, NOT NULL, default now()
  - UNIQUE (user_id, key)
```

`processed_at` was dropped in `sql/024` — written on every claim, read never. `expires_at` followed in `sql/026` when the 24-hour TTL itself was deleted (while it existed, it was anchored to claim time, `now() + 24 hours` — never to `processed_at`, despite what an earlier revision of this document claimed).

**Concurrency:** Every write handler acquires a transaction-scoped Postgres advisory lock (`pg_advisory_xact_lock`) hashed from `(user_id, key)` as the first statement inside the write transaction. Two concurrent requests with the same key serialize at the DB — the second blocks until the first commits, then reads the stored snapshot and returns it. This closes the check-then-store race that would otherwise allow duplicate side effects.

---

### activity_log

Immutable append-only audit trail. Every mutation to any mutable table produces a row here. No soft delete, no version, no updated_at — rows are never modified after creation.

This table is both a correctness requirement (answers "why does my balance look wrong?") and the foundation for an Activity Feed UI feature.

```
activity_log
  - id               UUID, primary key, default uuid_generate_v4()
  - user_id          UUID, NOT NULL, FK → users
  - resource_type    text, NOT NULL
                     — e.g. 'expense_transaction', 'expense_bank_account', 'expense_category'
  - resource_id      UUID, NOT NULL
  - action           smallint, NOT NULL — CHECK (action IN (1, 2, 3, 4)), sql/029
                     — 1=created, 2=updated, 3=deleted, 4=restored
  - before_snapshot  jsonb, nullable
                     — full row state before the change. null on creates.
  - after_snapshot   jsonb, nullable
                     — full row state after the change. null on deletes.
  - changed_by       UUID, NOT NULL, FK → users
                     — the user-id anchor; the resource owner's user_id when the
                       actor is the user themself.
  - created_at       timestamptz, NOT NULL, default now()
```

`actor_type` was dropped in `sql/024` — with one user and no admin surface, every actor is the same person. Its removal is a recorded wire change (`docs/client-breaking-changes.md`, 2026-08-06).

---

### personal_access_tokens

Long-lived engine-issued credentials — the only live auth mechanism under the local profile (the JWT branch was deleted 2026-08-03; see `CLAUDE.md`). The middleware in `app/deps.py` resolves the bearer token into an `AuthUser`; downstream endpoints are unaware of the mechanism. Added in sql/016.

Security model: only the SHA-256 hash is stored. The plaintext is returned once on creation and never recoverable. Revocation is a soft-delete (`revoked_at`), and the active-lookup index filters revoked rows out so a revoked PAT stops authenticating on the very next request.

```
personal_access_tokens
  - id            UUID, primary key, default uuid_generate_v4()
  - user_id       UUID, NOT NULL, FK → users
  - token_hash    text, NOT NULL, UNIQUE
                  — SHA-256 hex digest of the plaintext token. The engine hashes
                    incoming Authorization headers the same way and looks up by
                    this column.
  - token_prefix  text, NOT NULL
                  — first 12 chars of the plaintext ('ewe_pat_' + 4 random).
                    Stored cleartext for display in a future management UI and
                    for GitHub/GitGuardian-style leak scanners.
  - name          text, nullable
                  — optional user-supplied label ('laptop', 'render-cron').
  - created_at    timestamptz, NOT NULL, default now()
  - revoked_at    timestamptz, nullable
                  — soft-delete marker. NULL = active. Non-null = revoked and no
                    longer resolves in auth.

Indexes:
  - idx_pat_token_hash_active (token_hash) WHERE revoked_at IS NULL
    Partial index backing the per-request auth lookup.
```

No `version` or `updated_at`: PATs are immutable between creation and revocation; they are never edited in place. No list endpoint and no `last_used_at` — deliberate (see `CLAUDE.md`, single-user-shaped table).

---

## Expense Tables

### expense_bank_accounts

Real bank accounts and person virtual accounts (`is_person = true`). One currency per account. A real-world multi-currency card is modelled as separate accounts, one per currency. The same rule applies to person virtual accounts — if someone shares expenses in both PEN and USD, they have two rows.

`current_balance_cents` is **not a column** (`sql/022`, 2026-08-06). It is computed at read time as the signed sum of the account's non-deleted transactions, including its `@Opening` seed, in the account's own currency — `app/helpers/account_balance.py`. It remains on the wire unchanged; only its source moved. It was a cached column until sql/022, updated by hand on every transaction write, and that made it a derived value with two sources of truth that could disagree permanently and silently.

Historical balance (e.g. "what was my balance on March 1?") is the same sum with a date bound: `SUM(<signed amount> WHERE date <= target_date)`. It has always been computed on demand; now the current balance is too, by the same rule.

```
expense_bank_accounts
  - id                     UUID, primary key, default uuid_generate_v4()
  - user_id                UUID, NOT NULL, FK → users
  - name                   text, NOT NULL
  - currency_code          text, NOT NULL, default 'PEN', FK → global_currencies.code
                           — immutable after creation
  - is_person              boolean, NOT NULL, default false
                           — true for virtual accounts representing people (debt tracking).
                             ⚠️ Currently unreachable: no endpoint can set it (the create
                             INSERT omits the column and the schemas reject the field).
                             Open product question — see TODO.md.
  - color                  text, NOT NULL, default '#3b82f6'
                           CHECK accounts_color_is_hex: IS NOT NULL AND ~ '^#[0-9a-fA-F]{6}$' (sql/031)
  - is_archived            boolean, NOT NULL, default false
                           — hides from pickers and entry flows but preserves all history.
                             Accounts with transactions can be archived, not hard-deleted.
                             (Categories/hashtags lost their is_archived in sql/024;
                             accounts deliberately kept theirs.)
  - sort_order             integer, NOT NULL, default 0
  - created_at             timestamptz, NOT NULL, default now()
  - updated_at             timestamptz, NOT NULL, default now()
  - version                integer, NOT NULL, default 1
  - deleted_at             timestamptz, nullable
  - UNIQUE partial index (user_id, LOWER(name), currency_code) WHERE deleted_at IS NULL
                           — sql/028 replaced the original table-level
                             UNIQUE (user_id, name, currency_code), which was
                             case-sensitive and spanned soft-deleted rows.
                             Same shape sql/012 gave categories/hashtags,
                             plus the currency_code scope column.
```

---

### expense_categories

Flat category list. No hierarchy. No type restriction — any category can be used on any transaction type. System categories are auto-created and non-deletable, but can be renamed by the user (the engine identifies them by `system_key`, not by display name).

```
expense_categories
  - id          UUID, primary key, default uuid_generate_v4()
  - user_id     UUID, NOT NULL, FK → users
  - name        text, NOT NULL
                — display label. Free to rename, including for system categories.
  - color       text, NOT NULL, default '#6b7280'
                CHECK categories_color_is_hex: IS NOT NULL AND ~ '^#[0-9a-fA-F]{6}$' (sql/031)
  - is_system   boolean, NOT NULL, default false
                — true for system-managed categories (@Opening).
                  Cannot be deleted.
  - system_key  text, nullable
                — immutable discriminator for system categories
                  ('opening_balance' — the only value since the transfer removal
                  deleted 'debt'/'transfer', 2026-08-10). NULL for regular
                  user-created categories. The engine looks up system rows by
                  (user_id, system_key) so display renames are safe — added in
                  sql/010.
  - sort_order  integer, NOT NULL, default 0
  - created_at  timestamptz, NOT NULL, default now()
  - updated_at  timestamptz, NOT NULL, default now()
  - version     integer, NOT NULL, default 1
  - deleted_at  timestamptz, nullable
  - PARTIAL UNIQUE INDEX (user_id, LOWER(name)) WHERE deleted_at IS NULL
                — case-insensitive uniqueness scoped to non-deleted rows (sql/012).
                  Lets a user recreate a name they previously soft-deleted.
  - PARTIAL UNIQUE INDEX (user_id, system_key) WHERE system_key IS NOT NULL AND deleted_at IS NULL
```

`is_archived` was dropped in `sql/024` along with the archive/unarchive routes — after the dashboard's archived panels died in the read-time-currency rework, nothing displayed the state.

**System categories (auto-seeded on first use, `is_system = true`):**
- `@Opening` (`system_key = 'opening_balance'`) — assigned to opening-balance seed transactions created via `POST /accounts/{id}/opening-balance`. Transactions under this category are excluded from flow reports (dashboard month panel + monthly report) but included in balances, and the category row itself is hidden from those panels. The only system category since 2026-08-10 — `@Transfer`/`@Debt` left with the auto-paired transfer feature.

The display name is user-renameable; the engine always resolves system categories by `system_key`.

**Refunds:** Use the same category as the original expense. Tag the refund as an inflow (`transaction_type = 2`). The category accumulates both directions — net spend in that category across the month reflects the true cost.

---

### expense_transaction_inbox

Incomplete transactions waiting to be promoted to the ledger. Fields are nullable — the inbox exists precisely because the user doesn't have all the information yet. The inbox is a draft ledger row: looser about *which fields are null*, never about *how a field encodes its meaning* (`CLAUDE.md`, sign convention).

```
expense_transaction_inbox
  - id            UUID, primary key, default uuid_generate_v4()
  - user_id       UUID, NOT NULL, FK → users
  - title         text, nullable
  - description   text, nullable
  - amount_cents  bigint, nullable             — always positive when set (CHECK)
  - date          timestamptz, nullable
  - account_id    UUID, nullable, FK → expense_bank_accounts
  - category_id   UUID, nullable, FK → expense_categories
  - status        smallint, NOT NULL, default 1 — CHECK (status IN (1, 2)), sql/029
                  — 1=pending (active in inbox, or dismissed if deleted_at is set)
                  — 2=promoted (moved to ledger; row is soft-deleted)
                  — there is NO 3=dismissed value: a dismissed row is
                    status=1 + deleted_at. status records how far the row
                    got; deleted_at records that it left the inbox. (The
                    phantom value 3 stood here undocumented-in-code until
                    the sql/029 audit — nothing ever wrote it.)
  - transaction_type smallint, nullable
                  — 1=outflow, 2=inflow — same enum as expense_transactions, no value 3.
                  — inferred by the engine from the signed request amount_cents.
                  — nullable because a draft may not have an amount yet.
  - created_at    timestamptz, NOT NULL, default now()
  - updated_at    timestamptz, NOT NULL, default now()
  - version       integer, NOT NULL, default 1
  - deleted_at    timestamptz, nullable
```

**CHECK constraints:**
- `inbox_amount_positive` — `amount_cents IS NULL OR amount_cents > 0`
- `inbox_transaction_type_valid` — `transaction_type IS NULL OR transaction_type IN (1, 2)` (`sql/020`; there is no draft type 3)

`exchange_rate` was dropped in `sql/021` (its `DEFAULT 1.0` was bug 1.4 — items promoted at a fabricated rate), `transfer_direction` in `sql/020` (direction folded into `transaction_type`), and the transfer draft columns — `transfer_account_id`, `transfer_amount_cents`, with their `inbox_transfer_amount_positive` and `inbox_transfer_fields_coherent` CHECKs — in `sql/030` (the transfer removal, 2026-08-10).

**Promotion flow:** User-initiated. When `title`, `amount_cents`, `date`, `account_id`, and `category_id` are all present and `date ≤ now()`, the item is eligible. Promoting atomically:
1. Creates a new row in `expense_transactions` with all validated data. `transaction_type` is copied directly from the inbox row.
2. Sets `inbox_id` on the new transaction row to link back to this item.
3. Sets `status = 2` (promoted) on this inbox row.
4. Sets `deleted_at` on this inbox row (soft delete).

There is no balance step (writing the ledger row *is* the balance change — `sql/022`) and no rate lookup (conversion happens at read time — `sql/021`).

**Hashtags:** The inbox has no hashtag support — no `hashtag_ids` field on its schemas, and promotion attaches none, so tags are silently lost by drafting through the inbox. Open product question — see TODO.md.

**Deferred features:** Recurring expenses (`is_recurring`), CSV import (`source_text`), and receipt capture (`receipt_photo_url`) are not in Phase 1.

---

### expense_transactions

Confirmed transactions — the clean, reliable ledger.

**Balance rule:** The engine writes no balance at all. An account's balance is the signed sum of its non-deleted transactions (`sql/022`), so every row below contributes by existing. A future splits feature must exclude parent container rows from that sum in the query predicate itself — the reserved column for it (`parent_transaction_id`) was dropped in `sql/024`, and the predicate a splits migration must reinstate is written out in `sql/022`'s header and `app/helpers/account_balance.py`.

```
expense_transactions
  - id                        UUID, primary key, default uuid_generate_v4()
  - user_id                   UUID, NOT NULL, FK → users
  - title                     text, NOT NULL
  - description               text, nullable
  - amount_cents              bigint, NOT NULL
                              — always positive (CHECK amount_cents > 0). Magnitude only.
                              — direction is transaction_type; no column's sign means anything.
                              — read-only while part of a completed reconciliation.
  - transaction_type          smallint, NOT NULL
                              — 1=outflow (balance decreases), 2=inflow (balance increases)
                              — CHECK (transaction_type IN (1, 2)); on EVERY row.
                                There is no value 3 (sql/020).
  - date                      timestamptz, NOT NULL, default now()
  - account_id                UUID, NOT NULL, FK → expense_bank_accounts
  - category_id               UUID, NOT NULL, FK → expense_categories
  - cleared                   boolean, NOT NULL, default false
                              — true when confirmed on a bank statement; drives reconciliation.
  - inbox_id                  UUID, nullable, FK → expense_transaction_inbox
                              — lineage back to the inbox item this was promoted from
  - reconciliation_id         UUID, nullable, FK → expense_reconciliations
  - created_at                timestamptz, NOT NULL, default now()
  - updated_at                timestamptz, NOT NULL, default now()
  - version                   integer, NOT NULL, default 1
  - deleted_at                timestamptz, nullable
```

Dropped by the deletion program: `transfer_direction` (`sql/020` — direction folded into `transaction_type`), `amount_home_cents` and `exchange_rate` (`sql/021` — conversion moved to read time), `parent_transaction_id` (`sql/024` — reserved for splits, never non-null; a splits migration re-adds it with the balance predicate), `transfer_transaction_id` (`sql/030` — the self-FK that paired transfer legs, removed with the transfer feature, 2026-08-10).

**Indexes (`sql/022`) — load-bearing; the computed balance depends on the first:**

```
idx_expense_transactions_user_account         (user_id, account_id)              WHERE deleted_at IS NULL
idx_expense_transactions_user_date            (user_id, date, created_at)        WHERE deleted_at IS NULL
idx_expense_transactions_user_category        (user_id, category_id)             WHERE deleted_at IS NULL
idx_expense_transactions_user_reconciliation  (user_id, reconciliation_id)       WHERE reconciliation_id IS NOT NULL AND deleted_at IS NULL
```

`sql/022`'s header records the measured plans (50k seeded rows) and the deliberate omissions — no `hashtag_id` index on the junction (the report joins the other direction), no `INCLUDE` columns (they defeat HOT updates). (Its "no index on `transfer_transaction_id`" omission now describes a column `sql/030` dropped.) The seven `(user_id, updated_at)` sync indexes died with `/sync` (`sql/023`).

**Field locking on reconciliation:** When `reconciliation_id` references a completed reconciliation (`status = 2`), these four fields are read-only: `amount_cents`, `account_id`, `title`, `date`. All other fields remain editable. Reverting the reconciliation to draft unlocks them.

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
  - created_at  timestamptz, NOT NULL, default now()
  - updated_at  timestamptz, NOT NULL, default now()
  - version     integer, NOT NULL, default 1
  - deleted_at  timestamptz, nullable
  - PARTIAL UNIQUE INDEX (user_id, LOWER(name)) WHERE deleted_at IS NULL
                — case-insensitive uniqueness scoped to non-deleted rows (sql/012).
                  Mirrors the expense_categories constraint.
```

`is_archived` was dropped in `sql/024` with the archive/unarchive routes.

---

### expense_transaction_hashtags

Junction table linking hashtags to ledger transactions. A transaction with 3 hashtags produces 3 rows here — same `transaction_id`, three different `hashtag_id` values.

```
expense_transaction_hashtags
  - id                  UUID, primary key, default uuid_generate_v4()
  - transaction_id      UUID, NOT NULL
                        — references expense_transactions (no formal FK; see
                          transaction_source)
  - transaction_source  smallint, NOT NULL, CHECK (transaction_source = 1)  [sql/027]
                        — 1 = written by the ledger attach path. This is the only value
                          that has ever been written, and every reader filters on it.
                          The column was designed to let transaction_id reference either
                          the ledger or the inbox; the inbox writer was never built
                          (see the inbox section — tags are lost by drafting there).
                          Owner decision 2026-08-07: inbox hashtags are a wanted future
                          feature, so the column stays and the CHECK pins today's single
                          value — the migration shipping the inbox writer widens it to
                          IN (1, 2). ⚠️ An earlier revision of this document defined
                          1=inbox, 2=ledger; the implementation has always written 1 for
                          ledger rows.
  - hashtag_id          UUID, NOT NULL, FK → expense_hashtags
  - user_id             UUID, NOT NULL, FK → users
  - created_at          timestamptz, NOT NULL, default now()
  - updated_at          timestamptz, NOT NULL, default now()
  - deleted_at          timestamptz, nullable
  - UNIQUE (transaction_id, hashtag_id)

Indexes:
  - idx_expense_transaction_hashtags_tx (transaction_id, transaction_source)
    WHERE deleted_at IS NULL — backs the per-transaction hashtag read-back.
```

`version` was dropped in `sql/024` — versioning lives on the parent. **Parent version-bump rule:** any mutation to a junction row (attach, detach, cascade soft-delete from `DELETE /hashtags/{id}`) bumps `version` and `updated_at` on the parent `expense_transactions` row in the same DB transaction. This keeps the parent's optimistic version honest for the embedded `hashtag_ids` array on the wire — a hashtag-only edit is an edit to the transaction as clients see it.

---

### expense_reconciliations

Batch reconciliation records. Each batch belongs to one account and covers a date range. The user opens a reconciliation, assigns transactions to it, and completes it when the batch matches the bank statement.

```
expense_reconciliations
  - id                        UUID, primary key, default uuid_generate_v4()
  - user_id                   UUID, NOT NULL, FK → users
  - account_id                UUID, NOT NULL, FK → expense_bank_accounts
  - name                      text, NOT NULL
  - date_start                timestamptz, nullable
  - date_end                  timestamptz, nullable
  - status                    smallint, NOT NULL, default 1
                              — 1=draft, 2=completed
                              — CHECK (status IN (1, 2)) — sql/025
  - beginning_balance_cents   bigint, NOT NULL, default 0
                              — always user-entered, never derived. Required on POST
                                (omitting it is a 422; the DB default is vestigial).
  - ending_balance_cents      bigint, NOT NULL, default 0
                              — user-entered from the bank statement. Editable while draft.
  - created_at                timestamptz, NOT NULL, default now()
  - updated_at                timestamptz, NOT NULL, default now()
  - version                   integer, NOT NULL, default 1
  - deleted_at                timestamptz, nullable
```

`sort_order`, `beginning_balance_source`, and the chained-neighbor index were dropped in `sql/025`, deleting reconciliation chaining entirely — no path derives one reconciliation's balances from another's, and no cascade rewrites completed records.

**Ordering:** account-scoped lists sort `date_start ASC NULLS LAST, created_at ASC` — the dates order the list; undated rows sort last. The cross-account list is `created_at DESC`. There is no user-controlled ordering.

**`difference_cents` (read-time, never stored):** every read projects `(ending_balance_cents − beginning_balance_cents) − SUM(signed amount of assigned non-deleted transactions)` in SQL via `home_currency.signed_expr` — the same single sign-matrix rendering balances use. A zero difference means the batch adds up; completing with a non-zero difference is allowed — the figure informs, the user decides. Native currency only.

**Field locking on completion:** When `status = 2`, four fields lock on every assigned transaction (`amount_cents`, `account_id`, `title`, `date`) and four on the reconciliation row itself (`beginning_balance_cents`, `ending_balance_cents`, `date_start`, `date_end`). Reverting to draft unlocks everything; revert restores only the status. Soft-deleting a reconciliation unassigns its transactions; restoring it deliberately does not re-link them.

---

## Deferred Tables (Later Phases)

### expense_budgets *(Phase 3+)*

Monthly per-category budget targets. Deferred to the budgeting phase. No schema defined yet.

### transaction_shares *(Phase 4+)*

Cross-user shared expenses. Deferred to the people and sharing phase. When implemented, follow the Splitwise sharing model (see git history for `docs/lessons-splitwise.md`): separate `paid_share_cents` and `owed_share_cents`, pre-computed balance cache, settlements as standard transactions.

---

## Multi-Currency Model

**Authority: `docs/currency-model-decision.md`.** The schema-level summary:

- Amounts are stored in the account's native currency, always positive cents. **No conversion result is ever stored** (`sql/021`).
- Conversion happens at read time, only on figures the user compares or sums across currencies (report/dashboard aggregates, `current_balance_home_cents`). Individual transactions, inbox items, and reconciliations carry native currency only.
- A conversion is a lookup of the rate for that row's date in the user's `display_timezone`, carried forward from the most recent rate on or before it. No rate → the row converts to `null`, and every home-currency `SUM` is paired with an `unconverted_count` that nulls the total rather than understating it. `app/helpers/home_currency.py` is the single implementation.
- The home currency is locked to `'PEN'` by a CHECK (`sql/018`); currencies are locked to USD/PEN (`sql/015`). Both are labelled scale boundaries, not defects.
- An account-to-account move is two ordinary rows (the paired-transfer machinery was removed by `sql/030`, 2026-08-10); each row converts independently by its own date's rate, so any FX spread between the two lands in whatever user categories the rows carry.

---

## People Model

People are bank accounts with `is_person = true`. There is no separate people table. If someone shares expenses in multiple currencies, they have multiple accounts — one per currency — both shown in the People sidebar section.

> ⚠️ **Structurally complete, functionally unreachable.** The engine reads `is_person` (accounts list filter, dashboard split, the opening-balance guard) but no endpoint can set it, so no row can currently be a person. Decided 2026-08-10: the feature stays and `POST /people` ships as its own work item — see TODO.md. The model below is the design the machinery implements.

**Debt tracking model:** A person account's balance represents the financial position with that person. Positive balance = they owe you money. Negative balance = you owe them money. All rows are ordinary transactions with ordinary user categories — the `@Debt` auto-assignment left with the transfer feature (`sql/030`, 2026-08-10).

**Full debt cycle example (you pay $100 lunch, split $50 with John):**

| Step | Transaction | Account | Category | Balance effect |
|---|---|---|---|---|
| 1. Pay lunch | $100 outflow | Checking | Food | Checking −100 |
| 2. Register John's share | $50 inflow | John (person) | user's choice (e.g. "Loans") | John +50 (he owes you) |
| 3. John pays you back | $50 outflow | John (person) | user's choice | John −50 = 0 |
| 4. Receive John's payment | $50 inflow | Checking | Food | Checking +50 |

End state: Checking −50 (your true out of pocket), Food −50 (your true food spend), John 0 (debt cleared).

---

## Split Transactions *(deferred — no schema support today)*

The reserved column (`parent_transaction_id`, always null) was dropped in `sql/024`; with no column, a naive balance `SUM` is correct by construction. The documented rule — a parent row is a display container that does not move the balance; only its children do — survives as the predicate a splits migration must add to the balance sum, written out in `sql/022`'s header and `app/helpers/account_balance.py`. Splits must be created atomically (parent + all children in one call) when the feature ships.

---

## Recurrence *(Phase 5 — Fully Deferred)*

Recurring expenses are not part of Phase 1 through 4. No recurring-related columns exist in the current schema. Full recurrence architecture (patterns, anchor modes, generation logic) will be designed and added as a schema migration when Phase 5 begins.
