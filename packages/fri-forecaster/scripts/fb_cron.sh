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
# a notice. One that is not ready needs them sooner: a page.
if [[ "$rc" == 0 && "${ready:-0}" == "$sets" ]]; then
  severity=notice; title="$DUE: $sets sets ready to upload"; what="Upload by 23:59 UTC: $ROOT"
else
  severity=page; title="$DUE: round exited $rc, ${ready:-0}/$sets sets ready"
  what="The sets are due by 23:59 UTC. See summary.txt in $ROOT, then rerun scripts/fb_round.sh $DUE; what is done is kept."
fi
"$PY" -m fri_forecaster.alert send --deployment forecastbench --severity "$severity" --title "$title" \
  --log "$ROOT/logs/round.log" "$what" >> "$ROOT/logs/cron.out" 2>&1
exit $rc
