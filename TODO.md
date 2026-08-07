# TODO

Operational / deployment tasks, plus accepted design changes awaiting scheduling — work that is not part of normal code review. Each entry describes what needs to happen, why, and when it becomes blocking. **Delete an entry when it closes — git history holds the record; do not keep tombstones here.**

Two parked product questions are the only open items.

---

## People API — build `POST /people`, or delete the `is_person` axis — 🅿️ PARKED product question

Person accounts are **structurally complete and functionally unreachable**: `is_person` is read by the accounts list filter (`?include_people`), the dashboard `people` panel split, the transfer engine's `@Debt` branch, and the opening-balance guard — but **no endpoint can set it**. The INSERT in `app/helpers/accounts.py` omits the column entirely, and `AccountCreateRequest` rejects the field (`extra="forbid"`). No production row can ever have it true, so the `people` dashboard panel is always `[]` and the `@Debt` leg of the transfer pair is unreachable.

The three options, most expensive first:

1. **Status quo** — full machinery, no entry point. The most expensive option: every future agent re-discovers the dead axis, and every transfer-engine change must keep a branch alive that nothing can execute.
2. **Build `POST /people`** (spec §People already sketches it: explicit creation only, never auto-created by a transfer — see decision D7 in [docs/open-bugs.md](docs/open-bugs.md) and the design rule in `engine-spec.md`).
3. **Delete the axis** — drop `is_person`, the `@Debt` branch, the `people` panel, `?include_people`, and the `@Debt` system category.

**When it becomes blocking:** the first time the owner wants to track a debt. Decide before building anything on top of the transfer engine's person branch.

---

## Inbox hashtags — `transaction_source` depends on it — 🅿️ PARKED product question

**Tags are silently lost by using the inbox.** The inbox schemas have no `hashtag_ids` field and promotion attaches none — a user who drafts through the inbox cannot tag, and nothing tells them. Whether the inbox should support hashtags is the product question; the column follows from the answer:

- `expense_transaction_hashtags.transaction_source` was designed to let junction rows reference either the ledger or the inbox, but only the ledger writer was ever built. Only the value `1` is ever written and every read filters on it (`app/helpers/transactions.py`). No CHECK constrains the value (bug 6.3's remainder).
- ⚠️ The numeric mapping is muddled: the pre-WP7 schema doc said `1=inbox, 2=ledger`, but the implementation has always written `1` for **ledger** rows. If inbox support is ever built, pick the mapping deliberately — do not trust old documentation.
- If the answer is "no inbox hashtags", the column is a one-value discriminator and can be dropped; if "yes", build the inbox writer, the promote carry-over, and the CHECK together.
- Related ⚪ low in [docs/open-bugs.md](docs/open-bugs.md): `compute_month_flow`'s hashtag aggregation is missing a `transaction_source = 1` filter — harmless today precisely because no other value exists.

**When it becomes blocking:** the first time a tagged draft matters. Cheap while the junction table holds zero rows.
