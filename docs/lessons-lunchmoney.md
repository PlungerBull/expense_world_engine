# API Lessons — Lunch Money

> Lessons from studying Lunch Money's API design. Several of these directly validate decisions already made; others add net-new API conventions. See `api-design-principles.md` for the principles derived from these.

---

## 1. The `to_base` Pattern — Dual-Field Approach Validated

Every Lunch Money transaction carries both `amount` + `currency` (the native value) and `to_base` (the amount converted to the user's primary currency). The raw amount is immutable. The base-converted amount is the reporting currency. This dual-field approach is industry-standard.

**Validation:** Our `amount_cents` (native) + `exchange_rate` + `amount_home_cents` (home currency cache) follows this exact pattern. The design is correct.

---

## 2. `to_base` Must Propagate to Every Layer

Budget summaries expose `spending_to_base` and `budget_to_base` alongside native amounts. It is not enough to have a home-currency value at the transaction level. Every aggregated view — monthly summary, category totals, account balances, P&L — must consistently return a home-currency version of every amount.

**Our decision (API principle):** The engine is responsible for currency conversion at every level of aggregation. No API response containing an amount — transaction, category total, account balance, or summary — returns only native amounts without a corresponding `_home_cents` value. The frontend never performs currency conversion.

---

## 3. Historical Rates, Not Spot Rates — Validated

Lunch Money fetches exchange rates daily and uses the historical rate for each transaction's date. A February transaction and an August transaction each reflect the true rate of their respective day.

**Validation:** Our locked-rate-at-entry model is correct. Additionally, this surfaces a concrete engine behaviour to lock in: when a user corrects a transaction's date after creation, the engine automatically recalculates `amount_home_cents` using the historical rate for the new date. The user never manages this manually.

---

## 4. `external_id` for Idempotent Inserts

Transactions support a user-defined `external_id`, unique per asset, used to prevent duplicate entries when running the same import script twice. A stable `external_id` (e.g. a hash of the source row) makes imports safely re-runnable.

**Our decision (deferred):** Validates our deferred `import_id` concept. When CSV import is built, implement a structured, deterministic key per imported row. Deferred because our multiple-transaction-same-day collision problem needs more design thought first.

---

## 5. Transaction Grouping as a First-Class Concept

A transaction can represent a group of child transactions. The parent's `amount` reflects the totalled amount of its children — children carry the parent's `id` as their `group_id`. This is the proven design for one parent transaction linking multiple underlying expense records.

**Validation:** Our `parent_transaction_id` FK on `expense_transactions` follows this exact pattern. Design confirmed.

---

## 6. `debit_as_negative` — Sign Convention as a Caller-Side Flag

The update and fetch endpoints accept a `debit_as_negative` boolean. If true, negative values are expenses and positive values are credits. Sign conventions are not baked into the schema — the API layer interprets sign based on the flag. The frontend and CLI can each work in whatever convention is natural for them.

**Our decision (API principle):** The engine supports a `debit_as_negative` flag on relevant endpoints. The schema stores raw signed integers (`amount_cents`; negative = outflow, positive = inflow). The flag is an API-layer convenience — it does not affect storage.

---

## 7. v2 Dehydration — IDs Only, Cache Locally

In Lunch Money's v2 API, transaction objects no longer hydrate related objects like category names or account names — they return IDs only. Developers maintain a local cache of those objects. This reduces payload size and server load as the system scales.

**Our decision (future direction):** Phase 1 responses may include hydrated names alongside IDs for developer convenience (e.g. `category_name` next to `category_id`). As the system matures, the direction is IDs-only. No client code should assume hydrated names will always be present — always read the ID as the authoritative value.
