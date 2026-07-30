# Deployment profile: cloud (mothballed 2026-07-30)

> **Status:** mothballed — superseded by [../local/README.md](../local/README.md) per the 2026-07-30 local-first decision (`expense_world_CLI/docs/decisions.md`). This file preserves what the cloud deployment was and is the **reactivation checklist** for the day a second client (iOS / web) needs an always-reachable engine.

## What the cloud deployment was

- **Engine:** Render web service, free tier → `https://expense-world-engine.onrender.com` (spins down after ~15 min idle; the wake-up latency was a motivation for going local).
- **Database:** Supabase managed Postgres, free tier. Engine connected via the pgBouncer transaction-mode pooler (port 6543, asyncpg `statement_cache_size=0` — see TODO.md).
- **Auth:** Supabase Auth (JWT) for bootstrap/PAT minting; PATs (`ewe_pat_`) for the CLI.
- **Never wired:** the daily FX cron (Render cron jobs are paid-only; see TODO.md 2026-04-28 entry). The local profile runs it via launchd instead.

## Mothball procedure (part of Step 11)

1. Final full `pg_dump` of the Supabase project (direct connection) — this is the export the local profile restores from. Keep a copy with the iCloud backups.
2. Verify the local deployment passes its verification gate (local README).
3. Let the Supabase project pause (free-tier idle). **Do not rely on it surviving** — long-paused free projects can eventually be removed; the local Postgres + rotated backups are the truth from this point.
4. Render service can be suspended or left idle (stateless — nothing to lose).

## Reactivation checklist (iOS / multi-client day)

1. Create (or revive) a Supabase project; apply `sql/001`→current, or restore the newest local backup directly (schema travels inside the dump).
2. `pg_restore` the newest nightly backup — the entire ledger moves up unchanged (Postgres → Postgres, same schema, same engine).
3. Configure Supabase Auth providers (Apple + Google sign-in) — the pre-client task already flagged in `docs/roadmap.md` "Web Dashboard — Expand Later".
4. Deploy the engine (Render or any host): three env vars from `app/config.py`, pooler-aware pool sizing per the `app/config.py` comment.
5. Wire the FX daily cron in the host's scheduler (the Render steps preserved in TODO.md).
6. Repoint clients (`expense config set --engine-url ...`) — replicas auto-wipe and cold-start by design.
7. Retire the local launchd services; the Mac becomes just another client. **From this moment the cloud engine is again the single write authority — at no point do two engines accept writes.**

Architecture invariants (one engine, thin clients, §3b) are deployment-independent; iOS's offline outbox is a client-repo concern (`docs/api-design-principles.md` §3b) and needs no engine changes.
