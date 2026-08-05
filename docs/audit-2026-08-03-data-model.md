> ## ⚠️ Superseded — 2026-08-04
>
> This document is kept as the **pre-cut census**: its Part 1 (table-by-table column
> inventory, with readers and writers traced by grep) is still accurate and still useful
> reference. **Its Part 3 and Part 4 are stale** — the deletion candidates it ranks have
> since been decided, and it references `docs/currency-rework/`, a directory that no
> longer exists.
>
> For what was decided and what to do about it, go to
> [`rework/README.md`](rework/README.md). Do not plan work from this file.
>
> Known inaccuracies corrected on 2026-08-04, listed so they are not re-inherited:
> `expense_transaction_hashtags.version` **is** written (`helpers/transactions.py`, the
> re-tag upsert) though never read; categories and hashtags expose **8** routes each, not
> 7; the `COALESCE(amount_home_cents, amount_cents)` expression is live in **12** places,
> not 8, and is **latent rather than firing** because `lookup_exchange_rate` raises
> `RATE_UNAVAILABLE` instead of defaulting the rate to 1.0.

# Data model & feature audit — 2026-08-03

**Purpose: input to the feature-deletion program.** This is a census, not a bug list.
For defects see [`open-bugs.md`](open-bugs.md); for the currency work in flight see
[`currency-rework/`](currency-rework/README.md). Where this audit touches something
already decided, it says so and does not relitigate.

**Method.** Read every migration in `sql/`, every module in `app/`, and traced each
column to its readers and writers by grep. Every "unused" claim below was verified by
searching `app/` for the column name and inspecting the hits — not inferred.

---

## Ground truth, verified today

| Fact | Value |
|---|---|
| Tests | **213 passed** in 1.47s (`pytest`, no flags) |
| Tables | **15** (7 infrastructure, 7 domain, 1 auth) |
| Routes | **61** (60 authenticated + `/health`) |
| Migrations | **19** |
| App code | ~7,700 lines across 60 files; tests ~11,200 |
| **Rows in every domain table** | **0** |

The last row is the headline. `expense_transactions`, `expense_transaction_inbox`,
`expense_bank_accounts`, `expense_categories`, `expense_hashtags`,
`expense_reconciliations`, `expense_transaction_hashtags` and `activity_log` are all
**exactly empty** (counted, not estimated). Only `exchange_rates` (884 rows), `users`
(1), `user_settings` (1), `global_currencies` (2) and `personal_access_tokens` (2)
hold anything.

**Therefore every deletion in this document costs a migration statement and zero data
migration.** There is no backfill to write, no freeze-the-computed-value step, no
window where old and new shapes coexist. This is the cheapest this program will ever
be, and the cost rises the day the first real transaction lands.

---

# Part 1 — The data model, table by table

Legend for column status:

- 🟢 **Load-bearing** — engine logic reads it and behaves differently based on its value.
- 🔵 **Carried** — written and returned honestly, but no engine logic branches on it (display data, audit metadata). Legitimate; not automatically a deletion candidate.
- 🟡 **Echo-only** — the engine stores it and hands it back unchanged. It is client state living in the engine's database.
- 🔴 **Dead** — never written, never read, or written and never read by anything.

---

## Infrastructure tables (`sql/002`)

### `users` — 1 row

Identity anchor. Every other table's `user_id` FKs here. Written by `POST /auth/bootstrap`
(upsert) and `PUT /auth/profile`.

| Column | Status | Who uses it |
|---|---|---|
| `id` | 🟢 | Tenancy key. FK target from 13 tables; the `user_id` predicate in every query is the *only* isolation guard (RLS is inert). |
| `display_name` | 🔵 | Written at bootstrap, mutable via `PUT /auth/profile`, returned. No logic reads it. |
| `email` | 🔴 **newly dead** | See finding **A** below — always written `NULL` since the JWT deletion. |
| `last_login_at` | 🔵 | Bumped on every `/auth/bootstrap`. Deliberately not activity-logged (`helpers/auth.py:59-67`). Nothing reads it. |
| `created_at` / `updated_at` | 🟢 | `updated_at` drives nothing here (users is not a synced table), `created_at` is provenance. |

> **Finding A — `users.email` is now permanently `NULL`.**
> `deps.py:71` returns `AuthUser(id=..., email=None)` unconditionally under PAT-only
> auth, and `bootstrap` inserts `auth_user.email` verbatim. The column was populated
> from the JWT claim; that claim source was deleted on 2026-08-03 (audit 2.1) and
> nothing replaced it. The field is still on the `UserResponse` wire shape. Either
> accept `email` on `BootstrapRequest`/`ProfileUpdateRequest`, or drop the column and
> the response field. Not a defect — nothing reads it — but it is a lie by omission on
> the wire.

### `user_settings` — 1 row

One row per user, synced to clients as a singleton. Written by bootstrap and
`PUT /auth/settings`.

| Column | Status | Who uses it |
|---|---|---|
| `display_timezone` | 🟢 | `compute_month_bounds` — decides which UTC instants a calendar month covers. The single genuinely load-bearing preference. |
| `main_currency` | 🟢 | Read at ~10 sites for conversion. **But locked to `'PEN'` by a CHECK (`sql/018`)** and `PUT /auth/settings` 422s any attempt to change it. It is a one-value column kept as a chokepoint rather than a scattered literal — a deliberate call recorded in `sql/018`'s header. `currency-rework` D-i deletes most of its callers. |
| `theme` | 🟡 | Stored, returned. Zero engine reads. |
| `start_of_week` | 🟡 | Stored, returned. Zero engine reads. Note: **nothing in the engine computes a week** — no weekly report exists. |
| `transaction_sort_preference` | 🟡 | Stored, returned. Zero engine reads. `GET /transactions` ordering is driven by its own `sort`/`order` query params, not by this. |
| `sidebar_show_bank_accounts` | 🟡 | Stored, returned. Pure client chrome. |
| `sidebar_show_people` | 🟡 | Same. Also describes a sidebar section that can never be populated — see `is_person` below. |
| `sidebar_show_categories` | 🟡 | Same. |
| `version` | 🟢 | Optimistic-concurrency / sync marker. |
| `deleted_at` | 🔴 | Added by `sql/009` "to satisfy the every-mutable-table-has-`deleted_at` convention". A settings row is never soft-deleted; no code path sets it, and `_fetch_user_settings` does not filter on it. |
| `created_at` / `updated_at` | 🟢 | `updated_at` gates the sync delta. |

> **Finding B — six of the nine settings columns are client state in the engine's database.**
> `theme`, `start_of_week`, `transaction_sort_preference` and the three `sidebar_show_*`
> flags are written by the client, stored, and handed back. The engine never branches on
> any of them. Their entire value is cross-device preference propagation via `/sync` —
> which, at one user with one active client (the CLI), currently propagates nothing to
> nobody. They are not *wrong*; they are a feature ("your preferences follow you") whose
> beneficiary does not exist yet.

### `global_currencies` — 2 rows

Static lookup, FK target for four currency columns. `CHECK (code IN ('USD','PEN'))`
since `sql/015` — the single chokepoint that makes adding a third currency an explicit,
reviewable migration.

| Column | Status | Who uses it |
|---|---|---|
| `code` | 🟢 | FK target from `expense_bank_accounts.currency_code`, `user_settings.main_currency`, `exchange_rates.base_currency`/`target_currency`. Validated on account create (`helpers/accounts.py:75`). |
| `name` | 🔴 | Zero reads in `app/`. |
| `symbol` | 🔴 | Zero reads in `app/`. Clients hardcode `S/` and `$`. |

### `exchange_rates` — 884 rows

The only genuinely populated domain data in the system. Append-only, one row per
(pair, date). Written by `app/jobs/fetch_exchange_rates.py` (daily launchd) and
`app/jobs/backfill_exchange_rates.py` (manual, ran 2024-03-02 → 2026-07-31).

All six columns are load-bearing. Rows are stored canonically USD-based; `get_rate`
handles `X→USD` by inversion and returns `None` for non-USD↔non-USD (unreachable under
the two-currency lock).

The critical read semantic: `rate_date <= $1 ORDER BY rate_date DESC LIMIT 1` — the
most recent rate on or before the requested date is carried forward. So coverage
density buys accuracy, not availability; one row before your earliest transaction is
the hard requirement.

### `sync_checkpoints` — 0 rows

One row per `(user_id, client_id)`. Stores the opaque token last issued and the
snapshot timestamp it represents. Every column is used by `helpers/sync.py`.

Zero rows means **no client has ever completed a sync against this database.** The CLI
uses the direct REST endpoints, not `/sync`. See Part 3.

### `idempotency_keys` — 0 rows

24-hour dedup for every `POST`/`PUT`/`DELETE`. `_claim` inserts a placeholder, the work
runs, `_store` writes the response envelope; a replay within TTL returns the stored body
**and status code** verbatim (`response_status`, added by `sql/011`, so replay can't
silently downgrade 201→200).

| Column | Status | Note |
|---|---|---|
| `key`, `user_id`, `response_snapshot`, `response_status`, `expires_at` | 🟢 | |
| `processed_at` | 🔴 | Inserted, never read. `expires_at` is the only temporal guard. |
| `id`, `created_at` | 🔵 | |

Two known defects live here (open-bugs **4.1** expired-key duplication, **2.4** PAT
plaintext sitting in `response_snapshot` for 24h). Both are already tracked.

### `activity_log` — 0 rows

Immutable audit trail; one row per mutation across every mutable table, with full
before/after JSON snapshots. Read by `GET /activity` (filterable by `resource_type` /
`resource_id`).

| Column | Status | Note |
|---|---|---|
| `resource_type`, `resource_id`, `action`, `before_snapshot`, `after_snapshot`, `user_id` | 🟢 | |
| `changed_by` | 🔵 | Always equals `user_id` today. |
| `actor_type` | 🔴 in practice | Added by `sql/013` to distinguish `user` / `system` / `admin`. **No caller ever passes a non-default value** — verified: every `write_activity_log` call takes the `"user"` default. The column encodes a multi-actor future that does not exist at one user with no worker. |

> The activity log is the single most expensive convention in the codebase — every write
> path carries snapshot construction — and it currently holds zero rows. It earns its
> keep the first time a balance looks wrong. Keep it. But note that its `actor_type`
> discrimination and the deliberate `last_login_at` exception are both solving problems
> from the retired multi-user era.

---

## Auth table (`sql/016`)

### `personal_access_tokens` — 2 rows

The only authentication mechanism. Opaque `ewe_pat_…` secret, SHA-256 hashed, looked up
on every single request (`deps.py`). Revocation is `revoked_at IS NOT NULL`; the active
lookup index filters it out.

| Column | Status | Note |
|---|---|---|
| `token_hash` | 🟢 | The authentication lookup. |
| `user_id` | 🟢 | What the token resolves to. |
| `revoked_at` | 🟢 | Soft-delete under a domain-accurate name. |
| `token_prefix` | 🔵 | Display + leak-scanner discoverability. **Returned only in the create response** — there is no list endpoint, so after the create call returns, this value is unreadable through the API forever. |
| `name` | 🔵 | Same: write-once, then unreadable. |
| `created_at` | 🔵 | |

Deliberate omissions recorded in `sql/016` and `CLAUDE.md`: no `last_used_at` (avoids a
write per request), no list endpoint. Both are correct calls; the consequence is that
`token_prefix` and `name` are inert storage until a management UI ships.

**Note the bootstrap paradox:** every route requires a PAT, and `POST /auth/pat`
requires a PAT. The first token is minted by direct SQL insert
(`deploy/local/000_auth_standin.sql`). Documented, works, worth knowing.

---

## Domain tables (`sql/003` + later)

### `expense_bank_accounts` — 0 rows

Accounts *and* people, discriminated by `is_person`. Balance is a stored running total
updated atomically with every transaction write (`helpers/balance.py`).

| Column | Status | Who uses it |
|---|---|---|
| `name` | 🟢 | Unique per `(user_id, name, currency_code)`. |
| `currency_code` | 🟢 | **Governs the currency of every transaction on the account** — there is no per-transaction currency column. Immutable after create. |
| `current_balance_cents` | 🟢 | Mutated in the same DB transaction as every ledger write. |
| `is_archived` | 🟢 | Hidden from default lists, still participates in history. |
| `sort_order` | 🟢 | User ordering, ASC. |
| `is_person` | 🟢 **read, unwritable** | Filters the accounts list (`include_people`) and splits the dashboard into `bank_accounts` / `people`. **No endpoint can set it true.** `AccountCreateRequest` is `extra="forbid"` and comments say person accounts come from "the dedicated People API" — which does not exist. |
| `color` | 🔵 | Display. |
| `version`, `deleted_at`, `created_at`, `updated_at` | 🟢 | |

> **Finding C — the "people" feature is structurally present and functionally unreachable.**
> Confirmed already as decision **D7** ("parked feature gap, not a defect"). Recording the
> full blast radius, because it is wider than a missing router:
> - `is_person` column + every query that filters on it
> - `include_people` query param on `GET /accounts`
> - the entire `people` panel on `GET /dashboard` (always `[]`)
> - `user_settings.sidebar_show_people` (chrome for an empty section)
> - the `@Debt` system category and its `SystemCategoryKey.DEBT` constant
> - the person-leg branch in `create_transfer_pair` — a loan to a person is the *only*
>   thing that makes `@Debt` appear, and the only legitimate cause of a non-zero
>   `@Transfer` besides FX spread
> - `create_opening_balance`'s explicit "person accounts cannot carry an opening balance" guard
>
> Either build `POST /people` (memory records this as the intent) or delete the axis. The
> current state is the expensive one: full machinery, no entry point.

### `expense_categories` — 0 rows

| Column | Status | Who uses it |
|---|---|---|
| `name` | 🟢 | Case-insensitively unique among live rows (`sql/012`). Freely renameable, including system rows. |
| `system_key` | 🟢 | Immutable discriminator (`debt` / `transfer` / `opening_balance`). The engine resolves system categories by this, never by name — which is why renaming `@Transfer` doesn't fragment history. `opening_balance` drives the monthly-report exclusion. |
| `is_system` | 🟢 | Blocks user delete/update of system rows; sorts them first in the list. |
| `is_archived` | 🟢 → 🔴 pending | Only consumer is the `/dashboard` `archived_categories` panel, which **D-g deletes**. After CR2 nothing reads it: `compute_month_flow` deliberately has no `is_archived` filter (archived categories must stay in reports so visible rows sum to the total). |
| `sort_order`, `color`, `version`, `deleted_at`, timestamps | 🟢/🔵 | |

### `expense_hashtags` — 0 rows

Same shape as categories, minus system rows and color. `is_archived` has the same
fate — its only reader is the `/dashboard` panel D-g deletes.

### `expense_transaction_hashtags` — 0 rows

Junction. Many-to-many between transactions and hashtags.

| Column | Status | Who uses it |
|---|---|---|
| `transaction_id`, `hashtag_id`, `user_id` | 🟢 | |
| `deleted_at` | 🟢 | Soft-delete; filtered out of every aggregation. |
| `transaction_source` | 🔴 | **Only the value `1` is ever written** (`helpers/transactions.py:209` — the sole INSERT). Every read filters `= 1`. It exists to distinguish ledger rows from inbox rows in a shared junction — but the inbox has no hashtag support at all: `InboxCreateRequest` has no `hashtag_ids` field, and promote attaches none. This is a two-source design where the second source was never built. |
| `version` | 🔴 | Junction rows are inserted or soft-deleted, never updated. Nothing increments it. |
| `id`, `created_at`, `updated_at` | 🔵 | |

> Consequence worth flagging: **hashtags do not survive the inbox.** You cannot tag a
> draft, and promoting produces a transaction with zero hashtags. Whether that is a gap
> or a deliberate simplification is a product call — but the schema was built for the
> other answer.

### `expense_transaction_inbox` — 0 rows

A draft ledger row: same encoding as `expense_transactions`, looser about which fields
are required. `sql/019` (yesterday) finished the encoding parity that `sql/008` left
half-done, and added the fail-closed CHECK making a half-transfer row unrepresentable.

| Column | Status | Who uses it |
|---|---|---|
| `title`, `description`, `amount_cents`, `date`, `account_id`, `category_id` | 🟢 | All nullable — that's the point of a draft. Promotion validates the full set. |
| `transaction_type` | 🟢 | Inferred from request sign. `CHECK IN (1,2,3)`. |
| `transfer_account_id`, `transfer_amount_cents`, `transfer_direction` | 🟢 | All-or-nothing triple, forced to `transaction_type = 3` by CHECK. `transfer_direction` describes the **primary** leg. |
| `status` | 🟢 | 1=pending / 2=promoted. Promoted rows are excluded from `GET /inbox` (hardcoded `i.status = 1`). |
| `exchange_rate` | 🔴 pending | Default `1.0`, the direct cause of open bug **1.4** ($100 worth 100 PEN cents). CR3 deletes it. |
| `version`, `deleted_at`, timestamps | 🟢 | |

### `expense_transactions` — 0 rows

The ledger. 21 columns.

| Column | Status | Who uses it |
|---|---|---|
| `amount_cents` | 🟢 | **Always stored positive.** Sign lives in `transaction_type` + `transfer_direction`, nowhere else. |
| `transaction_type` | 🟢 | 1=expense, 2=income, 3=transfer. Inferred from request sign — callers never set it. **No CHECK constraint** (open bug 6.3). |
| `transfer_direction` | 🟢 | 1=debit, 2=credit. Nullable, with nothing tying it to `type = 3` — the gap D-l calls out: a directionless transfer leg moves a balance and is invisible in every report. |
| `account_id` | 🟢 | Determines the row's currency. |
| `category_id` | 🟢 | `NOT NULL` — every transaction has one, transfers get `@Transfer`/`@Debt` auto-assigned. |
| `date` | 🟢 | Drives report bucketing and (today) the rate lookup. |
| `cleared` | 🟢 | Settable on create/update, filterable on list. Reconciliation-adjacent but independent of `reconciliation_id`. |
| `reconciliation_id` | 🟢 | Assignment to a reconciliation. |
| `transfer_transaction_id` | 🟢 | Self-FK pairing the two legs. |
| `inbox_id` | 🟢 | Promotion provenance. (Open bug 10.2: the transfer *sibling* doesn't get one.) |
| `amount_home_cents` | 🔴 pending | Stored derived value; the root cause of bugs 1.3/1.4/1.5. CR3 drops it. |
| `exchange_rate` | 🔴 pending | Same. `currency-model-decision.md` argues at length that it never held a fact — for cross-currency transfers it is literally `sibling.amount_cents ÷ primary.amount_cents`. CR3 drops it. |
| `parent_transaction_id` | 🔴 | **Never written by any code path.** Self-FK for the unbuilt split-transaction feature. On the wire in every response as a permanent `null`. Decision **D8** parks it. |
| `version`, `deleted_at`, timestamps | 🟢 | |

### `expense_reconciliations` — 0 rows

Statement-matching: name a period, record beginning and ending balance, assign
transactions, complete or revert. Largest helper in the codebase (1,066 lines) and by
some distance the most complex feature.

| Column | Status | Who uses it |
|---|---|---|
| `account_id`, `name`, `beginning_balance_cents`, `ending_balance_cents` | 🟢 | |
| `status` | 🟢 | 1=draft / 2=completed. Completion locks fields. |
| `sort_order` | 🟢 | Per-account ordering; also the chain order. Mutable only via `PUT /accounts/{id}/reconciliations/order`. |
| `beginning_balance_source` | 🔴 pending | 1=manual / 2=chained. **D3 deletes it** along with the whole cascade. |
| `date_start` | 🔵 | Stored, echoed, editable, nullable. **Never used to select transactions** — assignment is by explicit `reconciliation_id`, not by date range. A pure label. |
| `date_end` | 🔵 → 🔴 pending | Same, except it currently supplies the as-of date for `resolve_home_rates`. **D-i deletes that use**, after which `date_end` is also a pure label. |
| `version`, `deleted_at`, timestamps | 🟢 | |

> The chaining machinery (`_cascade_chained_recalc` + 5 call sites, ~90 lines, plus
> `_previous_chained_neighbor`, `_shift_sort_orders_at_or_above`, `_serialize_with_neighbor`)
> is the single largest block of scheduled-for-deletion code in the repo. Already
> decided (**D3**, `TODO.md`). Note its stated reason: the cascade has **no status
> predicate**, so editing an upstream draft silently rewrites the beginning balance of a
> `COMPLETED` reconciliation — doing through the back door exactly what the field lock
> refuses at the front.

---

# Part 2 — Features & workflows

Eleven feature areas across 61 routes.

| # | Feature | Routes | How it works | Status |
|---|---|---|---|---|
| 1 | **Auth (PAT)** | 4 | Opaque `ewe_pat_…`, SHA-256 hashed, looked up per request. Create returns plaintext once; revoke is `revoked_at`. No list endpoint. | ✅ Live, sole auth path |
| 2 | **Identity & settings** | 4 | `/auth/bootstrap` upserts `users` + `user_settings`; `/auth/me` reads; `PUT /auth/settings` and `PUT /auth/profile` mutate. `main_currency` 422s. | ✅ Live, mostly echo storage |
| 3 | **Accounts** | 10 | CRUD + soft-delete/restore + archive/unarchive + opening balance + reconciliation reorder. Balance is a stored running total. | ✅ Live |
| 4 | **Categories** | 7 | CRUD + delete/restore + archive/unarchive. System rows (`@Transfer`/`@Debt`/`@Opening`) are auto-seeded on demand, identified by `system_key`, protected from user mutation. | ✅ Live |
| 5 | **Hashtags** | 7 | Same lifecycle as categories, minus system rows. Attached to transactions via the junction, flattened to `hashtag_ids[]` on the wire. | ✅ Live |
| 6 | **Inbox** | 7 | Draft rows with everything nullable. `?ready=true` lists promotable items; `POST /promote` converts a draft to a real ledger row (or transfer pair) and marks `status=2`. | ✅ Live |
| 7 | **Transactions** | 7 | Create/update/delete/restore/list/get + `POST /batch` (all-or-nothing). Signed request in, positive storage, positive response, `?debit_as_negative` for display. | ✅ Live |
| 8 | **Transfers** | (via #7) | Not a separate route — a `transfer` field on a transaction request. Creates a paired row with inverse `transfer_direction`, auto-assigns `@Transfer` (both real accounts) or `@Debt` (person leg). Zero-sum validated. | ⚠️ USD→USD 500s (bug 1.3) |
| 9 | **Reconciliations** | 8+1 | Draft → assign transactions → complete → optionally revert. Chained beginning balances cascade downstream. Bulk reorder. | ⚠️ Chaining scheduled for deletion (D3) |
| 10 | **Reports & dashboard** | 2 | `compute_month_flow` is the single source of truth for both. Signed per-row amounts; categories sum them; totals split into inflow/outflow/net. `@Opening` excluded, transfers included (legs cancel in net). | ⚠️ Being rewritten (CR2) |
| 11 | **Sync** | 1 | `GET /sync?sync_token=*|<uuid>` + `X-Client-Id`. Whole-delta, no cursor, `REPEATABLE READ`, rotating opaque token. | ⚠️ Can drop writes (bug 3.1); **0 checkpoints ever created** |
| 12 | **Exchange rates** | 2 | Read-only lookup + history. Populated by two offline jobs. Carry-forward semantics. | ✅ Live, real data |
| 13 | **Activity log** | 1 | Read-only feed over the audit trail. | ✅ Live, 0 rows |

### Cross-cutting mechanisms

Every mutation route passes through the same four:

1. **Idempotency** (`run_idempotent`) — claims the key, runs the work in one transaction, stores body + status.
2. **Activity log** — before/after snapshots, no exceptions except `last_login_at` and balance writes (**D4**).
3. **Soft delete** — `deleted_at` everywhere; restore endpoints on all six mutable resources.
4. **Balance atomicity** — `apply_balance` / `reverse_balance` in the same DB transaction as the ledger write.

### Two notable workflow observations

**The report's hashtag breakdown groups by *combination*, not by hashtag.**
`monthly_report.py` groups on `(category_id, sorted hashtag_id array)`, so a transaction
tagged `#food #work` lands in one row keyed by both, not in a `#food` row and a `#work`
row. This is what makes breakdown rows sum exactly to their parent category — and it is
also why the D-f correction was needed (nothing double-counts). Worth knowing before
anyone "fixes" it into a per-hashtag view: that change breaks the summation invariant.

**Native cross-currency aggregates are already known-meaningless.** `spent_cents` sums
`amount_cents` across accounts with no currency partition, so a category spanning both
currencies yields `$15 + S/25 = 4000` — a number in no currency. D-h deletes these
fields rather than nulling them. Flagged here only because the fields are still live on
the wire today.

---

# Part 3 — Deletion candidates, ranked

Ordered by (value removed) ÷ (cost to remove). Everything already decided is marked as
such and included only for completeness of the picture.

## Tier 0 — already decided, just not executed

| Item | Size | Decision |
|---|---|---|
| Reconciliation chaining (`_cascade_chained_recalc` + 5 call sites + `beginning_balance_source`) | ~90 lines + column + spec §588-607, §620, §648, §650, §671-681 | **D3**, `TODO.md` |
| `expense_transactions.amount_home_cents` + `.exchange_rate` | 2 columns + every write path's rate lookup | **CR3** |
| `expense_transaction_inbox.exchange_rate` | 1 column | **CR3** |
| `/dashboard` `archived_categories` + `archived_hashtags` panels | 2 query builders (~70 lines) + `_SIGNED_HOME_CENTS_SQL` | **D-g** |
| `current_balance_home_cents`, reconciliation `*_home_cents` | 3 response fields + `resolve_home_rates` + `get_home_balance` + `batch_get_rates` | **D-i** |
| Native cross-account aggregates (`spent_cents`, `inflow_/outflow_/net_cents`) | response fields | **D-h** |
| Per-record PEN (`amount_home_cents` on transaction + inbox responses) | response fields | **D-e** |

**Execute the currency rework before adding anything to it.** CR1 has shipped
(`helpers/home_currency.py`, 284 lines) and is **wired to nothing** — verified: no module
in `app/` imports it. Meanwhile the code it replaces is still live and still doing the
exact thing its docstring forbids: `dashboard.py:114-117` and `monthly_report.py:126-135`
both use `COALESCE(t.amount_home_cents, t.amount_cents)`, which reads USD cents as PEN
cents — a 3.58× understatement rendered without complaint. An unwired replacement plus a
live defect is the worst of both states; it should not sit here long.

## Tier 1 — free deletions, no decision needed

Nothing reads these. Zero rows anywhere. Each is a one-line migration.

| Item | Why it's free |
|---|---|
| `global_currencies.name`, `.symbol` | Zero reads in `app/`. Clients hardcode the symbols. |
| `user_settings.deleted_at` | Added for convention conformance; a settings row is never deleted, no code sets or filters it. |
| `idempotency_keys.processed_at` | Written, never read. |
| `expense_transaction_hashtags.version` | Junction rows are never updated. |
| `users.email` | See Finding A — permanently `NULL` since the JWT deletion. Drop it, or start populating it. Do one. |
| `transactions.fetch_hashtag_ids_map` | Orphan function — zero references in `app/` **and** zero in `tests/`. |

## Tier 2 — real feature decisions

These delete something a user might conceivably want. Each needs a yes/no, not just a migration.

### 2a. `expense_transaction_hashtags.transaction_source` → **delete the column**

Only the value `1` is ever written; every read filters `= 1`. The second source (inbox
hashtags) does not exist and nothing in the current design proposes it. Deleting removes
a filter from 6 queries and a column from the junction.

Counter-case: if you *do* want to tag drafts, this column is the design already half-built.
That is the real question — **should the inbox support hashtags?** Answer that first; the
column follows.

### 2b. The six echo-only settings columns → **delete unless a second client is imminent**

`theme`, `start_of_week`, `transaction_sort_preference`, `sidebar_show_bank_accounts`,
`sidebar_show_people`, `sidebar_show_categories`. Six columns, six request fields, six
response fields, and one `PUT` endpoint whose only remaining load-bearing field is
`display_timezone`. Their sole purpose is propagating preferences across clients; there is
one client. `start_of_week` is the clearest cut — no code in the engine computes a week.

Keeping them is defensible (they're cheap, and the iOS app was the intended consumer).
Deleting them shrinks `PUT /auth/settings` to a single meaningful field.

### 2c. The `is_person` axis → **build `POST /people` or delete it**

Full inventory in Finding C. The status quo — complete machinery, no entry point — is
strictly the most expensive of the three options. Memory records the intent as "add
`POST /people`". If that is still true this is a build, not a delete; if it isn't, the
deletion reclaims a column, a query param, a dashboard panel, a settings flag, the
`@Debt` system category, and a branch of the transfer engine.

### 2d. `parent_transaction_id` → **close it or keep it, but stop deferring**

**D8** parks it and the docs are honest about it (spec §432 says "reserved, always null"),
so there's no drift to fix. But it has now survived two audits as a "reviewed, parked"
item. If splits are never shipping, one migration and one response-field removal closes
it permanently. `TODO.md` already frames this exact fork.

### 2e. `/sync` → **the largest open question in this audit**

`sync_checkpoints` has **zero rows**: no client has ever completed a sync. The CLI uses
the direct REST endpoints. The endpoint carries real weight — `helpers/sync.py` (221
lines), `routers/sync.py` (103), `tests/test_sync.py` (335), a `REPEATABLE READ`
transaction wrapper, token rotation, per-table delta reads, tombstone semantics, an
`(user_id, updated_at)` index on six tables, and the `version`/`updated_at` discipline on
every mutable table — plus open bug **3.1**, which is a *dropped-writes* bug and therefore
🔴 severity work.

It exists to serve an offline-capable iOS app. That app does not exist. Ask plainly
whether it is going to, because the answer decides whether 3.1 is urgent or moot.

**Note the coupling:** deleting `/sync` does *not* free `version` or `updated_at` — both
are load-bearing for optimistic concurrency and ordinary auditing. It frees the six
composite indexes, the delta machinery, and the checkpoint table.

## Tier 3 — do not delete

Recorded so a future pass doesn't relitigate them:

- **`activity_log`** — 0 rows and expensive on every write path. It is the answer to "why does my balance look wrong?", and it earns its cost the first time that question is asked. `actor_type` specifically is dead weight, but the table is not.
- **`main_currency`** — one legal value, but deliberately kept as the single chokepoint rather than a `'PEN'` literal scattered across ~10 sites. `sql/018` records this reasoning.
- **`SIGNED_CENTS_EXPR`** — loses its last aggregate caller under D-h but becomes the basis of `UNCLASSIFIED_FLAG_EXPR`. The currency-rework README explicitly warns against garbage-collecting it.
- **`is_archived` on accounts** — an archived account still holds real money (unlike an archived category, which holds only history). That asymmetry is why D-g keeps `archived_accounts` and drops the other two panels.

---

# Part 4 — Findings not already tracked

Five things this pass surfaced that are not in `open-bugs.md`.

| # | Finding | Severity |
|---|---|---|
| **A** | `users.email` is written `NULL` unconditionally since the JWT branch was deleted — `deps.py` has no email source and `bootstrap` stores the `None` verbatim. Still advertised on the wire. | ⚪ cosmetic, but it's an untrue field |
| **B** | `activity_log.actor_type` has no non-default caller anywhere. Every row will read `'user'` forever. | ⚪ |
| **C** | Hashtags cannot be attached to inbox items at all — no `hashtag_ids` on the inbox schemas, none attached at promote — despite the junction table being explicitly designed for two sources. Tags are lost by using the inbox. | 🟡 product gap |
| **D** | `helpers/home_currency.py` (CR1, 284 lines) is imported by **nothing** in `app/`, while the `COALESCE(amount_home_cents, amount_cents)` expression it was written to replace is still live in both `dashboard.py` and `monthly_report.py`. | 🟠 the fix exists and is not connected |
| **E** | `reconciliations.resolve_home_rates` selects accounts by ID with **no `user_id` filter** (`helpers/reconciliations.py:60`). Already noted as 2.3 and slated to close by deletion under D-i — recording that it is reachable from `/sync` as well as `/reconciliations`, so it is two entry points, not one. | 🟡 (tracked, scope corrected) |

---

## The one-paragraph summary

The engine is feature-complete, green (213 tests), well-documented, and **holds no
data**. Its design problems are all the same shape: *machinery built for a world with
more than one user and more than one client*. Six settings columns propagate preferences
to nobody; `/sync` serves an app that doesn't exist; `actor_type` distinguishes actors
that are all the same person; `transaction_source` discriminates a second source that was
never built; `is_person` models a relationship no endpoint can create. None of it is
wrong code — it is correct code for a retired target. The 2026-08-01 single-user pivot
has been applied to the *documentation* thoroughly and to the *schema* not at all, and
the empty ledger means today it costs one migration per column to close that gap.
