"""The download: one zip beside the page, everything the page is built from.

    README.md        what is in here
    forecasts.csv    one line per number: run, model, question, horizon, scenario, percentile
    rationales.csv   each model's written rationale per question
    ai_progress.csv  the Part 1 answers: scenario probabilities, indicator weights, the two milestones
    questions.json   the questions and scenarios as asked
    history.csv      the record drawn behind each forecast
    runs/*.jsonl     the run files: every row, with each call's prompt, searches and page reads

Written by `python3 -m eeai build`; the page's "Download data" button serves it.
"""
import csv
import io
import zipfile
from pathlib import Path

from . import config, survey as S

NAME = "eeai-data.zip"
FORECAST_COLUMNS = ["run_id", "run_date", "model", "model_id", "question_id", "question", "unit",
                    "horizon", "scenario", "percentile", "value"]
RATIONALE_COLUMNS = ["run_id", "run_date", "model", "model_id", "question_id", "question", "rationale"]
PROGRESS_COLUMNS = ["run_id", "run_date", "model", "model_id", "item", "field", "value"]

README = """# EEAI (Economic Effects of AI): the data

{note}A panel of frontier AI models takes the Forecasting Research Institute's
Economic Effects of AI survey. Each model answers the whole survey in one call,
searching the web first. The page shows, for each number, the median across
the models of a run. Instrument: {instrument}. Runs: {runs}.

- forecasts.csv: one line per number a model gave.
  Columns: {forecast_columns}.
  `scenario` is unconditional, slow, moderate or rapid; `percentile` is p50
  (the median), and p10 and p90 where the survey asks for them.
- rationales.csv: each model's written rationale for each question.
- ai_progress.csv: the answers about AI itself: the probability of each
  scenario, the weight on each capability indicator, and two milestones.
- questions.json: the questions and scenario definitions as the models saw them.
- history.csv: the record drawn behind each forecast (series, date, value).
- runs/: the run files, one JSON object per line and one line per model and
  question. A call's first line carries the prompt, every search and page
  read, and the token usage.
"""


def _csv(columns, lines):
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(columns)
    w.writerows(lines)
    return out.getvalue()


def forecast_lines(survey, runs):
    qs = {q["id"]: q for q in survey["questions"]}
    for _, rows in runs:
        for r in rows:
            q = qs.get(r["question_id"])
            if not q:
                continue
            for h, s, p in S.asked(survey, q):
                v = ((r["forecasts"].get(h) or {}).get(s) or {}).get(p)
                if v is not None:
                    yield [r["run_id"], r["run_date"], r["label"], r["model"], q["id"], q["short"], q["unit"], h, s, p, v]


def rationale_lines(survey, runs):
    qs = {q["id"]: q for q in survey["questions"]}
    for _, rows in runs:
        for r in rows:
            if r["question_id"] in qs and r.get("rationale"):
                yield [r["run_id"], r["run_date"], r["label"], r["model"], r["question_id"],
                       qs[r["question_id"]]["short"], r["rationale"]]


def progress_lines(runs):
    for _, rows in runs:
        for r in rows:
            for item, answer in ((r.get("call") or {}).get("part1") or {}).items():
                for field, v in (answer.items() if isinstance(answer, dict) else [("", answer)]):
                    yield [r["run_id"], r["run_date"], r["label"], r["model"], item, field, v]


def bundle(out, survey, runs, runs_dir=None):
    """Write the zip. -> its member names."""
    synthetic = any(r.get("synthetic") for _, rows in runs for r in rows)
    readme = README.format(
        note="PLACEHOLDER DATA: these numbers are made up to show the layout.\n\n" if synthetic else "",
        instrument=survey["instrument"], runs=", ".join(run_id for run_id, _ in runs),
        forecast_columns=", ".join(FORECAST_COLUMNS))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("README.md", readme)
        z.writestr("forecasts.csv", _csv(FORECAST_COLUMNS, forecast_lines(survey, runs)))
        z.writestr("rationales.csv", _csv(RATIONALE_COLUMNS, rationale_lines(survey, runs)))
        z.writestr("ai_progress.csv", _csv(PROGRESS_COLUMNS, progress_lines(runs)))
        z.write(config.QUESTIONS, "questions.json")
        z.write(config.HISTORY, "history.csv")
        for run_id, _ in runs:
            z.write(Path(runs_dir or config.RUNS) / f"{run_id}.jsonl", f"runs/{run_id}.jsonl")
        return z.namelist()
