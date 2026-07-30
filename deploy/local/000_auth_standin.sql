-- deploy/local/000_auth_standin.sql
-- Minimal stand-in for the Supabase surface the schema expects, for the
-- local deployment profile (see README.md). Run BEFORE sql/001-017 (or
-- before restoring a --schema=public dump, which carries the same
-- policies/triggers).
--
-- What the schema actually needs from "Supabase":
--   * an `auth.users` table            (sql/006 trigger target)
--   * `auth.uid()` and `auth.role()`   (sql/005 + 016 RLS policies)
--   * an `extensions` schema with uuid functions (Supabase installs
--     uuid-ossp/pgcrypto there; column defaults may reference it)
--
-- RLS note: the engine connects as the table owner locally, and the
-- policies use ENABLE (not FORCE) row level security, so the owner
-- bypasses RLS — identical effective behavior to production, where the
-- engine's pooled role also bypasses and RLS is defense-in-depth for
-- direct-DB access paths that don't exist locally. The stubs exist so
-- DDL applies cleanly, not to simulate per-request identity.

CREATE SCHEMA IF NOT EXISTS auth;

CREATE TABLE IF NOT EXISTS auth.users (
    id          uuid PRIMARY KEY,
    email       text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Local stand-ins: no request-scoped JWT exists, so uid() is NULL and
-- role() reports 'authenticated'. Both are inert given the owner-bypass
-- note above.
CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid
    LANGUAGE sql STABLE AS $$ SELECT NULL::uuid $$;

CREATE OR REPLACE FUNCTION auth.role() RETURNS text
    LANGUAGE sql STABLE AS $$ SELECT 'authenticated'::text $$;

-- Supabase keeps extensions in their own schema; cover both the plain
-- and schema-qualified default expressions.
CREATE SCHEMA IF NOT EXISTS extensions;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA extensions;
CREATE EXTENSION IF NOT EXISTS pgcrypto   WITH SCHEMA extensions;
GRANT USAGE ON SCHEMA extensions TO PUBLIC;

-- The owner's auth identity. Insert AFTER the public schema + data
-- exist (the sql/006 trigger auto-creates public.users/user_settings
-- with ON CONFLICT DO NOTHING, so this is safe in either order):
--
--   INSERT INTO auth.users (id, email)
--   VALUES ('f47da468-fc22-4200-8673-0aadd7d1a861', '<owner email>')
--   ON CONFLICT (id) DO NOTHING;
