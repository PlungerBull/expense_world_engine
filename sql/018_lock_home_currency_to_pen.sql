-- 018: Lock the home currency to PEN at the schema level.
--
-- sql/015 locked the *set* of currencies to {USD, PEN}. This locks the
-- *home* currency to PEN specifically. Multi-home-currency support is
-- retired: the engine converts to PEN and only to PEN.
--
-- Consequences already applied in the same change:
--   * PUT /auth/settings rejects main_currency with 422 (helpers/auth.py).
--   * app/helpers/recalculate_home_currency.py is DELETED — it existed
--     solely to rewrite amount_home_cents across the ledger when
--     main_currency changed, which can no longer happen.
--   * UserSettingsResponse no longer carries a `recalculation` field.
--
-- Reversing this is not just a matter of dropping the constraint. A future
-- author who wants a switchable home currency must also restore the
-- recalculation pass (git history: app/helpers/recalculate_home_currency.py,
-- deleted 2026-08-01) INCLUDING a fix for its silent 1.0 rate fallback, which
-- is why it was deleted rather than kept dormant. See WP1.1 in
-- docs/audit-2026-08-01-remediation-plan.md.
--
-- The main_currency column is deliberately KEPT and still read by ~10 call
-- sites. The policy lives here, at one chokepoint, rather than as a 'PEN'
-- literal scattered through the codebase.
--
-- Data migration: none expected — sql/002 defaults main_currency to 'PEN'.
-- If this migration fails, a settings row holds a non-PEN currency and its
-- ledger must be converted before the lock can be applied.

ALTER TABLE user_settings
    ADD CONSTRAINT user_settings_home_currency_pen
    CHECK (main_currency = 'PEN');
