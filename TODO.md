# TODO

Operational / deployment tasks, plus accepted design changes awaiting scheduling — work that is not part of normal code review. Each entry describes what needs to happen, why, and when it becomes blocking. **Delete an entry when it closes — git history holds the record; do not keep tombstones here.**

The open items, in execution order: the bug burn-down, the People API, and the inbox-hashtags feature. *(The transfer removal that preceded them landed 2026-08-11 — `sql/030`, commits `05b5eb8`…`316ef7d`.)*

---

## Bug burn-down — the remaining open defects, in fix order

The 2026-08-07 verification audit confirmed every previously closed bug is genuinely fixed (each pinned by a test) and these are what remain. **Detail lives only in [docs/open-bugs.md](docs/open-bugs.md)** — this entry is the schedule, not a second copy; delete a line here when its row leaves that file.

Three ⚪ lows remain, planned as two phases sequenced by cost of being wrong — the four
independent single-file fixes landed together on 2026-08-13 as phase 1.

1. **`1.7-round`** — FX rounding split. `Decimal` through `exchange_rate.py` **plus**
   `ROUND_HALF_UP` quantize at `account_balance.py`'s two rate×cents sites — note
   `round(Decimal)` is still banker's rounding, so the type change alone fixes nothing.
   Its own phase: the only remaining item that can produce a wrong number. Finishing it
   means seeding a tie-producing rate in `tests/test_home_currency_parity.py` (today's
   fixture multiplies 2500 cents by 2-decimal rates, so it can never produce a tie) and
   converting that file's rate comparison to exact `Decimal` equality.
2. **`account-color`** — reject anything that isn't a 6-digit hex color (owner decision
   2026-08-13). Its own phase: ~10 files, a `sql/031` CHECK, and the only client-breaking
   change left — `expense_world_CLI` documents `--color` as a free-form string.
   Bigger than the bug row implies: `CategoryCreateRequest.color` is a **required**
   unvalidated `str`, so `POST /categories {"color": ""}` stores `""` today.
3. **`1.7-archived`** — leave alone. Inert under `sql/015`; fix it in the change that
   lifts the currency CHECK, where it can actually be tested.

**When it becomes blocking:** nothing here corrupts data today — that severity tier is
empty — but the burn-down should still precede any new feature work.

---

## People API — build `POST /people` — decided 2026-08-10, ships after the bug burn-down

The parked question is resolved: **keep the person feature, build the entry point.** With auto-paired transfers gone, a person account is just an account you register ordinary rows against — its balance *is* the debt. What's missing is unchanged: **no endpoint can set `is_person`** (the INSERT in `app/helpers/accounts.py` omits the column; `AccountCreateRequest` rejects the field), so the `people` dashboard panel and `?include_people` remain dead surfaces until this ships.

Shape is already decided (spec §People sketch + decision D7 in [docs/open-bugs.md](docs/open-bugs.md)): explicit creation only — `POST /people`, never auto-created as a side effect of anything.

**When it becomes blocking:** the first time the owner wants to track a debt. Sequenced after the bug burn-down. (The transfer removal landed 2026-08-11, deleting the `@Debt` auto-branch this replaces.)

---

## Inbox hashtags — decided YES (2026-08-07), feature not yet scheduled

**Tags are silently lost by using the inbox.** The inbox schemas have no `hashtag_ids` field and promotion attaches none — a user who drafts through the inbox cannot tag, and nothing tells them. **Owner decision 2026-08-07: the inbox should support hashtags** — "the inbox is the same copy [of a transaction] but with relaxed rules", and hashtags belong to that copy. What remains is scheduling the feature, which ships as one unit:

- the inbox `hashtag_ids` field (create/update schemas + storage),
- the promote carry-over (tags survive promotion into the ledger),
- widening `sql/027`'s `hashtags_transaction_source_valid` CHECK from `= 1` to `IN (1, 2)` — the CHECK pins the ledger-only reality until the inbox writer exists.
- ⚠️ The numeric mapping is muddled: the pre-WP7 schema doc said `1=inbox, 2=ledger`, but the implementation has always written `1` for **ledger** rows. Pick the mapping deliberately when building — do not trust old documentation.
- `compute_month_flow`'s hashtag aggregation now carries the `transaction_source = 1` filter (closed 2026-08-07) — load-bearing the day the inbox writer ships, since inbox junction rows must not leak into ledger reports.

**When it becomes blocking:** the first time a tagged draft matters. Cheap while the junction table holds zero rows.
