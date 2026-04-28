-- 017: Add user-controlled ordering + explicit beginning-balance source
-- to expense_reconciliations.
--
-- Two columns:
--   sort_order              — per-(user_id, account_id) integer, ASC, mutated
--                             only via the dedicated bulk reorder endpoint.
--   beginning_balance_source — 1=manual (sacred, never overwritten), 2=chained
--                             (derived from previous neighbor's ending_balance).
--
-- Backfill rules:
--   - sort_order is renumbered per-account by created_at ASC for non-deleted
--     rows. Soft-deleted rows keep the default of 0 (they're not part of the
--     active sequence; restoring re-inserts them by appending — see helper).
--   - beginning_balance_source defaults to 1=manual on every existing row so
--     the engine NEVER silently rewrites a balance the user has already
--     stored. Future rows opt into chaining only when the create request
--     omits beginning_balance_cents.
--
-- Index supports the per-account chained-neighbor lookup that runs on every
-- POST/PUT/reorder/delete/restore in the cascade walk.

ALTER TABLE expense_reconciliations
    ADD COLUMN IF NOT EXISTS sort_order integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS beginning_balance_source smallint NOT NULL DEFAULT 1;

WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY user_id, account_id
               ORDER BY created_at, id
           ) AS rn
    FROM expense_reconciliations
    WHERE deleted_at IS NULL
)
UPDATE expense_reconciliations r
SET sort_order = ranked.rn
FROM ranked
WHERE r.id = ranked.id;

CREATE INDEX IF NOT EXISTS expense_reconciliations_account_sort_idx
    ON expense_reconciliations (user_id, account_id, sort_order)
    WHERE deleted_at IS NULL;
