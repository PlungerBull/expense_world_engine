#!/bin/zsh
# Is the local deployment actually healthy? Checks the plumbing that no unit
# test covers: engine reachable, on the right database, background agents
# registered and last-exited cleanly, backup recent, FX rate current.
#
# Run by hand any time; the natural moment is right after a reboot, which is
# the Step 11.4 "survives reboot" gate.
#   ~/Documents/productivity_world/expense_world_engine/deploy/local/healthcheck.sh

export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
DB="expense_world"
BK="$HOME/Library/CloudStorage/GoogleDrive-alexterfer@gmail.com/My Drive/expense_world/backups"
FAIL=0

ok()   { print -P "%F{green}  OK%f   $1" }
bad()  { print -P "%F{red}  FAIL%f $1"; FAIL=1 }
warn() { print -P "%F{yellow}  WARN%f $1" }

print -P "\n%BLocal deployment health%b"

# 1. Engine responding
if curl -sf --max-time 5 http://127.0.0.1:8000/health > /dev/null 2>&1; then
  ok "engine responding on 127.0.0.1:8000"
else
  bad "engine NOT responding on 127.0.0.1:8000"
fi

# 2. Postgres up and the database present
if psql -d "$DB" -tAc 'select 1' > /dev/null 2>&1; then
  ok "postgres up, '$DB' reachable"
else
  bad "cannot reach postgres database '$DB'"
fi

# 3. Engine is talking to the LOCAL database, not a remote one
conns=$(psql -d "$DB" -tAc "select count(*) from pg_stat_activity where datname='$DB' and application_name <> 'psql'" 2>/dev/null)
if [[ -n "$conns" && "$conns" -gt 0 ]]; then
  ok "engine holds $conns connection(s) to local postgres"
else
  warn "no engine connections to local postgres (idle pool, or engine points elsewhere)"
fi

# 4. All three launchd agents registered, and none exited non-zero
for label in engine fx-fetch backup; do
  line=$(launchctl list 2>/dev/null | grep "com.expenseworld.$label")
  if [[ -z "$line" ]]; then
    bad "agent '$label' NOT registered — it will not run"
  else
    # NB: `status` is a read-only builtin in zsh — do not name this variable that.
    last_exit=$(print -r -- "$line" | awk '{print $2}')
    if [[ "$last_exit" == "0" || "$last_exit" == "-" ]]; then
      ok "agent '$label' registered (last exit $last_exit)"
    else
      bad "agent '$label' last exited $last_exit"
    fi
  fi
done

# 5. A backup exists and is recent
setopt null_glob
dumps=("$BK"/expense_world-*.dump)
unsetopt null_glob
if (( ${#dumps[@]} == 0 )); then
  bad "no backups found in Google Drive folder"
else
  newest=${${(On)dumps}[1]}
  age_days=$(( ( $(date +%s) - $(stat -f %m "$newest") ) / 86400 ))
  if (( age_days <= 2 )); then
    ok "newest backup ${age_days}d old (${#dumps[@]} kept): ${newest:t}"
  else
    bad "newest backup is ${age_days} days old — backups may not be running"
  fi
fi

# 6. Today's FX rate present (cross-currency writes 422 without it)
rate=$(psql -d "$DB" -tAc "select round(rate,4) from exchange_rates where base_currency='USD' and target_currency='PEN' and rate_date=CURRENT_DATE" 2>/dev/null | tr -d ' ')
if [[ -n "$rate" ]]; then
  ok "USD->PEN rate for today present: $rate"
else
  warn "no USD->PEN rate for today yet (fx-fetch runs at login + every 6h)"
fi

if (( FAIL )); then
  print -P "\n%F{red}Something is wrong — see FAIL lines above.%f\n"
  exit 1
else
  print -P "\n%F{green}All good.%f\n"
fi
