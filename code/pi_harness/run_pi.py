#!/usr/bin/env python3
"""Run ONE forecasting call through pi (badlogic/earendil pi coding agent)
instead of our own loop, on the same prompt, the same tools and the same
condition set, and write rows in the same schema -- the pi side-by-side
trial (Nick, 2026-09-02).

    ~/Projects/xrisk-canaries/.venv/bin/python code/pi_harness/run_pi.py \
        --model anthropic/claude-fable-5 --label "Fable 5" --thinking max \
        --conditions data/combined_conditions.json \
        --out results/experiments/pi_trial/<stamp>.jsonl

What is the same: the prompt and system text (run_unified.build_prompt_joint,
SYSTEM), the questions, the conditions, the tool implementations
(tool_cli.py -> redlines.tools), the research floor, the cell cleaning and
the row schema. What differs: the loop. pi drives the model, handles the
provider, retries and the context; its JSON event stream is the trace, kept
whole beside the rows (events.jsonl) and folded into `evidence` and `usage`.

Rows carry protocol "pi-joint-<slug>-v1" and experiment "pi-trial", so no
view ever pools them with the cron's series.
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
EXT = os.path.join(ROOT, "code", "pi_harness", "redlines_tools.ts")


def load_runner():
    spec = importlib.util.spec_from_file_location("run_unified", os.path.join(ROOT, "code", "run_unified.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="pi model id, e.g. anthropic/claude-fable-5")
    ap.add_argument("--label", required=True, help="the dashboard label, e.g. 'Fable 5'")
    ap.add_argument("--thinking", default="max", help="pi thinking level: off|minimal|low|medium|high|xhigh|max")
    ap.add_argument("--conditions", default=os.path.join(ROOT, "data", "combined_conditions.json"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--workdir", default=None, help="where the trace and pieces go (default: beside --out)")
    ap.add_argument("--pi", default=os.path.expanduser("~/.local/bin/pi"))
    ap.add_argument("--experiment", default="pi-trial")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ru = load_runner()
    from redlines.llm import load_keys
    from redlines.questions import resolves_on
    load_keys()
    today = date.today()
    policies = ru.resolve_set(ru.load_policies(args.conditions), today)
    conds = list(policies["conditions"])
    groups, by_group, horizons, skipped, spec = ru.load_batch(ru.LADDER, ru.CROSS, ru.UNBATCHED)
    horizons = ru.set_horizons(policies, horizons)
    prompt, n, want_cells = ru.build_prompt_joint(groups, by_group, horizons, spec, conds, policies, today)
    qids = [q["id"] for g in groups for q in by_group[g["key"]]]
    allowed = {q["id"]: [h for h in horizons if h in q["horizons"]] for g in groups for q in by_group[g["key"]]}
    cond_ids = [c["id"] for c in conds]
    elicits = ru.set_elicits(policies)
    slug = ru.set_slug(policies)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%MZ")
    work = args.workdir or os.path.join(os.path.dirname(os.path.abspath(args.out)), f"{stamp}-{args.label.replace(' ', '_')}")
    os.makedirs(work, exist_ok=True)
    final_tool = ru.submit_tool_joint(qids, horizons, cond_ids, elicits)
    cells_tool = ru.submit_cells_tool(qids, horizons, cond_ids)
    with open(os.path.join(work, "spec.json"), "w") as f:
        json.dump({"repo": ROOT, "allowed": allowed, "want_cells": want_cells, "min_research": ru.MIN_RESEARCH,
                   "elicits": [{"key": e["key"], "fields": e["fields"]} for e in elicits],
                   "tools": {"web_search": {"description": ru.FORECAST_TOOLS[0]["description"]},
                             "read_page": {"description": ru.FORECAST_TOOLS[1]["description"]},
                             "submit_cells": {"description": cells_tool["description"]},
                             "submit_forecast": {"description": final_tool["description"]}}}, f, indent=1)
    open(os.path.join(work, "prompt.txt"), "w").write(prompt)
    open(os.path.join(work, "system.txt"), "w").write(ru.SYSTEM)
    cmd = [args.pi, "-p", "--mode", "json", "--no-builtin-tools", "--no-session", "--no-extensions", "-e", EXT,
           "--model", args.model, "--thinking", args.thinking, "--system-prompt", ru.SYSTEM, prompt]
    print(f"pi trial [{slug}] {args.label} ({args.model}, thinking {args.thinking}): {n} questions, "
          f"{want_cells} cells x {len(conds) + 1} keys, prompt {len(prompt)} chars -> {work}")
    if args.dry_run:
        print(" ".join(cmd[:-2]), "<prompt>")
        return
    env = dict(os.environ, REDLINES_PI_OUT=work, REDLINES_PI_PY=sys.executable)
    t0 = datetime.now(timezone.utc)
    with open(os.path.join(work, "events.jsonl"), "w") as ev, open(os.path.join(work, "stderr.txt"), "w") as err:
        rc = subprocess.run(cmd, stdout=ev, stderr=err, env=env, cwd=ROOT).returncode
    secs = (datetime.now(timezone.utc) - t0).total_seconds()
    print(f"  pi exited {rc} after {secs:.0f}s")

    # --- the trace -> evidence + usage, the same fields our loop stamps
    events = [json.loads(l) for l in open(os.path.join(work, "events.jsonl")) if l.strip()]
    evidence, usage, turns = [], {"turns": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "cost_usd": 0.0}, 0
    for e in events:
        if e.get("type") == "tool_execution_end" and e.get("toolName") in ("web_search", "read_page"):
            res = e.get("result")
            det = (res or {}).get("details") if isinstance(res, dict) else None
            evidence.append({"tool": e["toolName"], "args": e.get("args"), "result": det if det is not None else res})
        if e.get("type") == "message_end" and (e.get("message") or {}).get("role") == "assistant":
            u = (e["message"].get("usage") or {})
            turns += 1
            usage["input_tokens"] += int(u.get("input") or 0)
            usage["output_tokens"] += int(u.get("output") or 0)
            usage["cached_tokens"] += int(u.get("cacheRead") or 0)
            usage["cost_usd"] = round(usage["cost_usd"] + float(((u.get("cost") or {}).get("total")) or 0), 6)
    usage["turns"] = turns
    usage["harness"] = "pi"
    usage["seconds"] = round(secs)
    # --- the answer: pieces then the final, merged as run_one_joint merges
    received, delivery = {}, {"cells_calls": 0, "cells_in_pieces": 0, "cells_in_final": 0}
    pieces = os.path.join(work, "pieces.jsonl")
    if os.path.exists(pieces):
        for l in open(pieces):
            if not l.strip():
                continue
            p = json.loads(l)
            delivery["cells_calls"] += 1
            for f in p.get("forecasts") or []:
                if isinstance(f, dict) and f.get("question_id") in allowed and f.get("horizon") in allowed[f["question_id"]]:
                    received[(f["question_id"], f["horizon"])] = f
    delivery["cells_in_pieces"] = len(received)
    final_path = os.path.join(work, "final.json")
    answer = json.load(open(final_path))["answer"] if os.path.exists(final_path) else {}
    for f in answer.get("forecasts") or []:
        if isinstance(f, dict) and "question_id" in f and "horizon" in f:
            received[(f["question_id"], f["horizon"])] = f
            delivery["cells_in_final"] += 1
    answer["forecasts"] = list(received.values())
    grids = ru.clean_forecasts_joint(answer.get("forecasts"), {q: set(h) for q, h in allowed.items()}, horizons, cond_ids)
    got = sum(len(v) for g in (grids or {}).values() for v in g.values())
    elicited = {e["key"]: ru.clean_elicit(answer.get(e["key"]), e) for e in elicits} if elicits else None
    print(f"  {got}/{want_cells * (len(conds) + 1)} cells x conditions; delivery {delivery}; "
          f"elicited {[k for k, v in (elicited or {}).items() if v]}; {len(evidence)} evidence; "
          f"{turns} turns; ${usage['cost_usd']}")
    read = {(e.get("args") or {}).get("url") for e in evidence if e["tool"] == "read_page"}
    unread = [u for u in (answer.get("key_sources") or []) if isinstance(u, str) and u not in read]
    hits = sum(len((e.get("result") or {}).get("results") or []) for e in evidence if e["tool"] == "web_search" and isinstance(e.get("result"), dict))
    block = ru.conditions_block(conds, policies)
    import hashlib
    sha = hashlib.sha256(block.encode()).hexdigest()
    cgroups = {g["key"]: g for g in ru.set_groups(policies)}
    stamps = {ru.UNCONDITIONAL_KEY: None}
    for c in conds:
        g = cgroups.get(c.get("group")) if cgroups else None
        stamps[c["id"]] = {"id": c["id"], "leap_id": c.get("leap_id"), "label": c["label"],
                           "source": ((g or {}).get("source") or {}).get("wave") or policies["source"]["wave"],
                           "sha256": sha, "set": slug, **({"group": c["group"]} if cgroups else {})}
        if c.get("value") is not None:
            stamps[c["id"]]["value"] = c["value"]
        elif c.get("field"):
            key = c.get("elicit") or (elicits[0]["key"] if elicits else None)
            stamps[c["id"]]["value"] = ((elicited or {}).get(key) or {}).get(c["field"])
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run_id = stamp
    call_id = f"{run_id}:pi#1:{args.label}"
    rows, first = [], True
    for key in [ru.UNCONDITIONAL_KEY] + cond_ids:
        grid = (grids or {}).get(key) or {}
        for g in groups:
            for q in by_group[g["key"]]:
                rows.append({
                    "run_id": run_id, "elicited_at": at, "question_id": q["id"], "model": args.model,
                    "label": args.label, "forecasts": grid.get(q["id"]),
                    "rationale": answer.get("rationale"), "key_sources": answer.get("key_sources"),
                    "grounded": hits > 0, "search_hits": hits,
                    "evidence": evidence if first else [], "usage": usage if first else None,
                    "delivery": dict(delivery) if first else None, "unread_sources": unread if first else None,
                    "resolves_on": {h: resolves_on(h, today, spec) for h in horizons},
                    "run_date": today.isoformat(), "protocol": f"pi-joint-{slug}-v1",
                    "condition_set": slug, "call_id": call_id, "attempts": 1, "batch_size": len(qids),
                    "cause": q.get("cause"), "group": g["key"], "asked_horizons": q.get("horizons"),
                    "raw_forecasts": (None if grids else answer.get("forecasts")) if first else None,
                    "condition": stamps[key],
                    "elicited": ({**(elicited or {}), "target_date": next((e.get("target_date") for e in elicits if e.get("target_months")), elicits[0].get("target_date") if elicits else None),
                                  "targets": {e["key"]: e.get("target_date") for e in elicits}} if elicits else None),
                    "experiment": args.experiment, "arm": "pi#1",
                    "joint_conditions": [ru.UNCONDITIONAL_KEY] + cond_ids,
                    "panel": None, "harness": {"name": "pi", "thinking": args.thinking, "trace": os.path.relpath(os.path.join(work, "events.jsonl"), ROOT)},
                })
                first = False
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"  {len(rows)} rows -> {args.out}")


if __name__ == "__main__":
    main()
