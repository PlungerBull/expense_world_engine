# The deletion program — start here

**Transient. Created 2026-08-04. Delete this directory when WP7 lands.**

Seven work packages, one per agent, executing the audit of 2026-08-04. Each is sized to
be done in a single fresh session without running out of context. Read this file first,
then read only your work package.

---

## Why this exists

The engine was designed for 1000+ public users and multiple client apps. On 2026-08-01
that target was retired: **one user, one machine, one client** (see `CLAUDE.md`, "Who
this is for"). The documentation absorbed that pivot thoroughly. The schema did not.

The 2026-08-04 audit traced every column to its readers and writers and found that
**22 of 157 columns were dead or pure client echo** — machinery serving a world with
more than one user and more than one client. Six settings columns propagate preferences
to nobody. `/sync` serves an app that doesn't exist. `actor_type` distinguishes actors
who are all the same person. None of it is wrong code; it is correct code for a retired
target.

Separately, three pieces of *money* logic were found to be storing derived values that
can drift from their inputs — an account balance, a currency conversion, and a direction
encoded across two columns instead of one.

## The one fact that makes all of this cheap

**Every domain table holds exactly zero rows.** Counted, not estimated, on 2026-08-04:

| Table | Rows |
|---|---|
| `exchange_rates` | 884 |
| `global_currencies` | 2 |
| `personal_access_tokens` | 2 |
| `users`, `user_settings` | 1 each |
| **everything else — 10 tables including the entire ledger** | **0** |

No transaction, no account, no category, no inbox draft, no reconciliation and no
activity-log row has ever existed in this database.

**Therefore every change in this program costs a migration statement and zero data
migration.** There is no backfill to write, no dual-shape window, no freeze step. Write
migrations that would be correct if they ran against real data — but know that here they
will not. The cost of all of this rises sharply the day the first real transaction lands,
which is why **WP1 has a deadline and the rest do not**.

---

## Order and dependencies

```
WP1  transfer collapse          ← do first; the only one with a deadline
 │
 ├─→ WP2  read-time currency    ← needs WP1's collapsed sign matrix
 │
WP3  computed balances + indexes
 │
 └─→ WP4  delete /sync          ← must not land before WP3 (index dependency)

WP5  schema slimming            ← independent, any time
WP6  reconciliation simplify    ← independent, any time
WP7  documentation              ← last, after everything above
```

Three dependencies are real:

1. **WP1 before WP2.** `app/helpers/home_currency.py` — the read-time converter WP2 wires
   in — has a four-branch sign matrix keyed on `transaction_type = 3` and
   `transfer_direction`. WP1 collapses that to two branches. Wire it before WP1 and you
   will rewrite it immediately.
2. **WP3 before WP4.** Every domain table's only non-unique index is `(user_id,
   updated_at)`, which exists to serve `/sync`. WP4 deletes those indexes. WP3 adds the
   indexes that replace them. Landing WP4 first leaves the ledger table with nothing but
   its primary key.
3. **WP7 last.** It rewrites `engine-spec.md` and `schema-reference.md` against the final
   shape. Doing it earlier means doing it twice.

`WP5` and `WP6` touch nothing the others depend on. Run them whenever.

---

## Rules for every work package

**Verify before you act.** This document and your work package cite file paths, line
numbers and counts that were true on 2026-08-04. Line numbers drift. Treat every specific
as a starting point for a `grep`, never as a fact. If a citation doesn't match what you
find, trust the code and say so in your summary.

**The engine comes first.** From `CLAUDE.md`: prefer breaking a client and fixing the root
cause over patching around a design flaw. Never weigh "this would be less work for the
CLI" against engine correctness. Record the break in `docs/client-breaking-changes.md` and
proceed.

**Fix at the root, not the call site.** A guard added to stop one symptom is a smell — ask
what design let the symptom exist.

**Fail closed.** Enumerate what is permitted, never what is forbidden. New fields default
to blocked, unknown input 422s rather than being silently dropped, and missing data
surfaces as `null` plus a flag rather than a convenient substitute value. Several of the
defects this program removes are exactly this rule being broken.

**Reuse before writing.** Check for an existing helper before adding one. Duplicate logic
is a bug waiting to happen.

**Tests are the arbiter.** `pytest`, no flags, no env — it runs against a dedicated
`expense_world_test` database. **238 passing as of 2026-08-06.** Your package must leave
the suite green. Deleting a feature means deleting its tests; changing behaviour means
changing the tests that assert the old behaviour, deliberately and visibly, not by
loosening assertions until they pass.

**Update the convention you invalidate, immediately.** `CLAUDE.md` is loaded into context
at the start of every session. **The package that invalidates a convention rewrites it in
the same change.** Both conventions that carried a ⏳ marker have now been rewritten by the
package that invalidated them — the sign convention by WP1, and "Balance updates are
atomic" by WP3, which replaced it with "Balances are computed at read time, never stored". Do not defer that
to WP7 — an agent starting the next package would read a rule that no longer holds. WP7
handles the larger `engine-spec.md` / `schema-reference.md` sweep, not this.

**Append to `docs/client-breaking-changes.md`** whenever a wire shape changes. The CLI
lives at `../expense_world_CLI` and can be read to check whether a field is actually
consumed — several assumptions in the audit were confirmed or refuted that way.

---

## Shared vocabulary

The audit classified every column into four states. Work packages use these words:

- **Load-bearing** — engine logic reads it and behaves differently based on its value.
- **Carried** — written and returned honestly, but nothing branches on it. Legitimate
  (display data, audit metadata); not automatically a deletion candidate.
- **Echo-only** — client state living in the engine's database. The engine cannot use it
  and does not know what it means.
- **Dead** — never written, never read, or written and never read.

---

## Where the truth lives

| Question | File |
|---|---|
| Endpoints, business rules, validations | `docs/engine-spec.md` |
| Database schema | `docs/schema-reference.md` |
| Known defects, by severity | `docs/open-bugs.md` |
| How multi-currency works | `docs/currency-model-decision.md` |
| Wire changes clients must absorb | `docs/client-breaking-changes.md` |
| Conventions that apply everywhere | `CLAUDE.md` |
| Pre-cut column census (Part 1 only) | `docs/audit-2026-08-03-data-model.md` |

`engine-spec.md` and `schema-reference.md` will be wrong in the areas you change until
WP7. That is expected. Note what you invalidated in your summary so WP7 can find it.

---

## What this program does *not* decide

Two product questions are open. **Neither blocks any work package**, and no package
should resolve one as a side effect.

**`is_person` — build `POST /people`, or delete the axis.** Person accounts are
structurally complete and functionally unreachable: `is_person` is read by the accounts
list, the dashboard split, and the transfer engine's `@Debt` branch, but no endpoint can
set it — the INSERT in `app/helpers/accounts.py` omits the column entirely. The blast
radius includes the `people` dashboard panel (always `[]`), the `include_people` query
param, the `@Debt` system category, and the person-leg branch of the transfer pair. The
status quo — full machinery, no entry point — is the most expensive of the three options.

**`transaction_source` on `expense_transaction_hashtags` — depends on whether the inbox
should support hashtags.** Only the value `1` is ever written and every read filters on
it. The second source was never built: there is no `hashtag_ids` field on the inbox
schemas and promotion attaches none, so **tags are silently lost by using the inbox**.
Answer the product question first; the column follows.

Also deliberately out of scope: the `Decimal` / `ROUND_HALF_UP` money-rounding cleanup
noted in `app/helpers/home_currency.py`, and anything in `CLAUDE.md`'s
"single-user-shaped" table (pooling, rate limiting, RLS enforcement) — those are scale
boundaries, not defects.

---

## The packages

| WP | Scope | Blocks / blocked by |
|---|---|---|
| [WP1](WP1-transfer-collapse.md) | `transaction_type` becomes direction on every row; `transfer_direction` deleted | Blocks WP2. **Has a deadline.** |
| [WP2](WP2-read-time-currency.md) | Delete stored home values and rates; convert at read time | Needs WP1 |
| [WP3](WP3-computed-balances-and-indexes.md) | Delete `current_balance_cents`; compute it; **add the missing indexes** | ✅ **Landed 2026-08-06** (`sql/022`). Unblocks WP4 |
| [WP4](WP4-delete-sync.md) | Delete `/sync`, `sync_checkpoints`, and the `updated_at` indexes | ✅ **Landed 2026-08-06** (`sql/023`) — see deviation note below |
| [WP5](WP5-schema-slimming.md) | 15 columns and 4 routes with no readers | ✅ **Landed 2026-08-06** (`sql/024`) — see deviation note below |
| [WP6](WP6-reconciliation-simplification.md) | Delete the chaining cascade; shrink the largest helper | Independent |
| [WP7](WP7-documentation.md) | Reconcile spec, schema reference and conventions; delete this directory | Last |

### Bugs that close as a side effect

`docs/open-bugs.md` lists five 🔴 critical defects. Three of them are defects in machinery
this program removes:

| Bug | Closed by |
|---|---|
| ~~3.1 — delta sync can permanently drop committed writes~~ | ✅ **Closed by WP4**, by deletion (with the ⚪ `X-Client-Id` normalisation nit) |
| ~~1.4 — inbox items promote at exchange rate 1.0~~ | ✅ **Closed by WP2**, by deletion |
| ~~1.5 — changing `account_id` never re-rates~~ | ✅ **Closed by WP2**, by deletion |
| ~~2.3 — `resolve_home_rates` reads an account with no `user_id` filter~~ | ✅ **Closed by WP2**, by deletion — a live cross-tenant read while it lasted |
| ~~1.3 — every USD→USD transfer returns 500~~ | ✅ **Closed by WP1**, by repair rather than deletion — owner decision, see below. WP2 then deleted the block it lived in, so it is now unrepresentable as well |
| ~~1.2 — surviving dominant-side implementation is the buggy one~~ | ✅ **Closed by WP1** with 1.3 |
| 4.1 — expired idempotency keys duplicate financial writes | **Nothing.** Survives the program. |

**WP2 opened one, too.** Removing `exchange_rate` and `amount_home_cents` from the
transfer-leg edit guard left `{amount_cents, account_id, date}` — a deny-list, which is
the shape `CLAUDE.md`'s "fix at the root" corollary warns about, and `category_id` is
still not in it. Filed as **6.5**; out of WP2's scope, and `CLAUDE.md` currently claims
this guard was already inverted to an allow-list, which it was not.

> **WP1 deviated from its stated scope here, deliberately.** 1.3 would *not* have fallen out
> of the transfer collapse — the `raise RuntimeError` is in the dominant-side currency block,
> which WP1 declared out of scope and `open-bugs.md` assigned to WP2. Reproduced first
> (`RuntimeError` at `transfers.py:165`, uncaught, 500), then fixed by reordering that block
> to match `engine-spec.md` §Transfers point 7, which also fixed a second defect nobody had
> filed: a home-currency primary with a caller-supplied `exchange_rate` valued itself at
> `amount × rate`.
>
> **The block still forces the two legs to net to zero, so no FX spread is visible yet.**
> WP2 inherits an already-correct branch order to delete, not a buggy one.
>
> ⚠️ **Correction, 2026-08-05.** This note originally said the owner had chosen to have
> WP2 introduce `@FX` alongside deleting the forcing rule. When the choice was put with
> real numbers — a $1,000 → S/3,450 exchange at a market rate of 3.58, and where the
> resulting S/130 should appear — **the owner chose to leave the spread in `@Transfer`**.
> `@FX` stays deferred exactly as `docs/currency-model-decision.md` has always said. WP2
> shipped that way.

Delete the row from `open-bugs.md` when it closes — it is a work queue, not a changelog.


---

## WP3 landed, 2026-08-06 — three deviations from its own spec, all deliberate

`sql/022` drops `current_balance_cents`, adds the indexes, and deletes
`app/helpers/balance.py`. Suite green at 238 (was 228; the ten new ones are
`tests/test_wp3_computed_balances.py`). Recorded here because WP4 reads this file
and three of WP3's stated decisions did not survive contact with the code.

**1. Three of the six recommended indexes were not created.** The package's own
table says it is a recommendation, not a prescription, and says to confirm each
one against the queries that exist. Two did not survive that check:

- `(transfer_transaction_id)` — **no query filters on it, anywhere.** Every use
  reads the value off a row in Python and looks the sibling up by primary key.
  "After WP1 this column is the discriminator" is true about semantics and says
  nothing about a query plan.
- `expense_transaction_hashtags (hashtag_id)` — the report joins the *other*
  direction (`monthly_report.py:194-198` correlates on `th.transaction_id`),
  which is already indexed twice.
- `INCLUDE (amount_cents, transaction_type)` — included columns defeat HOT
  updates, so every amount edit would bloat every index on the row.

`sql/022`'s header records each omission and why. **WP4: the four indexes that
DO exist are `idx_expense_transactions_user_{account,date,category,reconciliation}`.
Confirm those before dropping the seven `idx_*_user_updated` sync indexes.**

**2. The `EXPLAIN` definition-of-done was split in two.** The test database holds
a handful of rows, so the planner picks a sequential scan whichever indexes exist
— an `EXPLAIN`-asserting test would be theatre. The measured plans were captured
by hand against 50,000 seeded rows and live in `sql/022`'s header, following the
`sql/012:13-24` convention of shipping verification as a record. The half that IS
testable — *the query count does not grow with the number of accounts* — is a real
test, and it was mutation-checked: injecting an N+1 into `GET /accounts` moves it
from 6 statements to 9 and the assertion fires.

Worth knowing before WP4 tunes anything: **every balance read is scoped to the
accounts the caller is actually rendering**, so all of them take the bitmap-index
path (1 ms at 50k rows). An unscoped sum would be a sequential scan — correctly,
since with one user there is no selective predicate — which is why
`account_balance.py` exposes no ledger-wide variant at all. Each caller already
knows its ids: the account list has its page, each dashboard panel its slice,
`/sync` its delta.

And to head off a reasonable misreading: these sums are **per account**, never a
total across accounts. Every query is `GROUP BY account_id`, and an account holds
one immutable currency, so each sum stays inside one currency by construction. A
PEN balance is never added to a USD one — the only cross-currency figure is
`current_balance_home_cents`, which converts each account before combining.

**3. The activity-log exception WP3 asks you to remove does not exist.** Its
checklist says to "remove the activity-log exception for balance writes if it is
now vacuous". `engine-spec.md` lists three exceptions — hashtag junction rows, a
retired currency one, and `users.last_login_at` — and none concerns balances.
Nothing was removed. The *de facto* exception was real, though: balance UPDATEs
mutated an account row and wrote no `activity_log` entry, silently contradicting
`CLAUDE.md`'s "No exceptions". Deleting the writes resolves it.

**One thing WP3 did not anticipate, now written down.** The balance `UPDATE` also
bumped `updated_at`/`version` on the account row, which is what re-entered an
account into the next `/sync` delta after a ledger write. Nothing writes the
account row on a transaction now, so a balance can change without the account
being re-delivered. Filed in `docs/client-breaking-changes.md`; **WP4 retires it
by deleting `/sync`.**

**Scope beyond the package, by owner decision (2026-08-06):** the sentences in
`engine-spec.md` and `schema-reference.md` that WP3 made *false* were corrected in
the same change rather than left for WP7 — chiefly `schema-reference.md`'s
"Reading an account's balance is a single row lookup, never an aggregation". The
broader rewrite of those two documents is still WP7's. Untouched WP2-era drift
remains in both (`schema-reference.md:424` and `:633` still describe stored
`exchange_rate` / `amount_home_cents`).

---

## WP4 landed, 2026-08-06 — one factual premise was wrong, and it changed the scope

`sql/023` drops the seven `(user_id, updated_at)` indexes and `sync_checkpoints`;
the sync router, helper, schemas and tests are deleted; `GET /v1/sync` 404s.
Suite green at 215 (was 238; the deleted 23 were `tests/test_sync.py` plus three
`/sync`-specific tests in other files whose sibling coverage on `/inbox`,
`/transactions` and the list endpoints survives).

**1. "The CLI never called `/sync`" was false.** The package (and the audit it
came from) asserted the CLI uses only direct REST endpoints; in fact its
cache-by-default read path hydrated a local SQLite replica from `/sync` on every
default-mode read, and `expense sync` was a user-facing command. The zero rows in
`sync_checkpoints` reflected a reset production DB, not an unused endpoint. By
owner decision the deletion proceeded anyway, with a **companion CLI change that
landed first**: the CLI's whole cache layer was deleted (replica, `expense sync`,
`--no-cache`/`--no-sync-after`, config `client_id`, TUI Sync screen, post-write
refresh, exit codes 4/5) and every read is now a live loopback call. Rationale in
the CLI repo's `docs/decisions.md` ("Delete the local replica"); the wire change
is recorded in `docs/client-breaking-changes.md`.

**2. Everything else went as written.** WP3's four replacement indexes were
verified present before the drops; no query outside `helpers/sync.py` filtered or
ordered on `updated_at` (the seven indexes were sync-only);
`idx_expense_transaction_hashtags_tx` was kept; `resolve_home_rates` was already
gone (WP2); `X-Client-Id` lived only in the sync router. `version`, `updated_at`,
`deleted_at` and client UUIDs are untouched on every table. Optimistic-concurrency
coverage that survives: `tests/test_concurrency_hazards.py` (row-lock
serialisation) plus version-bump assertions in the reconciliation/restore/archive
suites — note honestly: there is no `If-Match`/409-version mechanism and never
was; `version` is a server-incremented counter clients read but never send.

**For WP7:** `engine-spec.md` still documents `GET /sync`, `sync_checkpoints`,
and `X-Client-Id`; `schema-reference.md` still lists the table and the seven
indexes. Route count is now 60.

---

## WP5 landed, 2026-08-06 — four corrections to the package's own facts

`sql/024` drops the columns, deletes the four archive routes, replaces the
`sql/006` trigger function (it INSERTed `users.email`), and validates
`display_timezone` on write. Suite green at 210 (was 215; 13 deleted —
12 category/hashtag archive tests plus the `actor_type` wire test — 8 new in
`tests/test_wp5_schema_slimming.py`). Route count 56. The auth request schemas
are now `extra="forbid"` (advances bug 6.1); bug 6.4 closed; decision D8
superseded. **Engine-only by owner decision** — the CLI companion work is
recorded as a checklist in `docs/client-breaking-changes.md`, not done, so
`expense auth settings --theme` and the category/hashtag archive commands are
broken until the CLI absorbs it.

**1. It is 16 columns, not 15.** The package's headline number forgot to count
`user_settings.deleted_at`, which its own Group 3 table lists. All 16 dropped.
`CLAUDE.md`'s "Soft delete everywhere" was amended in the same change —
`user_settings` is now the named exception.

**2. `fetch_hashtag_ids_map` is NOT an orphan and was NOT deleted.** The audit
line the package inherited ("zero references") was false at HEAD:
`attach_hashtag_ids` calls it (`app/helpers/transactions.py:125`) and it feeds
`hashtag_ids` on every transaction response.

**3. `is_archived` had nine live guard readers, not zero.** The package said
nothing branches on it and the dashboard panels were its last reader. Wrong on
both counts post-WP2: list filters, the inbox `?ready` subquery, and the
attach/promote/restore/batch validation guards all read it to 422 archived
references. All died with the column — deliberately, since the archived state
they guarded against no longer exists — but each was an enumerated edit, not a
`DROP COLUMN` fallout. The soft-delete guards beside them are untouched.

**4. `actor_type` was on the wire.** "No caller has ever passed a non-default
value" is a statement about writers; `GET /activity` returned the column and a
test asserted it. Dropping it is a recorded breaking change in
`client-breaking-changes.md`, not a silent removal.

**One thing the package missed, now covered:** `POST /auth/bootstrap`'s
`timezone` field was a second unvalidated write path into `display_timezone`.
Both paths now share `helpers/validation.validate_timezone` (422 on a non-IANA
zone). The silent-UTC read fallback in `compute_month_bounds` stays — with
writes validated it covers only out-of-band DB writes, and a UTC-rendered
report beats a 500.

**For WP7:** `engine-spec.md` still documents the six settings fields, the four
archive routes, `?include_archived` on categories/hashtags, `users.email`, and
`parent_transaction_id`; `schema-reference.md` still lists all 16 columns
(including `global_currencies.name`/`symbol` and the junction `version`) and
claims `expires_at` is "processed_at + 24 hours" (it never was — it is
`now() + 24 hours`). The `actor_type` paragraph in the spec was corrected in
this change because it was load-bearing for audit-query guidance.
