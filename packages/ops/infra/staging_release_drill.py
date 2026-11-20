#!/usr/bin/env python3
"""Exercise checked deploy and rollback with real builders in disposable runtimes."""
import argparse
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
from tempfile import TemporaryDirectory
import time
from urllib.parse import quote

from build_staging_publications import DASHBOARDS, OPS, export_source, site


FRONTENDS = ("dashboards/airo/index.html", "dashboards/airo/web/demo/00-head.html",
             "dashboards/eeai/eeai/web/00-head.html", "dashboards/rsi/rsi_dashboard/web/head.html")
FRONTEND_EDIT = b"<title>Staging candidate | "


def run(*args, env, **kwargs):
    return subprocess.run(args, env=env, stdin=subprocess.DEVNULL, check=True, **kwargs)


def write_object(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def read_object(path):
    return json.loads(path.read_text())


def create_candidate(root, source_commit, env):
    repo = root / "fixture-repo"
    repo.mkdir()
    export_source(repo, source_commit, paths=())
    run("git", "init", "--initial-branch=main", repo, env=env, capture_output=True)
    git = ["git", "-C", repo, "-c", "user.name=Staging fixture", "-c", "user.email=fixture@example.invalid"]
    run(*git, "add", "-f", "--all", env=env, capture_output=True)
    run(*git, "commit", "-m", "Checked source archive for staging", env=env, capture_output=True)
    baseline = run(*git, "rev-parse", "HEAD", env=env, capture_output=True, text=True).stdout.strip()
    for relative in FRONTENDS:
        path = repo / relative
        source = path.read_bytes()
        if b"<title>" not in source:
            raise RuntimeError(f"Cannot apply candidate frontend edit to {relative}")
        path.write_bytes(source.replace(b"<title>", FRONTEND_EDIT, 1))
    run(*git, "commit", "-a", "-m", "Staging fixture frontend change", env=env, capture_output=True)
    candidate = run(*git, "rev-parse", "HEAD", env=env, capture_output=True, text=True).stdout.strip()
    return repo, baseline, candidate


def setup_checks(directory, baseline, candidate):
    gh = directory / "gh"
    gh.write_text(f"""#!/usr/bin/env python3
import json
import os
import sys
prefix = 'repos/fixture/checked-release/commits/'
assert sys.argv[1] == 'api' and sys.argv[2].startswith(prefix)
sha, endpoint = sys.argv[2].removeprefix(prefix).split('/', 1)
assert endpoint == 'check-runs?per_page=100' and sha in ({baseline!r}, {candidate!r})
names = ['test (py3.11)', 'test (py3.12)', 'test (py3.13)', 'reproduce']
if os.environ.get('FRI_FIXTURE_MISSING_CHECK') == sha:
    names.remove('reproduce')
print(json.dumps({{'check_runs': [dict(name=name, id=i, status='completed', conclusion='success',
                                     app={{'slug': 'github-actions'}}) for i, name in enumerate(names)]}}))
""")
    gh.chmod(0o700)


def fetch(socket_path, relative):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(5)
        stream.connect(str(socket_path))
        connection = http.client.HTTPConnection("localhost")
        connection.sock = stream
        connection.request("GET", "/" + quote(relative))
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"Staging origin returned {response.status} for {relative}")
        return response.read()


def verify_delivery(runtime, origin_socket, baseline, name, expected_frontend):
    selected = runtime / "current/site"
    release = read_object(selected / "release.json")
    if release["dashboard"] != name or release["frontend_commit"] != expected_frontend:
        raise RuntimeError(f"{name}: served frontend provenance differs from the selected frontend")
    expected = read_object(baseline / "release.json")["data_snapshot"]
    if release["data_snapshot"] != expected:
        raise RuntimeError(f"{name}: checked release changed its publication snapshot")
    if site.snapshot(selected, DASHBOARDS[name].downloads) != {
            key: expected[key] for key in ("blocks", "files", "sha256")}:
        raise RuntimeError(f"{name}: selected snapshot does not match actual data")
    for path in sorted(selected.rglob("*")):
        if path.is_file() and fetch(origin_socket, path.relative_to(selected).as_posix()) != path.read_bytes():
            raise RuntimeError(f"{name}: origin delivered different bytes for {path.name}")
    return release


def exercise(root, publications, candidate_output):
    fixtures = {name: (publications / name).resolve(strict=True) for name in DASHBOARDS}
    commits = {read_object(directory / "release.json")["commit"] for directory in fixtures.values()}
    source_commit, = commits
    bin_dir, config, scratch = [root / name for name in ("bin", "configs", "tmp")]
    for directory in (bin_dir, config, scratch):
        directory.mkdir()
    env = {key: os.environ[key] for key in ("PATH", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                                             "http_proxy", "https_proxy", "no_proxy") if key in os.environ}
    env.update(PATH=str(bin_dir) + os.pathsep + env.get("PATH", os.defpath), HOME=str(root / "home"),
               TMPDIR=str(scratch), DASHBOARD_CONFIG_DIR=str(config),
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
               PIP_CONFIG_FILE=os.devnull, PIP_NO_INPUT="1", PYTHONNOUSERSITE="1")
    repo, baseline, candidate = create_candidate(root, source_commit, env)
    setup_checks(bin_dir, baseline, candidate)
    if candidate_output:
        candidate_output.mkdir()

    for name, initial in fixtures.items():
        runtime = root / name
        origin_socket = root / (name + ".sock")
        write_object(config / (name + ".json"), {
            "dashboard": name, "root": str(runtime), "socket": str(origin_socket),
            "path": env["PATH"], "bootstrap_code": str(repo),
            "repository": str(repo), "github_repository": "fixture/checked-release",
            "bootstrap_publication": str(initial),
        })
        write_object(runtime / "home/.config/fri-forecaster/alerts.json",
                     {"channels": [{"kind": "ntfy", "server": "http://127.0.0.1:9", "topic": "staging-only"}]})
        command = [OPS / "dashboardctl", name]
        with subprocess.Popen([*command, "serve"], env=env, stdin=subprocess.DEVNULL) as origin:
            try:
                for _ in range(100):
                    if origin_socket.exists() and origin.poll() is None:
                        break
                    if origin.poll() is not None:
                        raise RuntimeError(f"{name}: staging origin exited before binding its socket")
                    time.sleep(0.05)
                else:
                    raise RuntimeError(f"{name}: staging origin socket did not appear")

                run(*command, "deploy", "--commit", baseline, "--keep-publication", env=env)
                seeded = verify_delivery(runtime, origin_socket, initial, name, source_commit)
                if fetch(origin_socket, "index.html") != (initial / "index.html").read_bytes():
                    raise RuntimeError(f"{name}: first deploy did not import the baseline publication")

                blocked = subprocess.run([*command, "deploy", "--commit", candidate],
                                         env={**env, "FRI_FIXTURE_MISSING_CHECK": candidate},
                                         capture_output=True, text=True)
                if blocked.returncode == 0 or "Required checks have not passed" not in blocked.stderr:
                    raise RuntimeError(f"{name}: unchecked commit did not fail the required-check gate")
                if read_object(runtime / "current/site/release.json") != seeded:
                    raise RuntimeError(f"{name}: rejected candidate changed the selected publication")

                run(*command, "deploy", "--commit", candidate, env=env)
                deployed = verify_delivery(runtime, origin_socket, initial, name, candidate)
                if FRONTEND_EDIT not in fetch(origin_socket, "index.html") or deployed["release"] == seeded["release"]:
                    raise RuntimeError(f"{name}: normal deploy did not activate the edited frontend")
                if candidate_output:
                    shutil.copytree(runtime / "current/site", candidate_output / name)

                run(*command, "rollback", env=env)
                if verify_delivery(runtime, origin_socket, initial, name, source_commit) != seeded:
                    raise RuntimeError(f"{name}: rollback did not restore the initial release")
                if fetch(origin_socket, "index.html") != (initial / "index.html").read_bytes():
                    raise RuntimeError(f"{name}: rollback did not restore the original frontend bytes")
                if list((runtime / "runs").iterdir()) or (runtime / "jobs-enabled").exists():
                    raise RuntimeError(f"{name}: release exercise unexpectedly ran scheduled work")
                print(f"{name}: cold import, check rejection, frontend-only deploy and rollback verified", flush=True)
            finally:
                origin.terminate()
                try:
                    origin.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    origin.kill()
        shutil.rmtree(runtime)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("publications", type=Path, help="output of build_staging_publications.py")
    parser.add_argument("--candidate-output", type=Path, help="new directory for the deployed candidate publications")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="fri-release-") as work:
        exercise(Path(work).resolve(), args.publications, args.candidate_output)


if __name__ == "__main__":
    main()
