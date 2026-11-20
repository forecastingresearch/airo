#!/usr/bin/env python3
"""Exercise the two provisioned roles with temporary directories and synthetic data."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from zipfile import ZipFile


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / 'dashboard'))
from dashboard import page, site as site_kit  # noqa: E402


def run(*argv, **kwargs):
    return subprocess.run(argv, check=True, **kwargs)


def write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def drill(root, tools):
    prod, web = [root / role for role in ('prod', 'web')]
    secrets = root / 'fixtures'
    secrets.mkdir()
    run('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', secrets / 'publisher')
    cert, key = secrets / 'localhost.pem', secrets / 'localhost.key'
    run('openssl', 'req', '-x509', '-sha256', '-nodes', '-newkey', 'rsa:2048', '-days', '1',
        '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost',
        '-out', cert, '-keyout', key, capture_output=True)
    credential = secrets / 'reader.json'
    write(credential, json.dumps({'username': 'reader', 'password': 'synthetic'}), 0o600)
    password_file = secrets / 'htpasswd'
    password = run('openssl', 'passwd', '-apr1', 'synthetic', capture_output=True, text=True).stdout
    write(password_file, 'reader:' + password, 0o600)
    restic_repo, restic_password = [secrets / name for name in ('repository', 'password')]
    write(restic_repo, str(root / 'restic') + '\n', 0o600)
    write(restic_password, 'fixture-only\n', 0o600)
    restic = [tools.restic, '--repository-file', restic_repo, '--password-file', restic_password]
    run(*restic, 'init', capture_output=True)

    backup = {'restic': tools.restic, 'repository_file': str(restic_repo), 'password_file': str(restic_password)}
    https_port = port()
    site_root = web / 'srv/forecast-sites/eeai'
    receiver = web / 'opt/fri-ops/publish_ssh.py'
    verifier = web / 'opt/fri-ops/verify-eeai'
    sender = prod / 'opt/fri-ops/publish_ssh.py'
    transport = ['python3', str(sender), 'send', '--ssh-config', '/fixture-unused',
                 '--target', 'forecast-web', '--receiver', str(receiver), '--root',
                 str(site_root), '--verify', str(verifier)]
    site = {'hostname': 'localhost', 'certificate': str(cert), 'certificate_key': str(key),
            'publisher_key': (secrets / 'publisher.pub').read_text(), 'access': 'basic',
            'ca_file': str(cert), 'password_file': str(password_file), 'verifier_credentials': str(credential),
            'downloads': ['eeai-data.zip']}
    roles = {
        prod: {'role': 'forecast-prod', 'projects': {
            'eeai': {'repository': 'https://git.example.org/fixture/repo.git',
                     'github_repository': 'fixture/repo', 'path': '/usr/local/bin:/usr/bin:/bin',
                     'public_publisher': transport}}},
        web: {'role': 'forecast-web', 'sites': {'eeai': site}, 'listen': f'127.0.0.1:{https_port}'},
    }

    def provision():
        for destination, settings in roles.items():
            inventory = root / f'{destination.name}_inventory.py'
            data = {'settings': {**settings, 'fixture_root': str(destination), 'backup': backup}}
            write(inventory, f'hosts = [("@local", {data!r})]\n')
            result = subprocess.run([tools.pyinfra, inventory, HERE / 'deploy.py', '--yes'],
                                    env={**os.environ, 'FRI_INFRA_FIXTURE': '1'},
                                    capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)

    provision()
    launcher = prod / 'usr/local/bin/forecast-prod'
    runtime = prod / 'srv/forecasts/eeai'
    state = runtime / 'state/results/synthetic.jsonl'
    receipt = runtime / 'runs/synthetic.json'
    write(state, '{"fixture":"completed forecast"}\n', 0o600)
    write(receipt, '{"status":"succeeded"}\n', 0o600)
    credentials = runtime / 'home/.config/fri-forecaster/env'
    write(credentials, 'FAKE_KEY=fixture-credential\n', 0o600)
    run(launcher, 'tick-all', capture_output=True)
    write(runtime / 'jobs-enabled', '')
    write(runtime / 'scheduler-v2.json', '{}')

    nginx_config = root / 'nginx.conf'
    write(nginx_config, f"""pid {root}/nginx.pid;
error_log stderr crit;
events {{}}
http {{
    access_log off;
    client_body_temp_path {root}/client;
    proxy_temp_path {root}/proxy;
    fastcgi_temp_path {root}/fastcgi;
    uwsgi_temp_path {root}/uwsgi;
    scgi_temp_path {root}/scgi;
    include {web}/etc/nginx/conf.d/*.conf;
}}
""")
    run(tools.nginx, '-t', '-p', root, '-c', nginx_config, capture_output=True)
    nginx = subprocess.Popen([tools.nginx, '-p', root, '-c', nginx_config, '-g', 'daemon off;'])
    try:
        for _ in range(100):
            try:
                with socket.create_connection(('127.0.0.1', https_port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError('Staging nginx did not start')

        authorized = web / 'var/lib/forecast-publish/.ssh/authorized_keys'
        # The transport substitute invokes the exact forced command generated by the web role.
        forced = authorized.read_text().split('"', 2)[1]
        ssh_fixture = secrets / 'bin/ssh'
        write(ssh_fixture, '#!/bin/sh\nfor last; do :; done\n'
              f'export SSH_ORIGINAL_COMMAND="$last"\nexec {forced}\n', 0o755)
        publisher_env = {**os.environ, 'PATH': str(ssh_fixture.parent) + ':' + os.environ['PATH']}
        candidate = runtime / 'sites/synthetic'
        baseline = {'source': 'survey', 'as_of': None}
        html = page.splice('<!doctype html><html><head></head><p>Fixture frontend</p></html>',
                           'EEAI', {'baseline': baseline})
        write(candidate / 'index.html', html)
        with ZipFile(candidate / 'eeai-data.zip', 'w') as archive:
            archive.writestr('baseline.json', json.dumps(baseline))
        expected = sha256((candidate / 'eeai-data.zip').read_bytes()).hexdigest()
        metadata = {'dashboard': 'eeai', 'release': 'synthetic', 'commit': 'fixture',
                    'data_snapshot': site_kit.snapshot(candidate, ('eeai-data.zip',))}
        write(candidate / 'release.json', json.dumps(metadata))
        release = runtime / 'releases/synthetic'
        release.mkdir(parents=True)
        (release / 'code').symlink_to(prod / 'opt/fri-ops/bootstrap')
        (release / 'site').symlink_to(candidate)
        (runtime / 'current').symlink_to(release)

        def send(source):
            return subprocess.run([*transport, source], capture_output=True, env=publisher_env)

        run(launcher, 'eeai', 'republish', env=publisher_env)
        current = (site_root / 'current').resolve(strict=True)
        bad = root / 'bad-site'
        shutil.copytree(candidate, bad)
        (bad / 'eeai-data.zip').unlink()
        assert send(bad).returncode and (site_root / 'current').resolve(strict=True) == current
        # An SSH request outside the issued key's single-site command is rejected before a receive.
        forbidden = subprocess.run(shlex.split(forced), env={**os.environ, 'SSH_ORIGINAL_COMMAND': 'id'},
                                   capture_output=True)
        assert forbidden.returncode

        quarantines = {}
        for destination, settings in roles.items():
            backup_cmd = [destination / 'opt/fri-ops/backup.py', destination / 'etc/fri-ops/backup.json']
            run(*backup_cmd, 'backup', capture_output=True)
            snapshots = json.loads(run(*restic, 'snapshots', '--tag', settings['role'], '--json',
                                       capture_output=True).stdout)
            snapshot, = snapshots
            quarantine = root / f'{destination.name}-restore'
            run(*backup_cmd, 'restore', snapshot['id'], quarantine, '--staging', capture_output=True)
            assert (quarantine / 'STAGING').is_file() and quarantine.stat().st_mode & 0o777 == 0o700
            restored = quarantine / 'snapshot' / destination.relative_to('/')
            quarantines[destination] = restored
            destination.rename(root / f'{destination.name}-original')
        run(*restic, 'check', capture_output=True)
        provision()

        for destination, subtree in ((prod, 'srv/forecasts'), (web, 'srv/forecast-sites')):
            shutil.copytree(quarantines[destination] / subtree, destination / subtree,
                            dirs_exist_ok=True, symlinks=True)
        assert state.read_text() == '{"fixture":"completed forecast"}\n'
        assert receipt.read_text() == '{"status":"succeeded"}\n'
        assert not credentials.exists() and not (runtime / 'jobs-enabled').exists()
        assert not (runtime / 'scheduler-v2.json').exists()
        assert (prod / 'etc/fri-ops/staging').exists()
        run(launcher, 'tick-all', capture_output=True)
        report = json.loads(run(launcher, 'eeai', 'status', capture_output=True).stdout)
        assert report['release'] == metadata and not report['jobs_enabled']
        run(verifier, candidate, capture_output=True)
        assert (site_root / 'current').resolve(strict=True) == current
        assert sha256((current / 'eeai-data.zip').read_bytes()).hexdigest() == expected
        run(launcher, 'eeai', 'republish', env=publisher_env)

        # A second apply leaves the selected site and downloaded bytes intact.
        selected = (site_root / 'current').resolve(strict=True)
        provision()
        assert (site_root / 'current').resolve(strict=True) == selected
        run(verifier, candidate, capture_output=True)
    finally:
        nginx.terminate()
        nginx.wait(timeout=10)
    print('Provisioned both roles, verified protected HTTPS and failed-publication rollback, '
          'restored private results and receipts with scheduling disabled, and republished identical bytes.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for tool in ('nginx', 'restic', 'pyinfra'):
        parser.add_argument('--' + tool, default=shutil.which(tool), required=shutil.which(tool) is None)
    args = parser.parse_args()
    # The drill passes executable paths into private inventory and subprocesses.
    for tool, value in vars(args).items():
        setattr(args, tool, str(Path(value).resolve(strict=True)))
    with TemporaryDirectory(prefix='fri-infra-') as work:
        drill(Path(work).resolve(), args)


if __name__ == '__main__':
    main()
