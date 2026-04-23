-- 016: Personal Access Tokens (PAT) for long-lived CLI auth.
--
-- JWT-only auth forces a re-login every ~1 hour, which is fine for the
-- iOS app (silent SDK refresh) but unusable for a CLI that captures
-- expenses from the terminal. PATs are opaque, engine-issued secrets
-- the user generates once and stores in ~/.expense-config. From the
-- engine's perspective, a PAT and a JWT are interchangeable — both
-- resolve to the same AuthUser in app/deps.py.
--
-- Security model:
--   * Only the SHA-256 hash is stored; plaintext is returned once on
--     creation and never recoverable. If a user loses their token they
--     must rotate it.
--   * token_prefix (first 12 chars of plaintext) is kept in cleartext
--     for display and leak-scanner discoverability, following the
--     GitHub/Stripe convention.
--   * Revocation is a soft-delete via revoked_at; the active-token
--     lookup index filters on revoked_at IS NULL so revoked rows do
--     not participate in authentication.
--
-- Deviations from the expense_hashtags template shape:
--   * No version/updated_at — PATs are immutable between create and
--     revoke, never edited.
--   * No sort_order — no ordering concern.
--   * revoked_at replaces deleted_at (semantically "revoked", same
--     soft-delete mechanism).

CREATE TABLE IF NOT EXISTS personal_access_tokens (
    id            uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id       uuid NOT NULL REFERENCES users(id),
    token_hash    text NOT NULL UNIQUE,
    token_prefix  text NOT NULL,
    name          text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    revoked_at    timestamptz
);

-- Partial index: every authenticated request hits this lookup, and
-- revoked rows are irrelevant to it. Keeping the index small also
-- keeps cache locality tight as the table grows.
CREATE INDEX IF NOT EXISTS idx_pat_token_hash_active
    ON personal_access_tokens(token_hash)
    WHERE revoked_at IS NULL;

ALTER TABLE personal_access_tokens ENABLE ROW LEVEL SECURITY;
CREATE POLICY personal_access_tokens_own_row ON personal_access_tokens
    FOR ALL USING (auth.uid() = user_id);
