#!/usr/bin/env bash
# cron's entry for one ForecastBench round: scripts/fb_round.sh <due>, then one
# alert so someone uploads the sets before the due date ends (UTC).
#
#   5 0 25 10 * /usr/bin/flock -n /tmp/fb-round.lock <checkout>/scripts/fb_cron.sh 2026-10-25
#
# Settings come from round.env like the round's (see examples/round.env.example);
# the alert goes where the installation's alerts file says (fri_forecaster.alert).
# Output goes to $ROUNDS_DIR/<due>/logs/cron.out.
set -uo pipefail

DUE="$1"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
CONF="${FORECASTER_ROUND_ENV:-$HOME/.config/fri-forecaster/round.env}"
if [[ -f "$CONF" ]]; then set -a; source "$CONF"; set +a; fi
ROOT="${ROUNDS_DIR:-$HERE/rounds}/$DUE"
PY="${PY:-$HERE/.venv/bin/python}"
mkdir -p "$ROOT/logs"

"$HERE/scripts/fb_round.sh" "$DUE" >> "$ROOT/logs/cron.out" 2>&1
rc=$?
sets="$("$PY" -m fri_forecaster.names conditions 2>/dev/null | wc -l | tr -d ' ')"
ready="$(grep -c "build exited 0" "$ROOT/logs/round.log" 2>/dev/null)"
# The sets are uploaded by hand, so a round that is ready still needs someone:
# a notice. One that is not ready is a failed run: a page, unless the round
# already sent one (a refused key).
if [[ "$rc" == 0 && "${ready:-0}" == "$sets" ]]; then
  "$PY" -m fri_forecaster.alert send --deployment forecastbench --severity notice --title "sets ready to upload" \
    "$DUE: $sets sets. Upload by 23:59 UTC: $ROOT" >> "$ROOT/logs/cron.out" 2>&1
elif [[ ! -e "$ROOT/logs/paged" ]]; then
  why="$(grep -hE 'FATAL|gave up|could not|NOT READY|SHORT' "$ROOT/logs/round.log" "$ROOT/summary.txt" 2>/dev/null | sort -u | tail -2 | cut -c1-200)"
  "$PY" -m fri_forecaster.alert send --deployment forecastbench --severity page --title "run FAILED" \
    --log "$ROOT/logs/round.log" "$DUE: exit $rc, ${ready:-0}/$sets sets ready, due by 23:59 UTC. $why
Rerun scripts/fb_round.sh $DUE once fixed; what is done is kept." >> "$ROOT/logs/cron.out" 2>&1
fi
exit $rc
