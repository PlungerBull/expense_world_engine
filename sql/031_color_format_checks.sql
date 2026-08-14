-- 031: colour columns hold a 6-digit hex value, or the row does not exist.
--
-- Closes bug account-color. Until now NOTHING validated colour anywhere:
--
--   * expense_bank_accounts.color -- create_account bound `color or '#3b82f6'`,
--     so an explicitly-sent empty string silently became the default blue
--     rather than being stored or refused. Exactly the collapse that `or 0`
--     caused for sort_order, fixed there and missed here; the comment
--     documenting that fix sat two lines below the surviving `or`.
--   * expense_categories.color -- worse, because CategoryCreateRequest.color is
--     *required*: it was bound verbatim, so '' and 'banana' were simply stored.
--   * both update paths reached dynamic_update with no check at all.
--
-- Owner decision 2026-08-13: reject anything that is not a real hex colour, at
-- every write. helpers/validation.validate_color is that check, and it runs
-- first on all four paths, so the constraints below only ever fire on a path
-- that skipped it -- which is a defect, and a 500 is the correct answer to one.
--
-- Why a CHECK at all, when the app already validates: the same argument
-- sql/027 made for `rate > 0`, which also shipped alongside a Python guard.
-- The constraint is the one layer a new writer cannot forget. `fail closed`
-- in CLAUDE.md is about enumerating what is permitted, and the column
-- definition is where that enumeration is binding rather than conventional.
--
-- Scope: exactly two columns. expense_hashtags has no colour column, so there
-- is no third table here despite hashtags routing through the same
-- update_named_resource path (their update schema has no `color` field).
--
-- Live data conforms: at time of writing the ledger holds 1 account and 1
-- category, both valid 6-digit hex, so this migration cannot fail on existing
-- rows and needs no backfill. Same check sql/027's header made before adding
-- its constraints.
--
-- Deliberately narrow, matching validate_color's regex exactly -- the two are
-- one rule written in two languages, and tests/test_sql031_color_checks.py
-- asserts they still agree:
--
--   * no 3-digit shorthand (#fff): one colour, two spellings, and clients
--     compare these as strings.
--   * no 8-digit alpha: nothing renders the channel.
--   * case is NOT normalised -- '#00AA00' stays '#00AA00'. Rewriting an
--     accepted value is the silent mutation this migration exists to stop.
--
-- The `IS NOT NULL` arm is redundant today (both columns are NOT NULL since
-- sql/003) and is written anyway, per CLAUDE.md's standing warning: a CHECK
-- rejects a row only when it evaluates to FALSE, and NULL passes. Spelling it
-- out means dropping the NOT NULL later cannot silently reopen the hole.
--
-- Postgres has no ADD CONSTRAINT IF NOT EXISTS, so the ADDs are plain (same as
-- sql/020/025/027) and the migration is not re-runnable past here.

ALTER TABLE expense_bank_accounts
    ADD CONSTRAINT accounts_color_is_hex
    CHECK (color IS NOT NULL AND color ~ '^#[0-9a-fA-F]{6}$');

ALTER TABLE expense_categories
    ADD CONSTRAINT categories_color_is_hex
    CHECK (color IS NOT NULL AND color ~ '^#[0-9a-fA-F]{6}$');
