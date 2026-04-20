-- 011: Capture HTTP status code alongside the idempotency snapshot.
--
-- Before this migration, idempotency_keys.response_snapshot stored only
-- the response body (jsonb). The HTTP status code was re-derived at the
-- router level on replay, which meant every POST endpoint had to remember
-- to wrap the cached return in JSONResponse(..., status_code=201). A
-- future handler that forgot would silently downgrade replay to 200.
--
-- Fix: store the status code alongside the body so replay reconstructs
-- the full envelope verbatim, with zero per-route drift surface.

ALTER TABLE idempotency_keys
    ADD COLUMN IF NOT EXISTS response_status smallint NOT NULL DEFAULT 200;
