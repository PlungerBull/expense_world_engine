# Deployment profile: local (current)

> **Status (2026-07-30): executed — this is the live deployment.** Roadmap Step 11 ran 2026-07-30: Postgres 17 + engine + FX + backups installed, contract suite passed 10/10 against `127.0.0.1:8000`, real CLI config cut over, cloud PAT revoked. The decision rationale (with rejected alternatives) lives in the CLI repo's decision record: `expense_world_CLI/docs/decisions.md` → "Local-first deployment (2026-07-30)". The cloud profile is mothballed in [../cloud/README.md](../cloud/README.md).

The whole system runs on the owner's Mac, single user, single writer:

```
CLI / TUI  →  engine (launchd, http://127.0.0.1:8000)  →  Postgres (Homebrew, localhost)
                                                              ↓ nightly pg_dump (launchd)
                                                         iCloud Drive (rotated backups)
```

**What this profile changes: nothing in `app/` or `sql/`.** The engine is stateless and env-configured; local vs cloud is a deployment difference only. Every architectural invariant holds — one engine as sole write authority, clients hold zero logic, server-first writes, §3b replica standard (see `docs/api-design-principles.md`).

## Components

| Component | Mechanism | Schedule |
|---|---|---|
| Postgres | Homebrew `postgresql` formula, `brew services` | always on |
| Engine | uvicorn via launchd (`com.expenseworld.engine`) | always on (login) |
| FX rate fetch | `python -m app.jobs.fetch_exchange_rates` via launchd | daily 16:15 UTC (after ECB publish; mirrors TODO.md schedule) |
| Backup | `pg_dump \| gzip` → iCloud Drive via launchd | nightly; keep last 30 |

## Install (one-time, in order)

1. **Postgres:** `brew install postgresql` (current default formula) → `brew services start postgresql...`. Create the database and role for the engine.
2. **Auth stand-in:** the migrations and engine expect the Supabase auth surface (`sql/005_rls_policies.sql` uses `auth.uid()`; `sql/006_auth_trigger.sql` triggers on `auth.users`). Locally, create a minimal `auth` schema: an `auth.users` table (the owner's existing `user_id` UUID as its one row, matching the cloud export so every ledger row stays owned) and an `auth.uid()` stub. PAT auth is engine-native (`personal_access_tokens`, migration 016) and works unchanged; locally a PAT is minted by direct insert (SHA-256 + `token_prefix`), so no JWT provider is needed at all. The SQL is committed here: [000_auth_standin.sql](000_auth_standin.sql).
3. **Schema:** run `sql/001` → `sql/017` in order against the local database.
4. **Engine env:** `.env` with the three settings from `app/config.py` (`supabase_url`, `supabase_db_url`, `supabase_jwt_secret`) pointed at localhost. Direct connection (no pgBouncer locally) → per the comment in `app/config.py`, drop `db_pool_max_size` to ~20.
5. **Data migration:** `pg_dump` the Supabase project over its **direct** connection string (not the pooler), restore into local Postgres. Verify: row counts per table match, and the `users.id` matches the auth stand-in row.
6. **Services:** install the three launchd plists (templates below), `launchctl load`, verify each fired once.
7. **FX catch-up:** trigger the fetch job once (verify `GET /v1/exchange-rates?target=PEN&base=USD` returns a current rate). Provider is **fawazahmed0/currency-api** (Frankfurter was dropped 2026-07-30 — ECB rates carry no PEN; see the job's docstring and TODO.md). Before importing historical spreadsheet data, run the **historical backfill** (TODO.md item — the job module's `_fetch_currency_api(version="YYYY-MM-DD")` wraps the dated endpoint) and a home-currency recalc so history converts at true point-in-time rates.

## Verification gate (before repointing the real CLI)

Run with `EXPENSE_CONFIG`/`EXPENSE_CACHE` pointed at a temp dir (the CLI's isolation levers — `expense_world_CLI/docs/cli-runtime.md` "Working against the live engine"):

1. `expense config set --engine-url http://127.0.0.1:8000 --token <local PAT>` → `expense ping`
2. `expense sync --full` → row counts match the cloud ledger
3. Contract suite against localhost: `PYTEST_LIVE=1 EXPENSE_PAT=<local PAT> EXPENSE_ENGINE_URL=http://127.0.0.1:8000 pytest tests/contract` (run from the CLI repo)
4. Only then: repoint `~/.expense-config` (this auto-wipes the SQLite replica by design; cold start against localhost is instant)

## Backup & restore

- **Backup:** nightly `pg_dump --format=custom` (already compressed; no extra gzip) → `~/Library/Mobile Documents/com~apple~CloudDocs/expense_world_backups/`, filename datestamped, delete oldest beyond 30 — script: [backup.sh](backup.sh). **With one machine, this is the ledger's survival — treat a silently-failing backup as a sev-1.** The plist writes a datestamped log line; check it when in doubt.
- **Restore drill (monthly, recommended):** restore the newest dump into a scratch database, `SELECT count(*) FROM expense_transactions`, drop it. A backup that's never been restored is a hope, not a backup.
- **Full recovery:** fresh Postgres → auth stand-in → `pg_restore` newest dump → restart engine → clients cold-start via `expense sync --full`.

## launchd templates

Instantiate with real absolute paths during Step 11 (launchd does not expand `~` or env vars in `ProgramArguments`); keep instantiated copies in `~/Library/LaunchAgents/`, not in this repo.

`com.expenseworld.engine.plist` — `ProgramArguments: [<venv>/bin/uvicorn, app.main:app, --host, 127.0.0.1, --port, 8000]`, `WorkingDirectory: <engine checkout>`, `RunAtLoad: true`, `KeepAlive: true`.

`com.expenseworld.fx-fetch.plist` — `ProgramArguments: [<venv>/bin/python, -m, app.jobs.fetch_exchange_rates]`, `WorkingDirectory: <engine checkout>`, `StartCalendarInterval: {Hour: 11, Minute: 15}` (16:15 UTC ≈ 11:15 Lima, UTC−5 year-round).

`com.expenseworld.backup.plist` — `ProgramArguments: [<engine checkout>/deploy/local/backup.sh]`, `StartCalendarInterval: {Hour: 2, Minute: 30}`. `backup.sh` (committed here once written during execution): pg_dump → gzip → iCloud path → rotate 30 → log line.
