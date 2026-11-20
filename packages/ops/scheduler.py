"""Durable, minute-bound dispatch using the project's existing run receipts."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os

MARKER = 'scheduler-v2.json'
PAID_KINDS = {'forecast', 'forecastbench'}


def lane_for(kind):
    return 'paid' if kind in PAID_KINDS else 'data'


def now_utc():
    return datetime.now(timezone.utc)


def minute(now):
    return now.replace(second=0, microsecond=0)


@contextmanager
def file_lock(path, *, shared=False, blocking=True):
    with path.open('a') as handle:
        flags = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(handle, flags)
        except BlockingIOError:
            yield None
            return
        yield handle.fileno()


def read_json(path):
    return json.loads(path.read_text())


class Scheduler:
    def __init__(self, runtime, *, write_json, scheduled_between):
        self.runtime = runtime
        self.root = runtime.root
        self.marker = self.root / MARKER
        self.enabled = self.root / 'jobs-enabled'
        self.write = write_json
        self.scheduled_between = scheduled_between

    def active(self):
        return self.marker.is_file()

    def mode_lock(self, *, shared=True, blocking=True):
        return file_lock(self.root / 'scheduler-mode.lock', shared=shared, blocking=blocking)

    def dispatch_lock(self):
        return file_lock(self.root / 'scheduler-dispatch.lock')

    def receipt(self, key):
        return self.root / 'runs' / (key + '.json')

    def schedule_identity(self):
        stat = self.enabled.stat()
        return [stat.st_dev, stat.st_ino, stat.st_mtime_ns]

    def start_allowed(self, record, now):
        try:
            identity = self.schedule_identity()
        except FileNotFoundError:
            return False
        return (self.active() and identity == record['schedule_marker']
                and minute(now) == datetime.fromisoformat(record['scheduled_at']))

    def tick(self):
        """Record occurrences only. Pace and availability of workers are independent."""
        with self.dispatch_lock():
            if not self.runtime.current.exists() or not self.enabled.exists():
                return
            release = self.runtime.current.resolve(strict=True)
            now = now_utc()
            try:
                marker = self.schedule_identity()
            except FileNotFoundError:
                return
            active_since = max(self.runtime.schedule_since(now, enabled_at=marker[2] / 1_000_000_000),
                               datetime.fromisoformat(read_json(self.marker)['eligible_from']) - timedelta(microseconds=1))
            code = release / 'code'
            for key, job, at in self.scheduled_between(self.runtime.jobs(code), active_since,
                                                       minute(now) + timedelta(seconds=1)):
                receipt = self.receipt(key)
                if not receipt.exists():
                    self.write(receipt, {'key': key, 'job': job['id'], 'kind': job['kind'],
                                         'job_spec': job, 'lane': lane_for(job['kind']),
                                         'release': release.name, 'status': 'queued',
                                         'scheduled_at': at.isoformat(), 'started': at.isoformat(),
                                         'schedule_marker': marker})

    def entries(self, *, statuses, lane=None):
        for path in (self.root / 'runs').glob('*.json'):
            record = read_json(path)
            if record.get('status') in statuses and (lane is None or record.get('lane') == lane):
                yield path, record

    def release_pins(self):
        # Retention already holds the operation lock. A concurrent tick can
        # only add work for the selected release, which retention protects.
        return {self.root / 'releases' / record['release']
                for _, record in self.entries(statuses={'queued'})}

    def reconcile_running(self, lane=None, *, now=None, jobs=None):
        # Projects serialize execution with their operation lock. The lease
        # stays held by active job subprocesses if their worker dies.
        now = now or now_utc()
        for path, record in self.entries(statuses={'running'}, lane=lane):
            job = record.get('job_spec')
            if job is None:
                job = self.runtime.legacy_job(record, jobs)
            self.runtime.report_interrupted(path, record, job, now)

    def worker(self, lane):
        if not self.active():
            return True
        # Check for dead attempts under the local operation lock without
        # waiting for another project's shared panel lock.
        with self.runtime.lock(blocking=False) as acquired:
            if acquired and self.active():
                self.reconcile_running(lane)
            blocked = not acquired and any(self.entries(statuses={'running'}, lane=lane))
        queued = sorted(self.entries(statuses={'queued'}, lane=lane),
                        key=lambda entry: entry[1]['scheduled_at'], reverse=True)
        for path, record in queued:
            job = record['job_spec']
            run_allowed = self.start_allowed(record, now_utc())
            with self.runtime.lock(shared=run_allowed and lane == 'paid', blocking=False) as acquired:
                if not acquired or not self.active():
                    return not self.active()
                if read_json(path)['status'] != 'queued':
                    continue
                self.reconcile_running()
                selected = self.runtime.current.resolve(strict=True)
                if run_allowed and self.start_allowed(record, now_utc()) and selected.name == record['release']:
                    self.runtime.execute_job(record['key'], job, queued=record)
                else:
                    reason = 'Selected release changed' if selected.name != record['release'] else None
                    if reason is None and not self.enabled.exists():
                        reason = 'Scheduling paused before work began'
                    self.close_queued(path, record, reason)
        return not blocked

    def close_queued(self, path, record, reason=None):
        at = datetime.fromisoformat(record['scheduled_at'])
        release = self.root / 'releases' / record['release']
        self.runtime.report_missed(path, record, record['job_spec'], at, now_utc(), release, reason)

    def cutover(self, *, legacy=False):
        """The caller must stop intake and drain legacy services before switching."""
        with self.mode_lock(shared=False, blocking=False) as mode:
            if mode is None:
                raise RuntimeError('Scheduler or project operation is active; stop intake and drain before cutover')
            with self.runtime.operation_locks(shared=True, blocking=False) as acquired:
                if not acquired:
                    raise RuntimeError('Project or shared forecast work is active; drain before cutover')
                if not self.runtime.current.exists():
                    raise RuntimeError('Select a deployed release before scheduler cutover')
                if self.active() == (not legacy):
                    return
                if legacy:
                    self.reconcile_running()
                    for path, record in self.entries(statuses={'queued'}):
                        self.close_queued(path, record, 'Scheduler returned to legacy mode')
                    self.marker.unlink()
                    fd = os.open(self.root, os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                else:
                    self.require_release(self.runtime.current, switching=True)
                    now = now_utc()
                    first_slot = minute(now) + timedelta(minutes=1)
                    if self.enabled.exists():
                        self.runtime.report_unrun_locked(self.runtime.jobs(self.runtime.current / 'code'), now, through=first_slot)
                    else:
                        self.reconcile_running()
                    for path, record in self.entries(statuses={'queued'}):
                        self.close_queued(path, record, 'Queue restored from an earlier scheduler')
                    self.write(self.marker, {'enabled_at': now.isoformat(), 'eligible_from': first_slot.isoformat()})

    def require_release(self, release, *, switching=False):
        if (switching or self.active()) and not (release / 'code/packages/ops/scheduler.py').is_file():
            raise RuntimeError('Release does not support queued scheduling; drain and return to legacy mode first')
