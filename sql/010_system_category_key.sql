-- 010: Stable system category identifier.
--
-- Before this migration, system categories (@Debt, @Transfer) were
-- looked up by name inside helpers/transfers.py::create_transfer_pair.
-- If a user renamed the display text, every subsequent transfer would
-- silently auto-create a new @Debt/@Transfer instead of reusing the
-- original, fragmenting the category history.
--
-- Fix: add an immutable `system_key` discriminator column. The display
-- `name` stays freely renameable. The engine looks up system categories
-- by (user_id, system_key) which is stable for the lifetime of the row.

ALTER TABLE expense_categories
    ADD COLUMN IF NOT EXISTS system_key text;

-- Backfill: match the two existing system names to their canonical keys.
-- Safe to re-run; idempotent on system_key.
UPDATE expense_categories
SET system_key = 'debt'
WHERE is_system = true
  AND name = '@Debt'
  AND system_key IS NULL;

UPDATE expense_categories
SET system_key = 'transfer'
WHERE is_system = true
  AND name = '@Transfer'
  AND system_key IS NULL;

-- At most one live system row per (user, key). Partial index so regular
-- categories (system_key IS NULL) are unconstrained, and soft-deleted
-- rows don't block re-seeding.
CREATE UNIQUE INDEX IF NOT EXISTS expense_categories_system_key_uq
    ON expense_categories (user_id, system_key)
    WHERE system_key IS NOT NULL AND deleted_at IS NULL;
