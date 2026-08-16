#!/bin/zsh
# Seed a practice user + PAT into the test database — deploy/local profile.
#
# Why this exists: the CLI's contract suite (expense_world_CLI/tests/contract)
# makes real writes against a real engine, and its cleanup is soft-delete. Run
# against `expense_world` it permanently buries tombstones in the owner's actual
# ledger — and on 2026-08-16 a mis-scoped test gate did exactly that, leaving four
# live junk accounts behind. Pointing that suite at `expense_world_test` instead
# gives it a real engine to verify against and a database nobody cares about.
#
# create-test-db.sh builds a schema-only clone with no users, so nothing can
# authenticate against it. This adds the missing pieces:
#   · an auth.users row (the auth stand-in, see 000_auth_standin.sql)
#   · the public.users row the engine's own FKs point at
#   · a user_settings row — without it every account route 422s SETTINGS_MISSING
#   · one personal_access_token, printed once in plaintext
#   · a copy of exchange_rates, which is global reference data (no user_id), so
#     cross-currency reads convert exactly as they do in production
#
# Safe to re-run; --force reseeds (drops the practice user's rows and remakes it).
#
#   deploy/local/seed-test-user.sh            # seed if absent
#   deploy/local/seed-test-user.sh --force    # reseed, new token
#
# Then start a throwaway engine over the same database and point the suite at it:
#
#   SUPABASE_DB_URL=postgresql:///expense_world_test \
#     python -m uvicorn app.main:app --port 8001
#   # (python -m uvicorn, NOT the console script — see README "TCC")
#
#   cd ../expense_world_CLI
#   PYTEST_LIVE=1 EXPENSE_PAT=<printed token> \
#     EXPENSE_ENGINE_URL=http://127.0.0.1:8001 pytest tests/contract
set -euo pipefail

export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
SRC="expense_world"
DB="expense_world_test"

# Hard allowlist, same discipline as create-test-db.sh: this script inserts
# credentials and must never be able to touch the real ledger.
if [[ "$DB" != "expense_world_test" ]]; then
  print -u2 "refusing to seed anything but expense_world_test"
  exit 1
fi

if ! psql -lqt | cut -d'|' -f1 | grep -qw "$DB"; then
  print -u2 "database '$DB' does not exist — run deploy/local/create-test-db.sh first"
  exit 1
fi

# Stable identity so re-seeding does not orphan previously-created rows.
USER_ID="00000000-0000-4000-8000-00000000c11e"
EMAIL="contract@localhost"

existing=$(psql -tAc "select count(*) from personal_access_tokens where user_id='$USER_ID' and revoked_at is null" -d "$DB")
if [[ "${1:-}" != "--force" && "$existing" != "0" ]]; then
  print "practice user already seeded in '$DB' — token is only shown at creation."
  print "re-run with --force to issue a new one."
  exit 0
fi

# The engine hashes PATs as a plain SHA-256 hex digest of the plaintext, and
# stores the first len('ewe_pat_')+4 chars as the display prefix — see
# app/helpers/auth_token.py. Mirror it exactly; drift here shows up as a 401
# that looks like a database problem.
TOKEN="ewe_pat_$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
TOKEN_HASH=$(printf '%s' "$TOKEN" | shasum -a 256 | cut -d' ' -f1)
TOKEN_PREFIX="${TOKEN:0:12}"

psql -v ON_ERROR_STOP=1 -q -d "$DB" <<SQL
begin;

insert into auth.users (id, email, created_at)
values ('$USER_ID', '$EMAIL', now())
on conflict (id) do nothing;

insert into users (id, display_name, created_at, updated_at)
values ('$USER_ID', 'Contract Practice User', now(), now())
on conflict (id) do nothing;

-- PEN home currency matches the real profile, so conversion behaves the same.
insert into user_settings (user_id, main_currency, display_timezone)
values ('$USER_ID', 'PEN', 'America/Lima')
on conflict (user_id) do nothing;

-- --force reissues: revoke every prior token rather than deleting the history.
update personal_access_tokens set revoked_at = now()
where user_id = '$USER_ID' and revoked_at is null;

insert into personal_access_tokens (id, user_id, token_hash, token_prefix, name, created_at)
values (gen_random_uuid(), '$USER_ID', '$TOKEN_HASH', '$TOKEN_PREFIX', 'contract suite', now());

commit;
SQL

# Reference data, not user data: no user_id column, so this cannot carry ledger
# rows across. Without it every cross-currency read returns a null aggregate.
#
# Truncate first. The engine's own integration suite seeds synthetic USD->PEN rows
# into this same table, and a single one of those collides with the real data on
# (base, target, rate_date) — which aborts the whole COPY at that line and silently
# leaves one row behind. That is precisely what happened on the first run of this
# script (896 expected, 1 loaded), so the refresh is not optional.
psql -v ON_ERROR_STOP=1 -q -d "$DB" -c "truncate exchange_rates;"
# -o /dev/null: the dump's set_config() call otherwise prints a stray result table.
pg_dump --data-only --table=exchange_rates "$SRC" | psql -v ON_ERROR_STOP=1 -q -o /dev/null -d "$DB"
rates=$(psql -tAc "select count(*) from exchange_rates" -d "$DB")

# Fail loudly rather than hand back a database that converts nothing.
src_rates=$(psql -tAc "select count(*) from exchange_rates" -d "$SRC")
if [[ "$rates" != "$src_rates" ]]; then
  print -u2 "exchange_rates copy incomplete: $rates of $src_rates rows landed"
  exit 1
fi

print "seeded practice user in '$DB' — $rates exchange rates available"
print ""
print "  EXPENSE_PAT=$TOKEN"
print ""
print "shown once; re-run with --force to issue a new one."
