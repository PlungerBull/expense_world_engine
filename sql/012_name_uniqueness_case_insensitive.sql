-- 012: Case-insensitive, soft-delete-aware uniqueness on category + hashtag names.
--
-- Before this migration:
--   * expense_categories.UNIQUE (user_id, name) was case-SENSITIVE, so
--     "Food" and "food" coexisted — the engine now treats them as
--     duplicates at the service layer and the DB constraint must match.
--   * The old constraint also spanned soft-deleted rows, which blocked
--     a user from recreating a name they had previously deleted.
--
-- This migration replaces those two constraints with partial unique
-- indexes on LOWER(name) WHERE deleted_at IS NULL. Belt-and-suspenders
-- with the helpers.categories / helpers.hashtags code-level check.
--
-- SAFETY: If production already contains case-dupes, the index creation
-- will fail. Before applying, run:
--
--   SELECT user_id, LOWER(name) AS folded, count(*)
--   FROM expense_categories
--   WHERE deleted_at IS NULL
--   GROUP BY user_id, LOWER(name) HAVING count(*) > 1;
--
--   (same query for expense_hashtags)
--
-- and merge or rename the offending rows first.

ALTER TABLE expense_categories DROP CONSTRAINT IF EXISTS expense_categories_user_id_name_key;
ALTER TABLE expense_hashtags   DROP CONSTRAINT IF EXISTS expense_hashtags_user_id_name_key;

CREATE UNIQUE INDEX IF NOT EXISTS expense_categories_user_lower_name_active
    ON expense_categories (user_id, LOWER(name))
    WHERE deleted_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS expense_hashtags_user_lower_name_active
    ON expense_hashtags (user_id, LOWER(name))
    WHERE deleted_at IS NULL;
