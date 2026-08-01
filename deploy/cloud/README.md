# Deployment profile: cloud (mothballed 2026-07-30)

> **Status:** mothballed — superseded by [../local/README.md](../local/README.md) per the 2026-07-30 local-first decision (`expense_world_CLI/docs/decisions.md`). This file preserves what the cloud deployment was and is the **reactivation checklist** for the day a second client (iOS / web) needs an always-reachable engine.

## What the cloud deployment was

- **Engine:** Render web service, free tier → `https://expense-world-engine.onrender.com` (spins down after ~15 min idle; the wake-up latency was a motivation for going local).
- **Database:** Supabase managed Postgres, free tier. Engine connected via the pgBouncer transaction-mode pooler (port 6543, asyncpg `statement_cache_size=0` — see the pooler comment in `app/config.py`). The test suite was validated against that pooler config.
- **Auth:** Supabase Auth (JWT) for bootstrap/PAT minting; PATs (`ewe_pat_`) for the CLI.
- **Never wired:** the daily FX cron. Render Cron Jobs are a paid-only resource type — both `render services create --type cron_job …` and the dashboard's "New + → Cron Job" return `402 Payment information is required` until a card is on file (cheapest tier ≈ $1/month, verified 2026-04-28). The local profile runs the same job via launchd instead, which is part of why the deployment went local.

## Mothball procedure (part of Step 11)

1. Final full `pg_dump` of the Supabase project (direct connection) — this is the export the local profile restores from. Keep a copy with the Google Drive backups (`deploy/local/backup.sh`'s target — iCloud was the original plan but fails silently under launchd; see the local README).
2. Verify the local deployment passes its verification gate (local README).
3. Let the Supabase project pause (free-tier idle). **Do not rely on it surviving** — long-paused free projects can eventually be removed; the local Postgres + rotated backups are the truth from this point.
4. Render service can be suspended or left idle (stateless — nothing to lose).

## Reactivation checklist (iOS / multi-client day)

1. Create (or revive) a Supabase project; apply `sql/001`→current, or restore the newest local backup directly (schema travels inside the dump).
2. `pg_restore` the newest nightly backup — the entire ledger moves up unchanged (Postgres → Postgres, same schema, same engine).
3. Configure Supabase Auth providers (Apple + Google sign-in) — the pre-client task already flagged in `docs/roadmap.md` "Web Dashboard — Expand Later".
4. Deploy the engine (Render or any host): three env vars from `app/config.py`, pooler-aware pool sizing per the `app/config.py` comment, **plus `EXPENSE_ALLOW_REMOTE_DB=1`** — `app/config.py` refuses non-local `SUPABASE_DB_URL` hosts at startup (a local-profile safeguard added 2026-07-30), and a cloud database is exactly the case that must opt in.
5. **Wire the FX daily fetch in the host's scheduler.** Without it no rows land in `exchange_rates`, and every cross-currency write (`POST /transactions`, a `PUT` that changes `date`, `POST /transactions/batch`, `POST /inbox`, `PUT /inbox/{id}` with a date change) fails `422 RATE_UNAVAILABLE` — same-currency writes are unaffected (identity short-circuit in `get_rate`). On Render, one-time via the dashboard (**New + → Cron Job**), billing required:
   - Connect the same GitHub repo as the web service · **Name:** `fetch-exchange-rates` · **Runtime:** Python
   - **Build:** `pip install -r requirements.txt` · **Command:** `python -m app.jobs.fetch_exchange_rates`
   - **Schedule:** `0 16 * * *` · **Environment:** link the web service's env group so it picks up `SUPABASE_DB_URL`
   - Keep it dashboard-managed — the web service is too, and adding a `render.yaml` Blueprint would conflict.
   - Smoke-test with **Trigger Run**, look for `inserted USD->PEN <date> = <rate>` in the logs, then confirm `GET /v1/exchange-rates?target=PEN&base=USD` returns it rather than 404.
   - Historical gaps: `python -m app.jobs.backfill_exchange_rates --from <date>` (idempotent; provider floor is 2024-03-02).
6. Repoint clients (`expense config set --engine-url ...`) — replicas auto-wipe and cold-start by design.
7. Retire the local launchd services; the Mac becomes just another client. **From this moment the cloud engine is again the single write authority — at no point do two engines accept writes.**

Architecture invariants (one engine, thin clients, §3b) are deployment-independent; iOS's offline outbox is a client-repo concern (`docs/api-design-principles.md` §3b) and needs no engine changes.
