"""Read-only fleet status for selected releases, scheduler receipts and delivery."""
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import re
import socket

import eeai_operator


SUBMISSION_KIND = 'forecastbench'
RETRY_STAGES = {'transfer', 'verify_public', 'select_publication'}
FAILURES = {'failed', 'missed', 'interrupted'}
JOB_STATES = FAILURES | {'queued', 'running', 'succeeded', 'skipped'}


def utc(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is not None:
            return stamp.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass
    return None


def read_object(path):
    with path.open() as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object')
    return value


def read_receipts(root):
    receipts, complete = [], True
    try:
        for path in (root / 'runs').iterdir():
            if path.suffix != '.json':
                continue
            try:
                record = read_object(path)
                if (record.get('key') != path.stem or not utc(record.get('started'))
                        or not isinstance(record.get('job'), str)):
                    raise ValueError('Invalid receipt')
                receipts.append(record)
            except (OSError, ValueError):
                complete = False
    except OSError:
        complete = False
    return sorted(receipts, key=lambda record: utc(record['started']), reverse=True), complete


def installation_matches(runtime, environment):
    if environment is not None and runtime.config_path:
        scheduled = Path(environment.get('DASHBOARD_CONFIG_DIR', '/etc/fri-dashboards')) / (runtime.name + '.json')
        if scheduled.is_absolute():
            try:
                return scheduled.samefile(runtime.config_path)
            except FileNotFoundError:
                return False
            except OSError:
                pass
    return None


def worker_status(runtime, lane):
    name = f'fri-dashboard-worker@{lane}.service'
    unit = eeai_operator.unit_properties(name, (*eeai_operator.UNIT_PROPERTIES, 'ActiveState'))
    activity, environment = 'unknown', None
    if eeai_operator.worker_command(unit, lane):
        activity = unit.get('ActiveState', 'unknown')
        if activity == 'active':
            environment, _ = eeai_operator.scheduler_environment(unit)
    return {'unit': name, 'activity': activity, 'installation_matches': installation_matches(runtime, environment)}


def timer_status(runtime, enabled, jobs):
    timer, environment, _ = eeai_operator.scheduler(runtime.config)
    try:
        marker = read_object(runtime.root / 'scheduler-v2.json')
        model = 'durable' if utc(marker.get('enabled_at')) else 'unknown'
    except FileNotFoundError:
        model = 'legacy'
    except (OSError, ValueError):
        model = 'unknown'
    modes = {'durable': ['tick-all', '--queued'], 'legacy': ['tick-all']}
    mode_matches = bool(timer['command']) and timer['command'][1:] == modes.get(model)
    matches = installation_matches(runtime, environment)
    lanes = sorted({'paid' if j['kind'] in ('forecast', SUBMISSION_KIND) else 'data' for j in jobs})
    workers = {lane: worker_status(runtime, lane) for lane in lanes} if model == 'durable' else {}
    if enabled is False:
        state = 'disabled'
    elif (enabled is True and timer['timer_activity'] == 'active' and timer['timer_dispatches_service'] is True
          and timer['minute_calendar'] is True and mode_matches and matches is True
          and all(worker['activity'] == 'active' and worker['installation_matches'] is True for worker in workers.values())):
        state = 'scheduled'
    elif (timer['timer_activity'] in ('inactive', 'failed')
          or any(w['activity'] in ('inactive', 'failed') for w in workers.values())):
        state = 'inactive'
    else:
        state = 'unknown'
    # Unit paths and process arguments are installation details.
    return {'state': state, 'model': model, 'timer_activity': timer['timer_activity'],
            'timer_enablement': timer['timer_enablement'], 'minute_calendar': timer['minute_calendar'],
            'timer_dispatches_service': timer['timer_dispatches_service'], 'mode_matches': mode_matches,
            'installation_matches': matches, 'workers': workers}


def round_receipts(job, receipts):
    return [r for r in receipts
            if (r.get('job') == job['id'] or
                (r.get('kind') == 'forecastbench-upload' and r.get('due') == job.get('due')))]


def receipt_summary(record):
    if not record:
        return None
    fields = ('key', 'status', 'stage', 'failure_detail', 'started', 'scheduled_at', 'finished',
              'due', 'lane', 'reason', 'retry_of', 'verified_at')
    return {field: record[field] for field in fields if field in record}


def recovery(runtime, job, record, selected, release, now, public=None):
    if not record or record.get('status') not in FAILURES:
        return None
    key = record.get('key', '')
    log = f' Inspect logs/{key}.log.' if re.fullmatch(r'[a-zA-Z0-9_-]+', key) and record['status'] != 'missed' else ''
    command = ['dashboardctl', runtime.name]
    stage = record.get('stage')
    kind = job['kind']

    def action(message, argv=None):
        return {'message': message + log, 'argv': [*command, *argv] if argv else None}

    if kind == SUBMISSION_KIND:
        due = job.get('due')
        if not due or (now.date().isoformat() > due):
            return action('The UTC submission window has closed. Review the round and escalate any unconfirmed submission.')
        if record['status'] == 'missed':
            return action('Check scheduler routing and the paid worker. Confirm the remaining UTC submission window before authorizing a paid run.')
        if stage == 'forecastbench_upload':
            return action('Fix uploader access or connectivity, then retry the saved sets within the UTC submission window.',
                          ['retry-forecastbench-upload', '--due', due])
        if stage == 'forecastbench_validate':
            return action('Inspect the saved sets, member coverage and build provenance. Rebuild valid sets from saved member results, then use the upload-only command within the UTC window.')
        if stage in ('preflight', 'preflight FAILED'):
            return action('Fix the runtime preflight and check the remaining UTC submission window before authorizing a paid run.')
        if stage == 'forecastbench_build':
            return action('Inspect question set availability, member failures and build diagnostics before deciding whether a paid run is needed.')
        return action('Determine whether the sets were built, validated or uploaded from the private round log and upload receipt.')

    if record['status'] == 'missed':
        return action('Check the timer, configuration and worker receipts for the scheduled UTC minute.')
    if kind == 'refresh':
        if stage in RETRY_STAGES:
            previous = record.get('previous_publication')
            candidate = runtime.root / 'sites' / ('job-' + key)
            if (record['status'] == 'failed' and selected and previous == selected.name
                    and record.get('release') == release.get('release')
                    and record.get('previous_snapshot') == release.get('data_snapshot', {}).get('sha256')
                    and record.get('candidate_snapshot') and (candidate / 'index.html').is_file()):
                hint = 'Fix public transfer or verification, then retry the saved candidate with a new receipt ID.'
                if record.get('recovery') != 'restored':
                    hint = 'Confirm the selected site is served, then fix public transfer or verification and retry the saved candidate.'
                return action(hint, ['refresh', '--verify', '--retry', key])
            return action('Verify the current public delivery and inspect the saved candidate and prior selection before recovery.')
        guidance = {
            'upstream': 'Check the FRED route and scheduler environment using EEAI doctor.',
            'build': 'Inspect saved forecast results and the EEAI build failure before starting a new refresh.',
            'validate': 'Inspect the baseline and ZIP validation failure before starting a new refresh.',
            'publish_preflight': 'Restore the private public publisher and HTTPS verifier configuration.',
            'load_retry': 'Check the saved candidate and its pinned release, previous publication and snapshot.',
        }
        return action(guidance.get(stage, 'Inspect the EEAI refresh receipt and last verified publication.'),
                      ['doctor'] if stage == 'upstream' else None)
    if kind == 'forecast' and stage == 'publish FAILED':
        if record.get('failure_detail') == 'transfer' and selected and selected.name == 'job-' + key:
            if public and public['result'] == 'verified':
                if public['scope'] == 'content':
                    return None
                return {'message': 'Public release metadata matches. Verify the served files to confirm recovery.',
                        'argv': ['dashboardctl', 'status', '--verify-content']}
            return action('Fix public delivery, then resend the selected snapshot and check fleet status.', ['republish'])
        if record.get('failure_detail') == 'retain_results':
            return action('Inspect the completed attempt and results directory before rebuilding the publication.')
        return action('Resolve the publication error, then validate and publish the saved results.',
                      ['deploy', '--publish-data'])
    if stage == 'preflight FAILED':
        return action('Resolve the runtime preflight failure and check storage and job configuration.')
    if kind == 'forecast':
        return action('Inspect panel and provider failures and any saved attempt. A new paid forecast needs an explicit run ID and authorization.')
    return action('Inspect the job receipt and private log before recovery.')


def job_status(runtime, job, receipts, complete, since, now, scheduled_between, site, release, public):
    at = utc(job.get('at'))
    if at:
        last, upcoming = (at, None) if at <= now else (None, at)
    else:
        slots = [slot for _, _, slot in scheduled_between([job], now - timedelta(days=8), now + timedelta(days=8))]
        last = max((slot for slot in slots if slot <= now), default=None)
        upcoming = min((slot for slot in slots if slot > now), default=None)
    round_runs = round_receipts(job, receipts) if job['kind'] == SUBMISSION_KIND else []
    record = next(iter(round_runs), None) if job['kind'] == SUBMISSION_KIND else next(
        (r for r in receipts if r.get('job') == job['id']), None)
    confirmation = next((r for r in round_runs if r.get('status') == 'succeeded'
                         and (r.get('stage') == 'uploaded' or r.get('kind') is None)), None)
    effective = utc(record.get('scheduled_at') or record.get('started')) if record else None
    if confirmation:
        state = 'succeeded'
    elif last and since and last >= since and (effective is None or effective < last):
        state = 'unknown'
    elif record and (record.get('status') in FAILURES or complete):
        state = record.get('status')
    elif last is None and upcoming:
        state = 'pending'
    else:
        state = 'unknown'
    deadline = None
    if job['kind'] == SUBMISSION_KIND:
        start = utc(job['due'] + 'T00:00:00+00:00')
        if start:
            deadline = start + timedelta(days=1)
    return {
        'id': job['id'], 'kind': job['kind'], 'state': state if state in JOB_STATES | {'pending'} else 'unknown',
        'latest_slot_utc': last.isoformat() if last else None,
        'next_slot_utc': upcoming.isoformat() if upcoming else None,
        'due': job.get('due'), 'submission_deadline_utc': deadline.isoformat() if deadline else None,
        'submission_confirmation': receipt_summary(confirmation),
        'receipt': receipt_summary(record),
        'recovery': None if confirmation else recovery(runtime, job, record, site, release, now, public),
    }


def snapshot_identity(metadata):
    return (metadata.get('dashboard', 'airo'), eeai_operator.frontend_commit(metadata),
            (metadata.get('data_snapshot') or {}).get('sha256'))


def socket_status(config, metadata):
    result = {'result': 'unknown', 'scope': 'release_metadata'}
    expected = snapshot_identity(metadata)
    if not all(expected) or not config.get('socket'):
        return result
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(4)
            stream.connect(config['socket'])
            connection = http.client.HTTPConnection('localhost', timeout=4)
            connection.sock = stream
            connection.request('GET', '/release.json')
            response = connection.getresponse()
            if response.status != 200:
                return {**result, 'http_status': response.status}
            actual = snapshot_identity(json.loads(response.read(65537)))
            return {**result, 'result': ('unknown' if not all(actual) else 'verified' if actual == expected else 'stale')}
    except (AttributeError, OSError, TypeError, ValueError, http.client.HTTPException):
        return result


def public_delivery(runtime, site, release, *, content=False):
    external = bool(runtime.config.get('public_publisher'))
    route = 'external' if external else 'socket'
    origin = socket_status(runtime.config, release)
    scope = 'content' if content else 'release_metadata'
    public = {'result': 'unknown', 'url': eeai_operator.public_url(runtime.config), 'scope': scope}
    if site:
        try:
            files = ()
            if content:
                if any(not (site / name).is_file() for name in runtime.dashboard.required):
                    raise ValueError('Selected site lacks required files')
                files = ('index.html', *(name for name in runtime.dashboard.downloads if (site / name).is_file()))
            probe = eeai_operator.check_public(runtime.config, site, release, content=content, files=files)
            public = {**probe, 'result': probe['result'] if probe['result'] in ('verified', 'stale') else 'unknown',
                      'probe_result': probe['result'], 'scope': scope}
        except (AttributeError, OSError, TypeError, ValueError):
            public['reason'] = 'Selected site or verification inputs unavailable'
    return {'route': route, 'origin': origin, 'public': public}


def freshness(runtime, site, release, jobs, receipts, complete, enabled):
    snapshot = release.get('data_snapshot') or {}
    source = snapshot.get('source_release')
    previous = next((r for r in receipts if source == 'job-' + r['key']), None)
    baseline = None
    if runtime.name == 'eeai':
        baseline = {'source': 'unknown', 'as_of': None}
        if site:
            try:
                baseline = runtime.published_baseline(site)
            except (OSError, KeyError, RuntimeError, TypeError, ValueError):
                pass
    latest = [j for j in jobs if j['kind'] in ('forecast', 'refresh')]
    status = 'unknown'
    published_at = None
    if previous and previous.get('status') == 'succeeded':
        published_at = utc(previous.get('verified_at') or previous.get('publication_ready_at') or previous.get('finished'))
    if (previous and previous.get('failure_detail') == 'transfer' and snapshot.get('sha256')
            and previous.get('published_snapshot') == snapshot['sha256']):
        published_at = utc(previous.get('publication_ready_at'))
    if (snapshot.get('sha256') and published_at
            and complete and latest and enabled is True and all(j['state'] != 'unknown' for j in latest)):
        status = 'current'
        for job in latest:
            record = job['receipt'] or {}
            started = utc(record.get('started'))
            if started and started > published_at and job['state'] in FAILURES:
                status = 'stale'
            elif status == 'current' and started and started > published_at and job['state'] in ('queued', 'running'):
                status = 'pending'
            elif (status == 'current' and started and started > published_at
                  and job['state'] == 'succeeded' and runtime.name != 'airo'):
                status = 'unknown'
    return {'state': status, 'data_snapshot_sha256': snapshot.get('sha256'), 'source_release': source,
            'published_at_utc': published_at.isoformat() if published_at else None,
            'published_receipt': receipt_summary(previous), 'baseline': baseline}


def report(runtime, scheduled_between, *, verify_content=False):
    now = datetime.now(timezone.utc)
    release, site, jobs = {}, None, []
    selection = 'unknown'
    reasons = []
    try:
        active = runtime.current.resolve(strict=True)
        site = (active / 'site').resolve(strict=True)
        release = runtime.release_metadata(active, site)
        if snapshot_identity(release)[0] != runtime.name or release.get('release') != active.name:
            raise ValueError('Selected release metadata identifies another installation')
        selection = 'selected'
    except (AttributeError, OSError, TypeError, ValueError):
        release, site = {}, None
        reasons.append('Active release or publication metadata unavailable')
    try:
        jobs = runtime.jobs(runtime.current / 'code')
    except (KeyError, OSError, TypeError, ValueError):
        reasons.append('Active schedule unavailable')
    enabled, since = None, None
    try:
        marker = (runtime.root / 'jobs-enabled').stat()
        enabled = True
        since = datetime.fromtimestamp(max(marker.st_mtime, runtime.current.lstat().st_mtime), timezone.utc)
    except FileNotFoundError:
        if enabled is None:
            enabled = False
    except OSError:
        pass
    receipts, complete = read_receipts(runtime.root)
    delivery = public_delivery(runtime, site, release, content=verify_content)
    visible_jobs = [job_status(runtime, job, receipts, complete, since, now, scheduled_between,
                              site, release, delivery['public'])
                    for job in jobs]
    if not complete:
        reasons.append('One or more run receipts were unavailable')
    return {
        'selection': selection,
        'release': {'id': release.get('release'), 'code_commit': release.get('commit'),
                    'frontend_commit': eeai_operator.frontend_commit(release)},
        'scheduler': {**timer_status(runtime, enabled, jobs), 'jobs_enabled': enabled},
        'jobs': visible_jobs,
        'freshness': freshness(runtime, site, release, visible_jobs, receipts, complete, enabled),
        'delivery': delivery,
        'warnings': reasons,
    }
