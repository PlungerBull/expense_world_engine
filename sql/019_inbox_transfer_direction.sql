-- 019: Give the inbox a transfer_direction column and store transfer amounts positive.
--
-- The inbox is a draft ledger row: same shape, looser rules about which fields
-- are REQUIRED. sql/007 added transaction_type for exactly that reason, so the
-- inbox could store amount_cents positive like expense_transactions does.
--
-- sql/008 then added the transfer columns and stopped halfway. It added
-- transfer_account_id and transfer_amount_cents but NOT transfer_direction, so
-- the sign of transfer_amount_cents became the only direction signal on the
-- row -- an encoding nothing else in the engine uses. Consequences, all real:
--
--   * The primary leg's sign was discarded by abs() on write and re-derived at
--     promote time as the NEGATION of the sibling's sign. create_transfer_pair's
--     same-sign guard was therefore unreachable from the inbox: an item saved as
--     two outflows promoted cleanly with one leg silently flipped.
--     (WP7.2, docs/audit-2026-08-01-remediation-plan.md:221)
--   * A PUT carrying both amount_cents and transfer left transaction_type at
--     1 or 2 with the transfer columns still populated -- a row promote treated
--     as a transfer and the read path treated as an expense.
--   * ?debit_as_negative could only read the sibling's sign, never flip it.
--     (WP10.2, same doc:297)
--
-- After this migration the inbox matches the ledger exactly: signed in the
-- request, encoded in storage, positive in the response beside a direction
-- field. transfer_direction describes the PRIMARY leg -- the same thing it
-- means on expense_transactions, because the inbox row IS the primary leg.
-- The sibling's direction is its inverse, structurally, so one column suffices.
--
-- Data migration: expense_transaction_inbox held 0 rows on 2026-08-03, so the
-- backfill below is a no-op here. It is written anyway so the migration is
-- correct wherever it runs -- it reproduces the exact rule the old promote path
-- used (helpers/inbox.py:434-435 before this change): a positive sibling amount
-- meant the sibling was receiving, so the primary was the outflow leg.
--
-- The ledger's equivalent CHECK constraints (expense_transactions) are NOT here.
-- They belong to the transfer collapse -- see docs/rework/WP1-transfer-collapse.md,
-- which also deletes the transfer_direction column this migration added. That is not
-- a reversal: the lesson holds (direction lives in a typed column, never in a sign),
-- the direction just moves to transaction_type, which every row already carries.

ALTER TABLE expense_transaction_inbox
    ADD COLUMN IF NOT EXISTS transfer_direction smallint;

-- Backfill: direction from the old sign, then normalise the amount.
UPDATE expense_transaction_inbox
SET transfer_direction = CASE WHEN transfer_amount_cents > 0 THEN 1 ELSE 2 END,
    transfer_amount_cents = abs(transfer_amount_cents)
WHERE transfer_amount_cents IS NOT NULL
  AND transfer_direction IS NULL;

-- Constraints. Postgres has no ADD CONSTRAINT IF NOT EXISTS, so these are plain
-- (same as sql/015 and sql/018) and the migration is not re-runnable past here.

ALTER TABLE expense_transaction_inbox
    ADD CONSTRAINT inbox_transaction_type_valid
    CHECK (transaction_type IS NULL OR transaction_type IN (1, 2, 3));

ALTER TABLE expense_transaction_inbox
    ADD CONSTRAINT inbox_amount_positive
    CHECK (amount_cents IS NULL OR amount_cents > 0);

ALTER TABLE expense_transaction_inbox
    ADD CONSTRAINT inbox_transfer_amount_positive
    CHECK (transfer_amount_cents IS NULL OR transfer_amount_cents > 0);

-- The fail-closed core: the three transfer columns are all-present or
-- all-absent, and a populated triple forces transaction_type = 3. This makes
-- the half-transfer row unrepresentable rather than merely guarded in Python.
-- Note amount_cents is deliberately NOT required here -- a transfer draft may
-- still be missing its primary amount, which is legitimate inbox looseness.
ALTER TABLE expense_transaction_inbox
    ADD CONSTRAINT inbox_transfer_fields_coherent
    CHECK (
        (transfer_account_id IS NULL
         AND transfer_amount_cents IS NULL
         AND transfer_direction IS NULL)
        OR
        (transfer_account_id IS NOT NULL
         AND transfer_amount_cents IS NOT NULL
         AND transfer_direction IN (1, 2)
         AND transaction_type = 3)
    );
