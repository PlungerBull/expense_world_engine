#!/bin/zsh
# Create (or recreate) the test database — deploy/local profile.
#
# Why this exists: the integration suite used to run against `expense_world`,
# the same database holding the real ledger. Its fixtures insert and delete
# rows, and the exchange_rates seed in tests/conftest.py is global (no user_id),
# so a test run could land a synthetic USD->PEN rate in the ledger that the
# daily fetch job could then never correct — ON CONFLICT DO NOTHING makes the
# first writer win. Giving the suite its own database removes that class of
# hazard entirely.
#
# Safe to re-run; --force drops and rebuilds.
#
#   deploy/local/create-test-db.sh            # create if absent
#   deploy/local/create-test-db.sh --force    # drop and rebuild (after a schema change)
#
# The suite refuses to run against anything but a database on conftest.py's
# allowlist, so the name below is not arbitrary — keep the two in sync.
set -euo pipefail

export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
SRC="expense_world"
DB="expense_world_test"

# Cloned from the live database's schema rather than replayed from sql/001-017.
# Those two are NOT equivalent: the live schema came from restoring the Supabase
# dump (README step 11.3.5), where uuid-ossp lives in the `extensions` schema,
# so its column defaults read `extensions.uuid_generate_v4()`. Replaying the
# migrations against a fresh database resolves that name differently and fails
# outright on sql/002. Cloning keeps the suite testing the schema the engine
# actually runs against, and picks up future schema changes for free — re-run
# with --force after any migration.
if [[ "${1:-}" == "--force" ]]; then
  dropdb --if-exists "$DB"
fi

if psql -lqt | cut -d'|' -f1 | grep -qw "$DB"; then
  print "database '$DB' already exists — nothing to do (use --force to rebuild)"
  exit 0
fi

createdb "$DB"
pg_dump --schema-only "$SRC" | psql -v ON_ERROR_STOP=1 -q -d "$DB"

# global_currencies is reference data, not user data: conftest.py's seed account
# is PEN and the currency_code FK points here, so the suite cannot run without
# it. Everything else stays empty — tests create and clean up their own rows.
pg_dump --data-only --table=global_currencies "$SRC" | psql -v ON_ERROR_STOP=1 -q -d "$DB"

tables=$(psql -tAc "select count(*) from information_schema.tables where table_schema='public'" -d "$DB")
currencies=$(psql -tAc "select string_agg(code, ',' order by code) from global_currencies" -d "$DB")
print "created '$DB' — $tables public tables, currencies: $currencies"
