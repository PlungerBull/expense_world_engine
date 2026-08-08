-- 028: Account names get the rules categories/hashtags gained in sql/012.
--
-- Before this migration expense_bank_accounts still carried the original
-- table-level UNIQUE (user_id, name, currency_code), which sql/012 fixed
-- for categories and hashtags but not accounts:
--   * Case-SENSITIVE — "Rent" and "rent" coexisted as different accounts.
--   * Spanned soft-deleted rows — a deleted account's (name, currency)
--     stayed locked forever, and the create-path pre-check (which filters
--     deleted_at IS NULL) let the request through to the constraint, which
--     409ed with the wrong message ("An account with id … already exists").
--
-- This migration replaces the constraint with a partial unique index on
-- (user_id, LOWER(name), currency_code) WHERE deleted_at IS NULL, matching
-- the sql/012 shape. Belt-and-suspenders with helpers.reference_data's
-- name_taken check. Owner decision 2026-08-08 (bloat-audit 2026-08-06,
-- Correctness §6).
--
-- SAFETY: the DO block aborts loudly if active rows already collide
-- case-insensitively — merge or rename them first, never let the index
-- pick a survivor. (Checked clean on the live ledger before applying.)

DO $$
DECLARE bad integer;
BEGIN
  SELECT count(*) INTO bad FROM (
    SELECT 1
    FROM expense_bank_accounts
    WHERE deleted_at IS NULL
    GROUP BY user_id, LOWER(name), currency_code
    HAVING count(*) > 1
  ) dupes;
  IF bad > 0 THEN
    RAISE EXCEPTION
      'sql/028 aborted: % case-insensitive (name, currency) collision group(s) in expense_bank_accounts — rename or merge before applying', bad;
  END IF;
END $$;

ALTER TABLE expense_bank_accounts
    DROP CONSTRAINT IF EXISTS expense_bank_accounts_user_id_name_currency_code_key;

CREATE UNIQUE INDEX IF NOT EXISTS expense_bank_accounts_user_lower_name_currency_active
    ON expense_bank_accounts (user_id, LOWER(name), currency_code)
    WHERE deleted_at IS NULL;
