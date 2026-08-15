-- 033: inbox hashtags -- the junction table becomes genuinely two-sourced.
--
-- Ships WITH the inbox hashtag writer, never ahead of it -- sql/027's header
-- set that sequencing and this migration is the other half of it. Owner
-- decision 2026-08-07: a draft is "the same copy of a transaction with relaxed
-- rules", and hashtags belong to that copy. Until now they were lost by
-- drafting: the inbox schemas had no hashtag_ids field at all.
--
-- Two changes, and only the first is the one TODO.md anticipated.
--
--   1. hashtags_transaction_source_valid: `= 1` -> `IN (1, 2)`.
--
--      1 = ledger, 2 = inbox. The mapping is settled the way the code has
--      always written it, not the way the pre-WP7 schema doc described it
--      (that revision said 1=inbox, 2=ledger and was simply wrong; every row
--      ever stored is a ledger row written as 1). Nothing to backfill: the
--      value being ADMITTED is new, so no existing row changes meaning.
--
--   2. UNIQUE (transaction_id, hashtag_id) -> UNIQUE (transaction_id,
--      transaction_source, hashtag_id).
--
--      This one is a latent correctness bug, not a widening. The old key
--      asserts that a (parent, hashtag) pair is unique ACROSS parent kinds,
--      which was true only while one kind existed. `transaction_id` carries no
--      FK precisely so it can name a row in either table, and the two tables
--      have independent id spaces -- so the same uuid can legitimately be an
--      inbox id AND a ledger id at once. That is not hypothetical:
--      POST /inbox/{id}/promote lets the client choose the ledger row's uuid,
--      and nothing forbids passing the draft's own.
--
--      The failure it produces is silent, which is why it must be fixed in
--      this migration rather than after the first report. Promoting a tagged
--      draft onto its own id runs the ledger-side upsert
--      `ON CONFLICT (transaction_id, hashtag_id) DO UPDATE ... WHERE
--      deleted_at IS NOT NULL`; the arbiter matches the INBOX junction row,
--      finds it active, and does nothing. The ledger row comes out untagged,
--      with no error anywhere. (If the inbox row had already been cascaded,
--      the same statement instead resurrects it -- a row stamped source = 2
--      that the ledger reader will never see and the inbox no longer owns.)
--      Every ON CONFLICT on this table now names all three columns, so the
--      arbiter is per-source and the upsert can only ever touch its own rows.
--
-- Fail-closed note for whoever widens this next: the CHECK is on a NOT NULL
-- column, so the bare `IN` predicate cannot evaluate to NULL -- the CLAUDE.md
-- warning about a null slipping past a closed-enum CHECK applies to nullable
-- columns only (same situation sql/025 and sql/027 document).
--
-- Data at authoring time (2026-08-14): expense_transaction_hashtags holds 0
-- rows in the live ledger, so the UNIQUE swap rebuilds an empty index and
-- nothing can violate either constraint. No backfill, no data-loss window.

-- The DROPs use IF EXISTS so a partially-applied run can be replayed; the ADDs
-- are plain (Postgres has no ADD CONSTRAINT IF NOT EXISTS), so the migration is
-- not re-runnable past this point -- same shape as sql/020, sql/025, sql/027.

ALTER TABLE expense_transaction_hashtags
    DROP CONSTRAINT IF EXISTS hashtags_transaction_source_valid;

ALTER TABLE expense_transaction_hashtags
    ADD CONSTRAINT hashtags_transaction_source_valid
    CHECK (transaction_source IN (1, 2));

-- The old name is Postgres's auto-generated one from the CREATE TABLE in
-- sql/003. The replacement is named explicitly: a three-column key that reads
-- "one row per (parent, kind of parent, hashtag)" deserves to say so.
ALTER TABLE expense_transaction_hashtags
    DROP CONSTRAINT IF EXISTS expense_transaction_hashtags_transaction_id_hashtag_id_key;

ALTER TABLE expense_transaction_hashtags
    ADD CONSTRAINT expense_transaction_hashtags_parent_source_hashtag_key
    UNIQUE (transaction_id, transaction_source, hashtag_id);
