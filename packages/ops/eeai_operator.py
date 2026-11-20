"""Read-only scheduler and HTTPS checks for the EEAI operator pilot."""
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import ssl
import stat
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, proxy_bypass_environment

FRED_HOST = 'api.stlouisfed.org'
FRED_URL = f'https://{FRED_HOST}/fred/series/observations?series_id=UNRATE&file_type=json'
ARCHIVE = 'eeai-data.zip'
ROUTE_VARS = {'HTTPS_PROXY', 'https_proxy', 'NO_PROXY', 'no_proxy', 'SSL_CERT_FILE', 'SSL_CERT_DIR'}
SCHEDULER_VARS = ROUTE_VARS | {'DASHBOARD_CONFIG_DIR'}
UNIT_PROPERTIES = ('LoadState', 'ExecStart', 'Environment', 'EnvironmentFiles',
                   'PAMName', 'PassEnvironment', 'UnsetEnvironment', 'MainPID',
                   'PrivateNetwork', 'NetworkNamespacePath', 'RootDirectory', 'RootImage')
STACK_STATES = {'verified', 'stale', 'login_redirect', 'access_denied', 'unverifiable',
                'timeout', 'dns', 'tls', 'proxy_connect_denied'}


def command_result(argv, *, timeout=4, **kwargs):
    """Child diagnostics are private, including on failure or timeout."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, **kwargs)
        if proc.returncode == 0:
            return proc.stdout, None
    except subprocess.TimeoutExpired:
        return None, 'timeout'
    except (OSError, UnicodeError, ValueError):
        pass
    return None, 'unverifiable'


def unit_properties(unit, properties):
    if not re.fullmatch(r'[A-Za-z0-9_@.:-]+', unit):
        return {}
    result, _ = command_result(['systemctl', 'show', '--all',
                                *('--property=' + prop for prop in properties), '--', unit])
    values = {}
    for line in (result or '').splitlines():
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        values[key] = (' '.join(filter(None, [values.get(key), value]))
                       if key == 'EnvironmentFiles' else value)
    if 'EnvironmentFiles' in properties and 'EnvironmentFiles' not in values and values.get('LoadState') == 'loaded':
        # Some systemd versions omit an empty EnvironmentFiles array even with --all.
        # Confirm that neither the installed unit nor a drop-in declares one.
        unit_text, _ = command_result(['systemctl', 'cat', '--', unit])
        if unit_text and not re.search(r'^\s*EnvironmentFile\s*=', unit_text, re.M):
            values['EnvironmentFiles'] = ''
    return values


def assignments(text):
    pairs = [word.split('=', 1) for word in shlex.split(text)]
    if any(len(pair) != 2 for pair in pairs):
        raise ValueError('Unsupported environment assignment')
    return dict(pair for pair in pairs if pair[0] in SCHEDULER_VARS)


def file_environment(path):
    # This accepts an auditable subset of systemd EnvironmentFile syntax.
    # A multiline/escaped or otherwise ambiguous file cannot attest the route.
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
        raise ValueError('Unsupported EnvironmentFile')
    body = path.read_text()
    if '\\' in body or '\0' in body:
        raise ValueError('Unsupported EnvironmentFile syntax')
    result = {}
    for line in body.split('\n'):
        line = line.strip()
        if not line or line.startswith(('#', ';')):
            continue
        name, sep, value = line.partition('=')
        if not sep or not re.fullmatch(r'[a-zA-Z_][a-zA-Z_0-9]*', name.strip()):
            raise ValueError('Unsupported EnvironmentFile assignment')
        value = value.strip()
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0] or value[0] in value[1:-1]:
                raise ValueError('Unsupported EnvironmentFile quoting')
            value = value[1:-1]
        if name.strip() in SCHEDULER_VARS:
            result[name.strip()] = value
    return result


def scheduler_environment(unit):
    if not all(prop in unit for prop in UNIT_PROPERTIES):
        return None, 'systemd properties unavailable'
    if unit['PrivateNetwork'] != 'no' or any(unit[key] for key in ('NetworkNamespacePath', 'RootDirectory', 'RootImage')):
        return None, 'systemd network or filesystem isolation'
    pid = unit['MainPID']
    if pid.isdecimal() and int(pid) > 0:
        try:
            encoded = Path(f'/proc/{pid}/environ').read_bytes()
            env = dict(os.fsdecode(entry).split('=', 1) for entry in encoded.split(b'\0') if b'=' in entry)
            return {name: env[name] for name in SCHEDULER_VARS if name in env}, 'service process'
        except OSError:
            pass
    if unit['PAMName'] or unit['PassEnvironment']:
        return None, 'systemd PAM or inherited environment'
    manager, _ = command_result(['systemctl', 'show-environment'])
    if manager is None:
        return None, 'systemd manager environment unavailable'
    try:
        if "$'" in manager or '\\' in unit['Environment']:
            raise ValueError('Unsupported systemd quoting')
        env = assignments(manager)
        env.update(assignments(unit['Environment']))
        spec = unit['EnvironmentFiles'].strip()
        files = re.findall(r'(/[^\s]+) \(ignore_errors=(yes|no)\)', spec)
        if ' '.join(f'{path} (ignore_errors={optional})' for path, optional in files) != spec:
            return None, 'systemd EnvironmentFile syntax unavailable'
        for path, optional in files:
            if any(char in path for char in '*?[%\\'):
                return None, 'systemd EnvironmentFile expansion unavailable'
            try:
                env.update(file_environment(Path(path)))
            except FileNotFoundError:
                if optional != 'yes':
                    return None, 'systemd EnvironmentFile missing'
            except (OSError, UnicodeError, ValueError):
                return None, 'systemd EnvironmentFile unreadable or unsupported'
        for assignment in shlex.split(unit['UnsetEnvironment']):
            name, sep, value = assignment.partition('=')
            if not sep or env.get(name) == value:
                env.pop(name, None)
        return env, 'systemd configuration'
    except ValueError:
        return None, 'systemd environment syntax unavailable'


def scheduler_timer(config):
    timer = config.get('scheduler_timer', 'fri-dashboard-tick.timer')
    unit = unit_properties(timer, ('LoadState', 'UnitFileState', 'ActiveState', 'Triggers', 'TimersCalendar'))
    calendar = unit.get('TimersCalendar', '')
    return {'timer': timer, 'timer_enablement': unit.get('UnitFileState') or 'unknown',
            'timer_activity': unit.get('ActiveState') or 'unknown',
            'minute_calendar': bool(re.search(r'OnCalendar=(?:\*-\*-\* \*:\*:00(?: UTC)?|minutely)\s*;',
                                              calendar)) if calendar else None}, unit.get('Triggers', '').split()


def launcher_command(unit):
    start = re.fullmatch(r'\{ path=(\S+) ; argv\[\]=([^;]*?)\s*; .*\}', unit.get('ExecStart', ''))
    if not start or unit.get('LoadState') != 'loaded':
        return []
    executable, argv = start.groups()
    args = argv.split()
    if not args or args[0] != executable:
        return []
    if executable in ('/usr/bin/python3', '/usr/local/bin/python3'):
        return args[1:]
    return args


def tick_command(unit):
    args = launcher_command(unit)
    if not args:
        return None, False
    eligible = Path(args[0]).name == 'dashboardctl' and args[1:] in (['tick-all'], ['tick-all', '--queued'])
    return args if eligible else [args[0], '[arguments omitted]'], eligible


def worker_command(unit, lane):
    args = launcher_command(unit)
    return bool(args and Path(args[0]).name == 'dashboardctl'
                and args[1:] == ['worker-all', '--lane', lane])


def scheduler(config, *, queued=False):
    timer, triggers = scheduler_timer(config)
    service = config.get('scheduler_service', 'fri-dashboard-tick.service')
    unit = unit_properties(service, UNIT_PROPERTIES)
    command, eligible = tick_command(unit)
    details = {**timer, 'service': service, 'command': command,
               'timer_dispatches_service': service in triggers if triggers else None}
    if not eligible:
        return details, None, 'systemd ExecStart unavailable or unsupported'
    if queued:
        execution_service = 'fri-dashboard-worker@data.service'
        details['execution_service'] = execution_service
        unit = unit_properties(execution_service, (*UNIT_PROPERTIES, 'ActiveState'))
        if not worker_command(unit, 'data') or unit.get('ActiveState') != 'active':
            return details, None, 'active data worker unavailable or unsupported'
    route, source = scheduler_environment(unit)
    return details, route, source


def proxy_settings(env):
    # urllib gives lower-case variables precedence, including an empty override.
    return (env.get('https_proxy', env.get('HTTPS_PROXY')) or None,
            env.get('no_proxy', env.get('NO_PROXY')) or None)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def network_error(error):
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return 'timeout'
    if isinstance(reason, socket.gaierror):
        return 'dns'
    if isinstance(reason, ssl.SSLError):
        return 'tls'
    if isinstance(reason, OSError) and re.search(r'\bTunnel connection failed: (?:401|403|405|407|451)\b', str(reason)):
        return 'proxy_connect_denied'
    return 'unverifiable'


def fred_probe():
    try:
        with build_opener(NoRedirect()).open(FRED_URL, timeout=8):
            return 'unverifiable'
    except HTTPError as error:
        try:
            payload = json.loads(error.read(4096))
            message = payload.get('error_message', '')
            if (error.code == 400 and payload.get('error_code') == 400
                    and re.search(r'\b(?:is not set|is required|must be set)\b', message, re.I)
                    and re.search(r'\bapi_key\b', message)):
                return 'missing_key_response'
        except (ValueError, AttributeError, TypeError):
            pass
        except OSError as read_error:
            return network_error(read_error)
        finally:
            error.close()
        return 'unverifiable'
    except (OSError, ValueError) as error:
        return network_error(error)


def check_fred(env):
    proxy, bypass = proxy_settings(env)
    route = 'proxy' if proxy and not proxy_bypass_environment(FRED_HOST, {'no': bypass or ''}) else 'direct'
    result, failure = command_result([sys.executable, __file__, 'fred'],
                                     env={k: v for k, v in env.items() if k in ROUTE_VARS}, timeout=12)
    result = (result or '').strip()
    known = {'missing_key_response', 'proxy_connect_denied', 'tls', 'dns', 'timeout'}
    return {'destination': FRED_HOST, 'route': route,
            'result': failure or (result if result in known else 'unverifiable')}


def public_url(config):
    url = config.get('public_url')
    try:
        parsed = urlsplit(url)
        if (parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment and not any(c.isspace() for c in url)):
            parsed.port  # validate an explicit port
            return url.rstrip('/') + '/'
    except (AttributeError, TypeError, ValueError):
        pass
    return None


def frontend_commit(metadata):
    if 'frontend_commit' in metadata:
        return metadata['frontend_commit']
    return metadata.get('publication_commit') or (metadata.get('commit') if metadata.get('publication') != 'preserved' else None)


def baseline_metadata(blob):
    baseline = blob.get('baseline')
    if isinstance(baseline, dict):
        if baseline.get('source') == 'survey' and baseline.get('as_of') is None:
            return {'source': 'survey', 'as_of': None}
        if baseline.get('source') == 'fred':
            try:
                as_of = date.fromisoformat(baseline['as_of']).isoformat()
                if as_of == baseline['as_of']:
                    return {'source': 'fred', 'as_of': as_of}
            except (KeyError, TypeError, ValueError):
                pass
    return {'source': 'unknown', 'as_of': None}


def public_manifest(site, metadata, url, content, files=None):
    expected = {'url': url, 'frontend_commit': frontend_commit(metadata),
                'data_snapshot_sha256': (metadata.get('data_snapshot') or {}).get('sha256'), 'files': {}}
    if files is not None:
        expected['dashboard'] = metadata.get('dashboard', 'airo')
    if content:
        for name in files if files is not None else ('index.html', ARCHIVE):
            with (site / name).open('rb') as f:
                expected['files'][name] = {'sha256': hashlib.file_digest(f, 'sha256').hexdigest(),
                                           'size': os.fstat(f.fileno()).st_size}
    return expected


def http_error(error):
    if error.code in (401, 403):
        return 'access_denied'
    if 300 <= error.code < 400 and re.search(r'(?:login|sign[-_]?in|auth)', error.headers.get('Location', ''), re.I):
        return 'login_redirect'
    return 'unverifiable'


def public_probe(expected, headers=None):
    opener = build_opener(NoRedirect())
    checks = {}
    for name in ('release.json', *expected['files']):
        request_url = expected['url'] + ('' if name == 'index.html' else name)
        try:
            with opener.open(Request(request_url, headers=headers or {}), timeout=8) as response:
                if response.status != 200:
                    checks[name] = 'unverifiable'
                    continue
                if name == 'release.json':
                    found = json.loads(response.read(65537))
                    snapshot = (found.get('data_snapshot') or {}).get('sha256')
                    commit = frontend_commit(found)
                    if not snapshot or not commit:
                        checks[name] = 'unverifiable'
                    else:
                        checks[name] = ('verified' if snapshot == expected['data_snapshot_sha256']
                                        and commit == expected['frontend_commit']
                                        and ('dashboard' not in expected or found.get('dashboard', 'airo')
                                             == expected['dashboard']) else 'stale')
                    continue
                manifest = expected['files'][name]
                digest = hashlib.sha256()
                remaining = manifest['size'] + 1
                while remaining:
                    piece = response.read(min(256 * 1024, remaining))
                    if not piece:
                        break
                    digest.update(piece)
                    remaining -= len(piece)
                checks[name] = ('verified' if remaining == 1
                                and digest.hexdigest() == manifest['sha256'] else 'stale')
        except HTTPError as error:
            checks[name] = http_error(error)
            error.close()
        except (AttributeError, TypeError, ValueError, OSError) as error:
            checks[name] = network_error(error)
    return checks


def private_headers(path):
    with Path(path).open() as f:
        if os.fstat(f.fileno()).st_mode & 0o077:
            raise ValueError('Expected a private header file')
        headers = json.load(f)
    if (not isinstance(headers, dict) or
            not all(key in ('Authorization', 'Cookie') and isinstance(value, str)
                    and '\r' not in value and '\n' not in value for key, value in headers.items())):
        raise ValueError('Expected private Authorization or Cookie headers')
    return headers


def check_public(config, site, metadata, *, content=False, files=None):
    url = public_url(config)
    result = {'url': url, 'source': 'public_url'}
    if not url or not frontend_commit(metadata) or not (metadata.get('data_snapshot') or {}).get('sha256'):
        return {**result, 'result': 'unverifiable', 'reason': 'configured HTTPS URL or expected release metadata missing'}
    manifest = public_manifest(site, metadata, url, content, files)
    custom = config.get('public_verifier')
    if custom is not None:
        result['source'] = 'public_verifier'
        if not isinstance(custom, list) or not custom or not all(isinstance(arg, str) and arg for arg in custom):
            return {**result, 'result': 'unverifiable', 'reason': 'public_verifier requires a private argv list'}
    command = custom if custom is not None else [sys.executable, __file__, 'public']
    raw, failure = command_result(command, input=json.dumps(manifest), timeout=45 if content else 15)
    paths = ('release.json', *manifest['files'])
    try:
        decoded = json.loads(raw) if raw else {}
        checks = {name: decoded.get(name) if decoded.get(name) in STACK_STATES else 'unverifiable' for name in paths}
    except (AttributeError, TypeError, ValueError):
        checks = dict.fromkeys(paths, 'unverifiable')
    summary = next((state for state in checks.values() if state != 'verified'), 'verified')
    return {**result, 'result': failure or summary, 'checks': checks}


if __name__ == '__main__':
    if sys.argv[1:] == ['fred']:
        print(fred_probe())
    elif sys.argv[1:2] == ['public']:
        import argparse
        parser = argparse.ArgumentParser(description='Compare served HTTPS files with the manifest on stdin')
        parser.add_argument('--headers-file')
        parser.add_argument('--expect-url', help='Expected public base URL, required with private headers')
        args = parser.parse_args(sys.argv[2:])
        try:
            manifest = json.load(sys.stdin)
            url = manifest.get('url')
            if not url or public_url({'public_url': url}) != url:
                sys.exit('Expected a normalized HTTPS URL in the manifest')
            if args.headers_file and public_url({'public_url': args.expect_url}) != url:
                sys.exit('The manifest URL differs from the authenticated verifier URL')
            print(json.dumps(public_probe(manifest, private_headers(args.headers_file) if args.headers_file else None)))
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            sys.exit('Cannot read the public verification manifest or private headers')
    else:
        sys.exit('Expected fred or public')
