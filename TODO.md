# TODO

Operational / deployment tasks, plus accepted design changes awaiting scheduling — work that is not part of normal code review. Each entry describes what needs to happen, why, and when it becomes blocking.

> **Removed 2026-08-01: "Wire up the Render Cron Job for daily exchange-rate fetching."** Never an open task on the local profile — the job runs as a daily launchd agent ([deploy/local/README.md](deploy/local/README.md), roadmap Step 11.5). The Render recipe it carried now lives where it is actually needed, in [deploy/cloud/README.md](deploy/cloud/README.md) step 5 (cloud reactivation). Full original entry in git history.

> **Removed 2026-08-01: "Home-currency recalculation: switch from per-row UPDATEs to bulk SQL."** Closed, not deferred — the target narrowed from 1000+ public users to the owner alone that day, which retires the entire premise. Its stated trigger was Render's HTTP timeout, and Render left the path at Step 11; the follow-on concern (a long synchronous recalc pinning a pool connection) only bites with concurrent users. At one user the recalc is fast, rare, and correct. Full original entry in git history.
>
> **Superseded later the same day:** the recalculation itself is now **gone**. The home currency is locked to PEN (`sql/018`), `PUT /auth/settings` rejects `main_currency` with `422`, and `app/helpers/recalculate_home_currency.py` was deleted along with its tests — it carried a silent `1.0` rate fallback (finding 1.1 of the 2026-08-01 audit). The sentence above claiming "nothing was deleted" no longer holds. The corresponding single-user-shaped row in CLAUDE.md is retired with it. *(The dominant-side zero-sum logic survived until `sql/021`, then went with the stored home values — cross-currency legs now convert independently at read time and the spread surfaces in `@Transfer`.)*

**Two parked product questions** (undecided design, ported from the deletion program's README before `docs/rework/` was deleted — 2026-08-06). Everything below them is a closed record, kept for the dated history.

> **Removed 2026-08-06: "Retire reconciliation chaining — 🔵 OPEN, decided 2026-08-01" (owner decision D3).** Executed by WP6 (`sql/025`), with two owner amendments over the D3 sketch: **no prefill** — `beginning_balance_cents` is required on `POST` (omitting it is a `422`, not an invitation to derive) — and **no `continuity_gap_cents`**; the read-time figure that shipped is `difference_cents`, the add-up check `(ending − beginning) − SUM(signed assigned transactions)`, projected on every read and never stored. The cascade, `beginning_balance_source`, `sort_order`, and the reorder endpoint are gone; account-scoped lists order by `date_start ASC NULLS LAST, created_at ASC`. Full original entry in git history; annotated on D3 in [docs/open-bugs.md](docs/open-bugs.md).

> **Removed 2026-08-06: "Split transactions (`parent_transaction_id`) — 🅿️ PARKED" (owner decision D8).** Superseded — the 2026-08-04 audit judged the column a placeholder, not a foundation, and `sql/024` dropped it (D8 annotated accordingly). Splits get designed fresh if they ever ship; the parent-exclusion predicate a future balance sum will need is preserved in `sql/022`'s header and `app/helpers/account_balance.py`. Full original entry in git history.

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

---

## Backfill historical exchange rates (manual, user-owned) — ✅ DONE 2026-07-31

> **Shipped as a job, not a one-off:** `app/jobs/backfill_exchange_rates.py`.
> `python -m app.jobs.backfill_exchange_rates --from 2024-03-02` inserted 881
> daily USD→PEN rows spanning 2024-03-02 → 2026-07-31. Idempotent and resumable
> — it skips dates already present before making any HTTP call, so re-run it
> freely to widen the range. Spot-checked against the provider at full 8dp
> precision; no day-over-day move above 5% in the whole series.
>
> **Two findings worth keeping:**
> 1. **You need far less history than it looks.** `get_rate` resolves with
>    `rate_date <= $1 ORDER BY rate_date DESC LIMIT 1` — it carries the last
>    earlier rate forward. So the hard requirement is *one row on or before your
>    earliest transaction date*; denser coverage buys accuracy, not availability.
>    Weekends and holidays need no handling at all.
> 2. **The provider floor is 2024-03-02.** Every earlier date 404s, including
>    the legacy `@1/<date>/` path (verified 2026-07-31). The job refuses earlier
>    `--from` values rather than leaving a silent hole. Pre-March-2024 history
>    needs a different source. One genuine gap exists inside the range —
>    2025-12-10, absent from the provider's dataset — which carry-back covers.

**What:** Populate `exchange_rates` with per-date rows going back to the earliest transaction date in the system, so historical transactions can be re-converted with accurate point-in-time rates.

**Why:** The daily cron only inserts `/latest` going forward. *(Historical note, written under the stored-conversion model: rows written under the old silent `1.0` fallback carried a wrong stored `amount_home_cents` until a recalc ran. Since `sql/021` nothing stores a conversion — a missing rate now surfaces at read time as `null` + `unconverted_count`, and backfilled rates take effect on the next read with no remediation step.)*

**Owner:** User (PlungerBull) will handle this directly against the database — not via engine code.

**When:** ~~at the very end of engine work~~ **Updated 2026-07-30:** folded into local-deployment Step 11.5 (local-deployment Step 11.5) — runs right after the data migration and daily-fetch verification, followed by a home-currency recalc, so PEN/USD history converts at true point-in-time rates. Easier now: the target database is local Postgres, no pooler in the way.

**Reference (provider corrected 2026-07-30):** Frankfurter **cannot** serve this — it carries ECB reference rates only, and the ECB list has no PEN (discovered when the daily job first ran for real; the 15 pre-existing `rate=3.75` rows turned out to be hand-inserted placeholders, ~10% off market, and were deleted). The provider is now **fawazahmed0/currency-api** (keyless, CDN-hosted), which the daily job uses via `app.jobs.fetch_exchange_rates`. It supports dated queries for backfill — `https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@YYYY-MM-DD/v1/currencies/usd.min.json` returns all USD rates for that date (lowercase codes) — and `_fetch_currency_api(version="YYYY-MM-DD")` in the job module already wraps this. Insert rows canonically as `(base_currency='USD', target_currency=<X>, rate_date=<date>, rate=<rate>)`, matching the daily job's format. **Run the backfill before relying on historical reports** — since `sql/021` a missing rate never blocks a write; it shows up at read time as a `null` aggregate with a non-zero `unconverted_count` until the rate row exists.

