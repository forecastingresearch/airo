"""Provision the forecasting runtime or static ingress from checked code and private inventory."""
from io import StringIO
import json
import os
from pathlib import Path
import re
import shlex

from pyinfra import host
from pyinfra.operations import apt, files, server, systemd


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
settings = host.data.settings
role = settings['role']
staging = settings.get('staging', True)
if not isinstance(staging, bool):
    raise ValueError('Select staging with a boolean inventory value')
fixture = host.name == '@local' and os.environ.get('FRI_INFRA_FIXTURE') == '1'
prefix = settings.get('fixture_root', '')
if bool(prefix) != fixture or (host.name == '@local' and not fixture):
    raise ValueError('An isolated local fixture requires @local, FRI_INFRA_FIXTURE=1 and fixture_root')
if fixture and (not staging or not re.fullmatch(r'/[a-zA-Z0-9/._-]+', prefix)
                or Path(prefix).resolve() != Path(prefix) or Path(prefix).parent == Path('/')):
    raise ValueError('The fixture must use a dedicated absolute staging directory')
if role not in ('forecast-prod', 'forecast-web'):
    raise ValueError('Unknown forecasting host role')


def path(value):
    if not re.fullmatch(r'/[a-zA-Z0-9/._-]+', value) or '..' in Path(value).parts:
        raise ValueError('Expected a safe absolute managed-host path')
    return prefix + value


def checked_path(value):
    path(value)
    return value


def name(value):
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', value):
        raise ValueError('Expected a safe account or site name')
    return value


root_owner = {} if fixture else {'user': 'root', 'group': 'root'}


def directory(destination, mode='0755', **owner):
    files.directory(path=destination, mode=mode, **(owner or root_owner))


def put(destination, source, mode='0644', **owner):
    files.put(src=source, dest=destination, mode=mode, **(owner or root_owner))


def content(destination, value, mode='0644', **owner):
    put(destination, StringIO(value), mode, **owner)


def json_file(destination, value, mode='0644', **owner):
    content(destination, json.dumps(value, indent=2) + '\n', mode, **owner)


def template(destination, filename, **variables):
    files.template(src=str(HERE / 'templates' / filename), dest=destination, mode='0644',
                   **root_owner, **variables)


def account_owner(user, group):
    return {} if fixture else {'user': user, 'group': group}


install = path('/opt/fri-ops')
private = path('/etc/fri-ops')
units = path('/etc/systemd/system')
bindir = path('/usr/local/bin')
if role == 'forecast-prod' and not fixture:
    server.shell(name='Check existing scheduler before writing units', commands=[fr"""
set -eu
timer=fri-dashboard-tick.timer
tick=fri-dashboard-tick.service
tick_state=$(systemctl show -p ActiveState --value "$tick")
if systemctl is-active --quiet "$timer" || systemctl is-enabled --quiet "$timer" ||
   [ "$tick_state" = activating ] || [ "$tick_state" = active ] || [ "$tick_state" = deactivating ]; then
    if [ {int(staging)} = 1 ] ||
       ! systemctl show -p ExecStart --value "$tick" |
           grep -Eq 'argv\[\]=/usr/local/bin/dashboardctl tick-all --queued( ;|$)'; then
        echo "Stop, disable and drain $timer and $tick before provisioning their replacements." >&2
        exit 1
    fi
fi
if [ {int(staging)} = 1 ]; then
    for lane in paid data; do
        worker=fri-dashboard-worker@$lane.service
        if systemctl is-active --quiet "$worker" || systemctl is-enabled --quiet "$worker"; then
            echo "Stop and disable $worker before applying a staging inventory." >&2
            exit 1
        fi
    done
fi
"""])
directory(install)
directory(private)
directory(units)
directory(bindir)
if staging:
    content(f'{private}/staging', '')
else:
    files.file(path=f'{private}/staging', present=False)

if not fixture:
    packages = ['ca-certificates', 'python3', 'restic']
    packages += ['git', 'gh', 'python3-venv', 'openssh-client'] if role == 'forecast-prod' else ['nginx', 'openssh-server']
    apt.packages(packages=packages, update=True, cache_time=3600)

put(f'{install}/backup.py', str(HERE / 'backup.py'), '0755')
put(f'{install}/publish_ssh.py', str(HERE.parent / 'publish_ssh.py'), '0755')
backup = settings['backup']
backup_config = f'{private}/backup.json'
backup_paths = []
backup_locks = []
backup_exclude = []
staging_exclude = []

if role == 'forecast-prod':
    account = name(settings.get('account', 'forecast'))
    group = name(settings.get('group', account))
    service_home = path(settings.get('service_home', '/var/lib/forecast'))
    configs = path('/etc/fri-dashboards')
    runtime_parent = path(settings.get('runtime_parent', '/srv/forecasts'))
    socket_dir = path('/run/fri-sites')
    bootstrap = f'{install}/bootstrap'
    owner = account_owner(account, group)
    if not fixture:
        server.group(group=group, system=True)
        server.user(user=account, group=group, system=True, home=service_home, ensure_home=False)
    directory(service_home, '0700', **owner)
    directory(runtime_parent)
    directory(configs, '0750', **account_owner('root', group))
    directory(path('/etc/tmpfiles.d'))
    directory(socket_dir, '0750', **owner)
    content(path('/etc/tmpfiles.d/fri-sites.conf'), f'd {socket_dir} 0750 {account} {group} -\n')
    toolchain = f'{install}/toolchain.py'
    put(toolchain, str(HERE / 'toolchain.py'), '0755')
    node = settings.get('node')
    if node:
        release = node['version']
        if not re.fullmatch(r'[0-9]+[.][0-9]+[.][0-9]+', release):
            raise ValueError('Expected a pinned three-part Node.js version')
        directory(f'{install}/node')
        node_root = f'{install}/node/{release}'
        command = [toolchain, 'install-node', checked_path(node['archive']), node['sha256'], release, node_root]
        server.shell(commands=[shlex.join(command)], name='Install pinned Node.js distribution')
    put(f'{bindir}/dashboardctl', str(HERE.parent / 'dashboardctl'), '0755')
    content(f'{bindir}/forecast-prod',
            f'#!/bin/sh\nexport DASHBOARD_CONFIG_DIR="${{DASHBOARD_CONFIG_DIR:-{configs}}}"\n'
            f'exec {bindir}/dashboardctl "$@"\n', '0755')
    for relative in ('', '/code', '/packages', '/packages/ops', '/packages/dashboard'):
        directory(bootstrap + relative)
    bootstrap_files = [REPO / 'code/production.py', HERE.parent / 'jobs.json',
                       *sorted(HERE.parent.glob('*.py'))]
    for source in bootstrap_files:
        put(f'{bootstrap}/{source.relative_to(REPO)}', str(source))
    files.sync(src=str(REPO / 'packages/dashboard/dashboard'), dest=f'{bootstrap}/packages/dashboard/dashboard',
               exclude=['*.pyc'], exclude_dir=['__pycache__'], mode='0644', dir_mode='0755', **root_owner)
    environment = settings.get('environment_file')
    if staging and environment:
        raise ValueError('Staging must not load the production forecasting environment')
    if environment:
        backup_paths.append(checked_path(environment))
        staging_exclude.append(environment)
    for dashboard, runtime in settings['projects'].items():
        name(dashboard)
        root = f'{runtime_parent}/{dashboard}'
        directory(root, '0700', **owner)
        files.file(path=f'{root}/operation.lock', mode='0600', **owner)
        if node:
            runtime = {**runtime, 'path': f"{node_root}/bin:{runtime['path']}"}
        json_file(f'{configs}/{dashboard}.json', {
            **runtime, 'dashboard': dashboard, 'root': root, 'bootstrap_code': bootstrap,
            'socket': f'{socket_dir}/{dashboard}.sock',
        }, '0640', **account_owner('root', group))
        backup_paths.append(root)
        backup_locks.append(f'{root}/operation.lock')
        backup_exclude += [f'{root}/{entry}' for entry in (
            'operation.lock', 'scheduler-dispatch.lock', 'scheduler-mode.lock', 'repository.git', 'tmp',
        )]
        staging_exclude += [f'{root}/{entry}' for entry in ('jobs-enabled', 'scheduler-v2.json', 'home')]
    backup_paths += [configs, service_home]
    staging_exclude += [configs, service_home]

    for unit, description, command, scheduled, kind, install_target in (
        ('fri-dashboard@.service', 'Serve the %i dashboard', '%i serve', False, 'simple', 'multi-user.target'),
        ('fri-dashboard-tick.service', 'Check all forecast schedules', 'tick-all --queued', True, 'oneshot', ''),
        ('fri-dashboard-worker@.service', 'Process %i dashboard jobs', 'worker-all --lane %i', True, 'simple', 'multi-user.target'),
    ):
        template(f'{units}/{unit}', 'runtime.service.j2', description=description, command=command,
                 scheduled=scheduled, kind=kind, install_target=install_target, restart=kind == 'simple',
                 account=account, runtime_group=group, configs=configs, launcher=f'{bindir}/dashboardctl',
                 private=private, environment_file=checked_path(environment) if environment else None)
    put(f'{units}/fri-dashboard-tick.timer', str(HERE.parent / 'examples/fri-dashboard-tick.timer'))
    if not fixture:
        server.shell(commands=['systemctl daemon-reload'], name='Load forecasting systemd units')
        if settings.get('start_origins'):
            for dashboard in settings['projects']:
                systemd.service(service=f'fri-dashboard@{dashboard}.service', running=True, enabled=True)

if role == 'forecast-web':
    publisher = name(settings.get('publisher', 'forecast-publish'))
    publisher_home = path('/var/lib/forecast-publish')
    sites = path('/srv/forecast-sites')
    nginx_sites = path('/etc/nginx/conf.d')
    verifier_dir = f'{private}/verify'
    listen = settings.get('listen', '443')
    if not re.fullmatch(r'(?:[0-9.]+:)?[0-9]{1,5}', listen):
        raise ValueError('Expected an nginx IPv4 listen address and TCP port')
    if not fixture:
        server.group(group=publisher, system=True)
        server.user(user=publisher, group=publisher, system=True, home=publisher_home,
                    ensure_home=False, shell='/bin/sh', password_lock=True)
    directory(publisher_home)
    directory(f'{publisher_home}/.ssh')
    directory(sites)
    directory(nginx_sites)
    directory(verifier_dir, '0750', **account_owner('root', publisher))
    put(f'{install}/verify_site.py', str(HERE / 'verify_site.py'), '0755')
    put(f'{install}/receive_site.py', str(HERE / 'receive_site.py'), '0755')
    authorized = []
    hostnames = set()
    keys = set()
    for site, config in settings['sites'].items():
        name(site)
        hostname = config['hostname']
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]*', hostname) or hostname.lower() in hostnames:
            raise ValueError('Expected distinct site hostnames')
        hostnames.add(hostname.lower())
        site_root = f'{sites}/{site}'
        verifier = f'{install}/verify-{site}'
        receiver = f'{install}/publish_ssh.py'
        verifier_config = f'{verifier_dir}/{site}.json'
        owner = account_owner(publisher, publisher)
        directory(site_root, **owner)
        files.file(path=f'{site_root}/publish.lock', mode='0600', **owner)
        backup_paths.append(site_root)
        backup_locks.append(f'{site_root}/publish.lock')
        backup_exclude.append(f'{site_root}/publish.lock')
        access = config['access']
        policy_include = None
        custom_verifier = None
        verify_settings = {'url': 'https://' + hostname,
                           'downloads': config.get('downloads', [])}
        port = listen.rsplit(':', 1)[-1]
        if int(port) != 443:
            verify_settings['url'] += ':' + port
        if config.get('ca_file'):
            verify_settings['ca_file'] = checked_path(config['ca_file'])
        if access == 'basic':
            password_file = checked_path(config['password_file'])
            verify_settings['credentials_file'] = checked_path(config['verifier_credentials'])
        elif access == 'public':
            password_file = None
        elif access == 'custom':
            password_file = None
            policy_include = checked_path(config['policy_include'])
            custom_verifier = checked_path(config['verifier'])
        else:
            raise ValueError('Select basic, custom or public access for every site')
        template(f'{nginx_sites}/fri-{site}.conf', 'site.conf.j2', listen=listen, hostname=hostname,
                 site_root=site_root, certificate=checked_path(config['certificate']),
                 certificate_key=checked_path(config['certificate_key']), password_file=password_file,
                 policy_include=policy_include)
        if custom_verifier:
            content(verifier, f'#!/bin/sh\nexec {custom_verifier} "$@"\n', '0755')
        else:
            json_file(verifier_config, verify_settings, '0640', **account_owner('root', publisher))
            content(verifier, f'#!/bin/sh\nexec {install}/verify_site.py {verifier_config} "$@"\n', '0755')
        key = config['publisher_key'].split()[:2]
        if (len(key) != 2 or key[0] != 'ssh-ed25519'
                or not re.fullmatch(r'[a-zA-Z0-9+/]+={0,2}', key[1]) or key[1] in keys):
            raise ValueError('Use a different SSH Ed25519 public key per publication site')
        keys.add(key[1])
        forced = shlex.join([f'{install}/receive_site.py', receiver, site_root, verifier])
        authorized.append(f'restrict,command="{forced}" {" ".join(key)}\n')
    if not settings['sites']:
        raise ValueError('Configure at least one forecast-web site')
    content(f'{publisher_home}/.ssh/authorized_keys', ''.join(authorized))
    content(f'{nginx_sites}/fri-default.conf',
            f'server {{ listen {listen} ssl default_server; ssl_reject_handshake on; }}\n')
    if not fixture:
        server.shell(commands=['nginx -t'], name='Validate all HTTPS virtual hosts')
        systemd.service(service='nginx.service', running=True, enabled=True, reloaded=True)

if backup.get('enabled', False) and staging:
    raise ValueError('A staging host must not enable its backup timer')
json_file(backup_config, {
    'role': role, 'paths': backup_paths, 'locks': backup_locks, 'exclude': backup_exclude,
    'staging_exclude': staging_exclude, 'repository_file': checked_path(backup['repository_file']),
    'password_file': checked_path(backup['password_file']),
    **({'restic': checked_path(backup['restic'])} if fixture else {}),
}, '0600')
template(f'{units}/fri-backup.service', 'backup.service.j2', role=role, install=install, backup_config=backup_config,
         backup_environment=checked_path(backup['environment_file']) if backup.get('environment_file') else None)
template(f'{units}/fri-backup.timer', 'backup.timer.j2', role=role,
         backup_time='*-*-* 05:30:00 UTC' if role == 'forecast-prod' else '*-*-* 05:45:00 UTC')
if not fixture:
    server.shell(commands=['systemctl daemon-reload'], name='Load backup systemd units')
    systemd.service(service='fri-backup.timer', running=backup.get('enabled', False),
                    enabled=backup.get('enabled', False))
