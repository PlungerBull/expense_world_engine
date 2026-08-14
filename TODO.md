# TODO

Operational / deployment tasks, plus accepted design changes awaiting scheduling — work that is not part of normal code review. Each entry describes what needs to happen, why, and when it becomes blocking. **Delete an entry when it closes — git history holds the record; do not keep tombstones here.**

The one open item is the inbox-hashtags feature. *(The People API shipped 2026-08-14 — `POST /people`, people-scoped `sort_order`, the `archived_people` dashboard panel; no migration. The bug burn-down before it completed 2026-08-13 — three phases, `sql/031`. Two ⚪ lows survive it deliberately and are documented in [docs/open-bugs.md](docs/open-bugs.md): `1.7-archived`, inert until a third currency is admitted, and `fx-store-float`, parity-neutral. Neither is scheduled and neither gates anything. The transfer removal before that landed 2026-08-11 — `sql/030`.)*

---

## Inbox hashtags — decided YES (2026-08-07), feature not yet scheduled

**Tags are silently lost by using the inbox.** The inbox schemas have no `hashtag_ids` field and promotion attaches none — a user who drafts through the inbox cannot tag, and nothing tells them. **Owner decision 2026-08-07: the inbox should support hashtags** — "the inbox is the same copy [of a transaction] but with relaxed rules", and hashtags belong to that copy. What remains is scheduling the feature, which ships as one unit:

- the inbox `hashtag_ids` field (create/update schemas + storage),
- the promote carry-over (tags survive promotion into the ledger),
- widening `sql/027`'s `hashtags_transaction_source_valid` CHECK from `= 1` to `IN (1, 2)` — the CHECK pins the ledger-only reality until the inbox writer exists.
- ⚠️ The numeric mapping is muddled: the pre-WP7 schema doc said `1=inbox, 2=ledger`, but the implementation has always written `1` for **ledger** rows. Pick the mapping deliberately when building — do not trust old documentation.
- `compute_month_flow`'s hashtag aggregation now carries the `transaction_source = 1` filter (closed 2026-08-07) — load-bearing the day the inbox writer ships, since inbox junction rows must not leak into ledger reports.

**When it becomes blocking:** the first time a tagged draft matters. Cheap while the junction table holds zero rows.
