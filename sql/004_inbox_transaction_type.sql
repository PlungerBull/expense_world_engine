-- Add transaction_type to inbox so income items can be distinguished from expenses.
-- Nullable: inbox items may not have an amount yet, so type is unknown.
-- 1=expense, 2=income, 3=transfer (same enum as expense_transactions.transaction_type).
-- Inferred by engine from signed amount_cents in request bodies.

ALTER TABLE expense_transaction_inbox
    ADD COLUMN transaction_type smallint;
