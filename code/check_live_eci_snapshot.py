#!/usr/bin/env python3
"""Preflight for the combined instrument: the live METR frontier-ECI snapshot
(data/live_metr_eci_frontier.json) must exist and be recent.

The capability conditions quote the frontier ECI, so the prompt must not lag
the index (a cached history once told models Fable 5 was still frontier after
Astra had passed it). The snapshot is retrieved with
code/refresh_live_eci_snapshot.cjs, which needs Node + Playwright, so a box
without them cannot refresh it before every run; the gate is therefore an
age, not a same-day requirement. Default: fail past --max-age-days (10: a
weekly cron cycle plus slack, since a snapshot refreshed the day before one
run is eight days old at the next), WARN when older than a day.

    python3 code/check_live_eci_snapshot.py                 # 10-day gate
    python3 code/check_live_eci_snapshot.py --max-age-days 0  # today only
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "data" / "live_metr_eci_frontier.json"
REFRESH = "code/refresh_live_eci_snapshot.cjs"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age-days", type=int, default=10,
                    help="fail when the snapshot is older than this (default 10; 0 = today only)")
    args = ap.parse_args(argv)
    try:
        data = json.loads(SNAPSHOT.read_text())
        retrieved = datetime.fromisoformat(data["retrieved_at"].replace("Z", "+00:00")).date()
        latest = data["frontier"][-1]
    except (OSError, KeyError, IndexError, ValueError, TypeError) as e:
        raise SystemExit(f"Live METR ECI preflight failed: {e} (run {REFRESH})")
    today = datetime.now(timezone.utc).date()
    age = (today - retrieved).days
    if age > args.max_age_days:
        raise SystemExit(f"Live METR ECI snapshot is stale ({retrieved}, {age} days old; "
                         f"max {args.max_age_days}). Run {REFRESH} first.")
    if age > 0:
        print(f"WARN: live METR ECI snapshot is {age} day(s) old ({retrieved}); "
              f"refresh with {REFRESH} when Node + Playwright are available", file=sys.stderr)
    print(f"Live METR ECI snapshot: {retrieved}; {latest['model']} {latest['score']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
