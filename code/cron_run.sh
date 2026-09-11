#!/usr/bin/env bash
# Scheduled forecast run. Lives on the box that serves airo.forecastingresearch.org, fired weekly
# by cron — FRIDAYS 12:00 UTC = 05:00 PT (moved from Mondays 2026-08-14) — and
# safe to run by hand for a one-off. The crontab is the authority on timing;
# this line is a comment and will drift if you let it.
#
# WHY THIS EXISTS
#   Graph 1 becomes the dashboard's headline only once several months of
#   forecasts have accrued (2026-08-10 call). That is the one deliverable whose
#   cost is elapsed time rather than effort, so the job starts now — months
#   before the over-time view it feeds is due. The unified-batch cutover
#   (2026-08-14) retired the earlier series, so the Timeline panel is rebuilding
#   from one snapshot and every run from here is load-bearing.
#
# WHAT IT RUNS
#   The WHOLE question set, in one call per model (code/run_unified.py
#   --joint), with NO relation between the questions stated in the prompt --
#   coherence is measured afterwards, never bought. The set is 35 questions in
#   5 groups (four AI incident types x 8 severity rungs, plus three
#   cross-cutting questions) over six horizons -- two of them ROLLING, so each
#   run stamps an absolute resolves_on per row. ONE call per model since
#   2026-09-02 (--repeats 1): the same-day repeats that preceded it measured a
#   re-asking spread nobody read as accuracy, and their budget went to the
#   agentic harness instead. The views draw no interval on a single-draw day.
#
# WHICH MODELS
#   THE PANEL (2026-08-28): run_unified.py --model-set defaults to eci_topk,
#   the four highest-ECI models the registry can run, re-ranked each run from
#   the newest data/epoch_capabilities_index_*.csv (redlines/eci.py). The run
#   prints the snapshot's date and age and WARNs in this log when the snapshot
#   is older than a month -- that WARN means: retrieve a new snapshot, the
#   panel may have moved. Rows stamp panel={set,k,snapshot}. The frontier five
#   the series ran on until then are `--model-set frontier5`.
#
# OUTPUT
#   One dated file per run under results/runs/. Never appends to the shared
#   results/forecast_runs.jsonl: this box's checkout is rsynced from a laptop,
#   and two writers on one tracked file means a merge conflict every week.
#   redlines/runlog.py::load_runlog() reads the log and this directory.
#
#   Pull them down with:
#     rsync -avz <box>:Projects/redlines/results/runs/ results/runs/
#
# SETUP
#   Keys live in ~/.config/redlines/env (mode 600), one KEY=value per line:
#   ANTHROPIC / OPENAI / GEMINI / GOOGLE / XAI / TAVILY (METACULUS is no
#   longer read; the lookup left the toolset on 2026-09-02). They are
#   sourced into the environment here; redlines.llm.load_keys() reads the SAME
#   file (and never overrides a variable already set), so the run works whether
#   or not this script sourced it first.
#
#   PY just has to be an interpreter with litellm — `pip install -e '.[acquire]'`
#   in any venv. It points at the xrisk-canaries venv on this box only because
#   that is where litellm was already installed; override with REDLINES_PY.
set -euo pipefail

# The repo is wherever this script lives; PY is any interpreter with litellm
# (`pip install -e '.[acquire]'`): the repo's own venv when there is one, else
# whatever REDLINES_PY names.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -z "${REDLINES_PY:-}" ] && [ -x "$REPO/.venv/bin/python" ]; then
    REDLINES_PY="$REPO/.venv/bin/python"
fi
PY="${REDLINES_PY:-$HOME/Projects/xrisk-canaries/.venv/bin/python}"
ENVFILE="$HOME/.config/redlines/env"

STAMP="$(date -u +%Y-%m-%dT%H%MZ)"
OUT="$REPO/results/runs/$STAMP.jsonl"
LOG="$HOME/logs/forecast-$STAMP.log"
mkdir -p "$REPO/results/runs" "$HOME/logs"

exec >>"$LOG" 2>&1
echo "=== $STAMP  scheduled run ==="

if [ ! -r "$ENVFILE" ]; then
    echo "FATAL: $ENVFILE missing or unreadable — no API keys, nothing to do."
    exit 1
fi
set -a; . "$ENVFILE"; set +a

cd "$REPO"
# ONE call per model, every question. The second command that used to run here
# forecast XPT question #2 (a natural pandemic), which was held out of the batch
# because it nested under no rung of the bio ladder. The Auto-ARC set retired
# that question and holds nothing out — config.UNBATCHED is empty — so the
# command is gone, along with code/run_forecasts.py, which imported a
# STABLE_SUBSET that no longer exists and would now fail on import.
# THE SINGLE INSTRUMENT (2026-08-27, --joint): each call answers every cell
# unconditionally AND under each condition of the set. The unconditional
# slice lands in $OUT (this series); the whole instrument is appended to
# results/conditional_runs_<slug>.jsonl for the conditional panels. The plain
# batch (--unconditional N) is deprecated.
# THE COMBINED INSTRUMENT (2026-08-28 evening): the LEAP policies AND the
# six-month capability conditions in one call, on THE PANEL (the four highest-
# ECI models; --model-set defaults to it). 2,520 probabilities a call, so two
# workers (three rate-limit the Anthropic endpoints when anything else runs on
# this box) and a 30-minute request timeout (GPT-5.5 Pro overran litellm's
# ten in the pilot).
# ONE ELICITATION PER MODEL since 2026-09-02 (a project decision): the repeats
# bought a re-asking spread nobody reads as accuracy, and the budget goes to
# the agentic harness instead -- every model now searches iteratively and
# reads pages (redlines/tools.py, MAX_ROUNDS in run_unified.py), so a
# call is longer and dearer than it was. 4 models x 1 = 4 calls. The views
# draw no interval on a single-draw day; the dots and the median are the
# reading.
export REDLINES_LLM_TIMEOUT="${REDLINES_LLM_TIMEOUT:-1800}"
# The runner exits non-zero when any panel model fails (the date cannot
# publish without all of them), which under `set -e` stops this script before
# the axes instruments spend on a date that will not publish. Its preflight
# also refuses a live-ECI snapshot older than ten days
# (code/check_live_eci_snapshot.py); refresh it weekly from a machine with Node +
# Playwright and rsync data/ over when that WARN turns into a failure.
if ! "$PY" code/run_unified.py --joint --repeats 1 --workers 2 \
        --conditions data/combined_conditions.json --out "$OUT"; then
    echo "FATAL: the combined instrument did not complete for every panel model; see above. Skipping the axes instruments and publication."
    exit 1
fi

# A run that produced nothing is a failure worth seeing in the log, not an
# empty file that quietly widens a gap in the time series.
if [ ! -s "$OUT" ]; then
    echo "FATAL: no rows written — removing empty $OUT"
    rm -f "$OUT"
    exit 1
fi
echo "=== done: $(wc -l < "$OUT") rows -> $OUT ==="

# THE AXES INSTRUMENTS (2026-09-03, the FRI economist's spec of 2026-09-02): the same
# questions at 2030/2050/2100 only, conditional on FIXED levels of an x-axis
# quantity -- for the dashboard the LEAP Wave 11 revenue run-rate, the LEAP
# Wave 8 year of Expert AGI and the frontier ECI (data/axes_conditions.json);
# for the paper LEAP's US GDP growth, labor-force participation and the METR
# 80% time horizon (data/paper_axes_conditions.json). One call per model
# each, on the panel, ~1,700-1,800 probabilities a call. Each is its own
# instrument: run_unified.py writes its rows to
# results/conditional_runs_<slug>.jsonl and its unconditional slice to
# results/<slug>_runs/, never into results/runs/ -- the headline series
# stays the combined instrument's. All three instruments must complete on the
# same date before publication; a failure preserves the last successful site.
for SET in data/axes_conditions.json data/paper_axes_conditions.json; do
    echo "=== axes: $SET ==="
    if ! "$PY" code/run_unified.py --joint --repeats 1 --workers 2 --conditions "$SET"; then
        echo "WARN: axes run on $SET failed — publication will preserve the last successful site until all instruments complete"
    fi
done

# Republish the dashboard with the new data point. A publish failure must not
# mask the successful run above — log it and carry on.
if ! "$REPO/code/publish_dashboard.sh"; then
    echo "WARN: publish_dashboard.sh failed — the last successful dashboard publication remains served"
fi
