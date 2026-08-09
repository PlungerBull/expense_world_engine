# TODO

Operational / deployment tasks, plus accepted design changes awaiting scheduling — work that is not part of normal code review. Each entry describes what needs to happen, why, and when it becomes blocking. **Delete an entry when it closes — git history holds the record; do not keep tombstones here.**

One parked product question, one accepted-but-unscheduled feature, and the bug burn-down are the open items.

---

## Bug burn-down — the remaining open defects, in fix order

The 2026-08-07 verification audit confirmed every previously closed bug is genuinely fixed (each pinned by a test) and these are what remain. **Detail lives only in [docs/open-bugs.md](docs/open-bugs.md)** — this entry is the schedule, not a second copy; delete a line here when its row leaves that file.

1. **6.7** — system categories accepted on ordinary transactions (three boundary call sites: create, update, batch). *(Its pair 6.6 — UUID-valued body fields — closed 2026-08-08 with bloat-audit Tier 3: malformed body FKs now 422.)*
2. **7.1** — inbox writes do no referential/ownership validation (narrowed 2026-08-08: malformed ids now 422 via 6.6; what remains is well-formed-but-nonexistent/deleted/cross-tenant ids).
3. **5.5** — the three reconciliation state-machine gaps *(was four; the sibling delete warning closed 2026-08-08 with bloat-audit Tier 2 §6)*.
4. **8.2** — batch/transfer CREATE snapshots log `hashtag_ids: []`.
5. **1.7 remainder** — dedicated FX-hygiene pass: rate plausibility vs prior day, negative-lookup cache TTL, archived-account currencies in the fetch list, `Decimal`/`ROUND_HALF_UP`.
6. **Four ⚪ lows** — `restore_category` skips the reserved-name check (7.4-r); `?hashtag_id=` filter lacks `transaction_source = 1` (hashtag-filter — or fold into the inbox-hashtags feature below, whichever ships first); inbox titles stored verbatim, whitespace-only can promote (inbox-title, found 2026-08-08 — fix wants an owner call on 422-vs-NULL); `color or` collapses an explicit empty string to the default (account-color, found 2026-08-08 — needs a reject-vs-store decision).

**When it becomes blocking:** nothing here corrupts data today — that severity tier is empty — but 7.1 turns typos into stored-then-rejected-at-promote surprises on a daily-use path, so the burn-down should precede any new feature work.

---

## People API — build `POST /people`, or delete the `is_person` axis — 🅿️ PARKED product question

Person accounts are **structurally complete and functionally unreachable**: `is_person` is read by the accounts list filter (`?include_people`), the dashboard `people` panel split, the transfer engine's `@Debt` branch, and the opening-balance guard — but **no endpoint can set it**. The INSERT in `app/helpers/accounts.py` omits the column entirely, and `AccountCreateRequest` rejects the field (`extra="forbid"`). No production row can ever have it true, so the `people` dashboard panel is always `[]` and the `@Debt` leg of the transfer pair is unreachable.

The three options, most expensive first:

1. **Status quo** — full machinery, no entry point. The most expensive option: every future agent re-discovers the dead axis, and every transfer-engine change must keep a branch alive that nothing can execute.
2. **Build `POST /people`** (spec §People already sketches it: explicit creation only, never auto-created by a transfer — see decision D7 in [docs/open-bugs.md](docs/open-bugs.md) and the design rule in `engine-spec.md`).
3. **Delete the axis** — drop `is_person`, the `@Debt` branch, the `people` panel, `?include_people`, and the `@Debt` system category.

**When it becomes blocking:** the first time the owner wants to track a debt. Decide before building anything on top of the transfer engine's person branch.

---

## Inbox hashtags — decided YES (2026-08-07), feature not yet scheduled

**Tags are silently lost by using the inbox.** The inbox schemas have no `hashtag_ids` field and promotion attaches none — a user who drafts through the inbox cannot tag, and nothing tells them. **Owner decision 2026-08-07: the inbox should support hashtags** — "the inbox is the same copy [of a transaction] but with relaxed rules", and hashtags belong to that copy. What remains is scheduling the feature, which ships as one unit:

- the inbox `hashtag_ids` field (create/update schemas + storage),
- the promote carry-over (tags survive promotion into the ledger),
- widening `sql/027`'s `hashtags_transaction_source_valid` CHECK from `= 1` to `IN (1, 2)` — the CHECK pins the ledger-only reality until the inbox writer exists.
- ⚠️ The numeric mapping is muddled: the pre-WP7 schema doc said `1=inbox, 2=ledger`, but the implementation has always written `1` for **ledger** rows. Pick the mapping deliberately when building — do not trust old documentation.
- `compute_month_flow`'s hashtag aggregation now carries the `transaction_source = 1` filter (closed 2026-08-07) — load-bearing the day the inbox writer ships, since inbox junction rows must not leak into ledger reports.

**When it becomes blocking:** the first time a tagged draft matters. Cheap while the junction table holds zero rows.
