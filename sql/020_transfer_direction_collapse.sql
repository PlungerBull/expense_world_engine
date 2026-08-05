-- 020: Collapse transaction_type into a direction and delete transfer_direction.
--
-- transaction_type was carrying two unrelated facts: which way money moved
-- (1=expense, 2=income) and who the counterparty was (3=transfer). Because 3
-- occupied a slot in what is otherwise a direction column, direction for
-- transfers had to be exiled into a SECOND column -- transfer_direction --
-- meaningful only when the first column held one specific value. Two columns
-- encoding one concept, only one valid at a time.
--
-- The decisive observation: every type=3 row carries a direction, and direction
-- already means in/out. So 3 said nothing about direction that transfer_direction
-- didn't. Its only remaining information was "the counterparty is an account you
-- own" -- which transfer_transaction_id IS NOT NULL already says, using a column
-- that already exists.
--
-- After this migration:
--
--   * transaction_type is 1 = OUTFLOW, 2 = INFLOW, on every row, never null,
--     and CHECK-enforced. There is no third value.
--   * a transfer is two ordinary rows paired by transfer_transaction_id.
--   * transfer_direction does not exist on either table.
--
-- This completes what sql/019:35-39 deferred ("The ledger's equivalent CHECK
-- constraints are NOT here -- they belong to the transfer collapse"). Dropping
-- the column sql/019 added is NOT a reversal of it: 019's lesson holds exactly
-- as written -- direction lives in a typed column, never in the sign of a value
-- -- the direction simply moves to a column every row already carries. What 019
-- fixed was the inbox encoding its direction in a sign; nothing here restores
-- that.
--
-- Data migration: every domain table held 0 rows on 2026-08-04, so the backfills
-- below are no-ops here. They are written anyway so the migration is correct
-- wherever it runs. The mapping is the identity -- debit(1) -> outflow(1),
-- credit(2) -> inflow(2) -- because the two enums always meant the same thing.
--
-- Postgres has no ADD CONSTRAINT IF NOT EXISTS, so the ADDs are plain (same as
-- sql/015, sql/018 and sql/019) and this migration is not re-runnable past the
-- first constraint.


-- ===========================================================================
-- expense_transactions
-- ===========================================================================

-- Fail closed. A transfer leg with no direction is exactly the row this
-- collapse exists to make unrepresentable, and its direction is not
-- recoverable from anything else on the row -- amount_cents is absolute and
-- transfer_transaction_id says only that a pair exists, not which way it ran.
-- Refuse rather than guess.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM expense_transactions
        WHERE transaction_type = 3 AND transfer_direction IS NULL
    ) THEN
        RAISE EXCEPTION
            'Cannot collapse transaction_type: % transfer row(s) have no '
            'transfer_direction, so their direction cannot be recovered. '
            'Resolve them by hand before re-running this migration.',
            (SELECT count(*) FROM expense_transactions
             WHERE transaction_type = 3 AND transfer_direction IS NULL);
    END IF;
END $$;

UPDATE expense_transactions
SET transaction_type = transfer_direction
WHERE transaction_type = 3;

-- The ledger half of open bug 6.3. expense_transactions.transaction_type has
-- had no CHECK at all since sql/003 -- it was declared NOT NULL and left open
-- to any smallint.
ALTER TABLE expense_transactions
    ADD CONSTRAINT transactions_transaction_type_valid
    CHECK (transaction_type IN (1, 2));

-- The ledger's missing counterpart to sql/019's inbox_amount_positive. With
-- direction now in a typed column on every row, "no column's sign means
-- anything" stops being a convention the code upholds and becomes a fact the
-- database enforces.
ALTER TABLE expense_transactions
    ADD CONSTRAINT transactions_amount_positive
    CHECK (amount_cents > 0);

ALTER TABLE expense_transactions
    DROP COLUMN transfer_direction;


-- ===========================================================================
-- expense_transaction_inbox
-- ===========================================================================

-- Both constraints must come off before the backfill: the coherence CHECK
-- requires transaction_type = 3 on any populated transfer triple, so setting
-- it to 1 or 2 would violate the very constraint we are replacing.
ALTER TABLE expense_transaction_inbox
    DROP CONSTRAINT IF EXISTS inbox_transfer_fields_coherent;
ALTER TABLE expense_transaction_inbox
    DROP CONSTRAINT IF EXISTS inbox_transaction_type_valid;

-- No guard needed here: sql/019's coherence CHECK already guaranteed
-- transfer_direction IN (1,2) wherever transaction_type = 3, so every row
-- reached by this UPDATE has a direction to move.
UPDATE expense_transaction_inbox
SET transaction_type = transfer_direction
WHERE transaction_type = 3;

-- Still nullable: an inbox draft may legitimately have no amount yet, and
-- therefore no direction.
ALTER TABLE expense_transaction_inbox
    ADD CONSTRAINT inbox_transaction_type_valid
    CHECK (transaction_type IS NULL OR transaction_type IN (1, 2));

-- The fail-closed core, rewritten. Previously this read:
--
--     (transfer_account_id IS NOT NULL
--      AND transfer_amount_cents IS NOT NULL
--      AND transfer_direction IN (1, 2)
--      AND transaction_type = 3)
--
-- The transfer columns are still all-present or all-absent, so a half-transfer
-- row stays unrepresentable in the database rather than merely guarded in
-- Python. The direction requirement has not been dropped -- it moved onto
-- transaction_type, which every row already carries.
--
-- ⚠️ The `transaction_type IS NOT NULL` below is load-bearing and must not be
-- pruned as redundant next to `IN (1, 2)`. A CHECK rejects a row only when it
-- evaluates to FALSE; NULL passes. With a null transaction_type,
-- `transaction_type IN (1, 2)` is NULL, not FALSE, so the whole expression
-- comes out NULL and a directionless transfer draft would be accepted --
-- reintroducing, through SQL's three-valued logic, exactly the state sql/019
-- was written to forbid. tests/test_inbox_transfers.py::
-- test_directionless_transfer_draft_violates_check_constraint pins it.
--
-- amount_cents is deliberately NOT required, unchanged from sql/019: a transfer
-- draft may still be missing its primary amount, which is legitimate inbox
-- looseness.
ALTER TABLE expense_transaction_inbox
    ADD CONSTRAINT inbox_transfer_fields_coherent
    CHECK (
        (transfer_account_id IS NULL
         AND transfer_amount_cents IS NULL)
        OR
        (transfer_account_id IS NOT NULL
         AND transfer_amount_cents IS NOT NULL
         AND transaction_type IS NOT NULL
         AND transaction_type IN (1, 2))
    );

ALTER TABLE expense_transaction_inbox
    DROP COLUMN transfer_direction;
