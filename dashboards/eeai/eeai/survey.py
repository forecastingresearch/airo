"""The question set (eeai/data/questions.json, from eeai/make_questions.py) and what it asks for."""
import csv
import json

from .config import HISTORY, QUESTIONS

UNCONDITIONAL = "unconditional"
PERCENTILES = ("p10", "p50", "p90")
# The survey's "uncertainty" tag: the 10th and 90th percentiles are asked for
# the unconditional forecast and the rapid scenario only.
UNCERTAINTY_SCENARIOS = (UNCONDITIONAL, "rapid")


def load(path=QUESTIONS):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def history(path=None):
    """{series: [(date, value)]}, oldest first."""
    if path is None:
        from .baselines import current
        path = current()
    with open(path, encoding="utf-8", newline="") as f:
        return read_history(f)


def read_history(f):
    out = {}
    for r in csv.DictReader(f):
        out.setdefault(r["series"], []).append((r["date"], float(r["value"])))
    return out


def scenario_ids(survey):
    return [UNCONDITIONAL] + [s["id"] for s in survey["scenarios"]["items"]]


def percentiles(question, scenario):
    return PERCENTILES if question["uncertainty"] and scenario in UNCERTAINTY_SCENARIOS else ("p50",)


def horizons(question, today=None):
    """The question's dates; with `today` (a date), only those still ahead.
    A date is the beginning of its year, as the survey words it: once it has
    passed, the panel is no longer asked about it and its last forecast stands."""
    return [h for h in question["horizons"] if today is None or int(h["id"]) > today.year
            or (int(h["id"]) == today.year and (today.month, today.day) == (1, 1))]


def open_survey(survey, today, resolved):
    """Select the instrument to send, preserving the original for publication."""
    from dashboard.resolutions import Target
    questions = []
    for q in survey["questions"]:
        version = q.get("resolution", {}).get("version", 1)
        remaining = [h for h in horizons(q, today) if Target(q["id"], h["id"], version) not in resolved]
        if remaining:
            questions.append(dict(q, horizons=remaining))
    scenario_version = survey["scenarios"].get("resolution", {}).get("version", 1)
    part1 = [p for p in survey["part1"] if today.year <= 2030 and
             (p["id"] != "scenario_probabilities" or Target("ai_scenario", "2030", scenario_version) not in resolved)]
    return dict(survey, questions=questions, part1=part1)


def asked(survey, question, today=None):
    """Every value the question asks for, as (horizon, scenario, percentile)."""
    return [(h["id"], s, p) for h in horizons(question, today) for s in scenario_ids(survey)
            for p in percentiles(question, s)]


def n_values(survey, today=None):
    return sum(len(asked(survey, q, today)) for q in survey["questions"])


def instruments(survey):
    """The instrument and the earlier ones whose rows it continues."""
    return [survey["instrument"]] + list(survey.get("continues") or [])


def resolution_text(rule):
    method = rule["method"]
    if method == "adjudicated":
        return rule["period"]
    descriptions = {
        "january": "Use January of the horizon year.",
        "five_year_growth": "Compound the five annual growth rates preceding the horizon year.",
        "q4_five_year_growth": "Compound the five Q4 year-over-year growth rates preceding the horizon year.",
        "employment_share": "Divide the sum of the January payroll counts by the January civilian labor force.",
        "anchored_index": "Scale the horizon year's Q1 index to the survey's Q1 2023 labor share.",
        "income_2023_dollars": "Use the horizon income year and convert the release to 2023 dollars using the 2023 nominal-to-real income ratio.",
    }
    return descriptions[method] + " Use the first FRED vintage containing all required inputs."
