-- 005: Enable Row-Level Security on every table
-- Policy: users can only access their own rows via auth.uid() = user_id

-- users (id = auth.uid(), not user_id)
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
CREATE POLICY users_own_row ON users
    FOR ALL USING (auth.uid() = id);

-- user_settings
ALTER TABLE user_settings ENABLE ROW LEVEL SECURITY;
CREATE POLICY user_settings_own_row ON user_settings
    FOR ALL USING (auth.uid() = user_id);

-- global_currencies: readable by all authenticated users (static lookup)
ALTER TABLE global_currencies ENABLE ROW LEVEL SECURITY;
CREATE POLICY global_currencies_read ON global_currencies
    FOR SELECT USING (auth.role() = 'authenticated');

-- exchange_rates: readable by all authenticated users (reference data)
ALTER TABLE exchange_rates ENABLE ROW LEVEL SECURITY;
CREATE POLICY exchange_rates_read ON exchange_rates
    FOR SELECT USING (auth.role() = 'authenticated');

-- sync_checkpoints
ALTER TABLE sync_checkpoints ENABLE ROW LEVEL SECURITY;
CREATE POLICY sync_checkpoints_own_row ON sync_checkpoints
    FOR ALL USING (auth.uid() = user_id);

-- idempotency_keys
ALTER TABLE idempotency_keys ENABLE ROW LEVEL SECURITY;
CREATE POLICY idempotency_keys_own_row ON idempotency_keys
    FOR ALL USING (auth.uid() = user_id);

-- activity_log
ALTER TABLE activity_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY activity_log_own_row ON activity_log
    FOR ALL USING (auth.uid() = user_id);

-- expense_bank_accounts
ALTER TABLE expense_bank_accounts ENABLE ROW LEVEL SECURITY;
CREATE POLICY expense_bank_accounts_own_row ON expense_bank_accounts
    FOR ALL USING (auth.uid() = user_id);

-- expense_categories
ALTER TABLE expense_categories ENABLE ROW LEVEL SECURITY;
CREATE POLICY expense_categories_own_row ON expense_categories
    FOR ALL USING (auth.uid() = user_id);

-- expense_transaction_inbox
ALTER TABLE expense_transaction_inbox ENABLE ROW LEVEL SECURITY;
CREATE POLICY expense_transaction_inbox_own_row ON expense_transaction_inbox
    FOR ALL USING (auth.uid() = user_id);

-- expense_transactions
ALTER TABLE expense_transactions ENABLE ROW LEVEL SECURITY;
CREATE POLICY expense_transactions_own_row ON expense_transactions
    FOR ALL USING (auth.uid() = user_id);

-- expense_hashtags
ALTER TABLE expense_hashtags ENABLE ROW LEVEL SECURITY;
CREATE POLICY expense_hashtags_own_row ON expense_hashtags
    FOR ALL USING (auth.uid() = user_id);

-- expense_transaction_hashtags
ALTER TABLE expense_transaction_hashtags ENABLE ROW LEVEL SECURITY;
CREATE POLICY expense_transaction_hashtags_own_row ON expense_transaction_hashtags
    FOR ALL USING (auth.uid() = user_id);

-- expense_reconciliations
ALTER TABLE expense_reconciliations ENABLE ROW LEVEL SECURITY;
CREATE POLICY expense_reconciliations_own_row ON expense_reconciliations
    FOR ALL USING (auth.uid() = user_id);
