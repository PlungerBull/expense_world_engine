-- 014: Add is_archived to expense_categories + expense_hashtags.
--
-- Mirrors the column already on expense_bank_accounts (sql/003).
-- Archive semantics match the account behaviour documented in
-- engine-spec.md §Bank Accounts: the row is hidden from default
-- pickers/lists but historical references remain intact and continue
-- to participate in reports.
--
-- Backfill is implicit: NOT NULL DEFAULT false applies to every
-- existing row. No data migration step required.

ALTER TABLE expense_categories
    ADD COLUMN IF NOT EXISTS is_archived boolean NOT NULL DEFAULT false;

ALTER TABLE expense_hashtags
    ADD COLUMN IF NOT EXISTS is_archived boolean NOT NULL DEFAULT false;
