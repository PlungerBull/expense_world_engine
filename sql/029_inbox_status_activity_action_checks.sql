-- 029: The two closed-enum columns bug 6.3's sweep actually missed --
-- expense_transaction_inbox.status and activity_log.action.
--
-- sql/027's header claims it "closes bug 6.3 entirely"; on re-audit
-- (bloat-audit 2026-08-06 §17f verification, 2026-08-08) two enum-valued
-- smallint columns still had no CHECK anywhere in sql/:
--
--   expense_transaction_inbox.status -- smallint NOT NULL DEFAULT 1 since
--     sql/003. Only 1 (pending) and 2 (promoted) are ever written. A
--     dismissed row is `status = 1` + `deleted_at` set, NOT a third value
--     (engine-spec, inbox section) -- schema-reference.md documented a
--     phantom `3 = dismissed`, corrected with this migration.
--
--   activity_log.action -- smallint NOT NULL since sql/002. Values are
--     app/constants.ActivityAction: 1 created, 2 updated, 3 deleted,
--     4 restored.
--
-- Why now (owner decision 2026-08-08): response models are gaining IntEnum
-- typing for these fields (bloat-audit §17f), which makes a rogue stored
-- value fail a read loudly instead of passing through as a bare int. That
-- trade is only safe when the DB refuses the rogue value at write time --
-- an unconstrained column would turn one corrupt row into a 500 on every
-- list. CHECK first, then type.
--
-- Both columns are NOT NULL, so the bare IN predicates below cannot
-- evaluate to NULL -- the CLAUDE.md warning about NULL slipping through a
-- closed-enum CHECK applies to nullable columns only (same note as
-- sql/025/027).
--
-- SAFETY: the DO block aborts loudly if any live row violates either
-- constraint. Data at authoring time (2026-08-08): both tables hold 0 rows
-- in the live ledger.

DO $$
DECLARE bad_status integer; bad_action integer;
BEGIN
  SELECT count(*) INTO bad_status
    FROM expense_transaction_inbox WHERE status NOT IN (1, 2);
  IF bad_status > 0 THEN
    RAISE EXCEPTION
      'sql/029 aborted: % expense_transaction_inbox row(s) with status outside (1, 2) — repair before applying', bad_status;
  END IF;

  SELECT count(*) INTO bad_action
    FROM activity_log WHERE action NOT IN (1, 2, 3, 4);
  IF bad_action > 0 THEN
    RAISE EXCEPTION
      'sql/029 aborted: % activity_log row(s) with action outside (1, 2, 3, 4) — repair before applying', bad_action;
  END IF;
END $$;

-- Postgres has no ADD CONSTRAINT IF NOT EXISTS, so the ADDs are plain (same
-- as sql/020/025/027) and the migration is not re-runnable past here.
ALTER TABLE expense_transaction_inbox
    ADD CONSTRAINT inbox_status_valid
    CHECK (status IN (1, 2));

ALTER TABLE activity_log
    ADD CONSTRAINT activity_log_action_valid
    CHECK (action IN (1, 2, 3, 4));
