#!/usr/bin/env python3
"""Exercise the installed dispatcher and systemd scheduler on a disposable staging guest."""
import argparse
from datetime import datetime, timedelta, timezone
import grp
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import subprocess
from tempfile import TemporaryDirectory
import time


LAUNCHER = '/usr/local/bin/dashboardctl'
BOOTSTRAP = Path('/opt/fri-ops/bootstrap')
FENCE = Path('/etc/fri-ops/staging')
SYSTEMD = Path('/run/systemd/system')
TICK = 'fri-dashboard-tick.service'
TIMER = 'fri-dashboard-tick.timer'
WORKER = 'fri-dashboard-worker@.service'
ISOLATION = [
    'PrivateNetwork=yes', 'RestrictAddressFamilies=AF_UNIX', 'NoNewPrivileges=yes',
    'ProtectHome=yes', 'InaccessiblePaths=-/etc/fri-dashboards -/srv/forecasts -/var/lib/forecast',
]

# Replace provider/upstream work and the delivery transport at their existing
# subprocess boundaries. The installed release code still owns every receipt.
PAYLOAD = """\
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
from zipfile import ZipFile

root = Path(__file__).parent
operation, *args = sys.argv[1:]
with (root / 'events').open('a') as events:
    print(operation, file=events)

if operation == 'forecast':
    time.sleep(600)
elif operation in ('build', 'seed'):
    site = Path(args[0])
    baseline = {'source': 'fred', 'as_of': '2030-01-02'}
    if operation == 'seed':
        baseline = {'source': 'survey', 'as_of': None}
    data = json.dumps({'baseline': baseline})
    (site / 'index.html').write_text(
        '<html><head><!--EEAI_DATA--><script>window.__EEAI__ = ' + data +
        ';</script><!--/EEAI_DATA--></head></html>')
    with ZipFile(site / 'eeai-data.zip', 'w') as archive:
        archive.writestr('baseline.json', json.dumps(baseline))
elif operation == 'publish':
    shutil.copytree(args[0], root / 'delivered', dirs_exist_ok=True)
elif operation == 'verify':
    manifest = json.load(sys.stdin)
    delivered = root / 'delivered'
    metadata = json.loads((delivered / 'release.json').read_text())
    assert metadata['frontend_commit'] == manifest['frontend_commit']
    assert metadata['data_snapshot']['sha256'] == manifest['data_snapshot_sha256']
    for name, expected in manifest['files'].items():
        blob = (delivered / name).read_bytes()
        assert len(blob) == expected['size']
        assert hashlib.sha256(blob).hexdigest() == expected['sha256']
    print(json.dumps(dict.fromkeys(('release.json', *manifest['files']), 'verified')))
elif operation not in ('refresh', 'alert'):
    sys.exit('Unknown fixture operation')
"""


def command(*argv, **options):
    return subprocess.run(argv, check=True, text=True, timeout=options.pop('timeout', 60), **options)


def systemctl(*argv):
    return command('systemctl', *argv, capture_output=True).stdout.strip()


def property_of(unit, prop):
    return systemctl('show', '--property=' + prop, '--value', unit)


def write(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)


def write_json(path, obj):
    write(path, json.dumps(obj) + '\n')


def verify_staging():
    if os.geteuid() or not FENCE.is_file():
        raise RuntimeError('Run as root on a disposable forecast guest with its staging fence intact')
    names = (TIMER, TICK, 'fri-dashboard-worker@paid.service', 'fri-dashboard-worker@data.service')
    for name in names:
        if property_of(name, 'ActiveState') != 'inactive':
            raise RuntimeError(f'Staging unit must be inactive: {name}')
    units = {}
    for name in (TICK, TIMER, WORKER):
        # Instance properties find the loaded fragment of the worker template.
        unit = name.replace('@.', '@paid.')
        units[name] = Path(property_of(unit, 'FragmentPath')).read_text()
    for name, argv in ((TICK, 'tick-all --queued'), (WORKER, 'worker-all --lane %i')):
        lines = units[name].splitlines()
        expected = {f'ExecStart={LAUNCHER} {argv}', 'Environment=DASHBOARD_CONFIG_DIR=/etc/fri-dashboards'}
        executable_settings = {line for line in lines if line.startswith(('Exec', 'Environment', 'LoadCredential'))}
        if f'ConditionPathExists=!{FENCE}' not in lines or executable_settings != expected:
            raise RuntimeError(f'Unexpected staging unit configuration: {name}')
    if 'OnCalendar=*-*-* *:*:00 UTC' not in units[TIMER].splitlines():
        raise RuntimeError('Expected the installed UTC minute timer')
    user, group = [property_of(TICK, name) for name in ('User', 'Group')]
    uid, gid = pwd.getpwnam(user).pw_uid, grp.getgrnam(group).gr_gid
    if not uid or not gid or [property_of('fri-dashboard-worker@paid.service', name)
                             for name in ('User', 'Group')] != [user, group]:
        raise RuntimeError('Expected the same unprivileged account for the tick and workers')
    return units, uid, gid


def standalone(root, uid, gid, *argv):
    properties = [arg for value in ISOLATION for arg in ('--property', value)]
    return command(
        'systemd-run', '--quiet', '--wait', '--pipe', '--collect', f'--uid={uid}', f'--gid={gid}',
        f'--setenv=DASHBOARD_CONFIG_DIR={root / "config"}', *properties, LAUNCHER, *argv,
        capture_output=True,
    ).stdout


def release(root, name):
    runtime = root / name
    chosen = runtime / 'releases/drill'
    code = chosen / 'code'
    shutil.copytree(BOOTSTRAP, code, ignore=shutil.ignore_patterns('jobs.json', '__pycache__'))
    interpreter = code / '.venv/bin/python'
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to('/usr/bin/python3')
    config = dict(root=str(runtime), dashboard=name, socket=str(runtime / 'unused.sock'),
                  path='/usr/bin:/bin', min_free_bytes=0)
    python = ['/usr/bin/python3', str(root / 'payload.py')]
    if name == 'airo':
        forecast = code / 'dashboards/airo/code/cron_run.sh'
        write(forecast, f'exec {python[0]} {python[1]} forecast\n')
        write(code / 'dashboards/airo/redlines/__init__.py', '')
        write(code / 'dashboards/airo/redlines/experiments.py', "print('airo aside fixture')\n")
    else:
        write(code / 'dashboards/eeai/eeai.py', f"""
import os
import sys
op = sys.argv[1]
args = [sys.argv[sys.argv.index('--site') + 1]] if op == 'build' else []
os.execv({python[0]!r}, {python!r} + [op, *args])
""")
        seed = runtime / 'sites/seed'
        seed.mkdir(parents=True)
        command(*python, 'seed', seed)
        write_json(seed / 'release.json', {'dashboard': name, 'release': 'drill', 'commit': 'a' * 40})
        (chosen / 'site').symlink_to(seed)
        config.update(panel_runtime_config=str(root / 'config/airo.json'),
                      public_url='https://scheduler.invalid/',
                      public_publisher=[*python, 'publish'], public_verifier=[*python, 'verify'])
    write(code / 'fri_forecaster/__init__.py', '')
    write(code / 'fri_forecaster/alert.py', f"import os\nos.execv({python[0]!r}, {python!r} + ['alert'])\n")
    write_json(chosen / 'release.json', {'dashboard': name, 'release': 'drill', 'commit': 'a' * 40})
    (runtime / 'current').symlink_to(chosen)
    (runtime / 'jobs-enabled').touch()
    write_json(root / 'config' / f'{name}.json', config)
    return code / 'packages/ops/jobs.json'


def wait_for(check, description, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.2)
    raise RuntimeError(f'Timed out waiting for {description}')


def wait_until(when):
    wait_for(lambda: datetime.now(timezone.utc) >= when, str(when), 95)


def drill(root, units, uid, gid):
    prefix = root.name
    names = {TICK: prefix + '.service', TIMER: prefix + '.timer', WORKER: prefix + '-worker@.service'}
    tick, timer = names[TICK], names[TIMER]
    paid, data = [names[WORKER].replace('@.', f'@{lane}.') for lane in ('paid', 'data')]
    fence = root / 'staging'
    fence.touch()
    write(root / 'payload.py', PAYLOAD)
    manifests = [release(root, name) for name in ('airo', 'eeai')]

    for path in [root, *root.rglob('*')]:
        os.chown(path, uid, gid, follow_symlinks=False)
    files = {SYSTEMD / name: units[original] for original, name in names.items()}
    try:
        for path, contents in files.items():
            if path.suffix == '.service':
                contents = contents.replace(str(FENCE), str(fence)).replace('/etc/fri-dashboards', str(root / 'config'))
                contents = contents.replace('[Service]', '[Service]\n' + '\n'.join(ISOLATION), 1)
            path.write_text(contents)
        systemctl('daemon-reload')
        systemctl('start', tick, paid, data)
        assert all(property_of(unit, 'ConditionResult') == 'no' for unit in (tick, paid, data))
        fence.unlink()

        now = datetime.now(timezone.utc)
        first = (now + timedelta(seconds=70)).replace(second=0, microsecond=0)
        later = first + timedelta(minutes=1)
        jobs = [
            {'id': 'drill-long', 'kind': 'forecast', 'at': first.isoformat()},
            {'id': 'drill-refresh', 'dashboard': 'eeai', 'kind': 'refresh', 'at': first.isoformat()},
            {'id': 'drill-missed', 'kind': 'forecast', 'at': later.isoformat()},
        ]
        for manifest in manifests:
            write_json(manifest, jobs)
        for name in ('airo', 'eeai'):
            standalone(root, uid, gid, name, 'scheduler-cutover')

        def receipt(project, job, at):
            key = f'{job}-{at:%Y%m%dT%H%M}'
            return root / project / 'runs' / (key + '.json')

        def status(path, expected):
            return path.is_file() and json.loads(path.read_text())['status'] == expected

        long = receipt('airo', 'drill-long', first)
        refresh = receipt('eeai', 'drill-refresh', first)
        missed = receipt('airo', 'drill-missed', later)
        systemctl('start', paid, data, timer)
        wait_for(lambda: 'forecast' in (root / 'events').read_text().splitlines(),
                 'timer to dispatch the synthetic forecast', 90)
        wait_for(lambda: status(refresh, 'succeeded'), 'EEAI verified refresh', 60)
        assert status(long, 'running')
        assert json.loads(refresh.read_text())['public_verification']['result'] == 'verified'
        assert property_of(timer, 'LastTriggerUSec') not in ('', 'n/a')

        saved = {path: path.read_bytes() for path in (long, refresh)}
        started = time.monotonic()
        command('systemctl', 'start', tick, capture_output=True, timeout=60)
        repeat_seconds = round(time.monotonic() - started, 3)
        repeated = standalone(root, uid, gid, 'eeai', 'refresh', '--verify', '--run-id', refresh.stem)
        assert json.loads(repeated)['key'] == refresh.stem
        assert all(path.read_bytes() == data for path, data in saved.items())

        systemctl('stop', paid)
        assert status(long, 'running')
        wait_for(lambda: status(missed, 'queued'), 'timer to queue a slot with the paid worker stopped', 90)
        systemctl('stop', timer)
        wait_until(later + timedelta(minutes=1))
        systemctl('start', paid)
        wait_for(lambda: status(long, 'interrupted') and status(missed, 'missed'),
                 'worker restart to reconcile interruption and miss', 60)
        try:
            standalone(root, uid, gid, 'airo', 'run-dashboard', '--run-id', long.stem)
        except subprocess.CalledProcessError as exc:
            assert json.loads(exc.stdout)['status'] == 'interrupted'
        else:
            raise RuntimeError('The interrupted paid run ID was accepted again')
        counts = (root / 'events').read_text().splitlines()
        assert all(counts.count(op) == 1 for op in ('forecast', 'refresh', 'build', 'publish', 'verify'))
        assert counts.count('alert') == 2
        print(json.dumps({
            'timer_slot_utc': first.isoformat(), 'fence_verified': True, 'eeai_completed_during_forecast': True,
            'repeat_tick_seconds': repeat_seconds, 'same_id_unchanged': True,
            'worker_restart': {'forecast': 'interrupted', 'unclaimed_slot': 'missed', 'forecast_invocations': 1},
        }, indent=2))
    except Exception:
        print(command('journalctl', '--no-pager', '-n', '80', '-u', tick, '-u', paid, '-u', data,
                      capture_output=True).stdout)
        raise
    finally:
        try:
            systemctl('stop', timer, tick, paid, data)
        finally:
            for path in files:
                path.unlink(missing_ok=True)
            systemctl('daemon-reload')
        assert FENCE.is_file()


def terminate(signum, _frame):
    raise SystemExit(128 + signum)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-staging', action='store_true', required=True,
                        help='Confirm this is the isolated disposable forecast guest')
    parser.parse_args()
    signal.signal(signal.SIGTERM, terminate)
    units, uid, gid = verify_staging()
    with TemporaryDirectory(prefix='fri-scheduler-drill-', dir='/var/tmp') as temp:
        drill(Path(temp), units, uid, gid)


if __name__ == '__main__':
    main()
