#!/usr/bin/env python3
"""Build browsable staging publications from checked source and a fictional export row."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

OPS = Path(__file__).resolve().parents[1]
REPO = OPS.parents[1]
sys.path[:0] = [str(OPS), str(REPO / "packages/dashboard")]
from dashboard import page, site  # noqa: E402
from dashboard_projects import DASHBOARDS  # noqa: E402


SOURCE_PATHS = ("packages/dashboard", "packages/forecaster", "dashboards/airo/redlines",
                "dashboards/airo/data", "dashboards/airo/index.html", "dashboards/airo/web",
                "dashboards/eeai/eeai", "dashboards/rsi/rsi_dashboard")
BANNER = ('<div role="status" style="padding:8px;text-align:center;background:#fff4b3;color:#201600">'
          'Staging publication for deployment testing</div>')


def export_source(tree, commit, paths=SOURCE_PATHS):
    with subprocess.Popen(["git", "-C", REPO, "archive", commit, *paths], stdout=subprocess.PIPE) as archive:
        subprocess.run(["tar", "-xf", "-", "-C", tree], stdin=archive.stdout, check=True)
        if archive.wait():
            raise RuntimeError("Unable to export checked staging inputs")


def build_airo(project, target, env):
    results = project / "results"
    results.mkdir()
    row = {"run_id": "staging-fixture", "model": "fixture/example", "label": "Fixture model",
           "elicited_at": "2026-11-01T12:00:00Z", "run_date": "2026-11-01",
           "question_id": "staging:example", "question_text": "Example question for the staging export.",
           "forecasts": [{"horizon": "2030", "probability": 10}],
           "rationale": "Fictional example for verifying the export schema.",
           "call_id": "staging-fixture:example",
           "debrief": {"staging:example": {"rationale": "Fictional example, generated without a model call."}}}
    (results / "forecast_runs_unified.jsonl").write_text(json.dumps(row) + "\n")
    subprocess.run([sys.executable, "-m", "redlines", "build", "--views", "csv"],
                   cwd=project, env=env, check=True)
    site.write_site(target, (project / "index.html").read_text(),
                    {dest: project / src for src, dest in DASHBOARDS["airo"].assets})
    for name in DASHBOARDS["airo"].downloads:
        shutil.copy2(results / name, target / name)


def build_publications(output, commit):
    output.mkdir()
    with TemporaryDirectory(prefix=".publication-source-", dir=output.parent) as work:
        tree = Path(work)
        export_source(tree, commit)
        projects = [tree / dash.project for dash in DASHBOARDS.values()]
        env = {"PATH": os.defpath, "HOME": work, "PYTHONNOUSERSITE": "1",
               "PYTHONPATH": os.pathsep.join(map(str, [*projects, tree / "packages/dashboard"])),
               "EEAI_RESULTS": str(tree / "eeai-state"), "TMPDIR": work}
        results = tree / "empty-runs"
        results.mkdir()
        for name, dashboard in DASHBOARDS.items():
            target = output / name
            if name == "airo":
                build_airo(tree / dashboard.project, target, env)
            else:
                # EEAI supports a cohort/history-only preview from reviewed public aggregates.
                # RSI accepts an empty results tree and displays its reviewed instrument.
                args = [arg.format(results=results, site=target) for arg in dashboard.build if
                        not (name == "eeai" and arg == "--publication")]
                subprocess.run([sys.executable, *args], env=env, cwd=tree, check=True)
            html = target / "index.html"
            source = html.read_bytes()
            text = source.decode()
            body = text.index(">", text.index("<body")) + 1
            html.write_text(text[:body] + BANNER + text[body:])
            if page.blocks(source) != page.blocks(html.read_bytes()):
                raise RuntimeError("Staging banner altered a dashboard data block")
            site.verify(target, dashboard.required)
            snapshot = site.snapshot(target, dashboard.downloads)
            release = "staging-" + commit[:12]
            metadata = {"dashboard": name, "release": release, "commit": commit,
                        "publication": "preserved", "publication_commit": commit, "frontend_commit": commit,
                        "data_snapshot": {**snapshot, "source_release": release, "origin_commit": commit}}
            (target / "release.json").write_text(json.dumps(metadata, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new output directory, with existing parent")
    parser.add_argument("--commit", default="HEAD", help="source commit for all reviewed builder inputs")
    args = parser.parse_args()
    commit = subprocess.check_output(["git", "-C", REPO, "rev-parse", "--verify", args.commit + "^{commit}"],
                                     text=True).strip()
    build_publications(args.output.resolve(), commit)


if __name__ == "__main__":
    main()
