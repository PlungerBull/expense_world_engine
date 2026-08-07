-- 022: Stop storing the account balance. Compute it from the rows, and add the
--      indexes that computing it requires.
--
-- expense_bank_accounts.current_balance_cents was a running total kept in step
-- with the ledger by hand, through app/helpers/balance.py and eleven mutation
-- sites. It is a derived value with a second source of truth, which is the same
-- defect sql/021 removed for currency -- a stored balance is a stored
-- amount_home_cents with a different name. One missed update, one crash between
-- the two writes, one manual SQL correction, and the balance disagrees with the
-- rows it claims to summarise: permanently, silently, and with no way to tell
-- which of the two is right.
--
-- The drift was not hypothetical. tests/test_reconciliation_rules.py carried a
-- comment apologising that its raw-DELETE teardown bypassed the balance
-- reversal, "so we must manually credit the 500 back" -- a test compensating by
-- hand for exactly the divergence this column makes possible.
--
-- After this migration:
--
--   * an account's balance is the signed sum of its non-deleted transactions,
--     including its @Opening seed, in the account's own currency.
--   * nothing writes a balance, so nothing can forget to.
--   * current_balance_cents stays on the wire, unchanged. Only its source moves,
--     from a column read to a SUM. See app/helpers/account_balance.py.
--
-- Scale is not a counter-argument. beancount, hledger, ledger-cli and Actual
-- Budget all compute balances from transactions and store nothing. Banks
-- materialise balances because they sum millions of rows per request; a personal
-- ledger reaches perhaps 10k-100k rows in a lifetime. Measured below: 6 ms.
--
-- Data migration: every domain table held 0 rows on 2026-08-06 (counted, not
-- estimated), so there is nothing to reconcile before the drop and no backfill to
-- write. Were there rows, the drop would still need none -- the transactions the
-- new sum reads are already there, and the column being dropped is precisely the
-- copy that could be wrong.
--
-- sql/003:13 still declares the column inside its CREATE TABLE. That is left
-- alone deliberately, following sql/020 and sql/021: sql/ is never replayed
-- (deploy/local/create-test-db.sh clones the live schema instead), and the
-- convention here is that a later migration records the change rather than an
-- earlier file being edited into a lie about what it did.


-- ===========================================================================
-- expense_transactions -- the indexes
-- ===========================================================================
--
-- These must land in the same migration as the DROP below. Shipping the drop
-- alone turns balance reads into sequential scans of the ledger; shipping the
-- indexes alone is harmless but pointless.
--
-- Until now expense_transactions had exactly two indexes: its primary key and
-- idx_expense_transactions_user_updated (sql/009:29-30), which exists to serve
-- /sync. There was nothing on account_id, date, category_id or
-- reconciliation_id -- the columns every real query filters on. Nobody noticed
-- because the stored balance meant nothing ever queried transactions by account.
-- The denormalisation was buying an index the schema should have had anyway, and
-- charging a drift risk for it.
--
-- The sync indexes are NOT touched here. They belong to WP4 (sql/023), which
-- must land after this migration and removes them once these exist.
--
-- Each index below is justified by a query that exists today, verified by grep
-- rather than assumed. Three indexes the 2026-08-04 audit recommended are
-- deliberately NOT created; see "Indexes deliberately not created" at the end.
--
-- Every one is partial on `deleted_at IS NULL`. Stated so it is not read as an
-- oversight: that means none of them serves GET /transactions?include_deleted=true
-- (app/routers/transactions.py:77-78 drops the predicate) or /sync's delta, both
-- of which fall back to a sequential scan. Accepted -- include_deleted is rare,
-- and /sync is WP4's to delete.

-- Serves the per-account balance sum (app/helpers/account_balance.py), the
-- opening-balance existence check (app/helpers/accounts.py:161-169), the
-- account-delete guard (app/helpers/accounts.py:320-328), and
-- GET /transactions?account_id= (app/routers/transactions.py:81-82).
--
-- user_id leads and account_id follows so a GROUP BY account_id comes out of the
-- index already grouped, with no Sort node.
CREATE INDEX IF NOT EXISTS idx_expense_transactions_user_account
    ON expense_transactions (user_id, account_id)
    WHERE deleted_at IS NULL;

-- The strongest of the set. GET /transactions orders by `t.date DESC,
-- t.created_at DESC` on EVERY call (app/routers/transactions.py:129-131), and
-- both monthly-report CTEs bound on date (app/helpers/monthly_report.py:201-209,
-- :268-276).
--
-- created_at is the third key so the paginated list needs no Sort at all. The
-- keys are deliberately NOT declared DESC: a btree scans backwards when every
-- key shares a direction, so plain ASC serves both orders and one index does the
-- work of two.
CREATE INDEX IF NOT EXISTS idx_expense_transactions_user_date
    ON expense_transactions (user_id, date, created_at)
    WHERE deleted_at IS NULL;

-- Serves the category-delete guard (app/helpers/categories.py:229-233) and
-- GET /transactions?category_id= (app/routers/transactions.py:85-86).
--
-- The audit justified this one as "report grouping", which is not correct and is
-- corrected here rather than repeated: helpers/monthly_report.py:213 groups a set
-- already narrowed by the date predicate, so a category index cannot drive it.
-- The two real queries above are enough on their own, but this is the weakest
-- index in the set and the first to reconsider if index maintenance ever matters.
CREATE INDEX IF NOT EXISTS idx_expense_transactions_user_category
    ON expense_transactions (user_id, category_id)
    WHERE deleted_at IS NULL;

-- Serves five queries, two of them SELECT ... FOR UPDATE on the completion path:
-- app/routers/reconciliations.py:150-153 and :158-166,
-- app/helpers/reconciliations.py:620-624 and :699-703,
-- app/routers/transactions.py:98-99.
--
-- All five also filter user_id and deleted_at IS NULL, which is why user_id
-- leads here rather than the audit's bare (reconciliation_id). Same shape as
-- expense_reconciliations_account_sort_idx (sql/017:40-42).
CREATE INDEX IF NOT EXISTS idx_expense_transactions_user_reconciliation
    ON expense_transactions (user_id, reconciliation_id)
    WHERE reconciliation_id IS NOT NULL AND deleted_at IS NULL;


-- ===========================================================================
-- expense_bank_accounts -- the drop
-- ===========================================================================
--
-- Nothing depends on this column: no foreign key, no CHECK, no index, no view,
-- no generated column, no trigger. Verified against the live catalog on
-- 2026-08-06. A plain DROP COLUMN is therefore safe and complete.
ALTER TABLE expense_bank_accounts
    DROP COLUMN current_balance_cents;


-- ===========================================================================
-- Indexes deliberately not created
-- ===========================================================================
--
-- Recorded because an unexplained absence reads as an oversight, and because the
-- 2026-08-04 audit recommended all three. Its index table is explicitly labelled
-- a recommendation rather than a prescription
-- (WP3 work package, in git history).
--
-- expense_transactions (transfer_transaction_id) -- NO QUERY USES IT.
--     Not a judgement call: `grep -rn "transfer_transaction_id\s*=\s*\$"` over
--     app/ returns nothing at all. Every use reads the value off a row in Python
--     and then looks the sibling up by primary key (app/helpers/transactions.py:699,
--     :856). The audit's argument was that after sql/020 this column IS the
--     transfer discriminator, which is true about semantics and says nothing
--     about a query plan. The only other argument is that Postgres does not index
--     the referencing side of a foreign key -- but this engine never hard-deletes
--     (CLAUDE.md, "Soft delete everywhere"), so that cost is paid by test
--     teardowns and nothing else. And parent_transaction_id and inbox_id
--     (sql/003:83-84) have exactly the same FK shape with the same absence of
--     queries; indexing one and not the others would be arbitrary.
--
-- expense_transaction_hashtags (hashtag_id) -- THE JOIN RUNS THE OTHER WAY.
--     The audit's stated reason is that the report's hashtag breakdown joins the
--     junction in this direction. It does not: helpers/monthly_report.py:194-198
--     correlates on `th.transaction_id = t.id`, as does helpers/sync.py:81-84.
--     That direction is already indexed twice -- UNIQUE (transaction_id,
--     hashtag_id) at sql/003:116 and idx_expense_transaction_hashtags_tx at
--     sql/009:32-34. The only hashtag_id-leading access is the EXISTS semi-join
--     at routers/transactions.py:90-95 and the cascade UPDATE at
--     helpers/hashtags.py:180-183, neither of which is on a hot path. If it is
--     ever wanted, the useful shape is (hashtag_id, transaction_id) so the
--     semi-join can go index-only -- not the bare column.
--
-- INCLUDE (amount_cents, transaction_type) on the balance index -- NOT FREE.
--     Included columns count as indexed columns for HOT-update eligibility, so
--     adding amount_cents would make every PUT /transactions that changes an
--     amount a non-HOT update, bloating every index on the row rather than just
--     this one. Index-only scans also need a VACUUM'd visibility map, which an
--     actively written ledger often will not have. Measured below, the plain
--     partial index already does the job. WP3's own closing line is "do not
--     pre-optimise"; this is the case it had in mind.
--
-- expense_bank_accounts -- NOTHING AT ALL.
--     routers/accounts.py:54-64 and routers/dashboard.py:39-57 filter on four
--     columns and sort on two, which normally argues for an index. It does not
--     here: the table holds well under fifty rows for the foreseeable life of
--     this ledger, and a sequential scan of fifty rows beats an index lookup.
--     Said out loud so the next reader does not wonder.


-- ===========================================================================
-- Measured plans
-- ===========================================================================
--
-- Captured 2026-08-06 against expense_world_test seeded with 50,000
-- transactions across 8 accounts and 12 categories (4% soft-deleted), ANALYZEd,
-- then rolled back -- the sql/012:13-24 convention of shipping the verification
-- as a record rather than as an executable step. 50k is roughly a lifetime of
-- personal-ledger rows.
--
--   Balance sum, ONE account (the single-account read path):
--     HashAggregate (actual time=1.023..1.023 rows=1)
--       ->  Bitmap Heap Scan on expense_transactions t
--             ->  Bitmap Index Scan on idx_expense_transactions_user_account
--                   (actual time=0.094..0.094 rows=6000)
--     Execution Time: 1.029 ms
--
--   Balance sum with NO account filter, for contrast (not a path the engine
--   takes -- recorded because it is the shape to avoid):
--     HashAggregate (actual time=6.196..6.198 rows=8)
--       ->  Seq Scan on expense_transactions t (rows=48000)
--     Execution Time: 6.209 ms
--
--     Postgres correctly declines the index: with no account predicate and one
--     user, there is nothing selective to exploit, so summing every account
--     means reading every row. Every read path in the engine scopes to the
--     accounts it is actually rendering -- the account list to its page, each
--     dashboard panel to its slice, /sync to its delta -- so all of them take
--     the 1 ms bitmap-index path above instead. app/helpers/account_balance.py
--     deliberately exposes no ledger-wide variant.
--
--   Note what these sums are NOT: a total across accounts. Every query is
--   GROUP BY account_id and an account holds one immutable currency, so each
--   sum stays inside one currency. Adding a PEN balance to a USD one would be a
--   number in no currency; the only cross-currency figure is
--   current_balance_home_cents, converted per account before combining.
--
--   Paginated transaction list (ORDER BY date DESC, created_at DESC LIMIT 50):
--     Limit (actual time=0.006..0.013 rows=50)
--       ->  Index Scan Backward using idx_expense_transactions_user_date
--     Execution Time: 0.016 ms
--
--     No Sort node -- the third key (created_at) is what removes it. This query
--     runs on every transaction list call and was previously a full scan plus a
--     sort of the entire ledger.
--
--   Monthly-report month bucket (date >= $1 AND date < $2):
--     Aggregate -> Index Only Scan using idx_expense_transactions_user_date
--     Execution Time: 0.051 ms
--
-- Re-run after applying, if you want the same record on your own data:
--
--   BEGIN;
--   INSERT INTO expense_transactions SELECT ... generate_series(1, 50000) ...;
--   ANALYZE expense_transactions;
--   EXPLAIN (ANALYZE, BUFFERS) <the balance sum>;
--   ROLLBACK;


-- ===========================================================================
-- One obligation this migration creates, for whoever ships split transactions
-- ===========================================================================
--
-- parent_transaction_id (sql/003:83) is reserved and always null, so the sum
-- above is correct today. But the documented split rule is that a parent row is
-- a display container which does NOT move the balance, and only its children do
-- (docs/schema-reference.md, "Split Transactions"). Under the stored column that
-- rule was enforced by simply not calling apply_balance for a parent. A SUM has
-- no such escape hatch: the day splits ship, parent and children double-count
-- every split transaction.
--
-- The predicate the sum will need at that point, written out now so it is not
-- re-derived under pressure:
--
--     AND NOT EXISTS (SELECT 1 FROM expense_transactions c
--                     WHERE c.parent_transaction_id = t.id
--                       AND c.deleted_at IS NULL)
--
-- Note it is NOT `parent_transaction_id IS NULL`, which excludes the children
-- and keeps the parents -- precisely backwards. It is deliberately not added
-- today: no row can have a parent, and the predicate would be unindexed. The
-- same note is on app/helpers/account_balance.py.
