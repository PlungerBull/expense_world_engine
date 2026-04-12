-- Step 9 Part B: enable delta-sync for user_settings and add composite indexes
-- for every synced table's (user_id, updated_at) lookup.
--
-- user_settings was missing `version` and `deleted_at` columns, violating the
-- "every mutable table has version" convention documented in schema-reference.md.
-- Sync delta queries rely on `updated_at > $last_sync_at`, so without indexes
-- every sync becomes a full table scan per resource — fine for hundreds of rows,
-- painful at 1000+ users.

ALTER TABLE user_settings
    ADD COLUMN IF NOT EXISTS version integer NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_user_settings_user_updated
    ON user_settings (user_id, updated_at);

CREATE INDEX IF NOT EXISTS idx_expense_bank_accounts_user_updated
    ON expense_bank_accounts (user_id, updated_at);

CREATE INDEX IF NOT EXISTS idx_expense_categories_user_updated
    ON expense_categories (user_id, updated_at);

CREATE INDEX IF NOT EXISTS idx_expense_hashtags_user_updated
    ON expense_hashtags (user_id, updated_at);

CREATE INDEX IF NOT EXISTS idx_expense_transaction_inbox_user_updated
    ON expense_transaction_inbox (user_id, updated_at);

CREATE INDEX IF NOT EXISTS idx_expense_transactions_user_updated
    ON expense_transactions (user_id, updated_at);

CREATE INDEX IF NOT EXISTS idx_expense_transaction_hashtags_tx
    ON expense_transaction_hashtags (transaction_id, transaction_source)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_expense_reconciliations_user_updated
    ON expense_reconciliations (user_id, updated_at);
