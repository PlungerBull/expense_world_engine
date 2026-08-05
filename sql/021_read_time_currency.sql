-- 021: Stop storing currency conversions. Convert at read time instead.
--
-- Three columns stored a conversion frozen at write time:
--
--     expense_transactions.amount_home_cents
--     expense_transactions.exchange_rate
--     expense_transaction_inbox.exchange_rate
--
-- All three came from sql/003 and were never migrated since. Each is a derived
-- value with a second source of truth, and both sources drifted:
--
--   * open bug 1.4 -- the inbox column is `numeric DEFAULT 1.0` (sql/003:62) and
--     the engine only looks a rate up when BOTH account_id and date are present.
--     Capture a receipt with no date -- the normal case -- and the default
--     stands, so a $100 draft promotes as 100 PEN cents. It failed closed when
--     it looked and found nothing (422 RATE_UNAVAILABLE) and failed open when it
--     did not look at all.
--   * open bug 1.5 -- the re-rate trigger on PUT /transactions keyed on `date`
--     only, but the ACCOUNT decides the currency. Move a PEN row to a USD
--     account and it keeps rate 1.0 and its old home value forever.
--
-- Neither is a coding slip. Both are the same structural fact: a stored
-- derivation has to be re-derived by every path that touches an input, and
-- eventually one path forgets.
--
-- What replaces them: app/helpers/home_currency.py, whose LEFT JOIN LATERAL
-- takes the newest rate on or before the row's date (carry-forward), returns
-- the native amount for home-currency rows, and returns NULL -- never a
-- substituted native amount -- when no rate resolves. Change a row in
-- exchange_rates and every past report corrects itself; under the stored model
-- a bad rate was wrong forever.
--
-- The rate was never a fact anyway. A transaction belongs to one account and
-- the account governs the currency, so its home value is a reporting choice,
-- not a property of the world. The one case where a real rate exists -- a
-- cross-currency transfer -- already stores both native amounts, and the rate
-- is sibling.amount_cents / primary.amount_cents. See
-- docs/currency-model-decision.md, "The rate on a transaction was never a fact".
--
-- Consequence for writes, stated so it is not read as an oversight: the write
-- path now performs NO rate lookup, so it can no longer fail with 422
-- RATE_UNAVAILABLE. Recording what happened must never be blocked by a rate
-- lookup. Cross-currency writes stop failing when the FX job is stale, and
-- transactions dated before the provider floor (2024-03-02) become recordable.
--
-- Consequence for reads: a row whose date has no resolvable rate makes its
-- aggregate `null` plus a non-zero `unconverted_count`, never a partial total.
--
-- No index is added. home_rate_join's lateral filters base_currency +
-- target_currency and takes `ORDER BY rate_date DESC LIMIT 1`; the btree behind
-- sql/002:50's UNIQUE (base_currency, target_currency, rate_date) already
-- serves that scan exactly. A second index on the same columns would be dead
-- weight.
--
-- Data migration: every domain table held 0 rows on 2026-08-04, so dropping
-- these columns discards nothing. Written as plain DROPs because that is what
-- is correct wherever this runs -- there is no value here worth preserving even
-- against real data, since every one of them is recomputable from the rate
-- table and the account's currency.


-- ===========================================================================
-- expense_transactions
-- ===========================================================================

ALTER TABLE expense_transactions
    DROP COLUMN amount_home_cents;

ALTER TABLE expense_transactions
    DROP COLUMN exchange_rate;


-- ===========================================================================
-- expense_transaction_inbox
-- ===========================================================================

-- The DEFAULT 1.0 goes with the column. That default was one of the two
-- independent sources of bug 1.4 (the other was the conditional lookup in
-- helpers/inbox.py, deleted in the same change); leaving either behind would
-- have kept the bug reachable.
--
-- Note what is NOT touched here: inbox_transfer_fields_coherent and
-- inbox_transaction_type_valid (sql/020) both stand unchanged. This migration
-- removes a stored derivation; it does not relax any coherence rule. A
-- half-transfer draft is still unrepresentable in the database.
ALTER TABLE expense_transaction_inbox
    DROP COLUMN exchange_rate;
