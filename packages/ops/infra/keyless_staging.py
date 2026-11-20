#!/usr/bin/env python3
"""Exercise the installed pi, forecast validation and publication on a disposable staging pair."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent


def run(*argv, **kwargs):
    return subprocess.run(argv, check=True, **kwargs)


def installed(root):
    if os.geteuid() or not Path('/etc/fri-ops/staging').is_file():
        raise RuntimeError('Expected root on the marked disposable staging guest')
    config_path = Path('/etc/fri-dashboards/rsi.json')
    config = json.loads(config_path.read_text())
    runtime_root = Path(config['root'])
    if any((runtime_root / marker).exists() for marker in ('jobs-enabled', 'scheduler-v2.json')):
        raise RuntimeError('Keyless acceptance requires inactive staged scheduling')
    selected = (runtime_root / 'current').resolve(strict=True)
    code = (selected / 'code').resolve(strict=True)
    python = code / '.venv/bin/python'
    pi = code / 'packages/forecaster/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js'
    for path in (python, pi):
        if not path.is_file():
            raise RuntimeError(f'Missing installed release dependency: {path}')
    for path in [root, *root.rglob('*')]:
        shutil.chown(path, user='forecast', group='forecast')
    output = root / 'forecast'
    unit = 'fri-keyless-' + secrets.token_hex(6)
    run('systemd-run', '--quiet', '--wait', '--pipe', '--collect', f'--unit={unit}',
        '--uid=forecast', '--gid=forecast', '--property=PrivateNetwork=yes',
        '--property=NoNewPrivileges=yes', '--property=ProtectHome=yes',
        '--property=RuntimeMaxSec=660', '--property=RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6',
        '/usr/bin/env', '-i', f'PATH={config["path"]}', f'HOME={root}', f'TMPDIR={root}',
        str(python), str(root / 'keyless_forecast.py'), '--pi', str(pi), '--out', str(output))
    verdict = json.loads((output / 'verdict.json').read_text())
    if verdict.get('result') != 'passed':
        raise RuntimeError('Keyless forecast did not pass')
    run('runuser', '-u', 'forecast', '--', '/usr/bin/env', '-i', f'PATH={config["path"]}',
        f'HOME={root}', str(python), str(root / 'keyless_staging.py'), 'publish', str(root), str(code),
        str(config_path))
    verdict.update(publication=json.loads((root / 'publication.json').read_text()),
                   network='loopback-only systemd PrivateNetwork namespace',
                   installed_release=selected.name)
    (root / 'verdict.json').write_text(json.dumps(verdict, indent=2) + '\n')


def publish(root, code, config_path):
    sys.path.insert(0, str(code / 'packages/ops'))
    from production import Runtime, data_snapshot, link, write_json
    import eeai_operator

    config = json.loads(config_path.read_text())
    runtime = Runtime(config, config_path=config_path)
    site = root / 'forecast/site'
    release = runtime.root / 'releases' / ('keyless-' + secrets.token_hex(6))
    with runtime.lock():
        original = runtime.current.resolve(strict=True)
        if (original / 'code').resolve(strict=True) != code:
            raise RuntimeError('The installed release changed during the keyless forecast')
        release.mkdir()
        (release / 'code').symlink_to(code)
        (release / 'site').symlink_to(site)
        prior = runtime.root / 'previous'
        prior_target = os.readlink(prior) if prior.is_symlink() else None
        receipt = runtime.root / 'last-deploy.json'
        old_receipt = receipt.read_bytes() if receipt.is_file() else None
        metadata = {**runtime.release_metadata(original), 'release': release.name,
                    'publication': 'keyless-acceptance', 'fixture': True,
                    'data_snapshot': {**data_snapshot(site, runtime.dashboard.downloads),
                                      'origin_commit': runtime.release_metadata(original)['commit'],
                                      'source_release': release.name}}
        write_json(site / 'release.json', metadata)
        write_json(release / 'release.json', metadata)
        runtime.verify_site(site)
        try:
            runtime.activate(release)
            files = tuple(path.relative_to(site).as_posix() for path in sorted(site.rglob('*')) if path.is_file())
            delivery = eeai_operator.check_public(config, site, metadata, content=True, files=files)
            if delivery.get('result') != 'verified':
                raise RuntimeError('Keyless forecast did not verify at the reader endpoint')
            (root / 'publication.json').write_text(json.dumps({
                'result': 'passed', 'dashboard': 'rsi', 'public_url': config['public_url'],
                'snapshot_sha256': metadata['data_snapshot']['sha256'],
                'origin': {'result': 'verified', 'scope': 'release_metadata'},
                'public_content': 'verified', 'files': delivery['checks'],
            }, indent=2) + '\n')
        finally:
            try:
                runtime.activate(original)
                original_site = original / 'site'
                original_files = tuple(path.relative_to(original_site).as_posix()
                                       for path in sorted(original_site.rglob('*')) if path.is_file())
                restored = eeai_operator.check_public(config, original_site, runtime.release_metadata(original),
                                                       content=True, files=original_files)
                if restored.get('result') != 'verified':
                    raise RuntimeError('The original staging publication did not restore')
            finally:
                if runtime.current.resolve() == original:
                    if prior_target is None:
                        prior.unlink(missing_ok=True)
                    else:
                        link(Path(prior_target), prior)
                    if old_receipt is None:
                        receipt.unlink(missing_ok=True)
                    else:
                        receipt.write_bytes(old_receipt)
                    shutil.rmtree(release)



def controller(args):
    from accept_staging import Guest

    nonce = args.nonce_file.read_text().strip()
    if not re.fullmatch(r'[0-9a-f]{32,128}', nonce):
        raise ValueError('Expected the disposable pair nonce')
    guest = Guest(args.ssh_config.resolve(strict=True), 'stage-prod', 'forecast-prod')
    guest.command("""set -eu
IFS= read -r nonce
test "$(id -u)" = 0
test -f /etc/fri-ops/staging
test ! -L /etc/fri-acceptance-host
test "$(stat -c '%u:%a:%F' /etc/fri-acceptance-host)" = '0:600:regular file'
test "$(cat /etc/fri-acceptance-host)" = "$(printf '%s\nforecast-prod' "$nonce")"
""", input=nonce + '\n', text=True)
    remote = guest.output('mktemp -d /var/tmp/fri-keyless-XXXXXXXX')
    if not re.fullmatch(r'/var/tmp/fri-keyless-[a-zA-Z0-9]+', remote):
        raise RuntimeError('Unexpected guest fixture directory')
    with TemporaryDirectory(prefix='fri-keyless-controller-') as work:
        archive = Path(work) / 'fixture.tar'
        with tarfile.open(archive, 'w') as tar:
            for name in ('keyless_forecast.py', 'keyless_staging.py'):
                tar.add(HERE / name, arcname=name)
        with archive.open('rb') as source:
            guest.command(shlex.join(['tar', '-xf', '-', '-C', remote]), stdin=source)
    try:
        guest.command(shlex.join(['python3', remote + '/keyless_staging.py', 'installed', remote]))
        verdict = json.loads(guest.output(shlex.join(['cat', remote + '/verdict.json'])))
        archive = args.report.with_name(args.report.stem + '-' + Path(remote).name + '.tar.gz')
        with archive.open('wb') as destination:
            archive.chmod(0o600)
            guest.command(shlex.join(['tar', '-czf', '-', '-C', remote, 'forecast', 'publication.json']),
                          stdout=destination)
        with tarfile.open(archive, 'r:gz') as evidence:
            if not evidence.getmember('forecast/verdict.json').isfile():
                raise RuntimeError('The private forecast evidence archive is incomplete')
        verdict['finished_utc'] = datetime.now(timezone.utc).isoformat()
        verdict['evidence_archive'] = str(archive.resolve())
        verdict['trace_in_archive'] = str(Path(verdict['trace']).relative_to(remote))
        args.report.write_text(json.dumps(verdict, indent=2) + '\n')
        print(json.dumps(verdict, indent=2))
    except BaseException:
        print(f'Keyless evidence retained on stage-prod at {remote}', file=sys.stderr)
        raise
    else:
        guest.command(shlex.join(['rm', '-r', '--', remote]))


def main():
    if sys.argv[1:2] == ['installed']:
        installed(Path(sys.argv[2]).resolve(strict=True))
        return
    if sys.argv[1:2] == ['publish']:
        publish(*(Path(arg).resolve(strict=True) for arg in sys.argv[2:5]))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-config', type=Path, required=True)
    parser.add_argument('--nonce-file', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    args.report.write_text(json.dumps({'result': 'running'}, indent=2) + '\n')
    args.report.chmod(0o600)
    try:
        controller(args)
    except BaseException:
        args.report.write_text(json.dumps({'result': 'failed', 'finished_utc': datetime.now(timezone.utc).isoformat()},
                                         indent=2) + '\n')
        raise


if __name__ == '__main__':
    main()
