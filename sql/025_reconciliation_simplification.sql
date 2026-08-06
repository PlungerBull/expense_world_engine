-- 025: Reconciliation simplification (WP6) -- delete chaining and manual
-- ordering; a beginning balance is a fact read off a statement, not a value
-- the engine derives.
--
-- Both columns arrived together in sql/017 and leave together here:
--
--   beginning_balance_source -- 1=manual, 2=chained. Chained mode let a row
--     take its beginning balance from the previous row's ending balance and
--     recompute whenever that neighbor changed. The cascade that did the
--     recomputing (_cascade_chained_recalc) had NO status predicate: editing
--     an upstream DRAFT rewrote the beginning_balance_cents of a COMPLETED
--     reconciliation -- doing through the back door exactly what the
--     completion field-lock refuses at the front. The fix is not a status
--     check on the cascade; it is deleting the derived mode. Every beginning
--     balance is now user-entered, required on POST.
--
--   sort_order -- per-account manual ordering, mutated only via
--     PUT /accounts/{id}/reconciliations/order (route deleted with it). Its
--     second job was defining the chain; with chaining gone, what remained
--     was hand-ordering a list of statement periods that already carry
--     dates. Owner decision 2026-08-06: order by date_start ASC NULLS LAST,
--     created_at ASC. This also closes bug 5.3 (the sort_order guard in the
--     plain PUT was dead -- silent 200 instead of 422).
--
-- The index below existed to serve the chained-neighbor lookup that ran on
-- every reconciliation write; nothing orders or filters on sort_order after
-- this migration.
--
-- The status CHECK closes the reconciliation slice of bug 6.3. The column is
-- NOT NULL since sql/003, so `IN (1, 2)` cannot evaluate to NULL here -- the
-- CLAUDE.md warning about NULL slipping through a closed-enum CHECK applies
-- to nullable columns, and this one is not.
--
-- Zero rows in expense_reconciliations as of 2026-08-06, so no backfill and
-- no data loss window. The DROPs would also be correct against real data:
-- stored chained values are already materialized in beginning_balance_cents,
-- so dropping the source flag loses no balance.

DROP INDEX IF EXISTS expense_reconciliations_account_sort_idx;

ALTER TABLE expense_reconciliations
    DROP COLUMN IF EXISTS sort_order,
    DROP COLUMN IF EXISTS beginning_balance_source;

-- Postgres has no ADD CONSTRAINT IF NOT EXISTS, so the ADD is plain (same as
-- sql/020).
ALTER TABLE expense_reconciliations
    ADD CONSTRAINT reconciliations_status_valid
    CHECK (status IN (1, 2));
