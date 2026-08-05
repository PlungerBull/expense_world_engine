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
`expense_world_test` database. **213 passing as of 2026-08-04.** Your package must leave
the suite green. Deleting a feature means deleting its tests; changing behaviour means
changing the tests that assert the old behaviour, deliberately and visibly, not by
loosening assertions until they pass.

**Update the convention you invalidate, immediately.** `CLAUDE.md` is loaded into context
at the start of every session. Two of its "non-negotiable conventions" are marked ⏳ and
are scheduled to change (the sign convention → WP1, balance atomicity → WP3). **The
package that invalidates a convention rewrites it in the same change.** Do not defer that
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
| [WP3](WP3-computed-balances-and-indexes.md) | Delete `current_balance_cents`; compute it; **add the missing indexes** | Blocks WP4 |
| [WP4](WP4-delete-sync.md) | Delete `/sync`, `sync_checkpoints`, and the `updated_at` indexes | Needs WP3 |
| [WP5](WP5-schema-slimming.md) | 15 columns and 4 routes with no readers | Independent |
| [WP6](WP6-reconciliation-simplification.md) | Delete the chaining cascade; shrink the largest helper | Independent |
| [WP7](WP7-documentation.md) | Reconcile spec, schema reference and conventions; delete this directory | Last |

### Bugs that close as a side effect

`docs/open-bugs.md` lists five 🔴 critical defects. Three of them are defects in machinery
this program removes:

| Bug | Closed by |
|---|---|
| 3.1 — delta sync can permanently drop committed writes | WP4, by deletion |
| 1.4 — inbox items promote at exchange rate 1.0 | WP2, by deletion |
| 1.5 — changing `account_id` never re-rates | WP2, by deletion |
| 1.3 — every USD→USD transfer returns 500 | WP1, **probably** — prove it with a test, don't assume |
| 4.1 — expired idempotency keys duplicate financial writes | **Nothing.** Survives the program. |

Delete the row from `open-bugs.md` when it closes — it is a work queue, not a changelog.
