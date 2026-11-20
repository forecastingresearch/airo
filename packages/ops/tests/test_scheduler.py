"""Queued scheduling exercises local files and dummy jobs only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
import time

import pytest

from test_production import p, release, runtime  # noqa: F401


SLOT = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)
FORECAST = {'id': 'airo', 'kind': 'forecast', 'daily_utc': '12:00'}
NEWS = {'id': 'news-poll', 'kind': 'news-poll', 'daily_utc': '12:01'}
REFRESH = {'id': 'eeai-economic-data', 'dashboard': 'eeai', 'kind': 'refresh', 'daily_utc': '12:01'}


def selected(runtime, jobs, name='20290101T000000-aaaaaaaaaaaa', *, capable=True):
    active = release(runtime, name)
    metadata = {'release': name, 'dashboard': runtime.name, 'commit': 'a' * 40}
    p.write_json(active / 'release.json', metadata)
    p.write_json(active / 'code' / p.JOBS, jobs)
    if capable:
        (active / 'code/packages/ops/scheduler.py').touch()
    p.link(active, runtime.current)
    stamp = (SLOT - timedelta(hours=1)).timestamp()
    os.utime(runtime.current, (stamp, stamp), follow_symlinks=False)
    return active


def enable(runtime):
    runtime.scheduler.enabled.touch()
    stamp = (SLOT - timedelta(hours=1)).timestamp()
    os.utime(runtime.scheduler.enabled, (stamp, stamp))


@pytest.fixture
def clock(monkeypatch):
    state = {'now': SLOT - timedelta(minutes=1)}
    monkeypatch.setattr(p.scheduler, 'now_utc', lambda: state['now'])
    return state


def test_long_paid_attempt_and_repeat_ticks_do_not_block_eeai_refresh(tmp_path, runtime, clock, monkeypatch):
    primary_config = tmp_path / 'airo.json'
    p.write_json(primary_config, runtime.config)
    eeai = p.Runtime({'dashboard': 'eeai', 'root': str(tmp_path / 'eeai'), 'path': '/usr/bin',
                      'socket': str(tmp_path / 'eeai.sock'), 'panel_runtime_config': str(primary_config)})
    monkeypatch.setattr(p, 'output', lambda *args, **kw: 'airo aside forecast')
    paid_started, finish_paid, refresh_finished = Event(), Event(), Event()

    def run(argv, **kwargs):
        assert str(argv[1]).endswith('/code/cron_run.sh')
        paid_started.set()
        assert finish_paid.wait(10)

    def refresh(*args):
        refresh_finished.set()

    monkeypatch.setattr(p, 'run', run)
    monkeypatch.setattr(eeai, 'economic_refresh', refresh)

    for site, jobs in ((runtime, [FORECAST, NEWS]), (eeai, [REFRESH])):
        selected(site, jobs)
        enable(site)
        site.scheduler.cutover()

    clock['now'] = SLOT
    with ThreadPoolExecutor(max_workers=4) as pool:
        pool.submit(runtime.tick).result(timeout=2)
        worker = pool.submit(runtime.scheduler.worker, 'paid')
        try:
            assert paid_started.wait(2)
            assert runtime.scheduler.receipt('airo-20300102').is_file()
            clock['now'] += timedelta(minutes=1)
            # Both minute ticks still record their occurrences during the paid attempt.
            ticks = [pool.submit(site.tick) for site in (runtime, runtime, eeai, eeai)]
            for tick in ticks:
                tick.result(timeout=2)
            pool.submit(runtime.scheduler.worker, 'paid').result(timeout=2)
            pool.submit(eeai.scheduler.worker, 'data').result(timeout=2)
            assert refresh_finished.is_set()
            assert p.scheduler.read_json(eeai.scheduler.receipt('eeai-economic-data-20300102'))['status'] == 'succeeded'
            assert p.scheduler.read_json(runtime.scheduler.receipt('airo-20300102'))['status'] == 'running'
            assert p.scheduler.read_json(runtime.scheduler.receipt('news-poll-20300102'))['status'] == 'queued'
        finally:
            finish_paid.set()
        worker.result(timeout=2)
    runtime.scheduler.worker('paid')
    assert p.scheduler.read_json(runtime.scheduler.receipt('airo-20300102'))['status'] == 'succeeded'
    assert len(list((runtime.root / 'runs').glob('*.json'))) == 2


@pytest.mark.parametrize('mode', ['expired', 'disabled', 'reenabled', 'preflight_expired'])
def test_paid_occurrence_is_never_caught_up(runtime, clock, monkeypatch, mode):
    selected(runtime, [FORECAST])
    enable(runtime)
    runtime.scheduler.cutover()
    clock['now'] = SLOT
    runtime.tick()

    def cadence(*args, **kwargs):
        if mode == 'preflight_expired':
            clock['now'] += timedelta(minutes=1)
        return 'airo aside forecast'

    monkeypatch.setattr(p, 'output', cadence)
    alerts = []
    monkeypatch.setattr(runtime, 'alert', lambda *args, **kwargs: alerts.append(args))
    monkeypatch.setattr(p, 'run', lambda *args, **kwargs: pytest.fail('A stale forecast started'))
    if mode == 'expired':
        clock['now'] += timedelta(minutes=1)
    elif mode in ('disabled', 'reenabled'):
        runtime.scheduler.enabled.unlink()
        if mode == 'reenabled':
            runtime.scheduler.enabled.touch()
            stamp = (SLOT + timedelta(seconds=1)).timestamp()
            os.utime(runtime.scheduler.enabled, (stamp, stamp))
    runtime.scheduler.worker('paid')
    runtime.scheduler.worker('paid')
    assert p.scheduler.read_json(runtime.scheduler.receipt('airo-20300102'))['status'] == 'missed'
    assert not runtime.execute_job('airo-20300102', FORECAST)
    assert len(alerts) == 1


def test_cutover_reconciles_old_receipts_and_refuses_legacy_rollback(runtime, clock, monkeypatch):
    legacy = selected(runtime, [FORECAST], name='20291231T000000-bbbbbbbbbbbb', capable=False)
    enable(runtime)
    with pytest.raises(RuntimeError, match='does not support'):
        runtime.scheduler.cutover()
    current = selected(runtime, [FORECAST])
    p.link(legacy, runtime.root / 'previous')
    alerts = []
    monkeypatch.setattr(runtime, 'alert', lambda *args, **kwargs: alerts.append(args))
    monkeypatch.setattr(runtime, 'verify_site', lambda _: None)
    monkeypatch.setattr(runtime, 'probe', lambda _: None)
    monkeypatch.setattr(p, 'output', lambda *args, **kwargs: 'airo aside forecast')
    dead = runtime.scheduler.receipt('old-forecast')
    p.write_json(dead, {'key': 'old-forecast', 'job': 'airo', 'status': 'running'})
    with runtime.lock():
        with pytest.raises(RuntimeError, match='drain before cutover'):
            runtime.scheduler.cutover()
    runtime.scheduler.cutover()
    assert p.scheduler.read_json(dead)['status'] == 'interrupted'
    with pytest.raises(RuntimeError, match='does not support'):
        runtime.rollback()
    assert runtime.current.resolve() == current

    clock['now'] = SLOT
    runtime.tick()
    scheduled = runtime.scheduler.receipt('airo-20300102')
    assert p.scheduler.read_json(scheduled)['status'] == 'queued'
    runtime.scheduler.cutover(legacy=True)
    assert p.scheduler.read_json(scheduled)['status'] == 'missed'
    assert not runtime.scheduler.active()
    runtime.rollback()
    assert runtime.current.resolve() == legacy
    assert not runtime.execute_job('airo-20300102', FORECAST)
    assert len(alerts) == 2


def test_queue_pins_the_scheduled_data_release_until_reconciled(runtime, clock, monkeypatch):
    original = selected(runtime, [NEWS], name='20290101T000000-aaaaaaaaaaaa')
    keep = selected(runtime, [NEWS], name='20290102T000000-bbbbbbbbbbbb')
    p.link(original, runtime.current)
    enable(runtime)
    runtime.scheduler.cutover()
    clock['now'] = SLOT + timedelta(minutes=1)
    runtime.tick()

    latest = selected(runtime, [NEWS], name='20290103T000000-cccccccccccc')
    p.link(keep, runtime.root / 'previous')
    runtime.config['keep_releases'] = 2
    assert not any(path.endswith(original.name) for path in runtime.prune_locked())
    assert original.exists()
    assert runtime.current.resolve() == latest

    monkeypatch.setattr(runtime, 'alert', lambda *args, **kwargs: None)
    monkeypatch.setattr(p, 'run', lambda *args, **kwargs: pytest.fail('Stale release executed'))
    runtime.scheduler.worker('data')
    record = p.scheduler.read_json(runtime.scheduler.receipt('news-poll-20300102'))
    assert record['status'] == 'missed' and record['release'] == original.name
    assert not p.scheduler.read_json(runtime.scheduler.receipt('news-poll-20300102')).get('error')
    assert runtime.prune_locked() == ['releases/' + original.name]


def test_job_child_holds_operation_lease_after_worker_dies(runtime, clock, monkeypatch, tmp_path):
    selected(runtime, [FORECAST])
    enable(runtime)
    runtime.scheduler.cutover()
    clock['now'] = SLOT
    runtime.tick()
    receipt = runtime.scheduler.receipt('airo-20300102')
    record = p.scheduler.read_json(receipt)
    p.write_json(receipt, {**record, 'status': 'running'})

    ready = tmp_path / 'child-ready'
    finish = tmp_path / 'child-finish'
    child = ('from pathlib import Path; import sys, time\n'
             'Path(sys.argv[1]).touch()\n'
             'deadline = time.monotonic() + 10\n'
             'while not Path(sys.argv[2]).exists() and time.monotonic() < deadline: time.sleep(.01)\n')
    supervisor = ('import sys; import production as p\n'
                  'runtime = p.Runtime(' + repr(runtime.config) + ')\n'
                  'with runtime.lock(shared=True): p.run([sys.executable, "-c", *sys.argv[1:]])\n')
    env = {**os.environ, 'PYTHONPATH': str(Path(p.__file__).parent)}
    worker = subprocess.Popen([sys.executable, '-c', supervisor, child, str(ready), str(finish)], env=env)
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists()
        worker.kill()
        worker.wait(timeout=3)
        monkeypatch.setattr(runtime, 'alert', lambda *args, **kwargs: None)
        runtime.scheduler.worker('paid')
        assert p.scheduler.read_json(receipt)['status'] == 'running'
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=3)
        finish.touch()
    deadline = time.monotonic() + 3
    while p.scheduler.read_json(receipt)['status'] == 'running' and time.monotonic() < deadline:
        runtime.scheduler.worker('paid')
        time.sleep(.01)
    assert p.scheduler.read_json(receipt)['status'] == 'interrupted'
    assert not runtime.execute_job('airo-20300102', FORECAST)


def test_queued_tick_refuses_legacy_mode(runtime, clock, monkeypatch):
    selected(runtime, [FORECAST])
    enable(runtime)
    monkeypatch.setattr(runtime, 'report_unrun', lambda *args, **kwargs: pytest.fail('Entered legacy tick'))
    with pytest.raises(RuntimeError, match='requires scheduler-cutover'):
        runtime.tick(queued_only=True)


def test_worker_supervisor_is_idle_until_receipt_changes_while_another_project_runs(tmp_path, monkeypatch):
    from runpy import run_path

    launcher = Path(p.__file__).with_name('dashboardctl')
    configs = tmp_path / 'configs'
    configs.mkdir()
    roots = {}
    for name in ('airo', 'eeai'):
        root = roots[name] = tmp_path / name
        (root / 'runs').mkdir(parents=True)
        p.write_json(root / 'scheduler-v2.json', {})
        p.write_json(configs / (name + '.json'), {'root': str(root)})

    class Finished:
        def poll(self):
            return 0

    class Working:
        def poll(self):
            return None

    starts = []

    def launch(command):
        name = command[2]
        starts.append(name)
        assert command[3:] == ['worker', '--lane', 'data']
        return Working() if name == 'airo' else Finished()

    class StopSupervisor(Exception):
        pass

    iteration = 0

    def poll(seconds):
        nonlocal iteration
        iteration += 1
        assert seconds == 2
        if iteration == 1:
            assert starts == ['airo', 'eeai']
        elif iteration == 2:
            assert starts == ['airo', 'eeai']
            p.write_json(roots['eeai'] / 'runs' / 'queued.json', {'status': 'queued'})
        else:
            assert starts == ['airo', 'eeai', 'eeai']
            raise StopSupervisor

    monkeypatch.setenv('DASHBOARD_CONFIG_DIR', str(configs))
    monkeypatch.setattr(sys, 'argv', [str(launcher), 'worker-all', '--lane', 'data'])
    monkeypatch.setattr(subprocess, 'Popen', launch)
    monkeypatch.setattr(time, 'sleep', poll)
    with pytest.raises(StopSupervisor):
        run_path(str(launcher), run_name='__main__')
