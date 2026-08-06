-- 023: Delete /sync -- the checkpoint table and the seven indexes that served it.
--
-- GET /sync handed a client the whole delta since a token, rotated the token,
-- and recorded the position in sync_checkpoints. It was built for an
-- offline-capable client keeping a local replica. The CLI was its only caller
-- -- its cache layer hydrated ~/.expense-cache.sqlite3 from the delta -- and
-- that layer was deleted the same day this migration landed (CLI repo,
-- decisions.md "Delete the local replica", 2026-08-06): with the engine on
-- loopback, a live read costs less than the staleness the replica charged.
--
-- Deleting the endpoint also deletes open bug 3.1 (delta sync can permanently
-- drop committed writes): the checkpoint stored now() at transaction start,
-- writers stamped updated_at at THEIR start, and a writer that began before a
-- sync and committed after it was never delivered. That is the most serious
-- defect class a ledger can have, and it was critical work for as long as the
-- endpoint existed.
--
-- What this migration does NOT touch -- the substrate sync was built on:
--
--   * version, updated_at, deleted_at stay on every mutable table, maintained
--     exactly as before. They are load-bearing for optimistic concurrency and
--     ordinary auditing, independently of sync.
--   * client-generated UUIDs stay. A future client can create rows offline
--     without coordination.
--
-- Rebuilding sync for a future mobile client is therefore additive work
-- against a schema that already supports it (roughly 1-2 days), and the
-- rebuild would not inherit bug 3.1.
--
-- Precondition, verified against the live database before this ran (WP4's
-- stated dependency on WP3): the four replacement indexes from sql/022 --
-- idx_expense_transactions_user_{account,date,category,reconciliation} --
-- all exist. Without them, dropping idx_expense_transactions_user_updated
-- would leave expense_transactions with nothing but its primary key.
--
-- Also verified: no query outside the deleted app/helpers/sync.py filters or
-- orders on updated_at (grepped 2026-08-06; the only three predicates were
-- sync.py's own deltas), so all seven (user_id, updated_at) indexes are
-- sync-only. idx_expense_transaction_hashtags_tx (sql/009:32-34) is NOT a
-- sync index and is kept -- sql/022's header relies on it for the report's
-- hashtag join.
--
-- Data migration: sync_checkpoints held 0 rows (counted 2026-08-06 -- no
-- client ever completed a sync against this database). Nothing to preserve.
--
-- sql/002 and sql/009 still declare what they created. Left alone
-- deliberately, following sql/020-022: sql/ is never replayed
-- (deploy/local/create-test-db.sh clones the live schema), and the convention
-- is that a later migration records the change rather than an earlier file
-- being edited into a lie about what it did.

BEGIN;

-- The seven (user_id, updated_at) indexes, all from sql/009, all sync-only.
DROP INDEX IF EXISTS idx_user_settings_user_updated;
DROP INDEX IF EXISTS idx_expense_bank_accounts_user_updated;
DROP INDEX IF EXISTS idx_expense_categories_user_updated;
DROP INDEX IF EXISTS idx_expense_hashtags_user_updated;
DROP INDEX IF EXISTS idx_expense_transaction_inbox_user_updated;
DROP INDEX IF EXISTS idx_expense_transactions_user_updated;
DROP INDEX IF EXISTS idx_expense_reconciliations_user_updated;

-- The checkpoint table (sql/002:53-65). Its RLS policy drops with it.
DROP TABLE IF EXISTS sync_checkpoints;

COMMIT;
