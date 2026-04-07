-- Step 7: Add transfer columns to inbox so transfer intent survives until promotion.
-- transfer_account_id: the destination account for the paired transaction.
-- transfer_amount_cents: the signed amount for the paired transaction (sign preserved for zero-sum validation on promote).

ALTER TABLE expense_transaction_inbox
    ADD COLUMN transfer_account_id uuid REFERENCES expense_bank_accounts(id),
    ADD COLUMN transfer_amount_cents bigint;
