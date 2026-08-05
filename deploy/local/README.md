# Deployment profile: local (current)

> **Status (2026-07-30): executed — this is the live deployment.** Roadmap Step 11 ran 2026-07-30: Postgres 17 + engine + FX + backups installed, contract suite passed 10/10 against `127.0.0.1:8000`, real CLI config cut over, cloud PAT revoked. The decision rationale (with rejected alternatives) lives in the CLI repo's decision record: `expense_world_CLI/docs/decisions.md` → "Local-first deployment (2026-07-30)". The cloud profile is mothballed in [../cloud/README.md](../cloud/README.md).

The whole system runs on the owner's Mac, single user, single writer:

```
CLI / TUI  →  engine (launchd, http://127.0.0.1:8000)  →  Postgres (Homebrew, localhost)
                                                              ↓ nightly pg_dump (launchd)
                                                         Google Drive (rotated backups)
```

**What this profile changes: nothing in `app/` or `sql/`.** The engine is stateless and env-configured; local vs cloud is a deployment difference only. Every architectural invariant holds — one engine as sole write authority, clients hold zero logic, server-first writes, §3b replica standard (see `CLAUDE.md`).

## Components

| Component | Mechanism | Schedule |
|---|---|---|
| Postgres | Homebrew `postgresql` formula, `brew services` | always on |
| Engine | uvicorn via launchd (`com.expenseworld.engine`) | always on (login) |
| FX rate fetch | `python -m app.jobs.fetch_exchange_rates` via launchd | at login + every 6h of uptime |
| Backup | `pg_dump -Fc` → Google Drive via launchd | at login + every 24h of uptime; one dump per day, keep last 30 |
| FX backfill | `python -m app.jobs.backfill_exchange_rates --from <date>` | manual, one-off (done 2026-07-31 for 2024-03-02 → today) |
| Test database | `expense_world_test`, built by [create-test-db.sh](create-test-db.sh) | manual; rebuild `--force` after a schema change |

## Install (one-time, in order)

1. **Postgres:** `brew install postgresql` (current default formula) → `brew services start postgresql...`. Create the database and role for the engine.
2. **Auth stand-in:** the migrations and engine expect the Supabase auth surface (`sql/005_rls_policies.sql` uses `auth.uid()`; `sql/006_auth_trigger.sql` triggers on `auth.users`). Locally, create a minimal `auth` schema: an `auth.users` table (the owner's existing `user_id` UUID as its one row, matching the cloud export so every ledger row stays owned) and an `auth.uid()` stub. PAT auth is engine-native (`personal_access_tokens`, migration 016) and works unchanged; locally a PAT is minted by direct insert (SHA-256 + `token_prefix`), so no JWT provider is needed at all. The SQL is committed here: [000_auth_standin.sql](000_auth_standin.sql).
3. **Schema:** run `sql/001` → the highest-numbered file in `sql/`, in order, against the local database (`sql/021` as of 2026-08-05). There is no migration runner and no `schema_migrations` table — applying a new file is a manual `psql -f`, and `deploy/local/create-test-db.sh --force` afterwards, since the test database is cloned from this schema rather than replayed from `sql/`.
4. **Engine env:** `.env` with the settings from `app/config.py` (`supabase_db_url`, optionally `db_pool_max_size`) pointed at localhost. `supabase_url` / `supabase_jwt_secret` were removed 2026-08-03 with the JWT auth branch — auth is PAT-only. Direct connection (no pgBouncer locally) → `db_pool_max_size` now **defaults** to 20 for exactly this profile (changed 2026-07-30; it previously defaulted to the cloud pooler's 50 and had to be overridden by hand). No override needed locally; `.env.example` sets it explicitly anyway for visibility.
5. **Data migration:** `pg_dump` the Supabase project over its **direct** connection string (not the pooler), restore into local Postgres. Verify: row counts per table match, and the `users.id` matches the auth stand-in row.
6. **Services:** install the three launchd plists (templates below), `launchctl load`, verify each fired once.
7. **FX catch-up:** trigger the fetch job once (verify `GET /v1/exchange-rates?target=PEN&base=USD` returns a current rate). Provider is **fawazahmed0/currency-api** (Frankfurter was dropped 2026-07-30 — ECB rates carry no PEN; see the job's docstring and TODO.md). **Historical backfill: done 2026-07-31** — `python -m app.jobs.backfill_exchange_rates --from 2024-03-02` filled 881 daily USD→PEN rows covering 2024-03-02 → today. Re-run it (idempotent; it skips dates already present) if you ever need a wider range. No home-currency recalc was needed — the ledger was empty at the time. ⚠️ **Import ordering stopped mattering as of `sql/021` (2026-08-05):** writes no longer resolve a rate, so a cross-currency transaction dated before the coverage floor now succeeds instead of returning `422 RATE_UNAVAILABLE`. It simply has no home value until a rate exists for its date — the report shows `null` plus a non-zero `unconverted_count`, and filling the gap in `exchange_rates` corrects it retroactively with no ledger write.
8. **Test database:** `deploy/local/create-test-db.sh` — builds `expense_world_test` so the suite never touches the ledger. Do this before running `pytest` for the first time; see "Test database" below for why it clones the live schema instead of replaying the migrations.

## Health check

[healthcheck.sh](healthcheck.sh) verifies the things no unit test covers — engine
reachable, connected to the *local* database, all three agents registered and
last-exited cleanly, a recent backup on disk, today's FX rate present. Exits
non-zero on any failure, so it also works as a cron/CI gate.

```
deploy/local/healthcheck.sh
```

Run it after a reboot (the Step 11.4 "survives reboot" gate) or whenever
something feels off. Every failure this session — engine on the wrong database,
backups blind to their own folder, agents never registered — would have been
caught by it; the 1200-test suite caught none of them.

## When an agent goes red

`launchctl list | grep expense` gives each agent's last exit status (second
column). The *reason* is only ever in its stdout/stderr log — some failures
appear nowhere else:

| Agent | Log |
|---|---|
| `com.expenseworld.engine` | `/tmp/expenseworld-engine.log` |
| `com.expenseworld.fx-fetch` | `/tmp/expenseworld-fx.log` |
| `com.expenseworld.backup` | `/tmp/expenseworld-backup.log` |

Note the two backup logs are different files: `backup.log` inside `backups/`
records runs that *succeeded*; `/tmp/expenseworld-backup.log` records the ones
that died before they could write that line. A failing backup is invisible in the
first and obvious in the second.

**`backup` exited 1 — two unrelated causes, distinguished by that log:**

| Log says | Cause | Fix |
|---|---|---|
| `pg_dump: … socket "/tmp/.s.PGSQL.5432" failed` | Login race: the agent beat Postgres to the socket. Fixed 2026-07-31 by the `pg_isready` wait — if it recurs, Postgres genuinely failed to start | `brew services list`, start it, re-run |
| `operation not permitted: …/backup.log` | Folder blindness: something reached `backups/` by hand | Recovery below |

**`fx-fetch` exited 1** — `/tmp/expenseworld-fx.log` says `postgres not up after 60s`. Same login race, same conclusion: the wait is in place, so reaching the timeout means Postgres genuinely failed to start. Check `brew services list`, start it, then `launchctl kickstart -p gui/$(id -u)/com.expenseworld.fx-fetch`. (Exit 2 is unrelated — a provider/HTTP failure or a missing target currency; it self-heals on the next 6-hourly fire.)

**Blindness recovery** — the folder has to be recreated *by the agent*; there is
no way to restore its sight in place:

1. Move every dump out of `backups/` up into `expense_world/`
2. `rm -rf` the `backups/` directory
3. `launchctl kickstart gui/$(id -u)/com.expenseworld.backup` — `mkdir -p` recreates it, agent-owned
4. **Verify: kickstart a second time.** It must log `SKIP already have a dump` and leave the dump count unchanged

Step 4 is not optional — it is the only reliable test. A blind agent still writes
dumps and still looks fine by file age, so `healthcheck.sh` catches it solely via
the non-zero exit status, and a passing "recent backup" line proves nothing. The
second kickstart is what proves enumeration works: if a *second* dump appears
instead of a `SKIP`, the once-per-day guard never fired and it is still blind.

## Verification gate (before repointing the real CLI)

Run with `EXPENSE_CONFIG`/`EXPENSE_CACHE` pointed at a temp dir (the CLI's isolation levers — `expense_world_CLI/docs/cli-runtime.md` "Working against the live engine"):

1. `expense config set --engine-url http://127.0.0.1:8000 --token <local PAT>` → `expense ping`
2. `expense sync --full` → row counts match the cloud ledger
3. Contract suite against localhost: `PYTEST_LIVE=1 EXPENSE_PAT=<local PAT> EXPENSE_ENGINE_URL=http://127.0.0.1:8000 pytest tests/contract` (run from the CLI repo)
4. Only then: repoint `~/.expense-config` (this auto-wipes the SQLite replica by design; cold start against localhost is instant)

## Backup & restore

- **Backup:** nightly `pg_dump --format=custom` (already compressed; no extra gzip) → `~/Library/CloudStorage/GoogleDrive-<account>/My Drive/expense_world/backups/`, filename datestamped, delete oldest beyond 30 — script: [backup.sh](backup.sh). **With one machine, this is the ledger's survival — treat a silently-failing backup as a sev-1.** The plist writes a datestamped log line; check it when in doubt.
- **Restore drill (monthly, recommended):** a backup that's never been restored is a hope, not a backup. Counting rows is not enough — a dump can restore with the right row count and wrong values. Compare sums and an ID fingerprint too:

  ```
  DUMP=~/Library/CloudStorage/GoogleDrive-*/My\ Drive/expense_world/backups/<newest>.dump
  pg_restore -l "$DUMP" > /dev/null        # TOC readable = archive not truncated
  createdb expense_world_restoredrill
  pg_restore -d expense_world_restoredrill "$DUMP"   # must exit 0 with empty stderr
  # then run against BOTH databases and diff:
  #   select count(*) ... per table (exact counts, not pg_stat n_live_tup)
  #   select sum(amount_cents), sum(amount_home_cents), sum(current_balance_cents)
  #   select md5(string_agg(id::text, ',' order by id)) from expense_transactions
  dropdb expense_world_restoredrill
  ```

  **Last run: 2026-07-31 — PASS.** Clean restore, zero warnings, all 15 tables matched exactly, financial sums and transaction-ID fingerprint identical. Note the drill only exercised contract-test residue, and the ledger was wiped clean on 2026-07-31 to start fresh — so re-run it once real transactions exist.
- **Full recovery:** fresh Postgres → auth stand-in → `pg_restore` newest dump → restart engine → clients cold-start via `expense sync --full`.

## macOS TCC constraints (learned the hard way, 2026-07-30)

launchd agents do **not** inherit your Terminal's file-access grants. macOS TCC
authorizes per-executable, and an agent that works when you run it by hand can
fail under launchd. Three distinct bites, all confirmed by experiment:

| Symptom | Cause | Fix |
|---|---|---|
| `/bin/zsh: can't open input file: …/backup.sh` | `~/Documents` is TCC-protected; `/bin/zsh` has no grant (the venv `python` does — it can read the same file) | Run the script from `~/Library/Application Support/expense_world/`, which is not protected |
| `PermissionError: …/.venv/pyvenv.cfg` at engine start | `.venv/bin/uvicorn` is a separate executable from `.venv/bin/python` and carries no grant | Invoke `python -m uvicorn` instead of the `uvicorn` console script |
| Backup rotation keeps everything; `backup.log` append fails `EPERM`; a listing of a full directory returns 0 entries | Cloud-storage paths (`~/Library/Mobile Documents` for iCloud, `~/Library/CloudStorage` for Google Drive) are TCC-protected. An agent may only enumerate and modify content **inside a directory it created itself** | Let the agent's own `mkdir -p` create the backup directory. Never pre-create or seed it from a shell or Finder |

The cloud-storage case is the dangerous one: enumeration **fails silently**,
returning an empty listing rather than an error. Rotation then deletes nothing
and the once-per-day guard never fires — the backup looks like it works.

**The rule, established by experiment (2026-07-30):** a launchd agent can create
files anywhere in these locations, but can only *see* and *modify* files under a
directory it created. Files it wrote on previous runs are visible (verified
across three consecutive runs); files another process put there are not — not
even listed. Directory ownership is what matters, not file ownership: seeding
`expense_world_backups/` with `cp` from a shell left the agent blind to the whole
folder, including dumps it had written itself. Deleting the directory and
letting the agent recreate it fixed it immediately.

**Operational consequence:** never add, copy, or move files into
`expense_world/backups/` by hand — the agent will stop being able to see the
folder's contents and will silently re-dump and never rotate. Anything kept by
hand goes *beside* it in `expense_world/`, never inside `backups/`.
No Full Disk Access grant is required.

Only the *immediate* directory matters — verified 2026-07-31 by hand-placing both
a file and a subdirectory in the agent-created `expense_world/` root: the agent
still enumerated `backups/` correctly across four consecutive runs (SKIP fired,
no duplicate dump). A hand-touched parent is fine; a hand-touched `backups/` is not.

**Layout (2026-07-31):** the agent owns `backups/` and nothing else lives there.
The only hand-kept artifact is `supabase-export-2026-07-30.dump`, the original
cloud export — the one file the agent cannot regenerate. Everything else the
backup job reproduces daily, so no manual archive folder is maintained.

```
My Drive/expense_world/
├── backups/                            ← agent-owned, hands off
└── supabase-export-2026-07-30.dump     ← migration provenance
```

**Script install step:** `backup.sh` is canonical in this repo but must be copied
to where launchd can read it. Re-run after any edit:

```
cp deploy/local/backup.sh ~/Library/Application\ Support/expense_world/backup.sh
chmod +x ~/Library/Application\ Support/expense_world/backup.sh
```

## Scheduling: uptime-based, not wall-clock (changed 2026-07-30)

The original 11:15 / 02:30 `StartCalendarInterval` entries assumed the laptop is
open at fixed hours. It isn't, and a calendar job on a closed machine simply does
not run. Both periodic agents now use `RunAtLoad` + `StartInterval` instead, so
they fire on login and every N hours of *uptime*:

- **fx-fetch** — every 6h. Idempotent per day via `ON CONFLICT DO NOTHING`, so
  extra runs cost nothing. (It used to double as the mitigation for the test
  suite's seed row landing in the ledger; that hazard is gone since the test
  database was separated — see below.)
- **backup** — every 24h, with `backup.sh` self-limiting to one dump per calendar
  day so `KEEP=30` still means 30 days of history rather than 30 dumps over an
  arbitrary window. `FORCE=1 backup.sh` overrides.

## Test database (separated 2026-07-31)

The suite runs against **`expense_world_test`**, never the ledger. Create it once
with [create-test-db.sh](create-test-db.sh); re-run with `--force` after any
schema change. `pytest` needs no flags or env — `tests/conftest.py` points itself
there.

It is cloned from the live database's schema (`pg_dump --schema-only`) rather
than replayed from `sql/001`→`017`, because those two do not produce the same
thing: the live schema came from restoring the Supabase dump (step 5 above),
where uuid-ossp sits in the `extensions` schema and column defaults read
`extensions.uuid_generate_v4()`. A fresh replay of the migrations resolves that
name differently and dies on `sql/002`. Cloning also means the suite keeps
testing the schema the engine actually runs against. Only `global_currencies` is
copied as data — the `currency_code` FK needs it.

**The hazard this removed:** until 2026-07-31 the suite ran against
`expense_world` itself. Its cleanup deletes strictly by `user_id`, but
`exchange_rates` has no `user_id` to scope by, so the seeded USD→PEN row
survived every run. Whoever writes a given day's rate first wins
(`ON CONFLICT DO NOTHING`), so a test run before the day's fetch left a
synthetic rate in the ledger that the fetch job could then never correct. That
was tolerable only while the ledger was empty; the historical backfill and real
data made it a live risk.

`conftest.py` **fails closed**: it aborts the session unless the target database
is on its allowlist, so a stray `SUPABASE_DB_URL`, a bad `TEST_DATABASE_URL`, or
a future refactor of the import order stops the run instead of quietly deleting
real rows. Same pattern as the non-local-host guard in `app/config.py`. Keep the
allowlist and `create-test-db.sh`'s `DB` name in sync.

**Still shared:** the CLI repo's `tests/contract/` hits the live engine by
design, so it writes real rows to the real ledger and leaves soft-deleted
tombstones behind (visible under `--include-deleted`). Harmless against an empty
ledger, litter once real data lands — see `expense_world_CLI/docs/cli-runtime.md`
"Working against the live engine".

## launchd templates

Instantiate with real absolute paths during Step 11 (launchd does not expand `~` or env vars in `ProgramArguments`); keep instantiated copies in `~/Library/LaunchAgents/`, not in this repo.

`com.expenseworld.engine.plist` — `ProgramArguments: [<venv>/bin/python, -m, uvicorn, app.main:app, --host, 127.0.0.1, --port, 8000]` (**not** `<venv>/bin/uvicorn` — see TCC table), `WorkingDirectory: <engine checkout>`, `RunAtLoad: true`, `KeepAlive: true`.

`com.expenseworld.fx-fetch.plist` — `ProgramArguments: [<venv>/bin/python, -m, app.jobs.fetch_exchange_rates]`, `WorkingDirectory: <engine checkout>`, `RunAtLoad: true`, `StartInterval: 21600`. Unlike `backup.sh`, this runs the repo copy directly, so a code fix is live with no reinstall step.

`com.expenseworld.backup.plist` — `ProgramArguments: [~/Library/Application Support/expense_world/backup.sh]` (**not** the repo path — see TCC table), `RunAtLoad: true`, `StartInterval: 86400`. `backup.sh`: wait-for-postgres → skip-if-dumped-today → pg_dump custom format → Google Drive path → rotate 30 → log line.

**The login race (fixed 2026-07-31):** `RunAtLoad` starts these agents in parallel with Homebrew's postgres service — launchd has no notion of one agent depending on another — so after a reboot either can reach for a socket that does not exist yet. Both now wait instead of racing, on the same 60s budget (30 tries × 2s), which is far beyond a normal local start:

| Agent | Symptom before | Wait |
|---|---|---|
| `backup` | `pg_dump` hit a missing socket, `set -e` aborted the run, no dump that day | `pg_isready` poll in `backup.sh` |
| `fx-fetch` | `asyncpg.create_pool` raised `ConnectionRefusedError` — unhandled traceback, exit 1, no rate until the next 6-hourly fire, and every cross-currency write 422s until then | retry loop in `_create_pool_waiting_for_db()` |

Note that `fx-fetch` had *no* slack to lose: it must query the DB for the target-currency list before it knows what to ask the provider for, so `create_pool` is the first thing `run()` does — the HTTP call comes after and buys it nothing. Runs that passed before the fix did so only because postgres happened to win the race. The engine was never exposed: `KeepAlive: true` makes launchd relaunch it until postgres answers. Any *new* agent that needs a service which also starts at login inherits this same exposure and needs its own wait.

Register with `launchctl bootstrap gui/$(id -u) <plist>`; force a run with
`launchctl kickstart -p gui/$(id -u)/<label>`; check state with `launchctl list | grep expense`
(second column is the last exit status — non-zero means the job failed).
