-- 027: The last two CHECK constraints from bug 6.3 -- transaction_source and
-- exchange_rates.rate.
--
-- Closes bug 6.3 entirely. The closed-enum sweep landed in stages: sql/019
-- (inbox), sql/020 (ledger transaction_type + amount), sql/025
-- (reconciliation status). These two were deferred because each carried a
-- question the earlier migrations did not:
--
--   expense_transaction_hashtags.transaction_source -- smallint, NOT NULL
--     since sql/003, designed for two writers (ledger attach, inbox attach)
--     of which only the ledger one was ever built. Every writer inserts 1 and
--     every reader filters `transaction_source = 1`; the value 2 exists
--     nowhere in code. Whether to CHECK the column or drop it hinged on a
--     product question TODO.md parks: should inbox drafts carry hashtags?
--     Owner decision 2026-08-07: YES, eventually -- the inbox is a draft
--     ledger row and should mirror the ledger with relaxed nullability, so
--     the column stays. This CHECK pins the single value that exists today;
--     the migration that ships the inbox writer (and the promote carry-over)
--     widens it to IN (1, 2) as part of that feature. Do not widen it ahead
--     of the writer -- an admissible-but-unwritten value is how half-copied
--     conventions become load-bearing by accident.
--
--   exchange_rates.rate -- numeric NOT NULL since sql/002, no positivity
--     constraint. Since sql/021 this table is the only source of every
--     home-currency figure, so one bad provider row (0, negative) misprices
--     reports rather than one write. The fetch/backfill jobs validate
--     rate > 0 before inserting (app/jobs/fetch_exchange_rates.py, counted
--     into the run's `failed` tally); this CHECK is the fail-closed backstop
--     for any writer that forgets.
--
-- Both columns are NOT NULL, so the bare predicates below cannot evaluate to
-- NULL -- the CLAUDE.md warning about NULL slipping through a closed-enum
-- CHECK (`col IS NOT NULL AND col IN (...)`) applies to nullable columns
-- only, same situation sql/025 documents for reconciliation status.
--
-- Data at authoring time (2026-08-07): expense_transaction_hashtags holds
-- 0 rows in the live ledger (the test fixture writes 1s); exchange_rates
-- holds 887 rows, min(rate) = 3.3373. Nothing violates either constraint,
-- so no backfill and no data-loss window.

-- Postgres has no ADD CONSTRAINT IF NOT EXISTS, so the ADDs are plain (same
-- as sql/020/025) and the migration is not re-runnable past here.
ALTER TABLE expense_transaction_hashtags
    ADD CONSTRAINT hashtags_transaction_source_valid
    CHECK (transaction_source = 1);

ALTER TABLE exchange_rates
    ADD CONSTRAINT exchange_rates_rate_positive
    CHECK (rate > 0);
