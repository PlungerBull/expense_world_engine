-- 015: Lock global_currencies to USD + PEN at the schema level.
--
-- Phase 1 supports only USD and PEN. The seed in sql/004 already
-- contains exactly those two rows, and every currency-typed column
-- (expense_bank_accounts.currency_code, user_settings.main_currency,
-- exchange_rates.base_currency, exchange_rates.target_currency) FKs
-- into global_currencies, so the lookup table is the single chokepoint.
--
-- This CHECK promotes that policy from "implicit in the seed" to
-- "explicit in the schema": adding a third currency requires an
-- explicit migration that drops the constraint, which forces the
-- author to also revisit the cross-rate path that was removed in
-- this same change (see app/helpers/exchange_rate.py — the
-- get_pair_rate function no longer supports non-USD↔non-USD
-- conversions).
--
-- No data migration needed: the seed is already compliant.

ALTER TABLE global_currencies
    ADD CONSTRAINT global_currencies_phase1_only
    CHECK (code IN ('USD', 'PEN'));
