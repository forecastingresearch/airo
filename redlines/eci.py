"""redlines.eci -- the single source of truth for the Epoch Capabilities Index.

Every ECI number the repo uses comes from a dated snapshot of Epoch's index
under data/, named epoch_capabilities_index_YYYY-MM-DD.csv. Nothing is
hand-copied (docs/model-set.md). This module is the one place that knows
which snapshots exist, which is newest, how old it is, and how to read
either of the two formats they come in:

  rank format     rank,model,eci,ci_low,ci_high,retrieved -- the leaderboard
                  as retrieved by hand from epoch.ai, integer scores with a
                  confidence interval (2026-07-07: 188 models; 2026-08-28:
                  the top 17).
  raw export      Epoch's own CSV export: one row per model VARIANT
                  ("Model version" gpt-5.6-sol_high, "ECI Score" 161.08,
                  "Model name" GPT-5.6 Sol, ...), no interval. Read as the
                  best variant per model name, which is how the frontier
                  history and the metr_graph projection already read it
                  (code/make_eci_self_conditions.py, code/eci_projection_metrgraph.py).

Two vintages matter, for two different reasons:

  PINNED   the vintage Graph 4's capability-vs-skill ladder was built on.
           Refreshing it moves every tracked artifact, so it is a deliberate,
           separate change (re-baseline tests/), not something a newer file
           in data/ does on its own.
  latest() the newest snapshot by date in the filename. This is what the
           MODEL PANEL is selected from (redlines.registry.panel): the k
           highest-ECI models the registry can run. A panel picked from a
           stale index is a stale panel, so anything that selects from
           latest() checks its age and WARNS past STALE_AFTER_DAYS.

    python3 -m redlines.eci          # snapshots, age, the current top of the index
"""
import csv
import sys
from datetime import date
from pathlib import Path

from .config import REPO_ROOT

SNAPSHOT_DIR = REPO_ROOT / "data"
SNAPSHOT_PREFIX = "epoch_capabilities_index_"
SNAPSHOT_URL = "https://epoch.ai/benchmarks/eci"

# Graph 4's vintage (docs/model-set.md). The registry's per-model `eci` field
# is read from this file and only falls back to latest() for models the
# pinned file predates.
PINNED = date(2026, 7, 7)

# About a month (project lead, 2026-08-28). The index moves whenever a frontier
# model ships; a panel chosen on a snapshot older than this may no longer be
# the top of the index.
STALE_AFTER_DAYS = 31


def snapshot_path(day):
    return SNAPSHOT_DIR / f"{SNAPSHOT_PREFIX}{day.isoformat()}.csv"


def snapshots():
    """[(date, path)] of every snapshot under data/, oldest first."""
    out = []
    for p in SNAPSHOT_DIR.glob(f"{SNAPSHOT_PREFIX}*.csv"):
        stamp = p.stem[len(SNAPSHOT_PREFIX):]
        try:
            out.append((date.fromisoformat(stamp), p))
        except ValueError:
            continue           # not a dated snapshot; not ours to read
    return sorted(out)


def latest():
    """(date, path) of the newest snapshot. Raises if there is none."""
    snaps = snapshots()
    if not snaps:
        raise FileNotFoundError(f"no {SNAPSHOT_PREFIX}*.csv under {SNAPSHOT_DIR}")
    return snaps[-1]


def _rank_format(rows):
    out = {}
    for r in rows:
        out[r["model"]] = {"model": r["model"], "eci": float(r["eci"]),
                           "ci_low": int(r["ci_low"]) if r.get("ci_low") else None,
                           "ci_high": int(r["ci_high"]) if r.get("ci_high") else None,
                           "rank": int(r["rank"]), "retrieved": r.get("retrieved")}
    return out


def _raw_export(rows):
    best = {}
    for r in rows:
        name, score = r.get("Model name") or r.get("Model version"), r.get("ECI Score")
        if not name or not score:
            continue
        s = float(score)
        if name not in best or s > best[name]["eci"]:
            best[name] = {"model": name, "eci": s, "ci_low": None, "ci_high": None,
                          "variant": r.get("Display name") or r.get("Model version"),
                          "release_date": r.get("Release date")}
    for i, rec in enumerate(sorted(best.values(), key=lambda x: -x["eci"]), 1):
        rec["rank"] = i
    return best


def load(path):
    """One snapshot -> {model name: {model, eci, ci_low, ci_high, rank, ...}}.

    Either format. Model names are Epoch's display names, the same strings
    redlines.registry keys `epoch_name` on.
    """
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        rows = list(reader)
    if "eci" in fields and "model" in fields:
        return _rank_format(rows)
    if "ECI Score" in fields:
        return _raw_export(rows)
    raise ValueError(f"{path}: not an ECI snapshot in either known format (columns {fields})")


def age_days(today=None):
    """Days since the newest snapshot was retrieved (by its filename date)."""
    today = today or date.today()
    day, _ = latest()
    return (today - day).days


def stale_message(today=None):
    """The WARN text if the newest snapshot is older than STALE_AFTER_DAYS, else None."""
    today = today or date.today()
    day, path = latest()
    age = (today - day).days
    if age <= STALE_AFTER_DAYS:
        return None
    return (f"ECI snapshot {path.relative_to(REPO_ROOT)} is {age} days old "
            f"(> {STALE_AFTER_DAYS}); the model panel is selected from it and may no "
            f"longer be the top of the index. Retrieve a new one from {SNAPSHOT_URL} as "
            f"data/{SNAPSHOT_PREFIX}{today.isoformat()}.csv")


def warn_if_stale(today=None, file=sys.stderr):
    """Print the staleness WARN (if any) to `file`; return the message or None."""
    msg = stale_message(today)
    if msg:
        print(f"WARN: {msg}", file=file)
    return msg


def ranked(path=None):
    """The snapshot's models, best first: by ECI descending, then Epoch's rank."""
    idx = load(path or latest()[1])
    return sorted(idx.values(), key=lambda r: (-r["eci"], r["rank"]))


def main(argv=None):
    today = date.today()
    snaps = snapshots()
    print(f"ECI snapshots under {SNAPSHOT_DIR.relative_to(REPO_ROOT)}/:")
    for day, p in snaps:
        idx = load(p)
        tag = "  <- pinned (Graph 4)" if day == PINNED else ""
        tag += "  <- latest" if (day, p) == snaps[-1] else ""
        print(f"  {day}  {len(idx):4d} models  {p.name}{tag}")
    day, path = latest()
    print(f"\nlatest is {(today - day).days} days old (stale after {STALE_AFTER_DAYS})")
    msg = warn_if_stale(today, file=sys.stdout)
    if not msg:
        print("ok: not stale")
    print("\ntop of the index:")
    for r in ranked(path)[:8]:
        ci = f"  [{r['ci_low']}, {r['ci_high']}]" if r.get("ci_low") is not None else ""
        print(f"  {r['rank']:2d}  {r['eci']:6.1f}  {r['model']}{ci}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
