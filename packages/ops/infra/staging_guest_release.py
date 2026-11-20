#!/usr/bin/env python3
"""Rehearse an offline checked deploy on a provisioned staging guest."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


PROJECTS = ("airo", "eeai", "rsi")


def read(path):
    return json.loads(path.read_text())


def manifest(site):
    return {p.relative_to(site).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(site.rglob("*")) if p.is_file()}


def on_guest(fixture, projects, keep_verified_release=False):
    if os.geteuid() or not Path("/etc/fri-ops/staging").is_file():
        raise RuntimeError("Expected the admin account on a provisioned staging guest")
    shutil.chown(fixture, user="forecast", group="forecast")
    for path in fixture.rglob("*"):
        shutil.chown(path, user="forecast", group="forecast")
    candidate = read(fixture / "commits.json")["candidate"]
    for name in projects:
        config = Path("/etc/fri-dashboards") / (name + ".json")
        original = config.read_bytes()
        settings = json.loads(original)
        runtime = Path(settings["root"])
        mirror = runtime / "repository.git"
        if any((runtime / marker).exists() for marker in ("jobs-enabled", "scheduler-v2.json", "repository.git")):
            raise RuntimeError(f"{name}: expected an inactive scheduler and a fresh staging mirror")
        initial_release = (runtime / "current").resolve(strict=True)
        selected = runtime / "current/site"
        before = read(selected / "release.json")
        files = manifest(selected)
        env = {"PATH": str(fixture / "bin") + ":" + settings["path"], "HOME": str(fixture),
               "PIP_NO_INDEX": "1", "PIP_FIND_LINKS": str(fixture / "python-wheelhouse"),
               "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INPUT": "1",
               "NPM_CONFIG_OFFLINE": "true", "NPM_CONFIG_CACHE": str(fixture / "npm-cache"),
               "NPM_CONFIG_AUDIT": "false", "NPM_CONFIG_FUND": "false",
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
               "FRI_ALERTS_FILE": str(fixture / "alerts.json")}

        def cli(*args, **kwargs):
            return subprocess.run(
                ["runuser", "-u", "forecast", "--", "/usr/bin/env", "-i",
                 *(f"{k}={v}" for k, v in env.items()), "/usr/local/bin/forecast-prod", name, *args],
                stdin=subprocess.DEVNULL, check=True, **kwargs)

        def verify():
            report = json.loads(cli("operator-status", "--verify-content", capture_output=True).stdout)
            delivery = report["delivery"]
            if (delivery["origin"]["result"] != "verified" or delivery["public"]["result"] != "verified"
                    or delivery["public"]["scope"] != "content"):
                raise RuntimeError(f"{name}: staging origin or public HTTPS verification failed: {delivery}")

        config.write_text(json.dumps({**settings, "repository": str(fixture / "source.bundle"),
                                      "github_repository": "fixture/checked-release"}) + "\n")
        try:
            verify()
            try:
                cli("deploy", "--commit", candidate, *(["--wait"] if name == "eeai" else []))
                deployed = read(selected / "release.json")
                if deployed["frontend_commit"] != candidate or deployed["data_snapshot"] != before["data_snapshot"]:
                    raise RuntimeError(f"{name}: candidate did not retain the baseline data and downloads")
                edited = manifest(selected)
                unchanged = {path for path in files if path not in ("index.html", "release.json")}
                if edited["index.html"] == files["index.html"] or any(edited.get(path) != files[path] for path in unchanged):
                    raise RuntimeError(f"{name}: unexpected change to the candidate file manifest")
                verify()
            finally:
                if (runtime / "current").resolve(strict=True) != initial_release:
                    cli("rollback")
            if manifest(selected) != files:
                raise RuntimeError(f"{name}: rollback did not restore the exact previous bytes")
            verify()
            if keep_verified_release:
                cli("rollback")
                if manifest(selected) != edited:
                    raise RuntimeError(f"{name}: retained release differs from the tested candidate")
                verify()
            print(json.dumps({"dashboard": name, "frontend_commit": candidate,
                              "snapshot_sha256": deployed["data_snapshot"]["sha256"],
                              "public_verification": "verified", "rollback": "verified",
                              "selected": "candidate" if keep_verified_release else "original"}), flush=True)
        finally:
            config.write_bytes(original)
            if mirror.is_dir():
                shutil.rmtree(mirror)


def prepare_cache(target, repo, env):
    """Cache only public package downloads, with empty Git, pip and npm settings."""
    import tomllib

    target.mkdir(exist_ok=True)
    wheelhouse, npm_cache = (target / directory for directory in ("python-wheelhouse", "npm-cache"))
    wheelhouse.mkdir(exist_ok=True)
    packages = [tomllib.loads(path.read_text()) for path in
                [*repo.glob("packages/*/pyproject.toml"), *repo.glob("dashboards/*/pyproject.toml"),
                 *repo.glob("submissions/*/pyproject.toml")]]
    local = {package["project"]["name"] for package in packages}
    requirements = {requirement for package in packages for requirement in
                    package["build-system"]["requires"] + package["project"].get("dependencies", [])
                    if re.match(r"^[\w.-]+", requirement)[0] not in local}
    with TemporaryDirectory(prefix=".cache-build-", dir=target.parent) as work:
        work = Path(work)
        subprocess.run([sys.executable, "-m", "venv", work / "venv"], env=env, check=True)
        subprocess.run([work / "venv/bin/python", "-m", "pip", "download", "--only-binary=:all:", "--dest",
                        wheelhouse, "wheel", *sorted(requirements)], env=env, check=True)
        for name in ("package.json", "package-lock.json"):
            shutil.copy2(repo / "packages/forecaster" / name, work / name)
        subprocess.run(["npm", "ci", "--prefix", work, "--cache", npm_cache,
                        "--no-fund", "--no-audit", "--ignore-scripts"], env=env, check=True)
    return wheelhouse, npm_cache


def on_controller(args):
    from staging_release_drill import create_candidate, setup_checks

    ssh = ["ssh", "-F", args.ssh_config.resolve(strict=True), "-o", "BatchMode=yes", "--", args.host]
    with TemporaryDirectory(prefix="fri-checked-release-") as work:
        root = Path(work)
        env = {k: v for k, v in os.environ.items() if k.lower() in ("path", "http_proxy", "https_proxy", "no_proxy")}
        env.update(HOME=work, TMPDIR=work, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                   PIP_CONFIG_FILE=os.devnull, PIP_NO_INPUT="1", NPM_CONFIG_USERCONFIG=os.devnull)
        npm_global = root / "npm-global"
        npm_global.touch()
        env["NPM_CONFIG_GLOBALCONFIG"] = str(npm_global)
        repo, baseline, candidate = create_candidate(root, args.source_commit, env)
        fixture = root / "fixture"
        fixture.mkdir()
        (fixture / "bin").mkdir()
        setup_checks(fixture / "bin", baseline, candidate)
        (fixture / "commits.json").write_text(json.dumps({"baseline": baseline, "candidate": candidate}))
        (fixture / "alerts.json").write_text(json.dumps({"channels": [
            {"kind": "ntfy", "server": "http://127.0.0.1:9", "topic": "staging-only"}]}))
        shutil.copy2(__file__, fixture / "rehearse.py")
        wheelhouse, npm_cache = (prepare_cache(args.prepare_cache.resolve(), repo, env) if args.prepare_cache
                                  else (args.python_wheelhouse.resolve(strict=True), args.npm_cache.resolve(strict=True)))
        subprocess.run(["git", "-C", repo, "-c", "pack.window=0", "-c", "pack.compression=1", "-c", "pack.threads=1",
                        "bundle", "create", fixture / "source.bundle", "main", "HEAD"], env=env, check=True)
        shutil.rmtree(repo)
        (fixture / "python-wheelhouse").symlink_to(wheelhouse)
        (fixture / "npm-cache").mkdir()
        (fixture / "npm-cache/_cacache").symlink_to((npm_cache / "_cacache").resolve(strict=True))

        # The remote command checks the staging fence before creating any files.
        remote = subprocess.check_output([*ssh, "test -f /etc/fri-ops/staging && mktemp -d /var/tmp/fri-release-XXXXXX"],
                                         text=True).strip()
        if not re.fullmatch(r"/var/tmp/fri-release-[A-Za-z0-9]+", remote):
            raise RuntimeError("Unexpected staging guest temporary path")
        try:
            with subprocess.Popen(["tar", "-chf", "-", "-C", fixture, "."], stdout=subprocess.PIPE) as archive:
                subprocess.run([*ssh, shlex.join(["tar", "-xf", "-", "-C", remote])],
                               stdin=archive.stdout, check=True)
                if archive.wait():
                    raise RuntimeError("Unable to transfer release fixtures")
            subprocess.run([*ssh, shlex.join(["python3", remote + "/rehearse.py", "installed", remote, *args.projects,
                                                    *(["--keep-verified-release"] if args.keep_verified_release else [])])],
                           stdin=subprocess.DEVNULL, check=True)
        finally:
            subprocess.run([*ssh, shlex.join(["rm", "-r", "--", remote])], check=True)


def main():
    if sys.argv[1:2] == ["installed"]:
        installed = argparse.ArgumentParser()
        installed.add_argument("fixture", type=Path)
        installed.add_argument("projects", choices=PROJECTS, nargs="+")
        installed.add_argument("--keep-verified-release", action="store_true")
        args = installed.parse_args(sys.argv[2:])
        on_guest(args.fixture.resolve(strict=True), args.projects, args.keep_verified_release)
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-config", type=Path, required=True)
    parser.add_argument("--host", default="stage-prod")
    parser.add_argument("--source-commit", default="HEAD")
    parser.add_argument("--projects", choices=PROJECTS, nargs="+", default=PROJECTS)
    parser.add_argument("--keep-verified-release", action="store_true",
                        help="reselect the verified candidate after checking rollback")
    parser.add_argument("--prepare-cache", type=Path, help="populate a new public dependency cache")
    parser.add_argument("--python-wheelhouse", type=Path)
    parser.add_argument("--npm-cache", type=Path)
    args = parser.parse_args()
    if bool(args.prepare_cache) == bool(args.python_wheelhouse or args.npm_cache) or (
            not args.prepare_cache and not (args.python_wheelhouse and args.npm_cache)):
        parser.error("specify --prepare-cache or both --python-wheelhouse and --npm-cache")
    on_controller(args)


if __name__ == "__main__":
    main()
