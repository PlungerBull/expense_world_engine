-- Step 7: Add transfer columns to inbox so transfer intent survives until promotion.
-- transfer_account_id: the destination account for the paired transaction.
-- transfer_amount_cents: the signed amount for the paired transaction (sign preserved for zero-sum validation on promote).
--
-- ⚠️ SUPERSEDED by sql/019 (2026-08-03). The sign is no longer stored and the
-- claim above was never true: promote *derived* the primary's sign from this
-- column instead of validating against it, so the zero-sum guard was
-- unreachable (audit WP7.2). sql/019 adds transfer_direction and stores this
-- column positive. The statements below are left as applied — read 019 for the
-- current shape.

ALTER TABLE expense_transaction_inbox
    ADD COLUMN transfer_account_id uuid REFERENCES expense_bank_accounts(id),
    ADD COLUMN transfer_amount_cents bigint;
