-- 013: Separate "actor" from "resource owner" on activity_log.
--
-- Before: ``changed_by`` was hardcoded to the resource's ``user_id``, so
-- system jobs (cron-driven rate refreshes, admin back-office actions,
-- future multi-user share flows) were indistinguishable from direct
-- user actions in the audit trail.
--
-- Fix: add ``actor_type`` so every row declares who performed the
-- mutation — default 'user' for the existing steady state, with 'system'
-- / 'admin' available for non-user callers. ``changed_by`` stays as the
-- user-id anchor; pair the two fields to resolve attribution.

ALTER TABLE activity_log
    ADD COLUMN IF NOT EXISTS actor_type text NOT NULL DEFAULT 'user';
