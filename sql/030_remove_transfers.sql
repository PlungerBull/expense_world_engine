-- 030: Remove auto-paired transfers — drop the pairing FK and the inbox
-- transfer-draft columns.
--
-- Owner decision 2026-08-10 (TODO.md at commit b88ffa7; docs in commit
-- 05b5eb8): auto-paired transfers — one request carrying a `transfer`
-- object, the engine registering the opposite leg in the counterparty
-- account — were overly convoluted for current use. An account-to-account
-- move is recorded from now on as two ordinary rows (one outflow, one
-- inflow) with ordinary user categories, and monthly inflow/outflow totals
-- include such moves (owner-accepted; engine-spec "Moves between accounts").
-- No transfer concept remains in the schema after this migration. The code
-- side landed first (commit 89bd30c deleted app/helpers/transfers.py and
-- every reader/writer of these columns) because the compatibility is
-- asymmetric: transfer-free code runs fine on the old schema — all three
-- columns are nullable — but the old code bound transfer_transaction_id on
-- every insert and would 500 against the new one. Restart the engine onto
-- the new code BEFORE applying this file.
--
-- Drops exactly five things (no index, view, trigger, or generated column
-- depends on any of them):
--
--   expense_transaction_inbox: CHECK inbox_transfer_fields_coherent  (sql/020)
--   expense_transaction_inbox: CHECK inbox_transfer_amount_positive  (sql/019)
--   expense_transaction_inbox: transfer_account_id                   (sql/008;
--                              its FK to expense_bank_accounts drops with it)
--   expense_transaction_inbox: transfer_amount_cents                 (sql/008)
--   expense_transactions:      transfer_transaction_id               (sql/003;
--                              the self-FK drops with its column)
--
-- On the coherence CHECK: sql/020's header marked its `transaction_type IS
-- NOT NULL AND IN (1, 2)` clause load-bearing — the only DB-level rule that
-- a transfer draft must carry a direction. Dropping it is an intended
-- relaxation, not an oversight: the state it forbade (a directionless
-- transfer draft) is unrepresentable once the columns it guards are gone.
-- It retires with them. inbox_transaction_type_valid (sql/020) survives as
-- the only remaining constraint on inbox transaction_type.
--
-- Data at authoring time (2026-08-10, re-verified before applying): zero
-- rows in expense_transactions, zero inbox transfer drafts, zero rows in
-- expense_categories — pure schema deletion, no data migration. System
-- categories are seeded lazily in Python (app/helpers/categories.py), so
-- there are no @Transfer/@Debt rows to remove; the DO block below aborts
-- loudly if any exist rather than deleting data (sql/028/029 precedent —
-- this engine never hard-deletes financial-adjacent rows in a migration).
--
-- What is NOT changed here: is_person and every person read surface
-- (POST /people is a scheduled feature, TODO.md); @Opening and
-- system_key = 'opening_balance'; the transactions_transaction_type_valid
-- and transactions_amount_positive CHECKs (sql/020, pinned by
-- tests/test_sql020_checks.py); sql/027's hashtags_transaction_source_valid.
--
-- Historical prose in sql/003/008/019/020/021/022 describing these columns
-- is left untouched — sql/ is never edited retroactively (sql/024 rule); a
-- later migration records the change instead. sql/022's "no index on
-- transfer_transaction_id" note now describes a column that no longer
-- exists.
--
-- Unlike sql/019/020/027/029, this migration IS re-runnable: every DROP is
-- IF EXISTS and the guard only reads.

DO $$
DECLARE squatting integer;
BEGIN
  SELECT count(*) INTO squatting
    FROM expense_categories WHERE system_key IN ('transfer', 'debt');
  IF squatting > 0 THEN
    RAISE EXCEPTION
      'sql/030 aborted: % @Transfer/@Debt category row(s) exist — decide their fate before applying', squatting;
  END IF;
END $$;

-- Explicit constraint drops first: DROP COLUMN would take them along, but
-- the header's inventory should be literal in the DDL.
ALTER TABLE expense_transaction_inbox
    DROP CONSTRAINT IF EXISTS inbox_transfer_fields_coherent,
    DROP CONSTRAINT IF EXISTS inbox_transfer_amount_positive,
    DROP COLUMN IF EXISTS transfer_account_id,
    DROP COLUMN IF EXISTS transfer_amount_cents;

ALTER TABLE expense_transactions
    DROP COLUMN IF EXISTS transfer_transaction_id;
