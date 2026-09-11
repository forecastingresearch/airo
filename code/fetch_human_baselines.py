#!/usr/bin/env python3
"""Pull the human comparisons the Auto-ARC workbook points at, into one file.

Its "Human comparisons (Project)" column is the source of truth for WHICH of
our question x horizon cells a human panel has already answered, and by which
panel. The generator carries that column into data/autoarc_*.json as each
question's `human_comparisons`; this script reads it, goes to the panel's own
data, and writes the numbers to data/human_baselines.json.

    python3 code/fetch_human_baselines.py                # everything reachable
    python3 code/fetch_human_baselines.py --only XPT     # no warehouse needed
    python3 code/fetch_human_baselines.py --check        # verify, write nothing

Nothing here decides which comparisons exist. A cell gets a human number
because a row in her sheet names a project for it; a project we cannot reach
is recorded as unavailable, with the reason, rather than dropped. That is the
whole design: we pull what she asked for and we say what we could not get.

WHAT IS AND IS NOT DERIVED FROM HER SHEET

  from the sheet   which cells have a comparison, which project, the prior
                   question's own text, criteria, severity and elicitation
                   date (her third sheet, carried as `prior_work`).
  from this file   where each project's numbers physically live (SOURCES
                   below) and how the panel is aggregated. Her sheet names
                   "XPT"; only we know that means forecasts_anon.csv at a
                   pinned commit, own-belief rows, median by group.

THE NUMBERS ARE NOT ADJUSTED. Each prior question was asked in its own words,
and several differ from ours in ways that move the answer -- LEAP's "50 deaths
or $100 billion" against our 10,000-deaths rung, XPT counting deaths against
Auto-ARC's attributable excess mortality, and both prior panels requiring the
AI's involvement within a year of the event where ours sets no time limit. Every
baseline carries the source question so the reader can see the pairing; none is
silently reconciled. Reconciliation is a judgement, and a judgement made inside
a fetch script is a judgement nobody sees.

One number to expect a query about: LEAP's own background text quotes the XPT
report for AI catastrophe at 2100 as 2% super / 12% expert, and re-aggregating
the individual panel here gives 2.125% / 10.0%. The superforecaster figure
matches; the expert one does not, and the panel is complete (169 forecasters,
89 supers + 80 experts, none unclassified), so the difference is in how the
report aggregated rather than in who was counted.

OUTPUT (data/human_baselines.json) is a COMMITTED artifact, not a build step.
The box that runs the weekly forecast has no FRI warehouse credentials and no
reason to; these numbers move only when a new panel reports. Refresh by hand,
on a machine with warehouse access, and commit the result.
"""
import argparse
import csv
import json
import os
import re
import statistics as st
import subprocess
import sys
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CROSS = os.path.join(ROOT, "data", "autoarc_crosscutting.json")
LADDER = os.path.join(ROOT, "data", "autoarc_ladder.json")
OUT = os.path.join(ROOT, "data", "human_baselines.json")

# ── where each named prior question's numbers live ───────────────────────────
# Keyed by the id read_prior_work() gives one row of her "Questions from prior
# FRI work" sheet, so every entry joins to a question SHE wrote down. A project
# she names with no entry here is reported unavailable, not guessed at.
#
# This is the one thing her workbook does not tell us and cannot be derived:
# that "XPT" means a set name in a pinned git checkout and "LEAP" means a
# question group in a BigQuery warehouse. Everything above it — which cells
# have a comparison, which project, which of that project's questions — comes
# from her file.
SOURCES = {
    "xpt-general-catastrophe-10-of-population": {
        "kind": "xpt", "set_name": "9. Total Catastrophic Risk"},
    "xpt-ai-catastrophe-10-of-population": {
        "kind": "xpt", "set_name": "3. AI Catastrophic Risk"},
    "leap-general-catastrophe-10-of-population": {
        "kind": "leap", "question_group_name": "Total Catastrophic Risk",
        "survey_name": "Wave 9: Risks"},
    "leap-ai-catastrophe-10-of-population": {
        "kind": "leap", "question_group_name": "AI Catastrophic Risk",
        "survey_name": "Wave 9: Risks"},
    "leap-ai-catastrophe-50-deaths-or-100b": {
        "kind": "leap", "question_group_name": "First Major AI Global Harm Event (1)",
        "survey_name": "Wave 9: Risks"},
}

# Which prior question a cell pairs with, when her column names only a project.
# LEAP filed three AI-catastrophe questions and "LEAP" alone does not say which
# — so this table exists, and it is exactly the knowledge that belongs in her
# file rather than ours. Every entry here disappears the day her comparison
# column carries "LEAP:leap-ai-catastrophe-10-of-population" instead of "LEAP";
# split_comparison() in the generator already parses that form.
JOIN = {
    ("catastrophe:general", "XPT"): "xpt-general-catastrophe-10-of-population",
    ("catastrophe:general", "LEAP"): "leap-general-catastrophe-10-of-population",
    ("catastrophe:ai", "XPT"): "xpt-ai-catastrophe-10-of-population",
    ("catastrophe:ai", "LEAP"): "leap-ai-catastrophe-10-of-population",
    ("ladder:ai:10k", "LEAP"): "leap-ai-catastrophe-50-deaths-or-100b",
}


# ── XPT ──────────────────────────────────────────────────────────────────────
XPT_REPO = "https://github.com/forecastingresearch/xpt-lib.git"
XPT_COMMIT = "251acb5254448c9bdb4caa4b5011481c2c149aef"
XPT_CACHE = os.path.expanduser("~/.cache/redlines/xpt-lib")
XPT_ELICITED = "2022-10-01"     # her sheet's date for the XPT panel


def xpt_checkout(cache=XPT_CACHE):
    """The pinned xpt-lib working tree, cloned on first use.

    forecasts_anon.csv is 18 MB of individual forecasts and is not vendored
    anywhere in this repo. The published XPT tables give medians only at 2100;
    The workbook's comparison column asks for 2030 and 2050 as well, so the panel
    has to be re-aggregated from the individual rows. Pinned to a commit so the
    same call gives the same medians in a year.
    """
    if not os.path.isdir(os.path.join(cache, ".git")):
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", XPT_REPO, cache], check=True)
    subprocess.run(["git", "-C", cache, "checkout", "--quiet", XPT_COMMIT],
                   check=True)
    return cache


def xpt_medians(cache, set_name):
    """-> {year: {group: {median, mean, n, p25, p75}}} for one XPT question set.

    Own-belief, final forecasts only. XPT ran four persuasion stages and asked
    each forecaster both for their own view and for their guess at the other
    group's; `isCurrent` picks the final row and the "(Your beliefs)" suffix
    picks the own view. Aggregating without either filter mixes a forecaster's
    own belief with their model of someone else's.

    Reproduces xrisk-canaries/forecast/xpt_seed.py, which cuts to each
    question's longest horizon; this keeps every horizon, because the
    comparison column asks for all three.
    """
    data = os.path.join(cache, "data")
    supers = {r["x"] for r in csv.DictReader(open(os.path.join(data, "supers_anon.csv")))}
    experts = {r["userId"] for r in csv.DictReader(
        open(os.path.join(data, "expertsG1_anon.csv")))}
    agg = {}
    with open(os.path.join(data, "forecasts_anon.csv"), newline="") as f:
        for r in csv.DictReader(f):
            if r["setName"] != set_name or r["isCurrent"] != "TRUE":
                continue
            if "(your beliefs)" not in r["questionName"].lower():
                continue
            if r["answerText"].strip() != "Probability":
                continue
            m = re.search(r"(\d{4})", r["questionName"])
            if not m:
                continue
            grp = ("superforecaster" if r["userId"] in supers else
                   "expert" if r["userId"] in experts else None)
            if not grp:
                continue
            try:
                v = float(r["forecast"])
            except (TypeError, ValueError):
                continue
            agg.setdefault(m.group(1), {}).setdefault(grp, []).append(v)
    return {y: {g: summarize(vs) for g, vs in groups.items()}
            for y, groups in agg.items()}


def summarize(vs):
    """The five numbers we keep per panel. Percent, as both panels elicited."""
    vs = sorted(vs)
    return {
        "median": round(st.median(vs), 4),
        "mean": round(st.mean(vs), 4),
        "p25": round(vs[max(0, round(0.25 * (len(vs) - 1)))], 4),
        "p75": round(vs[max(0, round(0.75 * (len(vs) - 1)))], 4),
        "n": len(vs),
    }


# ── LEAP ─────────────────────────────────────────────────────────────────────
# The FRI data warehouse's BigQuery project, from the environment: it is
# internal infrastructure, not part of the public record.
LEAP_PROJECT = os.environ.get("FRI_WAREHOUSE_PROJECT") or sys.exit(
    "FRI_WAREHOUSE_PROJECT is unset: the BigQuery project of the FRI data warehouse (internal)")

# Unconditional forecasts only. LEAP asks the catastrophe pair four times per
# horizon -- once outright and once under each of three AI-progress scenarios
# -- and only the outright one answers the same question we do.
LEAP_SQL = """
with resp as (
  select q.question_horizon_date h,
         b.participant_group_name pg,
         r.response_value v
  from `{p}.fact.fact_response` r
  join `{p}.dim.dim_question` q using (question_id)
  join `{p}.dim.dim_question_group` qg using (question_group_id)
  join `{p}.dim.dim_survey` s on s.survey_id = qg.survey_id
  join `{p}.br.br_participant_group` b on b.participant_id = r.participant_id
  where qg.question_group_name = @group_name
    and s.survey_name = @survey_name
    and q.scenario_id is null
    and r.response_is_current
    and not r.response_should_be_excluded
    and r.response_value is not null
)
select h, pg, count(*) n, avg(v) mean,
       approx_quantiles(v, 100)[offset(25)] p25,
       approx_quantiles(v, 100)[offset(50)] median,
       approx_quantiles(v, 100)[offset(75)] p75
from resp group by 1, 2
"""


def leap_client():
    try:
        from google.cloud import bigquery
    except ImportError:
        sys.exit(
            "google-cloud-bigquery is not importable. It is not a dependency of\n"
            "this repo -- run this script with an interpreter that has it, e.g.\n"
            "  ~/Projects/data-warehouse/.venv/bin/python code/fetch_human_baselines.py\n"
            "or pass --only XPT to skip the warehouse entirely.")
    return bigquery.Client(project=LEAP_PROJECT)


def leap_medians(client, spec):
    """-> ({year: {group: {...}}}, survey_window) for one LEAP question group."""
    from google.cloud import bigquery
    job = client.query(
        LEAP_SQL.format(p=LEAP_PROJECT),
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("group_name", "STRING",
                                          spec["question_group_name"]),
            bigquery.ScalarQueryParameter("survey_name", "STRING",
                                          spec["survey_name"]),
        ]))
    out = {}
    for r in job.result():
        out.setdefault(str(r.h.year), {})[r.pg] = {
            "median": round(r.median, 4), "mean": round(r.mean, 4),
            "p25": round(r.p25, 4), "p75": round(r.p75, 4), "n": r.n,
        }
    return out


def leap_survey(client, survey_name):
    """The survey's own window, so a baseline states when it was elicited."""
    sql = (f"select survey_name, survey_start_date, survey_end_date "
           f"from `{LEAP_PROJECT}.dim.dim_survey` where survey_name = @n")
    from google.cloud import bigquery
    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("n", "STRING", survey_name)]))
    for r in job.result():
        return {"survey": r.survey_name,
                "start": str(r.survey_start_date), "end": str(r.survey_end_date)}
    return None


# ── assembly ─────────────────────────────────────────────────────────────────
def all_questions():
    """Every question in the set, cross-cutting and ladder, with its spec."""
    cross = json.load(open(CROSS, encoding="utf-8"))
    ladder = json.load(open(LADDER, encoding="utf-8"))
    return cross["questions"] + ladder["questions"], ladder.get("prior_work", [])


def wanted(questions):
    """-> [(question_id, horizon, project)] exactly as her sheet asks for it."""
    out = []
    for q in questions:
        for horizon, projects in (q.get("human_comparisons") or {}).items():
            for project in projects:
                out.append((q["id"], horizon, project))
    return sorted(out)


def build(only=None):
    questions, prior_work = all_questions()
    by_id = {q["id"]: q for q in questions}
    baselines, unavailable = [], []

    xpt_cache, xpt_cached, client, leap_cached, leap_windows = None, {}, None, {}, {}

    by_prior = {r["id"]: r for r in prior_work}

    for qid, horizon, token in wanted(questions):
        q = by_id[qid]
        # Her column, split. "LEAP" names a project and leaves the question to
        # our JOIN table; "LEAP:<prior-work id>" names both and skips it.
        project, ref = (token.split(":", 1) + [None])[:2] if ":" in token \
            else (token, None)
        ref = ref or JOIN.get((qid, project))
        row = by_prior.get(ref)
        spec = SOURCES.get(ref)

        if row is None or spec is None:
            unavailable.append({
                "question_id": qid, "horizon": horizon, "project": project,
                "reason": (f"her column names {token!r} and nothing joins it to "
                           f"a prior question we can fetch"
                           if ref is None else
                           f"prior question {ref!r} has no source mapping"),
            })
            continue
        if only and spec["kind"] not in only:
            continue

        if spec["kind"] == "xpt":
            if xpt_cache is None:
                xpt_cache = xpt_checkout()
            key = spec["set_name"]
            if key not in xpt_cached:
                xpt_cached[key] = xpt_medians(xpt_cache, key)
            groups = xpt_cached[key].get(horizon)
            elicited = {"date": row["elicited"] or XPT_ELICITED,
                        "commit": XPT_COMMIT, "repo": XPT_REPO}
        else:
            if client is None:
                client = leap_client()
            key = (spec["question_group_name"], spec["survey_name"])
            if key not in leap_cached:
                leap_cached[key] = leap_medians(client, spec)
                leap_windows[key] = leap_survey(client, spec["survey_name"])
            groups = leap_cached[key].get(horizon)
            elicited = {"date": row["elicited"], "project": LEAP_PROJECT,
                        **(leap_windows[key] or {})}

        if not groups:
            unavailable.append({
                "question_id": qid, "horizon": horizon, "project": project,
                "reason": f"{project} has no forecast at {horizon} for "
                          f"{spec.get('set_name') or spec.get('question_group_name')!r}",
            })
            continue

        baselines.append({
            "question_id": qid,
            "horizon": horizon,
            "project": project,
            "prior_question_id": ref,
            "units": "percent",
            "groups": groups,
            "elicited": elicited,
            # The question the humans actually answered, verbatim from her
            # sheet. A median without it is a number pretending to be a
            # comparison.
            "source_question": {
                "category": row["category"],
                "severity": row["severity"],
                "text": row["text"],
                "criteria": row["criteria"],
                "resolution_dates": row["resolution_dates"],
            },
            "our_question": {"text": q["text"], "severity": q["severity"]["label"]},
        })

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%MZ"),
        "generated_by": "code/fetch_human_baselines.py",
        "asked_for_by": "the 'Human comparisons (Project)' column of "
                        "data/auto-arc/questions-2026-08-18.xlsx, carried into "
                        "data/autoarc_*.json as each question's human_comparisons",
        "adjusted": False,
        "note": "Medians as each panel reported them, in percent, unadjusted. "
                "Every entry carries the question the panel answered; the "
                "wordings differ from ours and no reconciliation is applied "
                "here.",
        "baselines": baselines,
        "unavailable": unavailable,
    }


def dumps(doc):
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", choices=["XPT", "LEAP"],
                    help="fetch only these projects (repeatable)")
    ap.add_argument("--check", action="store_true",
                    help="report what her sheet asks for and what is on disk; "
                         "write nothing, touch no network")
    args = ap.parse_args()

    if args.check:
        questions, _ = all_questions()
        want = wanted(questions)
        have = set()
        if os.path.exists(OUT):
            doc = json.load(open(OUT, encoding="utf-8"))
            have = {(b["question_id"], b["horizon"], b["project"])
                    for b in doc["baselines"]}
            print(f"{os.path.relpath(OUT, ROOT)}: {len(have)} baselines, "
                  f"{len(doc['unavailable'])} unavailable, "
                  f"generated {doc['generated_at']}")
        else:
            print(f"{os.path.relpath(OUT, ROOT)}: absent")
        print(f"her sheet asks for {len(want)} question x horizon x project cells")
        missing = [w for w in want if w not in have]
        for qid, h, p in missing:
            print(f"  missing: {qid} @ {h} from {p}")
        return 1 if missing else 0

    only = {"XPT": "xpt", "LEAP": "leap"}
    kinds = {only[o] for o in args.only} if args.only else None
    doc = build(kinds)
    open(OUT, "w", encoding="utf-8").write(dumps(doc))
    print(f"wrote {os.path.relpath(OUT, ROOT)}")
    print(f"  {len(doc['baselines'])} baselines, "
          f"{len(doc['unavailable'])} unavailable")
    for b in doc["baselines"]:
        groups = ", ".join(f"{g} {v['median']}% (n={v['n']})"
                           for g, v in sorted(b["groups"].items()))
        print(f"  {b['question_id']:22} @{b['horizon']} {b['project']:5} {groups}")
    for u in doc["unavailable"]:
        print(f"  UNAVAILABLE {u['question_id']} @{u['horizon']} "
              f"{u['project']}: {u['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
