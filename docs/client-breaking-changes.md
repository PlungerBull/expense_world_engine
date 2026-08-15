# Client Breaking Changes

Engine changes that require work in a client repo (`expense_world_CLI`, and any
future iOS / web client). Newest first.

Two kinds of entry belong here, and each one says which it is:

- ⚠️ **BREAKING** — the client stops working until it changes. A removed field,
  a new rejection, a changed shape.
- ➕ **ADDITIVE** — nothing breaks, but new engine capability sits unusable until
  the client builds for it. A new endpoint, a new nullable response field.

The distinction matters because the two demand different urgency: a BREAKING
entry is a bug in the client until it is addressed, an ADDITIVE one is a feature
the client does not have yet. What they share is that the engine cannot deliver
either alone — which is why both are written down here rather than only in the
spec.

*(The file was breaking-only until 2026-08-14 — every entry below the People one
is unlabelled and BREAKING by construction. The People API forced the split: a
new endpoint that no client can call is invisible to the owner, and "not
breaking" is a bad reason to leave it unannounced.)*

Purely internal changes a client can never observe do not belong here at all.

Each entry states what changed, what breaks (if anything), and what the client
must do.

---

## 2026-08-14 ➕ ADDITIVE — inbox drafts carry hashtags

**Engine change** (`sql/033`; `app/helpers/hashtag_links.py` new,
`helpers/inbox.py`, `helpers/transactions.py`, `helpers/hashtags.py`,
`schemas/inbox.py`, `routers/inbox.py`, `routers/transactions.py`,
`routers/reconciliations.py`, `constants.py`; owner decision 2026-08-07,
scheduled and built 2026-08-14).

**Tagging a draft is possible for the first time.** The junction table has had
its `transaction_source` column since `sql/003`, reserved for exactly this, but
the inbox writer was never built: the inbox schemas had no `hashtag_ids` field,
promotion attached none, and a user who captured through the inbox simply could
not tag. `sql/033` widened the column's CHECK to `IN (1, 2)` and the writer
shipped with it.

### Nothing breaks

No field was removed, renamed, or retyped, and no previously-accepted request is
newly rejected. `hashtag_ids` is optional on both inbox write routes, and the new
response key is an array that is always present — a client that ignores it is
unaffected. *(A client that was sending `hashtag_ids` to `/inbox` and receiving a
`422` for an unknown field will now find it accepted, which is the fix, not a
break.)*

### What's new

- **`POST /inbox` and `PUT /inbox/{id}` accept `hashtag_ids: [uuid, ...]`.**
  Same shape and same rules as on `/transactions`: every id must reference an
  active hashtag you own, else `422` `"Some hashtag IDs are invalid."` with the
  offending ids in `fields.hashtag_ids`. On `PUT` the semantics are
  **replacement** — omit to leave alone, `[]` to clear, a list to become the
  whole set. Explicit `null` is `422`, as on every other inbox field.
- **Every inbox representation returns `hashtag_ids`** — `GET /inbox`,
  `GET /inbox/{id}`, and the `POST`/`PUT`/`DELETE` response bodies. Sorted
  ascending, `[]` when empty, never `null`, never omitted.
- **Promotion carries the tags into the ledger.** `POST /inbox/{id}/promote`
  returns a transaction whose `hashtag_ids` is the draft's set. Nothing to do
  client-side — but a client that re-attached tags manually after promoting
  should stop, or it will issue a redundant `PUT`.
- **A tags-only `PUT /inbox/{id}` bumps `version`.** Expected if you compare
  versions for conflict detection: the draft's wire shape changed.
- **`DELETE /hashtags/{id}` now also untags drafts**, bumping their `version`
  the same way it does for transactions. A cached inbox list goes stale on a
  hashtag delete exactly as a cached transaction list already did.
- **Dismissing a draft clears its tags** (`hashtag_ids: []` in the `DELETE`
  response, and on the row under `?include_deleted=true`). One-way — see the
  restore-removal entry below.

### What the client must do

Nothing, to keep working. To use the feature: send `hashtag_ids` when creating
or editing a draft, render the array on inbox rows, and drop any post-promote
re-tagging step.

### What does *not* change

- **The ledger side is untouched** — same field, same rules, same responses.
- **Reports and filters are ledger-only.** An inbox tag never surfaces a row in
  `GET /transactions?hashtag_id=` and never appears in a `hashtag_breakdown`;
  the two sources are separated in every query.
- **`GET /inbox` has no `?hashtag_id=` filter** (owner decision 2026-08-14 —
  deliberately out of scope; the inbox is a short working list).
- Hashtag CRUD, names, ordering: unchanged.

### Engine references

- `sql/033_inbox_hashtags.sql` — the widened CHECK and the three-column UNIQUE key
- `app/helpers/hashtag_links.py` — the single implementation, source-parameterized
- `docs/engine-spec.md` §`POST /inbox`, §`PUT /inbox/{id}`, §`DELETE /inbox/{id}`,
  §`POST /inbox/{id}/promote`, §`DELETE /hashtags/{id}`
- `tests/test_inbox_hashtags.py`, `tests/test_hashtag_source_filter.py`,
  `tests/test_sql033_checks.py`

---

## 2026-08-14 ⚠️ BREAKING — `POST /inbox/{id}/restore` is gone

**Engine change** (`app/routers/inbox.py`, `app/helpers/inbox.py`; no SQL
migration; owner decision 2026-08-14).

**Dismissing a draft is now final.** The route that un-dismissed a soft-deleted
inbox item no longer exists. Every other soft-deletable resource still has its
restore endpoint — the inbox is the single, stated exception to the spec's
Restore semantics convention, because a draft is not a financial record. It is
something the user wrote; deciding it was wrong and dismissing it is the end of
it.

### What breaks

- **Any call to `POST /v1/inbox/{id}/restore` now returns `404`** — the route is
  unregistered, so the response is FastAPI's unmatched-route 404, not the
  engine's `NOT_FOUND` envelope for a missing row. A client that distinguishes
  the two will see the difference.
- **The `409` "Inbox item was promoted to the ledger…" error is gone with it.**
  It only ever came from this route.

### What the client must do

- Remove the undo affordance from the inbox delete flow (undo prompt, toast
  action, `inbox restore` command — whatever exists).
- **Confirm before deleting instead.** There is no in-app way back now, so the
  confirmation is the safety net. A mis-tapped delete means retyping the draft.
- Nothing else about `DELETE /inbox/{id}` changed: same `200`, same response
  body, same `status = 1` + `deleted_at` end state.

### What does *not* change

- The delete is still **soft**. The row keeps its data, `GET /inbox?include_deleted=true`
  still lists it, and the activity log still holds the `CREATED`/`DELETED`
  snapshots — so a genuinely painful mistake is recoverable by hand at the
  database, just not through the API.
- Every other restore endpoint is untouched: accounts, categories, hashtags,
  transactions, reconciliations.
- To undo a *promotion*, the answer is unchanged and was never this route:
  delete the ledger transaction.

### Engine references

- `docs/engine-spec.md` §Restore semantics (the inbox exception), §`DELETE /inbox/{id}`
- `app/helpers/inbox.py` module docstring — "No restore (owner decision 2026-08-14)"
- `tests/test_restore_endpoints.py` — pins the route's absence

---

## 2026-08-14 ➕ ADDITIVE — the People API ships (`POST /people`)

**Engine change** (`app/routers/people.py` new; `helpers/accounts.py`,
`helpers/reference_data.py`, `routers/dashboard.py`, `schemas/dashboard.py`,
`schemas/accounts.py`, `main.py`; no SQL migration; owner decisions 2026-08-10
and 2026-08-13).

**Person accounts are creatable for the first time.** `is_person` has existed
since `sql/003` and every read surface has been shipped and tested for months —
`?include_people`, the dashboard `people` panel, the opening-balance refusal —
but no endpoint could ever set the flag, so the People panel was permanently
`[]`. It isn't any more.

A person is a bank account with `is_person = true`. Money lent or borrowed is
recorded as ordinary transactions against it, and the account's computed balance
*is* the debt: positive = they owe you, negative = you owe them.

### Nothing breaks

No field was removed, renamed, or retyped; no request is newly rejected. The one
new response field (`archived_people`) is nullable and additive, and the CLI
reads dashboard panels with `body.get(...)`, so it ignores the field safely today.

### What's new

- **`POST /people`** — creates a person account. Required `id` (client UUID),
  `name`, `currency_code`; optional `color`, `sort_order`. Same validation, same
  `409` name rules, same idempotency, same `AccountResponse` shape as
  `POST /accounts`, with `is_person: true` and `current_balance_cents: 0`.
  `is_person` in the body is a `422` here too — the endpoint implies it.
- **`GET /dashboard` gains `archived_people`** — same row shape as
  `bank_accounts`, populated only under `?include_archived=true`, `null`
  otherwise. It is a **separate panel from `archived_accounts`**, which stays
  `is_person = false`; the two never mix.
- **`GET /dashboard`'s `people` panel now filters `is_archived = false`.**
  Previously it had no archive filter while `GET /accounts?include_people=true`
  did. Unobservable before now (no person could exist), but the two surfaces
  agree from here on.
- **`sort_order` is scoped to `is_person`.** People number from 0 within their
  own section; real accounts number independently. Cross-section slot values are
  meaningless and must never be compared.

### What did NOT change

- **There is no `/people` namespace, and there never will be.** `POST /people`
  is the only route, because creation is the only moment `is_person` is
  settable. Rename, recolor, reorder, delete, restore, archive, unarchive and
  fetch all use `/accounts/{id}`, which has always accepted person rows.
  Listing is `GET /accounts?include_people=true` plus the dashboard split.
  Do not wait for `GET /people` / `PUT /people/{id}` — they are not coming.
- **Name uniqueness is shared, not scoped.** `(user, LOWER(name), currency)`
  spans people and real accounts together, so a person named "Eliana" in PEN
  collides with an account named "Eliana" in PEN and returns `409`.
- **`POST /accounts` still rejects `is_person`** with `422` — people are never
  created as a side effect of another write. Now pinned by a test.
- **Person accounts still cannot carry an opening balance** (`422`) — a debt is
  built from recorded rows, not seeded.
- Sign convention, computed balances, read-time conversion, activity logging,
  idempotency — all untouched. A person's rows are ordinary transactions with
  ordinary user categories; there is no `@Debt` category (it left with the
  transfer feature, `sql/030`).

### ⚠️ One design rule the client must respect

**A settled person is not hidden.** When a debt clears, the person stays in the
`people` panel with `current_balance_cents: 0`. The engine will never filter a
person out on the strength of a computed balance — this was proposed and
rejected (owner, 2026-08-13), because it makes "she paid me back" and "I never
recorded the loan" identical on screen, flickers people in and out of the list as
rows land, and misreads a coincidental net zero (lent 200, borrowed 200 — two
live debts) as nothing to show.

Decluttering a long People list is a **client display choice**, and the
recommended shape is to collapse the settled ones rather than drop them:

```
People
  Eliana          S/ 200
  Marco          −S/ 450
  ▸ 3 settled
```

Archiving is the engine-side answer for someone you are finished with: it moves
them to `archived_people` and makes the engine refuse to record against them.

### What the CLI must do

1. **Add a create-person command.** `expense/tui/screens/create_forms.py:7`
   currently notes "New account is bank-only — the engine forbids `is_person`";
   that is still true of `POST /accounts`, but `POST /people` is now the door.
2. **Render `archived_people`** next to the existing `archived_accounts` handling
   in `expense/commands/dashboard_cmd.py:134` and the archived view in
   `expense/tui/screens/accounts.py`.
3. **Collapse settled people** in the People section (`home.py:101`,
   `outstanding.py:168`) rather than hiding zero balances outright.
4. **Do not add a client-side people list/edit path against `/people`** — use the
   account routes, which already work on person rows.

### Engine references

- `tests/test_people_api.py` (the new pins — all four shape decisions plus the
  settled-visibility rule)
- `docs/engine-spec.md` — §People / Person Accounts, §`GET /dashboard`
- `docs/design-philosophy.md` — §People (debt tracking)
- `docs/schema-reference.md` — §People Model

---

## 2026-08-13 — FX correctness batch: home-cent rounding unified, implausible provider rates refused

**Engine change** (`helpers/exchange_rate.py`, `helpers/account_balance.py`,
`jobs/fetch_exchange_rates.py`; `sql/032`; open-bugs 1.7 split + fx-store-float,
owner decisions 2026-08-13). Four related FX fixes. No request or response
*shape* changes anywhere — this entry exists because two of the fixes can move
a *value* a client renders.

**1. Python-side conversion now rounds half-up, the way Postgres does.** The
accounts list and dashboard converted rate × cents with banker's rounding while
every report figure used SQL `round(numeric)` (half-up), so a half-cent product
differed by one cent between surfaces: at rate 3.3373 a $50.00 balance was
S/166.87 in a report and S/166.86 on `GET /accounts`.
`current_balance_home_cents` on `/accounts` and `/dashboard` can therefore
shift by one cent on exact half-cent values — and now always agrees with the
monthly report and dashboard flow figures.

**2. The fetch job refuses implausible provider rates.** A rate moving more
than ±10% from the most recent stored rate within 7 days is not stored
(misplaced-decimal protection: `33.373` for `3.3373` would previously have
stood all day, since the first rate written for a date wins). A refused date
simply has no row; every read carries forward the last good rate and
self-heals the moment a sane one lands. Client-visible only as: `rate_date`
can trail `date` in `GET /exchange-rates` on such a day — which is the
standing staleness signal (`rate_date < date`), not a new field.

**3–4. Fidelity fixes, not wire changes:** stored rates now hold the
provider's exact decimal digits instead of a bound float's binary expansion
(`parse_float=Decimal`; `sql/032` rewrites all 893 pre-existing rows,
losslessly — every rewrite was verified to round-trip), and a *missing*-rate
cache entry now expires after 60s instead of an hour, so a backfilled rate is
picked up promptly. `rate` stays `float` on the wire and serializes to the
same JSON number as before.

**What the client must do:** nothing. If the TUI caches home-cent figures,
expect one-cent shifts on half-cent conversions; the cross-surface
disagreement those could produce is gone.

### Engine references

- `tests/test_home_currency_parity.py` (SQL/Python conversions now pinned
  exactly equal), `tests/test_fx_plausibility.py`,
  `tests/test_fx_decimal_fidelity.py`, `tests/test_rate_cache.py`
- `docs/engine-spec.md` — §Rate ingestion (plausibility band), the
  `rate_date < date` staleness contract under `GET /exchange-rates`

---

## 2026-08-13 — `color` must be a 6-digit hex value

**Engine change** (`helpers/validation.validate_color`, `helpers/accounts.py`,
`helpers/categories.py`, `helpers/reference_data.py`, `sql/031`; open-bugs
account-color, owner decision 2026-08-13).

`color` was previously **unvalidated everywhere**. Two different bugs sat under
that:

- `POST /accounts` bound `color or '#3b82f6'`, so an explicitly-sent `""` came
  back as the default blue with a `201` — the caller's value silently replaced.
- `POST /categories` — where `color` is *required* — bound it verbatim, so `""`
  and `banana` were simply stored. Both `PUT` paths were unchecked too, which
  meant create and update already disagreed about `""` on accounts.

**Now:** every `color` on `POST`/`PUT` for accounts and categories must match
`^#[0-9a-fA-F]{6}$`, else `422 VALIDATION_ERROR` with
`fields: {"color": "Must be a 6-digit hex color, e.g. '#3b82f6'."}`. Rejected:
`""`, whitespace, `banana`, `3b82f6` (no `#`), `#12`, `#fff` (3-digit shorthand),
`#ff00ff00` (8-digit alpha), and any value with surrounding whitespace. `CHECK`
constraints on both columns (`sql/031`) back the rule.

**Unchanged:** omitting `color` on an account still yields `#3b82f6`; a valid
value is stored **verbatim including case** (`#00AA00` stays `#00AA00` — the
engine does not normalize what it accepted); categories still require the field.

**What clients must do:**

- **CLI** — `expense accounts create --color` and `expense accounts update
  --color` document the flag as *"Color hint (free-form string)"*
  (`expense/commands/accounts_cmd.py`). That is now false; update the help text
  and expect a `422` for anything non-hex. The TUI's colour picker
  (`tui/screens/create_forms.py`) only emits 6-digit hex and needs no change.
- **Any client** with a free-text colour input should validate client-side for a
  better message, but the engine's `422` is well-formed and renderable as-is
  through the standard error path.

No stored data changed: the live ledger's rows were already valid hex, so the
migration is constraint-only with no backfill.

---

## 2026-08-13 — inbox titles are trimmed; a whitespace-only title is an unfilled field

**Engine change** (`helpers/inbox.py`; open-bugs inbox-title, owner decision
2026-08-13). `POST /inbox` and `PUT /inbox/{id}` now pass `title` through the
same trim the ledger has always applied: surrounding whitespace is stripped,
and an empty or whitespace-only title is stored as `null` — the draft still
saves (that is what the inbox is for), but the field counts as unfilled.

### What breaks

- **A title no longer round-trips byte-for-byte.** `"  Coffee  "` comes back
  `"Coffee"` on the write response and every later read.
- **A whitespace-only title no longer makes a draft ready.** `{"title": "   "}`
  was previously stored verbatim, passed both readiness checks, and promoted
  into the ledger as a blank-looking row. Now it stores `title: null`, the
  draft is excluded from `GET /inbox?ready=true`, and
  `POST /inbox/{id}/promote` returns the standing `422`
  `"Inbox item is not ready to promote."` until a real title is typed.

### What does *not* change

- **`{"title": null}` is still `422`** — deliberately asymmetric: whitespace
  is a field left blank, explicit null is a clear operation the inbox does not
  offer (no inbox field accepts explicit null).
- `description` is untouched — stored verbatim, exactly as the ledger stores it.
- The ledger side is unchanged: direct transaction writes have always trimmed
  titles and rejected empty ones with `422`.

**What the client must do:** nothing structurally. Drop any assumption that a
saved draft's title equals the sent string, and treat `title: null` coming
back after sending whitespace as "unfilled", not an error.

### Engine references

- `tests/test_inbox_title_normalization.py` (the new pins)
- `docs/engine-spec.md` — §`POST /inbox` (the trim rule and the
  whitespace-vs-null asymmetry), §readiness definition

---

## 2026-08-13 — restoring a category re-checks the reserved-name rule

**Engine change** (`helpers/categories.py`; open-bugs 7.4-r, closing the gap
the bug 7.4 create/update guard left). `POST /categories/{id}/restore` now
rejects a soft-deleted *user* category whose stored name is reserved for
system categories (today: `@Opening`, compared case-insensitively) with
`422 VALIDATION_ERROR`
(`fields: {"name": "'…' is reserved for system categories."}`). Restore was
the third write path into a category name and the one the original guard
missed — the name comes off the stored row, not the request, so a pre-guard
squatter could be soft-deleted and restored straight back into the squat that
breaks opening-balance seeding.

**What breaks:** only the restore of a squatter row created before the
create/update guard shipped — no API call since then can have produced one.
The `422` is checked before the existing name-collision `409`, which is
unchanged for ordinary names.

**What the client must do:** nothing — render the `422` through the standard
error path like every other restore rejection.

### Engine references

- `tests/test_reserved_category_names.py` (the restore pins)
- `docs/engine-spec.md` — §Reserved names

---

## 2026-08-11 — completed reconciliations fully freeze their transactions; DELETE loses its `warnings` key; one PUT is one `version` bump

**Engine change** (`helpers/transactions.py`, `helpers/query_builder.py`,
`routers/transactions.py`, `schemas/transactions.py`; open-bugs 5.5, owner
decision 2026-08-11). Three related fixes to the transaction ↔ reconciliation
state machine:

1. **Unassigning or moving a transaction out of a completed reconciliation is
   now locked** — `PUT /transactions/{id}` with `reconciliation_id` (null or
   any value) on a row whose reconciliation is `status = 2` returns the same
   `422` as the existing amount/account/title/date field lock
   (`fields: {"reconciliation_id": "Locked by completed reconciliation."}`).
   Previously it succeeded silently and drifted the completed batch's
   `difference_cents`. Revert the reconciliation to draft first.
2. **Deleting a transaction assigned to a completed reconciliation is now
   `409 CONFLICT`** (`"Cannot delete a transaction assigned to a completed
   reconciliation. Revert the reconciliation to draft first."`), previously a
   `200` carrying a staleness warning. With that warning gone, **the
   `warnings` key is removed from the `DELETE /transactions/{id}` response**
   — it returns the plain transaction shape. `POST /transactions/{id}/restore`
   keeps its `warnings` envelope unchanged and is now the channel's sole
   member.
3. **A `PUT` that changes `reconciliation_id` alongside other fields bumps
   `version` by exactly 1**, previously 2 (two internal UPDATEs). A client
   doing read-modify-write conflict detection sees one step per edit.

Also in this batch (shared-helper hardening, no contract change for correct
clients): a delete/restore that races a concurrent state flip on accounts,
categories, hashtags, reconciliations, or inbox items now returns
`409 CONFLICT` instead of an uncontrolled `500`.

**What the CLI must do:** stop reading `warnings` on transaction delete
responses (restore is unaffected); surface the new `409` on delete and the
`reconciliation_id` field-lock `422` as "revert the reconciliation first".
No request shape changes.

### Engine references

- `tests/test_reconciliation_rules.py` (new pins), `tests/test_transaction_restore.py`,
  `tests/test_openapi_contract.py` (updated)
- `docs/engine-spec.md` — §`PUT /transactions/{id}` (field locking, version),
  §`DELETE /transactions/{id}`, Warnings channel convention, §Assigning
  transactions, §complete/§revert

---

## 2026-08-11 — inbox writes validate `account_id`/`category_id` references (was: stored unvalidated, rejected at promote)

**Engine change** (`helpers/inbox.py`; open-bugs 7.1, the last item the 6.7
entry below deferred). `POST /inbox` and `PUT /inbox/{id}` now run the same
reference validation as the ledger write paths: a well-formed `account_id` or
`category_id` that is nonexistent, soft-deleted, archived (accounts), or
another tenant's returns `422 VALIDATION_ERROR` with
`fields: {"account_id": "Must reference an active, non-archived account."}` /
`fields: {"category_id": "Must reference an active category."}`, where it was
previously stored and only rejected at promote. (A nonexistent id previously
hit the table's FK and returned a `500`.)

**What breaks:** a client that saved drafts with stale ids and relied on
promote-time rejection now gets the `422` at save. Unchanged: sparse drafts
(the fields stay optional — only supplied ids are checked), drafts pointing
at a system category (still accepted at save, refused only at promote), and
edits to a draft whose stored reference has since died (untouched fields are
not re-validated).

**What the CLI must do:** nothing structurally — surface the `422` at draft
save like it already does for ledger writes.

---

## 2026-08-11 — system categories rejected as `category_id` on every write (was: silently accepted)

**Engine change** (`helpers/transactions.py`, `helpers/inbox.py`,
`helpers/validation.py`, `routers/inbox.py`; open-bugs 6.7, owner decision).
System categories (`is_system = true` — only `@Opening` exists) are
engine-assigned only. A write naming one as `category_id` now returns
`422 VALIDATION_ERROR` with
`fields: {"category_id": "Must not reference a system category."}`
(batch: `fields.items[i].category_id`), where it was previously accepted —
storing a row that moved the balance while vanishing from every flow report.

**What breaks:**

- **`POST /transactions`**, **`PUT /transactions/{id}`**,
  **`POST /transactions/batch`**: `@Opening` as `category_id` → `422`.
- **`POST /inbox/{id}/promote`**: a draft whose `category_id` is `@Opening`
  → `422` (`"Inbox item is not ready to promote."`), and such drafts no
  longer appear in `GET /inbox?ready=true`. Saving the draft itself is still
  accepted — inbox write-side validation is bug 7.1, not this change.
- `POST /accounts/{id}/opening-balance` is unaffected — it remains the one
  path that files under `@Opening`.

**What the CLI must do:** nothing structurally — no field or shape changed.
But `@Opening` arrives on `GET /categories` (`is_system: true`), so any
category picker that offers it should filter `is_system` rows out of
write flows; a user who picked it previously created the invisible-row bug
this closes, and now gets the `422`.

### Engine references

- `tests/test_system_category_boundary.py` (the new pins)
- `docs/engine-spec.md` — §`POST /transactions`, §`PUT /transactions/{id}`,
  §`POST /transactions/batch`, §`POST /inbox/{id}/promote`
- `docs/currency-model-decision.md` — "system categories are engine-assigned
  only", now marked enforced

## 2026-08-10 — the transfer feature is removed (was: auto-paired legs via a `transfer` request field)

**Engine change** (`helpers/transfers.py` deleted; `helpers/transactions.py`,
`helpers/inbox.py`, `schemas/transactions.py`, `schemas/inbox.py`,
`routers/inbox.py`, `constants.py`; `sql/030`; owner decision 2026-08-10).
Auto-paired transfers are gone: no request may carry a `transfer` object, no
ledger row is paired to a sibling, and the `@Transfer`/`@Debt` system
categories no longer exist. A move between accounts is recorded as **two
ordinary rows** — one outflow, one inflow, ordinary user categories — and
monthly inflow/outflow totals include such moves (owner-accepted). `is_person`
and every person read surface (`?include_people`, the dashboard `people`
panel) are **kept**; `POST /people` ships later as its own item.

### What breaks

- **`POST /transactions`**: the `transfer` object 422s as an unknown field
  (`fields.transfer`, `extra="forbid"`). `category_id` is now
  **unconditionally required** — omitting it 422s as a plain missing field
  (`fields: {"category_id": "Field required"}`); the old conditional message
  `"Required for non-transfer transactions."` is gone.
- **`POST /inbox` / `PUT /inbox/{id}`**: the `transfer` object 422s as
  unknown. `"transfer": null` — formerly the inbox's only explicit-null
  field — is likewise rejected; no inbox field accepts explicit null now.
- **`POST /inbox/{id}/promote`**: `transfer_id` 422s as unknown; the body is
  `{"id": …}` alone.
- **Responses**: `transfer_transaction_id` is gone from every transaction
  payload; `transfer_account_id` and `transfer_amount_cents` are gone from
  every inbox payload. Deleted, not nulled — same treatment as
  `exchange_rate` (`sql/021`).
- **`GET /inbox?ready=true`**: no transfer arms — every ready row simply has
  title/amount/date/account/category present.
- **`DELETE`/`restore /transactions/{id}`**: no sibling cascade, and the
  `"Transfer sibling: "` warning prefix can no longer occur. The `warnings`
  channel itself stays (reconciliation-staleness notes are real).
- **Batch**: the "transfers not supported in batch" rejection is gone —
  a `transfer` key on an item now fails as an unknown field like any other.

### What does *not* change

- `is_person`, `?include_people`, the dashboard `people` panel, and the
  person guard on opening balances.
- `@Opening` and the whole opening-balance flow; `system_key` on category
  responses (values shrink to `'opening_balance'`).
- Sign convention, computed balances, read-time conversion, idempotency,
  activity logging — untouched.

### What the CLI must do

1. Remove the transfer commands and any `transfer`/`transfer_id` payload
   fields; drop `transfer_transaction_id`/`transfer_account_id`/
   `transfer_amount_cents` from response models.
2. Always send `category_id` on ledger creates.
3. If a paired-entry convenience is ever wanted, build it client-side as two
   ordinary `POST /transactions` calls.

### Engine references

- `tests/test_transfer_removal.py` (the new fail-closed pins);
  `tests/test_request_strictness.py`, `tests/test_uuid_body_fields.py` (updated)
- `docs/engine-spec.md` — "Moves between accounts" convention;
  `docs/currency-model-decision.md` — 2026-08-10 amendment
- Landed 2026-08-11: code in `89bd30c`, migration `sql/030` in `316ef7d`
  (applied to the live DB the same day); the engine has been serving the
  new contract since.

## 2026-08-08 — account routes 422 `SETTINGS_MISSING` when settings are absent (was: silent null home balances)

**Engine change** (`helpers/account_balance.py`, `helpers/settings.py`;
bloat-audit §15, owner decision). The three copies of the balance→home
conversion (per-account helper + two hand-rolled router batch loops) merged
into `fetch_home_balance`/`fetch_home_balances`, and the merged path reads
settings via the same `get_user_report_settings` the dashboard and reports
use — which refuses with `422 SETTINGS_MISSING` instead of silently
emitting `current_balance_home_cents: null`.

**What breaks:** `GET /accounts`, `GET /accounts/{id}`, and account
mutations now 422 when the user has no `user_settings` row. In practice
unreachable — bootstrap always creates the row — so no correct client sees
any difference; the change is that an impossible state now fails loudly
instead of rendering blanks.

**Client action:** none. Conversion math, rounding, and the null-when-no-rate
meaning of `current_balance_home_cents` are byte-identical.

### Engine references

- `tests/test_accounts_settings_missing.py` (new pin)
- `docs/engine-spec.md` — Settings preconditions paragraph

## 2026-08-08 — malformed UUIDs in request *bodies* return `422`, previously `500`

**Engine change** (`schemas/transactions.py`, `schemas/inbox.py`,
`schemas/reconciliations.py`; open-bugs 6.6, bloat-audit §17g). Every
UUID-valued FK field in a request body is now typed `UUID` like the `id` PKs
beside it: `account_id`, `category_id`, `hashtag_ids`, `reconciliation_id`
on transaction create/update, `transfer.account_id` on both transfer
fragments, the inbox create/update pair, and reconciliation create.

**What breaks:** a client sending a malformed UUID in one of these fields
got `500 INTERNAL_ERROR` before (the garbage reached SQL as a bind param);
it now gets the standard `422 VALIDATION_ERROR` with the field named in
`fields` (dotted for nested locations: `transfer.account_id`,
`transactions.0.category_id`). Clients treating 500 as "retry later" must
not retry these. Well-formed UUIDs behave exactly as before.

**Client action:** none for correct clients. The path/query half of this
landed 2026-08-07 (bug 6.2) — the two layers now agree.

### Engine references

- `tests/test_uuid_body_fields.py` (new pins), `tests/test_uuid_params.py`
  (the 6.2 precedent)

## 2026-08-08 — no-op `debit_as_negative` removed from `/dashboard` and `/reports/monthly`

**Engine change** (`routers/dashboard.py`, `routers/reports.py`; bloat-audit
2026-08-06 §16, owner decision). The two aggregate read routes no longer
declare the `debit_as_negative` query parameter. It was always a documented
no-op there (their figures are signed by construction); the parameter existed
only for surface uniformity.

**What breaks:** nothing at runtime — FastAPI ignores unknown query
parameters, so a client still sending the flag gets the same response as
before. The break is contractual: the flag no longer appears in OpenAPI for
these routes, and a client that *relied on it being documented as accepted*
(none known; the CLI never sends it to either route) should drop it.

**Client action:** none required. Remove the flag from any dashboard/report
request for tidiness.

### Engine references

- `docs/engine-spec.md` — Base Conventions sign-convention paragraph + the
  `/dashboard` section bullet
- The five real `debit_as_negative` surfaces are unchanged and now share one
  `DebitAsNegative` annotation (`app/deps.py`)

## 2026-08-08 — two validation `message` strings changed (bloat-audit Tier 2)

**Engine change** (`helpers/validation.py` + `schemas/transactions.py`;
bloat-audit 2026-08-06 §§9–10). Two error-envelope **top-level `message` /
field-message strings** changed when their duplicated validations were
single-sourced. Field keys, error codes, status codes, and response shapes
are unchanged — only clients that string-match these exact texts are
affected:

1. `PUT /transactions/{id}` with an empty/whitespace title: top-level
   `message` is now `"Title must not be empty."` (was
   `"Title validation failed."` — a drifted one-off; the field message
   `{"title": "Must not be empty."}` is unchanged).
2. `POST /transactions` (transfer body) with same-sign amounts: the field
   message on `transfer.amount_cents` is now
   `"Must have opposite sign to amount_cents."` (was `"…to primary
   amount_cents."` — "primary" named an engine-internal parameter; the
   inbox path already used the new wording, and the two paths are now
   pinned identical by test).

### Engine references

- `app/helpers/validation.py` (`normalize_name`, `clean_name`, message constants)
- `app/schemas/transactions.py` (`opposite_signs`, `MSG_OPPOSITE_SIGN`)
- `tests/test_low_bug_fixes.py::test_update_title_empty_top_message`,
  `tests/test_inbox_transfers.py::test_opposite_sign_message_identical_on_ledger_and_inbox`

---

## 2026-08-08 — omitted `sort_order` on create now appends instead of landing at 0

**Engine change** (`helpers/reference_data.next_sort_order`; bloat-audit
2026-08-06 Correctness §5, deferred by owner to this refactor). On
`POST /accounts`, `POST /categories`, `POST /hashtags`:

1. **Omitting `sort_order` now appends** — the new row gets
   `MAX(sort_order) + 1` within the user's collection (`0` when empty),
   implementing CLAUDE.md's collection-ordering convention. Previously every
   new row landed at `0`, so lists sorted by `sort_order ASC, created_at ASC`
   effectively fell back to creation order.
2. **An explicit `sort_order: 0` is now stored as `0`, distinguishably** —
   the old `or 0` collapsed it into the default; now only *omitted* values
   append.

A client that always sends explicit `sort_order` values sees no change. One
that omitted it and relied on the created-at fallback ordering will see new
rows sort after existing ones — which is what the spec always claimed.

### Engine references

- `app/helpers/reference_data.py` (`next_sort_order`), the three creates in
  `app/helpers/{accounts,categories,hashtags}.py`
- `docs/engine-spec.md` §POST /accounts, /categories, /hashtags
- `tests/test_sort_order_append.py`

---

## 2026-08-08 — account names: trimmed, blank rejected, case-insensitive uniqueness, deleted accounts release their name

**Engine change** (`sql/028` + `helpers/accounts.py`; bloat-audit 2026-08-06
Correctness §6, owner decision 2026-08-08). Account names now follow the same
rules categories and hashtags have had since `sql/012`:

1. **Names are trimmed on create and rename**; empty or whitespace-only names
   return `422` (`{"name": "Must not be empty."}`). Previously `"  Rent  "`
   and `"   "` were stored verbatim.
2. **Uniqueness is case-insensitive within (user, currency).** Creating or
   renaming to `"RENT"` beside an active `"Rent"` in the same currency is now
   `409`; previously both coexisted. Same name in a *different* currency is
   still allowed.
3. **A soft-deleted account releases its (name, currency).** Re-creating a
   deleted account's name now succeeds (`201`); previously it failed with the
   misleading `409 "An account with id … already exists."`.
4. **`POST /accounts/{id}/restore` can now return `409`** when an active
   account has retaken the deleted one's (name, currency) — same shape as the
   category/hashtag restore collision.

A client that never sends padded/duplicate names sees no difference. Clients
must not rely on case-sensitive name pairs or on deleted names staying locked.

### Engine references

- `sql/028_account_name_case_insensitive.sql` (partial unique index replaces
  the table-level `UNIQUE (user_id, name, currency_code)`)
- `app/helpers/accounts.py`, `app/helpers/reference_data.py` (`name_taken`)
- `docs/engine-spec.md` §Accounts; `tests/test_account_name_rules.py`

---

## 2026-08-07 — `/health` failure is `503`, not `500`; `?search=` matches `%`/`_`/`\` literally

**Engine change.** Two small behavior fixes (the four ⚪ Low bugs, open-bugs.md).

1. **`GET /health` with the database unreachable now returns `503`** in the
   standard error shape (`code: "SERVICE_UNAVAILABLE"`), previously an
   uncontrolled `500`. A client testing `status == 200` is unaffected; one
   branching specifically on `500` to mean "engine down" must accept `503`.
2. **`?search=` on `GET /v1/transactions` now treats `%`, `_` and `\` in the
   term as literal characters.** Previously they acted as SQL `ILIKE`
   wildcards, so e.g. `search=50%` matched any title containing `50`. It was
   always documented as a plain substring search; results simply get narrower
   (and correct). No request or response shape changes.

### Engine references

- `app/routers/health.py`, `app/routers/transactions.py` (`_escape_like`)
- `docs/engine-spec.md` §`GET /health`, §`GET /transactions`

---

## 2026-08-07 — error/shape fixes: FX lookup `422` for bad currency, report error fields pruned, transfer sibling carries `inbox_id`, OpenAPI shapes are real (bugs 10.1/10.2)

**Engine change.** Four behavioral pieces; the additive ones (`system_key` on
category responses — nullable, ignorable) are not listed per this doc's rule.

1. **`GET /exchange-rates` with an unsupported currency returns `422
   VALIDATION_ERROR`** (field-scoped: `base` and/or `target`), previously
   `404 NOT_FOUND`. `404` now means exactly one thing: a supported pair with
   no rate row on/before the requested date. A client branching on `404` to
   mean "no data yet" keeps working; one that sent unsupported codes and
   expected `404` must treat the new `422` as the input error it always was.
2. **`/reports/monthly` validation errors no longer include null-valued keys
   in `fields`.** `?year=2026` now yields `fields: {"month": "required"}`,
   previously `{"year": null, "month": "required"}`. A client iterating
   `Object.keys(error.fields)` stops rendering spurious "year: null" rows;
   nothing else changes.
3. **Promoting a transfer draft sets `inbox_id` on BOTH ledger legs**,
   previously the primary only. The sibling's `inbox_id: null` no longer
   reads as "never was in the inbox". Rows promoted before 2026-08-07 keep
   their null sibling backlink — treat null as "no recorded lineage", not
   "not from the inbox".
4. **`openapi.json` now documents every route's response shape**, and the
   default FastAPI `HTTPValidationError` 422 stub (a shape the engine never
   emitted) is gone — 422s document the real `{"error": {...}}` envelope.
   A client generating types from the OpenAPI doc will see them all appear;
   regenerate rather than pinning the old shapeless doc.

Also in this batch, invisible to correct clients: the `transfer.id ≠ id`
check is accumulated into the single 422 with the other transfer field
errors (same status, same field key — one round trip instead of two).

---

## 2026-08-07 — malformed UUID path/query params return `422`, previously `500`

**Engine change.** Bug 6.2. Every UUID-valued path parameter (`/{account_id}`,
`/{transaction_id}`, …) and query filter (`?account_id=`, `?category_id=`,
`?hashtag_id=`, `?reconciliation_id=`) is now typed `uuid.UUID`, so a
malformed id is rejected at the boundary with the standard `422`
`VALIDATION_ERROR` envelope (field name in `fields`) instead of reaching SQL
and returning `500 INTERNAL_ERROR`.

**What breaks.** Only a client that special-cased the old `500` for garbage
ids. Well-formed UUIDs — which every client generates client-side per the
UUID-first convention — behave identically. Treat `422` on an id param as a
client-side bug (mangled id), not a retryable server error.

---

## 2026-08-07 — idempotency keys are permanent; key reuse with a different request is `409`; PAT-create replay is `409`

**Engine change.** Bugs 4.1 (🔴) and 2.4, owner decision 2026-08-06; `sql/026`.
The 24-hour idempotency TTL is deleted — a used `X-Idempotency-Key` returns its
stored response forever. Each key now stores a request fingerprint
(sha256 of method, path, query string, raw body), and the engine refuses to
answer a reused key with a snapshot that belongs to a different request.
(Under the old code an expired key never re-armed — every retry past 24 h
re-executed the write with no dedup at all, and a reused key silently returned
the unrelated stored response.)

**What breaks.**

1. **Same key + different request body → `409 CONFLICT`** (code `CONFLICT`),
   where it previously returned the first request's stored response with the
   original status. A client that reuses one key for distinct writes now gets
   an error instead of a silent wrong answer. Correct retry behavior — resend
   the identical request — is unaffected; note the fingerprint is over the raw
   bytes, so a retry must not re-serialize JSON with different key order or
   whitespace.
2. **Replaying `POST /auth/pat` → `409 CONFLICT`**, previously `201` with the
   full body *including the plaintext token*. The response carries a one-time
   secret, and with permanent keys a stored snapshot would keep the plaintext
   in the database forever (bug 2.4). The key is still claimed, so concurrent
   retries cannot double-mint; on a `409` after a timeout, mint a fresh token
   with a new key and revoke strays via `DELETE /auth/pat/{id}`.
3. **Replays no longer expire.** A retry sent days later returns the original
   stored response instead of re-executing the write. If a client deliberately
   relied on key expiry to "re-send" an old request, it must use a new key.

**What the client must do.** Nothing, if it already follows the contract (one
fresh UUID per intended write, byte-identical retries). Otherwise: treat `409`
on a write as "this key is spent — inspect, then re-issue with a new key", and
handle the PAT-create case above.

**Engine references.** `sql/026_permanent_idempotency_keys.sql`,
`app/helpers/idempotency.py`, `docs/engine-spec.md` ("Idempotency" and
`POST /auth/pat`).

---

## 2026-08-06 — "today" for rate lookups is the user's `display_timezone`, not UTC

**Engine change.** Bloat audit 2026-08-06, Correctness §7, owner decision.
Current-date exchange-rate lookups (account balances on `/accounts` and
`/dashboard`, and the `GET /exchange-rates` default) resolved "today" as the
UTC date, while `/reports/monthly` and the dashboard month bounds already used
the date in the user's `display_timezone` — so between local midnight and UTC
midnight (7pm–midnight in `America/Lima`) a balance and a report could use
rates from different days. One helper (`exchange_rate.rate_lookup_date`) now
owns the definition, in `display_timezone`, with the same junk-timezone → UTC
fallback the reports use.

**What breaks.** The default for the optional `date` query param on
`GET /exchange-rates` changed: omitted `date` now means "today where the user
is" rather than "today in UTC". Near midnight the two differ by a day and can
resolve to a different carried-forward rate. A client wanting the old behavior
passes `date` explicitly. Consequentially (not breaking, but visible):
`current_balance_home_cents` on `/accounts` and `/dashboard` can shift by one
rate-day near midnight — and now always agrees with the monthly report about
which day "today" is. Explicit-`date` lookups and all report figures are
unchanged.

**What the client must do.** Nothing, unless it depended on the UTC default —
then pass `?date=` explicitly.

---

## 2026-08-06 — every request model now fails closed: unknown fields 422 on all writes

**Engine change.** Bloat audit 2026-08-06, Correctness §2. `extra="forbid"` was
copy-pasted onto 11 request models and missing from 10; all request models now
inherit a single `StrictModel` base (`app/schemas/__init__.py`), so every write
endpoint rejects unknown fields with `422 VALIDATION_ERROR` instead of silently
dropping them (fail-closed: unknown input must 422).

**What breaks.** A client sending retired or misspelled keys on these
previously-leaky shapes now 422s:

- `PUT /accounts/{id}`
- `POST /categories`, `PUT /categories/{id}`
- `POST /hashtags`, `PUT /hashtags/{id}`
- `POST /auth/pat`
- `POST /inbox/{id}/promote`
- `POST /transactions/batch` (top-level body; per-item junk already 422'd)
- junk **inside the nested `transfer` object** on `POST /transactions`,
  `POST /inbox`, `PUT /inbox/{id}` — Pydantic config does not propagate into
  nested models, so `transfer.{unknown}` was dropped even where the parent
  already forbade extras

The error names the offending key in `fields`, nested keys as dotted paths
(`transfer.bogus`, `transactions.0.bogus`).

**What the client must do.** Audit payload builders for the routes above and
stop sending anything not in the documented request shape. No well-formed
request changes.

---

## 2026-08-06 — reconciliation simplification: chaining and manual ordering deleted, beginning balance required, `difference_cents` added

**Engine change.** WP6 of the deletion program (`sql/025`; program docs in git history). Reconciliation chaining rewrote a
COMPLETED row's `beginning_balance_cents` whenever an upstream draft was edited —
the cascade had no status predicate — so the derived-beginning-balance concept was
deleted at the root, and `sort_order` (whose second job was defining the chain)
went with it. **Engine-only by owner decision** — the CLI work below is recorded,
not yet done, so the affected CLI commands and the TUI's default create path break
until it lands.

**1. `POST /reconciliations` requires `beginning_balance_cents`.** Omitting it —
previously the way to opt into chained mode — is now a `422`. There is no derived
mode and no prefill: both balances are typed off the statement. Both request
schemas are now `extra="forbid"`, so a body still carrying `sort_order` or
`beginning_balance_source` also `422`s (previously a silent drop — bug 5.3).

**2. Three response fields removed, one added.** `beginning_balance_source`,
`chained_from_reconciliation_id` and `sort_order` are gone from every
reconciliation response. Added: `difference_cents` — `(ending − beginning)` minus
the signed sum of the assigned non-deleted transactions, computed at read time,
zero when the batch adds up. Native currency, present on list rows and detail.

**3. `PUT /accounts/{id}/reconciliations/order` is gone** (404), and
`recalculated_count` with it. Account-scoped `GET /reconciliations` now orders by
`date_start ASC NULLS LAST, created_at ASC` — a reconciliation is a statement
period, so its date is its position. Undated rows sort last. The cross-account
list stays `created_at DESC`.

### What the CLI must do (not yet done — engine-only scope)

| Location | Required change |
|---|---|
| `expense/tui/screens/reconciliations.py:323-490` | **The default create path is broken:** the new-batch form starts at `source: "chained"` (`:328`), sends `beginning_balance_source` (`:478-480`) and omits the balance when chained — every TUI create now 422s. Make `begin` always-required and drop the source picker (`:311-318`, `:339-348`, `:433-441`). |
| `expense/tui/screens/reconciliations.py:71-74,265-287` | Delete `_sort_key` (client-side `sort_order` sort — the field no longer arrives) and the `ctrl+↑/↓` reorder actions calling the deleted route; rely on server order. Docstring `:1-22` describes the chain model. |
| `expense/commands/reconcile_cmd.py:646-770,807-891` | Delete the `move` and `reorder` commands whole — both end at the deleted route. `expense/_editor.py`'s only consumer is `reorder` (per `docs/cli-runtime.md:42`). |
| `reconcile_cmd.py:40-45,133-138,158-168,187,201,256-259,346-360,368-371,392-394,421-425,434-437,450,614-643` | Drop `--source`/`--sort-order` flags and their mutual-exclusion guards, the `Source` column and `_format_source_marker`, the chained-ambiguity 422 sniffer, `ReconciliationSource`, and `_render_reorder_response`. Make `--beginning-balance` required on `create`. |
| Anywhere rendering reconciliations | Optionally surface the new `difference_cents` — it is the add-up check the feature exists for. |
| Tests | `tests/unit/test_cmd_reconcile.py`, `test_tui_reconciliations.py`, `test_tui_reconcile_detail.py`. |
| Docs | `cli-spec.md:105-114` (sort contract, `move`/`reorder`, `--source`), `roadmap.md:170-186`, `tui-plan.md` chain references. |

---

## 2026-08-06 — schema slimming: 16 dead columns and the 4 category/hashtag archive routes deleted

**Engine change.** WP5 of the deletion program (`sql/024`; program docs in git history). Everything the 2026-08-04 audit
traced to zero readers is gone. **Engine-only by owner decision** — the CLI work
below is recorded, not yet done, so the affected CLI commands break until it lands.

**1. Six settings fields deleted, and `PUT /auth/settings` now fails closed.**
`theme`, `start_of_week`, `transaction_sort_preference`, `sidebar_show_bank_accounts`,
`sidebar_show_people`, `sidebar_show_categories` are gone from `user_settings`, from
`UserSettingsResponse`, and from `SettingsUpdateRequest`. The auth request schemas
are now `extra="forbid"`, so a caller still sending any of them — previously a
silent drop — gets a `422`. `display_timezone` is the one mutable settings field
left, and it is now **validated on write**: a value that is not an IANA zone name
`422`s on both `PUT /auth/settings` and `POST /auth/bootstrap` (it feeds
`AT TIME ZONE` on every report read, where a bad value would 500).
`deleted_at` is also gone from settings responses — a settings row is never
soft-deleted, so the field was permanently `null`.

**2. The four archive routes are gone and `is_archived` is dropped from categories
and hashtags.** `POST /categories/{id}/archive`, `/unarchive`, and the hashtag pair
now `404`. `is_archived` is absent from category and hashtag responses.
`?include_archived=` on `GET /categories` / `GET /hashtags` is no longer a declared
parameter — FastAPI ignores unknown query params, so old callers get a **silent
`200` with the full active list**, not an error. Archiving those two resources was
redundant with soft delete, which already hides a row from pickers while keeping
history intact. **Accounts keep their archive** — an archived account still holds
real money; nothing changes there.

**3. Dead wire fields removed.** `email` is gone from user responses (its populator
was the JWT claim deleted 2026-08-03; PAT auth has no email source, so the field
was permanently `null` — a field that lied). `actor_type` is gone from
`GET /activity` (every writer ever passed `"user"`). `parent_transaction_id` is
gone from transaction responses (never written; a placeholder for unbuilt splits).
None of these have a replacement; clients delete their references.

### What the CLI must do (not yet done — engine-only scope)

| Location | Required change |
|---|---|
| `expense/commands/auth_cmd.py:207-248` | Delete the six `--theme` / `--start-of-week` / `--transaction-sort-preference` / `--sidebar-show-*` options, their payload entries, and the docstring example. **Until then, `expense auth settings` with any of them returns 422.** |
| `expense/commands/categories_cmd.py:261-300`, `hashtags_cmd.py:237-275` | Delete the `archive` / `unarchive` commands (keep `run_toggle` and the accounts pair). They now 404. |
| `expense/tui/screens/_base.py:402,430-433,495-513` | Gate the `a` archive binding to the Accounts screen; the categories/hashtags screens lose it (and the system-category `check_action` branch). |
| `categories_cmd.py:52,62`, `hashtags_cmd.py:46,54`, `tui/screens/categories.py:14,20-28,38,48`, `tui/screens/hashtags.py:13,20-23,42` | Drop the `Archived`/`Status` columns and archived-dim styling; the field no longer arrives. |
| `_resource.py:262,272`, `quick_log.py:213-222`, `import_/apply.py:56-63` | Drop `include_archived` for categories/hashtags (keep for accounts); drop the `not c.get("is_archived")` suggestion filters. |
| `tui/screens/system.py:165` | Drop the `email` row (it already disappears via the `None` filter; this is tidying). |
| Tests | `test_cmd_auth`, `test_cmd_categories`, `test_cmd_hashtags`, `test_tui_categories_hashtags`, `test_tui_manage_actions`, `test_resource`, `test_tui_quick_log`, `test_tui_paging`, contract `test_resources_lifecycle`, and the `command_surface` docstring-example validator. |
| Docs | `cli-spec.md` (flags, archive verbs, `--include-archived`), `roadmap.md`, superseding notes on `decisions.md` "Archive is a prompt-free toggle" and the system-category `a`-key entry. |

---

## 2026-08-06 — `GET /v1/sync` deleted; `sync_checkpoints` dropped; `X-Client-Id` no longer read

**Engine change.** WP4 of the deletion program (program docs in git history). The delta-sync endpoint is gone: `app/routers/sync.py`,
`app/helpers/sync.py`, the `sync_checkpoints` table, the seven `(user_id, updated_at)`
indexes that served it (`sql/023`), and the `X-Client-Id` header handling. Requesting
`GET /v1/sync` now returns `404`. The substrate stays untouched — `version`, `updated_at`,
`deleted_at` tombstones, client-generated UUIDs — so a future mobile client can rebuild
sync additively (and without open bug 3.1, which this deletion closes).

**What breaks, and what absorbed it.** WP4's package text claimed the CLI never called
`/sync`; that was wrong — the CLI's cache-by-default read path was built on it. The CLI
was de-cached in a companion change that landed the same day, *before* this deletion:
its replica, `expense sync`, `--no-cache` / `--no-sync-after`, and the config `client_id`
are all deleted, and every read is a live engine call (CLI repo, `docs/decisions.md`
"Delete the local replica"). Clients may keep sending `X-Client-Id`; the engine ignores
unknown headers.

**This also retires the gap recorded in the WP3 entry below** ("an account's balance can
change without the account appearing in the next `/sync` delta") — there is no delta to
fall out of.

---

## 2026-08-06 — account balances are computed, not stored (wire shape unchanged; one `/sync` behaviour change)

**Engine change.** `expense_bank_accounts.current_balance_cents` was a stored running
total, updated by hand at eleven write sites. `sql/022` drops the column. An account's
balance is now the signed sum of its non-deleted transactions, computed at read time
(`app/helpers/account_balance.py`). This is WP3 of the deletion program (program docs in git history).

**Nothing breaks on the wire, and that is deliberate.** `current_balance_cents` appears
on exactly the same responses, in the same place, with the same type and the same
values: `GET /accounts`, `GET /accounts/{id}`, every account mutation response,
`/dashboard`'s three account panels, and `/sync`. Only its source changed. The CLI needs
no work — it reads the field off responses and never computed it. This entry exists
because the change is large internally, not because it costs a client anything.

**The one behaviour change, and it affects `/sync` only.** Until now, writing a
transaction bumped `updated_at` on the affected account row — a side effect of the
balance `UPDATE`. That is what re-entered the account into the next `/sync` delta
carrying its new balance. Nothing writes the account row on a transaction now, so:

> **An account's balance can change without the account appearing in the next
> `/sync` delta.** The value is never wrong when it *is* delivered; it just stops
> being pushed on every ledger write.

A client caching balances from `/sync` alone would show a stale figure until some
unrelated account edit. No client is affected today — `sync_checkpoints` holds zero
rows, no client has ever completed a sync, and the CLI reads balances from `/accounts`
and `/dashboard`, which are always live. WP4 (the entry above) deletes `/sync` outright and
retires this gap; it is written down rather than left to be rediscovered.

If you do need balances from a delta in the meantime, derive them client-side — `/sync`
ships every transaction, which is the same input the engine sums.

---

## 2026-08-05 — currency converts at read time; `exchange_rate` and every `amount_home_cents` deleted; report aggregates are home-only and nullable

**Engine change.** Three columns stored a currency conversion frozen at write time:
`expense_transactions.amount_home_cents`, `expense_transactions.exchange_rate`, and
`expense_transaction_inbox.exchange_rate`. A derived value with a second source of
truth goes stale, and both of them had: an inbox draft captured without a date kept
the column's `DEFAULT 1.0`, so a $100 receipt promoted as 100 PEN cents (open bug
1.4); and moving a transaction to an account in another currency never re-rated it,
because the trigger keyed on `date` and the *account* decides the currency (1.5).

`sql/021` drops all three. Conversion is now a read-time lookup of the rate for the
row's date — carried forward from the most recent rate on or before it, cast in the
user's `display_timezone` — implemented once in `app/helpers/home_currency.py`.

This is WP2 of the deletion program (program docs in git history). It also closes open bug 2.3 (a cross-tenant account read)
by deleting the helper that had it.

**Severity: breaking, and there is real work to do.** Unlike the WP1 entry, the CLI
both sends and renders the affected fields.

### What breaks

**1. `exchange_rate` is rejected on every write, with `422`.** Not ignored — the four
request schemas that carried it now set `extra="forbid"`, so a client still sending it
gets `{"code": "VALIDATION_ERROR", "fields": {"exchange_rate": "Extra inputs are not
permitted"}}`. On the batch endpoint the key is nested: `transactions.0.exchange_rate`.

**2. `amount_home_cents` and `exchange_rate` are gone from every transaction
response** — `GET/POST/PUT /transactions`, `/transactions/batch`, `/sync`, and the
embedded transaction list on reconciliation detail. **Absent, not null.**

**3. The inbox loses `amount_home_cents`, `transfer_amount_home_cents` and
`exchange_rate`.** Same rule: an inbox draft belongs to one account, so it has one
currency and nothing to convert.

**4. Reconciliations lose `beginning_balance_home_cents` and
`ending_balance_home_cents`.** A reconciliation is scoped to one account. This was the
"known inconsistency, deliberately left" in `docs/currency-model-decision.md`.

**5. The native cross-account aggregates are deleted, not nulled.** `spent_cents` per
category and per hashtag combination, and `inflow_cents` / `outflow_cents` /
`net_cents` on month totals, no longer exist on `/dashboard` or `/reports/monthly`.
`GROUP BY category_id` has no currency partition, so a category holding $15 and S/25
reported `4000` — a number in no currency. **Only the `_home_cents` forms remain.**

**6. Every remaining home aggregate is nullable, and carries an `unconverted_count`.**
When any row in a group has no resolvable rate, the figure is `null` and the count
says how many rows are behind it. `spent_home_cents`, the three month totals, and each
`hashtag_breakdown` row all follow this. A client that assumes an integer will crash.

⚠️ **A `null` here is not "zero" and not "missing".** It means the engine refused to
report a partial total. Render it as unavailable, with the count — never as `0`, and
never by falling back to a native figure, which is the exact bug being deleted
(`COALESCE(amount_home_cents, amount_cents)` read USD cents as PEN cents, a 3.58×
understatement).

**7. `/dashboard` loses `archived_categories` and `archived_hashtags`.**
`archived_accounts` stays — an archived account still holds real money; an archived
category holds only history, and soft delete already hides a row from pickers.
`?include_archived=true` now controls the accounts panel alone.

**8. A cross-currency transfer no longer nets to zero.** `@Transfer` shows the FX
spread. Send $1,000 and receive S/3,450 on a day the market rate is 3.58 and the
dollars were worth S/3,580 — `@Transfer` reports **−S/130**, the spread the bank
charged, which the old write-time rule hid by assigning both legs the same home value.
So a non-zero `@Transfer` means one of exactly two things: an FX spread, or a
loan/repayment with a person. **There is no `@FX` category** — owner decision,
2026-08-05, superseding the closing bullet of the entry below.

**9. `422 RATE_UNAVAILABLE` no longer exists.** The write path performs no rate
lookup, so a cross-currency transaction is recordable while the FX job is stale, and
one dated before the provider floor (2024-03-02) is recordable at all. Any client
branching on that code is branching on something unreachable.

### What the CLI must do

| Location | Current behaviour | Required change |
|---|---|---|
| `expense/commands/log_cmd.py:34,94` | `--exchange-rate` option, sent in the payload | **remove** — the request now `422`s |
| `expense/commands/inbox_cmd.py:226,278` | `--exchange-rate` on add and update | **remove** |
| `expense/commands/transactions_cmd.py:305` | `--exchange-rate` on update | **remove**; also update the help text at `:52`, which lists it among the transfer-leg read-only fields |
| `expense/commands/accounts_cmd.py:248` | `--exchange-rate` on opening balance | **remove** |
| `expense/import_/apply.py:198-216` | sends `exchange_rate` in the payload | **stop sending it** |
| `expense/import_/parse.py:206-213` | skips USD rows with no rate (`usd-no-rate`) | **delete the skip** — a USD row lands on the USD account and needs no rate to be recorded |
| `expense/commands/reports_cmd.py:42,53,112,121,126` | reads `spent_home_cents` / `net_home_cents` as numbers | **handle `null`** and surface `unconverted_count` |
| `expense/tui/screens/home.py:105-111` | reads `net_home_cents`, `outflow_home_cents` | **handle `null`** |
| `expense/tui/screens/outstanding.py:54` | `totals.get(f"{key}_home_cents")` | **handle `null`** |
| `expense/commands/_resource.py:453,461` | derives `_home_cents` from a native key | the native aggregate keys are gone — read the home keys directly |
| `dashboard` command | renders archived category / hashtag panels | **remove both**; `archived_accounts` is unchanged |
| `expense/tui/screens/home.py:105-107` | `current_balance_home_cents` | **none** — account balances keep their home value |

### What does *not* change

- **`current_balance_home_cents` on accounts and dashboard accounts stays.** The
  account list is the only surface showing all your money at once, and reading
  `S/8,500` beside `$1,200` with no common unit is what makes it unusable. It is still
  computed at today's rate and still `null` when no rate resolves.
- Native `amount_cents` on every record — still there, still always positive, still
  with `transaction_type` carrying direction.
- `?debit_as_negative=true` — still a display preference. There is simply one amount
  to flip now instead of two.
- Transfers stay visible in dashboards and reports and are never excluded from totals.
  Same-currency transfers still cancel to exactly zero.
- The engine remains the only thing that converts currency. Clients never compute it —
  that part is absolute, and removing the per-row rate makes it enforceable rather than
  merely stated.

### Engine references

- `sql/021_read_time_currency.sql` — the migration and why a stored conversion never
  held a fact
- the WP2 work package (`WP2-read-time-currency.md`, in git history)
- `docs/currency-model-decision.md` — the design record, amended to match what shipped
- `CLAUDE.md` § "Home currency" — rewritten in this change
- `docs/open-bugs.md` — 1.4, 1.5 and 2.3 deleted; 1.7 and 6.1 amended; **6.5 added**
- `tests/test_wp2_read_time_currency.py` — the new invariants, including the
  unconvertible contract and the FX spread
- `tests/test_home_currency_parity.py` — why the SQL and Python conversions still agree

---

## 2026-08-05 — `transaction_type` is direction on every row; `transfer_direction` deleted; `transaction_type = 3` retired

**Engine change.** `transaction_type` was carrying two unrelated facts: which way
money moved (1 = expense, 2 = income) and who the counterparty was (3 = transfer).
Because `3` occupied a slot in what is otherwise a direction column, direction for
transfers lived in a *second* column, `transfer_direction` (1 = debit, 2 = credit),
meaningful only when the first column held one specific value.

`sql/020` collapses them. `transaction_type` is now **1 = outflow, 2 = inflow**, on
every row, never null, with `CHECK (transaction_type IN (1, 2))` and
`CHECK (amount_cents > 0)`. `transfer_direction` is dropped from
`expense_transactions` *and* `expense_transaction_inbox`. **A transfer is two
ordinary rows paired by `transfer_transaction_id`** — that FK is now the only
discriminator.

This is WP1 of the deletion program (program docs in git history). It also closes open bugs 1.3 (every USD→USD transfer
returned an uncaught 500) and 6.3's `expense_transactions` half.

**Severity: breaking, but nothing in the CLI to break.** Verified by reading
`expense_world_CLI`: **`transaction_type` and `transfer_direction` appear zero times
in that repo.** Neither field is read, branched on, cached, or sent. The CLI already
detects transfers exactly the way the engine now requires.

### What breaks

**1. `transfer_direction` is gone from every response.** It disappears from
`GET/POST/PUT /transactions`, `/inbox`, and `/sync`. It was never accepted on a
request, so no write contract changes.

**2. `transaction_type` never returns `3` again.** A transfer's outgoing leg
returns `1`, its incoming leg `2` — the same values an ordinary expense and income
return. Any client branching on `== 3` to mean "transfer" silently stops matching.

**3. Transfer detection moves to `transfer_transaction_id != null`.** This is the
only supported discriminator. It is not new — the column has always been there and
has always been reciprocal on both legs.

**4. A same-currency transfer's legs are now distinguishable only by
`transaction_type` + `transfer_transaction_id`.** Previously `transfer_direction`
answered "which leg is this"; `transaction_type` answers it now.

⚠️ **Hard requirement on the engine side, stated so it is not lost:**
`transfer_transaction_id` must keep being emitted at top level on `/sync` and on the
transaction GET/list bodies. It is load-bearing for the CLI at
`expense/tui/screens/quick_log.py:171-174`, which uses it to lock `amount`/`account`/
`date` on a transfer leg. If it ever stops being emitted the lock silently stops
applying and the user gets an engine `422` on save instead of faded fields.

### What the CLI must do

| Location | Current behaviour | Required change |
|---|---|---|
| `expense/cache/db.py:142` | stores `transfer_transaction_id`, never queried | **none** |
| `expense/cache/sync.py:51` | extracts `transfer_transaction_id` from `/sync` rows | **none** |
| `expense/tui/screens/quick_log.py:171-174` | locks fields when `rec.get("transfer_transaction_id")` | **none** — already the correct discriminator |
| `expense/commands/log_cmd.py:101-108`, `quick_log.py:478-489` | send a nested `transfer` object | **none** — request shape unchanged |
| `transactions get`, `reconcile get` | dump every key of the response generically | **none** — two fewer lines in the human dump |

**Nothing is required.** The entry exists because the wire shape changed, not
because there is work to do.

### What does *not* change

- The request contract. `amount_cents` is still signed on the way in, transfers are
  still identified by the presence of a `transfer` object, and callers still never
  set `transaction_type`.
- `amount_cents` in responses — still always positive.
- `?debit_as_negative=true` — still a display preference, still flips the outflow
  leg and, on an inbox transfer draft, still flips the two legs opposite ways.
- Transfer legs still cancel, and transfers are still included in dashboards and
  reports.
- Cross-currency transfers still net to exactly zero in home currency. The FX spread
  becomes visible in WP2 (the read-time currency entry above) — deliberately not in this change.
  ⚠️ *Superseded the same day:* this bullet said the spread would arrive "together
  with the `@FX` category that will hold it". It did not. The owner chose to leave the
  spread in `@Transfer`; see the entry above and `docs/currency-model-decision.md`.

### Engine references

- `sql/020_transfer_direction_collapse.sql` — the migration, and why dropping
  `sql/019`'s column is not a reversal of it
- the WP1 work package (`WP1-transfer-collapse.md`, in git history)
- `CLAUDE.md` § "Sign convention" — rewritten in this change
- `docs/open-bugs.md` — 1.2 and 1.3 deleted; 6.3 amended
- `tests/test_wp1_transfer_collapse.py` — the new invariants, including the USD→USD
  regression
- `tests/test_inbox_transfers.py` — still the inbox transfer contract, end to end

---

## 2026-08-03 — Inbox transfers carry `transfer_direction`; `transfer_amount_cents` is now positive

> ⚠️ **Superseded 2026-08-05** — `transfer_direction` was itself deleted two days
> later by the transfer collapse (`sql/020`; entry above). Direction now lives on
> `transaction_type`, and a transfer is detected via `transfer_transaction_id !=
> null`. Read this entry for the positive-amounts change only; do not build
> against the column it introduces.

**Engine change.** The inbox stored no direction column, so the *sign* of
`transfer_amount_cents` was the only record of which way a transfer draft
pointed. `sql/019` adds `transfer_direction` to `expense_transaction_inbox` and
stores both amounts positive, matching `expense_transactions` exactly. The
signed value the client sends is unchanged — only the stored and returned
encoding moves.

This closes audit findings **WP7.2**, **WP7.3** and the inbox half of **WP10.2**.
The defect that forced it: the primary leg's sign was discarded by `abs()` on
write and re-derived at promote time as the negation of the sibling's, so
`create_transfer_pair`'s opposite-sign guard was unreachable — a draft saved as
two outflows promoted cleanly with one leg silently flipped.

**Severity: breaking, but nothing in the CLI to break.** `expense/commands/inbox_cmd.py`
contains zero occurrences of "transfer" and its `promote` (`:377`) never sends
`transfer_id`, so the CLI cannot create or promote an inbox transfer today. The
work below is to *add* the feature against the corrected shape, not to repair
existing code.

### What breaks

**1. `transfer_amount_cents` on inbox responses is now always positive.**

Any client reading its sign to decide direction must read `transfer_direction`
instead — `1` = debit (the inbox row's own account pays), `2` = credit (it
receives). The sibling's direction is always the inverse. This affects
`GET /inbox`, `GET /inbox/{id}`, `GET /sync`, and the `before_snapshot` /
`after_snapshot` payloads on `GET /activity`.

**2. `transfer.id` is no longer accepted on `POST /inbox` or `PUT /inbox/{id}`.**

The inbox has its own request model without it. The field was required by the
schema and then discarded — it is the sibling *ledger row's* UUID, and no ledger
rows exist at draft time. The sibling's id is still supplied at promote time as
`transfer_id`. Note `POST /transactions` is **unchanged** and still requires
`transfer.id`; the two endpoints now take deliberately different shapes.

**3. A contradictory transfer draft now returns `422` instead of being accepted.**

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Transfer validation failed.",
    "fields": {
      "transfer.amount_cents": "Must have opposite sign to amount_cents."
    }
  }
}
```

Previously `{"amount_cents": -6000, "transfer": {"amount_cents": -1500}}` was
stored, listed as ready, and promoted with the primary silently rewritten to
`+6000`.

**4. `POST /inbox/{id}/promote` gained two `422` conditions.**

- `transfer_id` supplied on a non-transfer item — `{"transfer_id": "Must be null for non-transfer promotions."}`. Previously discarded in silence; spec §383 always required this.
- `transfer_account_id` pointing at a deleted or archived account — `{"transfer.account_id": "Must reference an active, non-archived account."}`. Previously surfaced only from deep inside the transfer engine, after the item had already passed readiness.

**5. `GET /inbox?ready=true` now returns transfer items it previously hid.**

Transfers have no `category_id` and the filter required one unconditionally, so
every promotable transfer draft was invisible. Conversely, items whose sibling
account is archived are now excluded — they would have `422`'d on promote. A
client that assumed "ready ⇒ has a category" must stop.

⚠️ **`?debit_as_negative=true` now flips the sibling too.** On a transfer the two
legs are returned with opposite signs. Previously `transfer_amount_cents` was
emitted as-stored beside a flipped primary, rendering a transfer as two amounts
pointing the same way.

### What the CLI must do

| Location | Current behaviour | Required change |
|---|---|---|
| `expense/commands/inbox_cmd.py` | No transfer support at all — no `--transfer-account-id` / `--transfer-amount` on `inbox create` / `inbox update` | **Add them.** Send `transfer: {account_id, amount_cents}` with a signed amount and **no `id`**. |
| `expense/commands/inbox_cmd.py:377` | `promote` sends `{"id": new_transaction_id}` only | **Send `transfer_id`** when the item has `transfer_account_id`. Without it a transfer item now `422`s with a clear field error rather than failing obscurely. |
| `expense/tui/screens/inbox.py:141-153` | `action_promote` calls `confirm_write` with no body; `_base.py:476-486` forwards no body argument | **Pre-existing break, unrelated to this change** — `InboxPromoteRequest.id` is required, so the TUI promote cannot have worked. Fix while adding `transfer_id`. |
| `expense/cache/db.py` / `expense/cache/sync.py` | Cached inbox table drops the transfer columns entirely | Add `transfer_account_id`, `transfer_amount_cents`, `transfer_direction` if inbox transfers are to render from cache. |
| `expense/tui/screens/inbox.py` (rendering) | n/a | When showing a draft, read `transfer_direction` for the arrow, never the amount's sign. |

### What does *not* change

- **Request signs are untouched.** `amount_cents` and `transfer.amount_cents` are
  still signed on the wire, still negative-for-outflow. Only storage and
  responses changed.
- `POST /transactions` and every ledger response — identical, including
  `transfer.id`.
- No amounts, balances or promoted transactions changed. Both tables held 0 rows
  when this shipped, so there is nothing to re-sync.

### Engine references

- `sql/019_inbox_transfer_direction.sql` — the column, backfill and constraints
- `docs/engine-spec.md` §`POST /inbox`, §`POST /inbox/{id}/promote`, §Transfers
- `docs/schema-reference.md` §`expense_transaction_inbox`
- `docs/open-bugs.md` WP7.2, WP7.3, WP10.2
- `tests/test_inbox_transfers.py` — the contract, end to end

---

## 2026-08-01 — Home currency locked to PEN; `main_currency` no longer updatable

**Engine change.** The home currency is fixed at **PEN** and cannot be changed.
`sql/018` adds `CHECK (main_currency = 'PEN')` on `user_settings`, and the
home-currency recalculation helper was deleted (it carried a silent `1.0`
exchange-rate fallback that wrote wrong home amounts — audit finding WP1.1).

**Severity: breaking.** One CLI feature stops working; one response field
disappears.

### What breaks

**1. `PUT /v1/auth/settings` with `main_currency` now returns `422`.**

Previously `200` plus a full-ledger recalculation. Now:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Home currency cannot be changed.",
    "fields": {
      "main_currency": "The home currency is locked to PEN and is not updatable."
    }
  }
}
```

⚠️ **This includes setting it to its current value.** `{"main_currency": "PEN"}`
is *also* a `422`. The field is not updatable at all — there is no
"it works if you don't actually change it" case. This is deliberate: a
sometimes-accepted field is worse to code against than a never-accepted one.

Other fields in the same request are unaffected — `{"theme": 2}` still returns
`200`. But do not send `main_currency` alongside them, or the whole request
fails.

**2. The `recalculation` response field is removed from `UserSettingsResponse`.**

It is **absent**, not `null`. This is an intentional exception to the
null-over-omission rule: the field described an operation that can no longer
occur, so leaving it permanently `null` would be dead weight in every settings
response forever.

### What the CLI must do

| Location | Current behaviour | Required change |
|---|---|---|
| `expense/tui/screens/system.py:245-253` | `ConfirmModal` reading *"This triggers a home-currency recalculation across your transactions on the engine."* | **Remove the flow.** The copy describes a deleted feature. |
| `expense/tui/screens/system.py:255` | `PromptModal("Main currency", "USD or PEN")` | Remove — nothing to choose. |
| `expense/tui/screens/system.py:280-286` | `_set_currency()` PUTs `main_currency` | Remove. Currently always errors (safely — the error path skips the config write, so `~/.expense-config` is not corrupted). |
| `expense/tui/screens/system.py:288-300` | `_currency_saved()` mirrors the value into config | Remove with the above. |
| `expense/tui/screens/system.py:111, 210` | Displays main currency | **Keep** — still a valid read-only field, always `PEN`. |
| `expense/commands/auth_cmd.py:213` | `--main-currency` option on `auth settings` | **Remove the option.** Passing it now guarantees a `422`. |
| `expense/commands/auth_cmd.py:234` | Docstring: *"main_currency change triggers engine recalc"* | Correct the text. |
| `expense/commands/auth_cmd.py:50-53` | Reads `body.get("recalculation")` | Dead code — remove. Degrades safely (`.get` → `None`), so this is cleanup, not a crash fix. |
| `expense/commands/auth_cmd.py` (`_render_recalc_summary`) | Renders the recalc summary | Dead — remove if it has no other caller. |
| `expense/config.py:40` | `main_currency: str \| None` cached locally | **Keep.** Still populated from `GET /auth/me` (`_cache_main_currency`, `auth_cmd.py:84-87`). It just never changes now. |

### What does *not* change

- `main_currency` is still returned on every settings/bootstrap response. Read it,
  cache it, display it — only writes are refused.
- Multi-currency **accounts** are unaffected. USD accounts still work exactly as
  before; USD amounts are still converted to PEN for reporting.
- No amounts, balances, or transaction shapes changed. Nothing needs re-syncing.

> ✅ **Resolved 2026-08-06 — every part of this forward-looking note has landed.**
> The deletion program (WP1–WP6, program docs in git history) removed
> `amount_home_cents` from transactions and inbox items, the reconciliation home
> balances, the native report aggregates, and the dashboard's archived
> category/hashtag panels — and separately dropped `transaction_type = 3` and
> `transfer_direction` entirely (WP1), which *does* change how a client identifies
> a transfer. Each change has its own dated entry above: WP1 on 2026-08-05, the
> currency half (WP2) on 2026-08-05, computed balances (WP3), `/sync` deletion
> (WP4), schema slimming (WP5), and reconciliation simplification (WP6) on
> 2026-08-06. (`current_balance_home_cents` on accounts survived after all —
> the balance surfaces are the one place a home conversion is a cross-currency
> figure.) Read those entries, not the bullets above, for the current wire shape.

### Engine references

- `sql/018_lock_home_currency_to_pen.sql` — the constraint and the restoration path
- `docs/engine-spec.md` §`PUT /auth/settings`
- `docs/open-bugs.md` — finding 1.1 of the 2026-08-01 audit (closed; in git history)
