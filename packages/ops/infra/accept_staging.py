#!/usr/bin/env python3
"""Rehearse publication and recovery on two explicitly disposable Ubuntu guests."""
import argparse
import base64
from hashlib import file_digest
from ipaddress import IPv4Address, IPv4Interface
import json
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import tarfile
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
SITES = ('airo', 'eeai', 'rsi')
HOME = '/var/lib/fri-acceptance'
INSTALL = '/opt/fri-ops'
RUNTIMES = '/srv/forecasts'
WEB_ROOT = '/srv/forecast-sites'
FORECAST_HOME = '/var/lib/forecast'
ORIGINS = [f'fri-dashboard@{site}.service' for site in SITES]
WORKERS = ('fri-dashboard-tick.service', 'fri-dashboard-worker@paid.service', 'fri-dashboard-worker@data.service')


def run(*argv, **kwargs):
    return subprocess.run(argv, check=True, **kwargs)


def shell(*argv):
    return shlex.join(map(str, argv))


class Guest:
    def __init__(self, config, alias, role):
        self.alias = alias
        self.role = role
        self.transport = ['ssh', '-F', str(config), '-oBatchMode=yes',
                          '-oStrictHostKeyChecking=yes', '-oConnectTimeout=10', '--', alias]

    def command(self, command, **kwargs):
        return run(*self.transport, command, **kwargs)

    def output(self, command, **kwargs):
        return self.command(command, stdout=subprocess.PIPE, text=True, **kwargs).stdout.strip()

    def upload(self, archive, destination):
        with archive.open('rb') as source:
            self.command(f'umask 077; mkdir {shlex.quote(destination)} && '
                         f'tar -xf - -C {shlex.quote(destination)}', stdin=source)

    def preflight(self, nonce):
        paths = (HOME, INSTALL, '/etc/fri-ops', '/etc/fri-dashboards', RUNTIMES, WEB_ROOT,
                 FORECAST_HOME, '/var/lib/forecast-publish', '/usr/local/bin/dashboardctl',
                 '/usr/local/bin/forecast-prod', '/run/fri-sites', '/etc/tmpfiles.d/fri-sites.conf')
        units = ('/etc/systemd/system/fri-dashboard*', '/etc/systemd/system/fri-backup*',
                 '/etc/nginx/conf.d/*.conf', '/etc/nginx/sites-enabled/*')
        identity = self.output(f"""
set -eu
export LC_ALL=C
IFS= read -r nonce
test "$(id -u)" = 0
test "$(ps -p 1 -o comm=)" = systemd
. /etc/os-release
test "$ID" = ubuntu && test "$VERSION_ID" = 24.04
test ! -L /etc/fri-acceptance-host
test "$(stat -c '%u:%a:%F' /etc/fri-acceptance-host)" = '0:600:regular file'
if [ "$(cat /etc/fri-acceptance-host)" != "$(printf '%s\n%s' "$nonce" {self.role})" ]; then
    echo 'Mismatched disposable-host marker' >&2
    exit 1
fi
for path in {shell(*paths)} {' '.join(units)}; do
    if [ "$path" = /etc/nginx/sites-enabled/default ]; then
        case "$(readlink "$path")" in
            /etc/nginx/sites-available/default|../sites-available/default) continue ;;
        esac
    fi
    if [ -e "$path" ] || [ -L "$path" ]; then
        echo "Occupied acceptance path: $path" >&2
        exit 1
    fi
done
if getent passwd forecast forecast-publish | grep -q .; then
    echo 'Occupied acceptance service account' >&2
    exit 1
fi
cat /etc/machine-id
""", input=nonce + '\n')
        if not re.fullmatch(r'[0-9a-f]{32}', identity):
            raise RuntimeError('Guest must provide a valid machine ID')
        return identity


def fixtures(directory):
    sites = {}
    for project in SITES:
        site = directory / project
        paths = sorted(site.rglob('*'))
        if any(path.is_symlink() or not (path.is_file() or path.is_dir()) for path in paths):
            raise ValueError('Expected only regular fixture files and directories')
        metadata = json.loads((site / 'release.json').read_text())
        if metadata.get('dashboard') != project:
            raise ValueError(f'Incorrect dashboard metadata for {project}')
        release = metadata['release']
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', release):
            raise ValueError('Expected a simple release identifier')
        with (site / 'index.html').open('rb') as source:
            if not source.read(1):
                raise ValueError(f'Empty fixture page for {project}')
        files = {}
        for path in paths:
            if path.is_file():
                with path.open('rb') as source:
                    files[path.relative_to(site).as_posix()] = file_digest(source, 'sha256').hexdigest()
        sites[project] = (release, metadata['data_snapshot']['sha256'], files)
    return sites


def write(path, text, mode=0o600):
    path.write_text(text)
    path.chmod(mode)


def make_tar(source, destination):
    with tarfile.open(destination, 'w') as archive:
        for member in sorted(source.iterdir()):
            archive.add(member, arcname=member.name)


def prepare(root, prod, web, args):
    prod_dir, web_dir = (root / guest.alias for guest in (prod, web))
    for directory in (prod_dir, web_dir):
        directory.mkdir()
        write(directory / 'repository', HOME + '/repository-data\n')
        write(directory / 'backup-password', secrets.token_hex(32) + '\n')
    password = secrets.token_hex(24)
    auth = 'Basic ' + base64.b64encode(f'fixture:{password}'.encode()).decode()
    write(web_dir / 'eeai-reader.json', json.dumps({'username': 'fixture', 'password': password}))
    write(prod_dir / 'eeai-headers.json', json.dumps({'Authorization': auth}))
    password_hash = subprocess.check_output(['openssl', 'passwd', '-apr1', '-stdin'],
                                           input=password + '\n', text=True)
    write(web_dir / 'eeai.htpasswd', 'fixture:' + password_hash)
    domains = {site: f'{site}.forecast.test' for site in SITES}
    san = ','.join(f'DNS:{domain}' for domain in domains.values())
    run('openssl', 'req', '-x509', '-sha256', '-nodes', '-newkey', 'rsa:2048', '-days', '90',
        '-subj', '/CN=forecast.test', '-addext', f'subjectAltName={san}',
        '-keyout', web_dir / 'tls.key', '-out', web_dir / 'tls.pem', stderr=subprocess.DEVNULL)
    cert = (web_dir / 'tls.pem').read_text()
    write(prod_dir / 'tls.pem', cert, 0o644)
    (web_dir / 'tls.pem').chmod(0o644)

    ssh_config = []
    sites, projects = {}, {}
    web_host_key = web.output('cat /etc/ssh/ssh_host_ed25519_key.pub').split()[:2]
    write(prod_dir / 'web-known-hosts', f'{args.web_address} {" ".join(web_host_key)}\n')
    for project, domain in domains.items():
        run('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', prod_dir / f'publish-{project}')
        ssh_config.append(f"""Host publish-{project}
    HostName {args.web_address}
    User forecast-publish
    BatchMode yes
    IdentitiesOnly yes
    IdentityFile {HOME}/publish-{project}
    UserKnownHostsFile {HOME}/web-known-hosts
    StrictHostKeyChecking yes
""")
        url = f'https://{domain}/'
        verifier = ['/usr/bin/env', f'SSL_CERT_FILE={HOME}/tls.pem', '/usr/bin/python3',
                    f'{INSTALL}/bootstrap/packages/ops/eeai_operator.py', 'public']
        sites[project] = {'hostname': domain, 'certificate': f'{HOME}/tls.pem',
                          'certificate_key': f'{HOME}/tls.key', 'ca_file': f'{HOME}/tls.pem',
                          'publisher_key': (prod_dir / f'publish-{project}.pub').read_text(),
                          'access': 'public'}
        if project == 'eeai':
            sites[project].update(access='basic', password_file=f'{HOME}/eeai.htpasswd',
                                  verifier_credentials=f'{HOME}/eeai-reader.json', downloads=['eeai-data.zip'])
            verifier += ['--headers-file', f'{HOME}/eeai-headers.json', '--expect-url', url]
        elif project == 'airo':
            sites[project]['downloads'] = ['redlines-data.zip', 'forecasts.csv', 'rationales.csv']
        projects[project] = {
            'repository': 'https://git.example.org/test/forecast.git',
            'github_repository': 'test/forecast', 'path': '/usr/local/bin:/usr/bin:/bin',
            'public_url': url, 'public_verifier': verifier,
            'public_publisher': ['/usr/bin/python3', f'{INSTALL}/publish_ssh.py', 'send',
                                 '--ssh-config', f'{HOME}/publisher-config', '--target', f'publish-{project}',
                                 '--receiver', f'{INSTALL}/publish_ssh.py', '--root', f'{WEB_ROOT}/{project}',
                                 '--verify', f'{INSTALL}/verify-{project}'],
        }
        if project != 'airo':
            projects[project]['panel_runtime_config'] = '/etc/fri-dashboards/airo.json'
    write(prod_dir / 'publisher-config', '\n'.join(ssh_config))
    hostnames = f'{args.web_address} {" ".join(domains.values())}\n'
    write(prod_dir / 'test-hostnames', hostnames)
    write(web_dir / 'test-hostnames', hostnames)
    backup = {'repository_file': f'{HOME}/repository', 'password_file': f'{HOME}/backup-password'}
    role_settings = [
        {'role': prod.role, 'projects': projects, 'start_origins': True, 'backup': backup, 'staging': True,
         'node': {'archive': args.node_archive, 'sha256': args.node_sha256, 'version': args.node_version}},
        {'role': web.role, 'sites': sites, 'backup': backup, 'staging': True},
    ]
    inventory = root / 'inventory.py'
    write(inventory, 'hosts = ' + repr([(guest.alias, {
        'settings': settings, 'ssh_config_file': str(args.ssh_config),
        'ssh_strict_host_key_checking': 'yes', 'ssh_user': 'root',
    }) for guest, settings in zip((prod, web), role_settings)]) + '\n')
    for guest, directory in ((prod, prod_dir), (web, web_dir)):
        archive = root / (guest.alias + '.tar')
        make_tar(directory, archive)
        guest.upload(archive, HOME)
        guest.command(f'chmod 0755 {HOME}; cat {HOME}/test-hostnames >> /etc/hosts')
    return inventory, auth, domains


def seed(root, prod, first, second, revisions):
    archive = root / 'publications.tar'
    with tarfile.open(archive, 'w') as tar:
        for source, revision in zip((first, second), revisions):
            for project in SITES:
                release = revision[project][0]
                tar.add(source / project, arcname=f'{project}/sites/{release}')
    prod.upload(archive, HOME + '/publications')
    prod.command(f"""
set -eu
for project in {' '.join(SITES)}; do
    destination={RUNTIMES}/$project
    cp -a {HOME}/publications/$project/sites "$destination/"
    mkdir -p "$destination"/state/results "$destination"/runs "$destination"/home
    printf '%s\n' '{{"fixture":"accrued-data"}}' > "$destination"/state/results/fixture.jsonl
    printf '%s\n' '{{"key":"fixture","job":"smoke","status":"succeeded"}}' > "$destination"/runs/fixture.json
    touch "$destination"/home/fixture-credentials
done
""")
    for project in SITES:
        target = f'{RUNTIMES}/{project}'
        first_release, second_release = [revision[project][0] for revision in revisions]
        for release in (first_release, second_release):
            prod.command(f"""
set -eu
mkdir -p {target}/releases/{release}
ln -s {INSTALL}/bootstrap {target}/releases/{release}/code
ln -s {target}/sites/{release} {target}/releases/{release}/site
cp {target}/sites/{release}/release.json {target}/releases/{release}/release.json
""")
        prod.command(f"""
set -eu
ln -s {target}/releases/{first_release} {target}/current
ln -s {target}/releases/{second_release} {target}/previous
chown -R forecast:forecast {target}
""")


READER = """
import hashlib, json, ssl, sys
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import build_opener, HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None
configuration = json.load(sys.stdin)
opener = build_opener(NoRedirect(), ProxyHandler({}),
                      HTTPSHandler(context=ssl.create_default_context(cafile=configuration['ca'])))
for site in configuration['sites']:
    headers = {'Authorization': configuration['auth']} if site['protected'] else {}
    for path, expected in site['files'].items():
        url = site['url'] + ('' if path == 'index.html' else quote(path))
        with opener.open(Request(url, headers=headers), timeout=15) as response:
            if response.status != 200 or hashlib.file_digest(response, 'sha256').hexdigest() != expected:
                raise SystemExit(f'Served bytes differ: {site["url"]}{path}')
        if site['protected'] and path in ('index.html', 'release.json', 'eeai-data.zip'):
            for denied in ({}, {'Authorization': 'Basic aW52YWxpZDppbnZhbGlk'}):
                try:
                    with opener.open(Request(url, headers=denied), timeout=10):
                        raise SystemExit(f'Gate permitted an unauthorized request: {path}')
                except HTTPError as error:
                    if error.code != 401:
                        raise
    try:
        with opener.open(Request(site['url'] + 'state/results/fixture.jsonl', headers=headers), timeout=10):
            raise SystemExit('Ingress exposed private state')
    except HTTPError as error:
        if error.code != 404:
            raise
"""


def verify(prod, auth, domains, revision):
    spec = {'auth': auth, 'ca': f'{HOME}/tls.pem', 'sites': [
        {'url': f'https://{domain}/', 'files': revision[site][2], 'protected': site == 'eeai'}
        for site, domain in domains.items()]}
    prod.command(shell('python3', '-c', READER), input=json.dumps(spec), text=True)
    for project, (release, _, _) in revision.items():
        prod.command(f'test "$(basename "$(readlink {RUNTIMES}/{project}/current)")" = {shlex.quote(release)}')


def cli(guest, *argv, check=True):
    result = subprocess.run([*guest.transport, shell('runuser', '-u', 'forecast', '--',
                                                    '/usr/local/bin/forecast-prod', *argv)],
                            capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(f'Operator command {argv} failed:\n{result.stderr}')
    return result


def backup_restore(guest, tree):
    restic = f'restic --repository-file {HOME}/repository --password-file {HOME}/backup-password'
    backup = f'{INSTALL}/backup.py /etc/fri-ops/backup.json'
    guest.command(f'{restic} init && {backup} backup && {backup} check')
    snapshots = json.loads(guest.output(f'{restic} snapshots --tag {guest.role} --json'))
    snapshot, = snapshots
    guest.command(f"""
set -eu
{backup} restore {snapshot['id']} {HOME}/restored --staging
test -f {HOME}/restored/STAGING
mv {tree} {HOME}/before-restore
cp -a {HOME}/restored/snapshot{tree} {tree}
""")


def rehearsal(args, root, prod, web, revisions):
    inventory, auth, domains = prepare(root, prod, web, args)

    def provision():
        run(args.pyinfra, inventory, HERE / 'deploy.py', '--yes', '--fail-percent', '0')

    provision()
    prod.command(f"""
set -eu
chown forecast:forecast {HOME}/publish-* {HOME}/publisher-config {HOME}/web-known-hosts {HOME}/eeai-headers.json
runuser -u forecast -- {INSTALL}/toolchain.py check /etc/fri-dashboards/eeai.json
""")
    web.command(f'chown forecast-publish:forecast-publish {HOME}/eeai-reader.json && '
                f'chgrp www-data {HOME}/eeai.htpasswd && chmod 0640 {HOME}/eeai.htpasswd')
    print('Forecast and web roles ready on isolated guests', flush=True)
    seed(root, prod, args.first, args.second, revisions)
    for project in SITES:
        cli(prod, project, 'republish')
        denied = prod.output(f"""
set +e
runuser -u forecast -- ssh -F {HOME}/publisher-config publish-{project} id 2>&1
test "$?" -ne 0
""")
        if 'Invalid publication request' not in denied:
            raise RuntimeError('Site publisher key did not reach the restricted SSH receiver')
    verify(prod, auth, domains, revisions[0])
    print('Verified all initial site bytes, TLS, and anonymous/invalid EEAI access', flush=True)

    for project in SITES:
        cli(prod, project, 'rollback')  # Select pre-seeded second release.
    verify(prod, auth, domains, revisions[1])
    for project in SITES:
        verifier = f'{INSTALL}/verify-{project}'
        web.command(f"mv {verifier} {verifier}.ready && "
                    f"printf '%s\n' '#!/bin/sh' 'echo fixture-verifier-injected >&2' 'exit 1' > {verifier} && "
                    f'chmod 0755 {verifier}')
        try:
            attempted = cli(prod, project, 'rollback', check=False)
            if not attempted.returncode or 'fixture-verifier-injected' not in attempted.stderr:
                raise RuntimeError(f'{project}: expected a failure from the publication verifier')
        finally:
            web.command(f'mv -Tf {verifier}.ready {verifier}')
        verify(prod, auth, domains, revisions[1])
    for project in SITES:
        cli(prod, project, 'rollback')
    verify(prod, auth, domains, revisions[0])
    print('Verified failed publication retained both pointers and normal rollback works on all sites', flush=True)

    prod.command('test -f /etc/fri-ops/staging && systemctl start ' + shell(*WORKERS))
    for worker in WORKERS:
        if prod.output(shell('systemctl', 'show', '-p', 'ConditionResult', '--value', worker)) != 'no':
            raise RuntimeError(f'Staging did not block {worker}')
    for project in SITES:
        prod.command(f'touch {RUNTIMES}/{project}/jobs-enabled {RUNTIMES}/{project}/scheduler-v2.json')
    prod.command('systemctl stop ' + shell(*ORIGINS))
    web.command('systemctl stop nginx.service')
    backup_restore(prod, RUNTIMES)
    backup_restore(web, WEB_ROOT)
    for project in SITES:
        directory = f'{RUNTIMES}/{project}'
        prod.command(f"""
set -eu
test "$(cat {directory}/state/results/fixture.jsonl)" = '{{"fixture":"accrued-data"}}'
test "$(cat {directory}/runs/fixture.json)" = '{{"key":"fixture","job":"smoke","status":"succeeded"}}'
test ! -e {directory}/home/fixture-credentials
test ! -e {directory}/jobs-enabled
test ! -e {directory}/scheduler-v2.json
""")
    provision()
    prod.command('systemctl restart ' + shell(*ORIGINS))
    web.command('systemctl restart nginx.service')
    verify(prod, auth, domains, revisions[0])
    for project in SITES:
        cli(prod, project, 'republish')
    verify(prod, auth, domains, revisions[0])
    report = json.loads(cli(prod, 'status', '--verify-content', '--json').stdout)
    for project, status in report['dashboards'].items():
        delivery = status['delivery']
        if any(delivery[endpoint]['result'] != 'verified' for endpoint in ('origin', 'public')):
            raise RuntimeError(f'{project}: fleet status did not verify both delivery endpoints')
    if set(report['dashboards']) != set(SITES):
        raise RuntimeError('Fleet status did not report all installed projects')
    print('Restored both roles from verified snapshots; reapplied and restarted real services', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-config', type=Path, required=True)
    parser.add_argument('--nonce-file', type=Path, required=True)
    parser.add_argument('--web-address', type=IPv4Address, required=True)
    parser.add_argument('--pyinfra', type=Path, required=True)
    parser.add_argument('--node-archive', required=True)
    parser.add_argument('--node-sha256', required=True)
    parser.add_argument('--node-version', default='22.21.1')
    parser.add_argument('first', type=Path)
    parser.add_argument('second', type=Path)
    args = parser.parse_args()
    args.ssh_config = args.ssh_config.resolve(strict=True)
    args.pyinfra = args.pyinfra.resolve(strict=True)
    if subprocess.check_output([args.pyinfra, '--version'], text=True).strip() != 'pyinfra: v3.11.2':
        parser.error('Install the checked pyinfra requirement into a controller virtualenv')
    nonce = args.nonce_file.read_text().strip()
    if not re.fullmatch(r'[0-9a-f]{32,128}', nonce):
        parser.error('Expected a random hexadecimal nonce from the disposable-host factory')
    if (not re.fullmatch(r'/[a-zA-Z0-9/._-]+', args.node_archive) or '..' in Path(args.node_archive).parts
            or not re.fullmatch(r'[0-9a-f]{64}', args.node_sha256)
            or not re.fullmatch(r'[0-9]+[.][0-9]+[.][0-9]+', args.node_version)):
        parser.error('Expected an absolute guest Node archive path and its trusted SHA-256')
    revisions = [fixtures(directory) for directory in (args.first, args.second)]
    for project in SITES:
        first, second = [revision[project] for revision in revisions]
        if first[0] == second[0] or first[1] != second[1] or first[2]['index.html'] == second[2]['index.html']:
            parser.error(f'{project} needs two frontend revisions around the same data snapshot')
    prod = Guest(args.ssh_config, 'stage-prod', 'forecast-prod')
    web = Guest(args.ssh_config, 'stage-web', 'forecast-web')
    try:
        identities = [guest.preflight(nonce) for guest in (prod, web)]
    except subprocess.CalledProcessError:
        parser.error('Disposable-host preflight failed before either guest was changed')
    if identities[0] == identities[1]:
        parser.error('Acceptance needs two distinct disposable guests')
    addresses = web.output("ip -o -4 addr show | awk '{ print $4 }'").splitlines()
    if args.web_address not in {IPv4Interface(address).ip for address in addresses}:
        parser.error('The web address must belong to the marked web guest')
    if prod.output(shell('sha256sum', args.node_archive)).split()[0] != args.node_sha256:
        parser.error('The guest Node archive does not match its trusted SHA-256')
    with TemporaryDirectory(prefix='fri-acceptance-') as work:
        rehearsal(args, Path(work), prod, web, revisions)


if __name__ == '__main__':
    main()
