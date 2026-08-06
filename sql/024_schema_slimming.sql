-- 024: Schema slimming (WP5) -- drop the 16 columns and 4 routes nothing reads.
--
-- The 2026-08-04 audit traced every column to its readers and writers. What
-- this migration drops falls into three groups, each free for a different
-- reason:
--
-- Group 1 -- user_settings: six echo-only client-preference columns (theme,
-- start_of_week, transaction_sort_preference, sidebar_show_*). The engine
-- stored and returned them and branched on none; their justification was
-- propagating preferences between devices, and there is one device. Plus
-- deleted_at, added to satisfy the every-mutable-table convention: a settings
-- row is never soft-deleted, nothing set it, no read filtered on it. After
-- this, PUT /auth/settings mutates exactly one meaningful field:
-- display_timezone (main_currency stays as the sql/018 chokepoint).
--
-- Group 2 -- is_archived on expense_categories and expense_hashtags, plus the
-- four archive/unarchive routes. Redundant with soft delete: deleted_at
-- already hides a row from pickers while leaving history intact. Its last
-- display reader (the dashboard archived panels) went with sql/021 (WP2).
-- What the audit under-counted: nine live guard sites (list filters, inbox
-- ?ready, attach/promote/restore/batch validation) still read the column to
-- 422 archived references -- those guards die with the column, deliberately,
-- because the archived *state* they guard against no longer exists. The
-- soft-delete guards beside them are untouched. compute_month_flow never
-- filtered on is_archived, so no report figure changes.
-- is_archived on expense_bank_accounts STAYS: an archived account still holds
-- real money; an archived category held only history.
--
-- Group 3 -- dead columns with no reader:
--   global_currencies.name, .symbol   -- zero reads; only code is selected
--   activity_log.actor_type           -- every writer passed the "user"
--                                        default; it was on the GET /activity
--                                        wire, so this is a recorded breaking
--                                        change, not a silent one
--   users.email                       -- its populator (the JWT claim) was
--                                        deleted 2026-08-03; deps.py returned
--                                        email=None unconditionally, so the
--                                        field lied on the wire. Dropped, not
--                                        repopulated: PAT auth has no email
--                                        source. The sql/006 trigger function
--                                        is replaced below to match.
--   idempotency_keys.processed_at     -- written on every claim, read never;
--                                        expires_at is the only temporal guard
--   expense_transaction_hashtags.version -- bumped by seven statements, read
--                                        by none; junction rows never take
--                                        part in optimistic concurrency
--   expense_transactions.parent_transaction_id -- never written; a self-FK
--                                        for an unbuilt split feature,
--                                        permanently null on the wire.
--                                        Supersedes open-bugs decision D8.
--
-- NOT dropped, despite appearing in the audit's orphan list:
-- transactions.fetch_hashtag_ids_map is load-bearing -- attach_hashtag_ids
-- calls it to put hashtag_ids on every transaction response. The audit claim
-- ("zero references") was false at HEAD.
--
-- Data migration: every dropped column was either on a zero-row table or held
-- only defaults/nulls; users.email held one legacy value on the single owner
-- row, discarded knowingly. Nothing to preserve.
--
-- sql/002-014 still declare what they created. Left alone deliberately,
-- following sql/020-023: sql/ is never replayed (deploy/local/create-test-db.sh
-- clones the live schema), and the convention is that a later migration
-- records the change rather than an earlier file being edited into a lie.

BEGIN;

-- Group 1: echo-only client preferences + the never-used soft-delete
ALTER TABLE user_settings
    DROP COLUMN theme,
    DROP COLUMN start_of_week,
    DROP COLUMN transaction_sort_preference,
    DROP COLUMN sidebar_show_bank_accounts,
    DROP COLUMN sidebar_show_people,
    DROP COLUMN sidebar_show_categories,
    DROP COLUMN deleted_at;

-- Group 2: archive was redundant with soft delete (accounts keep theirs)
ALTER TABLE expense_categories DROP COLUMN is_archived;
ALTER TABLE expense_hashtags   DROP COLUMN is_archived;

-- Group 3: dead columns
ALTER TABLE global_currencies DROP COLUMN name, DROP COLUMN symbol;
ALTER TABLE activity_log      DROP COLUMN actor_type;
ALTER TABLE users             DROP COLUMN email;
ALTER TABLE idempotency_keys  DROP COLUMN processed_at;
ALTER TABLE expense_transaction_hashtags DROP COLUMN version;
ALTER TABLE expense_transactions DROP COLUMN parent_transaction_id;  -- self-FK drops with it

-- The sql/006 Supabase trigger function INSERTs users.email. Inert in the
-- local profile (no auth.users to fire on), but cloud reactivation would
-- break on first signup if the function body still named a dropped column.
-- The trigger itself (on auth.users) is unchanged where it exists.
CREATE OR REPLACE FUNCTION public.handle_new_auth_user()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    INSERT INTO public.users (id, created_at, updated_at)
    VALUES (NEW.id, now(), now())
    ON CONFLICT (id) DO NOTHING;

    INSERT INTO public.user_settings (user_id, created_at, updated_at)
    VALUES (NEW.id, now(), now())
    ON CONFLICT (user_id) DO NOTHING;

    RETURN NEW;
END;
$$;

COMMIT;
