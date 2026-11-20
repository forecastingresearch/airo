#!/usr/bin/env python3
"""Run the installed pi loop, RSI validation and publication with local model responses."""
import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading

PI_RELATIVE = Path('packages/forecaster/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js')
MODEL = 'keyless/deterministic'
WORKER_ENV = {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_CTYPE', 'PI_CODING_AGENT_DIR',
              'PI_OFFLINE', 'PI_TELEMETRY', 'FORECASTER_ENV_FILE'}
BANNER = ('<div role="status" style="padding:8px;text-align:center;background:#fff4b3;color:#201600">'
          'Staging forecast from a deterministic local model</div>')


def write(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')


def tool_text(body):
    messages = [message for message in body['messages'] if message.get('role') == 'tool']
    if not messages:
        raise ValueError('The local model did not receive a tool result')
    content = messages[-1]['content']
    return content if isinstance(content, str) else ''.join(piece.get('text', '') for piece in content)


class LocalModel:
    def __init__(self, output, snapshot, digest, sections):
        self.output, self.snapshot, self.digest, self.sections = output, snapshot, digest, sections
        self.requests, self.errors = [], []
        self.read_consumed = self.submission_consumed = False
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    if self.headers.get('Authorization') != 'Bearer keyless-local':
                        raise ValueError('Unexpected authentication on the local model request')
                    if self.path != '/v1/chat/completions':
                        raise ValueError('Unexpected local model endpoint')
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    model.requests.append(body)
                    write(output / 'model-requests.json', model.requests)
                    turn = model.next_turn(body)
                except Exception as error:
                    model.errors.append(str(error))
                    self.send_response(500)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'error': {'message': str(error)}}).encode())
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                for chunk in model.chunks(turn, len(model.requests)):
                    self.wfile.write(f'data: {json.dumps(chunk)}\n\n'.encode())
                self.wfile.write(b'data: [DONE]\n\n')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}/v1'

    def next_turn(self, body):
        from fri_forecaster.news_cache import URL
        from rsi_dashboard.instrument import synthetic, fields

        offered = {tool['function']['name'] for tool in body.get('tools', [])}
        if not offered <= {'web_search', 'read_page', 'submit_forecast'}:
            raise ValueError('Unexpected tools in the isolated pi session')
        number = len(self.requests)
        if number == 1:
            if 'read_page' not in offered:
                raise ValueError('The installed extension did not offer read_page')
            return 'read_page', {'url': URL}
        if number == 2:
            result = json.loads(tool_text(body))
            if (result.get('url') != URL or result.get('snapshot_sha256') != self.digest
                    or json.loads(result.get('text', '{}')) != self.snapshot):
                raise ValueError('The model did not receive the real pinned snapshot tool result')
            self.read_consumed = True
            fixture = json.loads(result['text'])
            probability = 100 * fixture['observed'] / fixture['trials']
            response = synthetic(self.sections)
            for section in self.sections:
                for question in section['questions']:
                    if fields(question) == ('probability',):
                        response['answers'][question['id']]['probability'] = probability
                response['rationales'][section['id']] = (
                    f"Local staging evidence {fixture['nonce']}: {fixture['observed']} of "
                    f"{fixture['trials']} observations. Deterministic acceptance forecast.")
            return 'submit_forecast', response
        if number == 3:
            result = json.loads(tool_text(body).split(' Done:', 1)[0])
            if result.get('ok') is not True:
                raise ValueError('The model did not receive the successful forecast submission result')
            self.submission_consumed = True
            return 'Keyless staging forecast complete.'
        raise ValueError('The local model received an unexpected additional turn')

    @staticmethod
    def chunks(turn, number):
        base = {'id': f'local-{number}', 'object': 'chat.completion.chunk', 'created': 0,
                'model': 'deterministic'}
        if isinstance(turn, str):
            delta, reason = {'role': 'assistant', 'content': turn}, 'stop'
        else:
            name, arguments = turn
            delta = {'role': 'assistant', 'tool_calls': [
                {'index': 0, 'id': f'local-tool-{number}', 'type': 'function',
                 'function': {'name': name, 'arguments': json.dumps(arguments)}}]}
            reason = 'tool_calls'
        yield dict(base, choices=[{'index': 0, 'delta': delta, 'finish_reason': None}])
        yield dict(base, choices=[{'index': 0, 'delta': {}, 'finish_reason': reason}])
        yield dict(base, choices=[], usage={'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15})

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def worker(pi, output):
    if set(os.environ) - WORKER_ENV:
        raise RuntimeError('Unexpected ambient variables in the isolated worker')
    if (Path(os.environ['HOME']) != output / 'home'
            or Path(os.environ['PI_CODING_AGENT_DIR']) != output / 'agent'
            or (output / 'agent/auth.json').exists()):
        raise RuntimeError('Expected fresh private forecasting HOME and agent directory')
    code = pi.parents[7]
    if code / PI_RELATIVE != pi:
        raise ValueError('Expected the installed fri-forecaster pi path')
    sys.path[:0] = [str(code / path) for path in
                   ('packages/forecaster', 'packages/dashboard', 'dashboards/rsi')]
    from dashboard import page, site
    from fri_forecaster import harness, news_cache
    from rsi_dashboard import instrument, results, run

    package = json.loads((pi.parents[2] / 'package.json').read_text())
    if package['version'] != harness.PINNED_PI_VERSION or harness.pi_version(str(pi)) != harness.PINNED_PI_VERSION:
        raise RuntimeError('The installed pi version must match the forecasting engine pin')
    source = instrument.SOURCE.read_text()
    sections = instrument.parse(source)
    fixture = {'nonce': secrets.token_hex(16), 'observed': 37, 'trials': 100}
    snapshot = output / 'snapshot.json'
    write(snapshot, fixture)
    digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    os.environ[news_cache.PATH_ENV] = str(snapshot)
    os.environ[news_cache.HASH_ENV] = digest
    model = LocalModel(output, fixture, digest, sections)
    agent = Path(os.environ['PI_CODING_AGENT_DIR'])
    write(agent / 'models.json', {'providers': {'keyless': {
        'baseUrl': model.url, 'api': 'openai-completions', 'apiKey': 'keyless-local',
        'models': [{'id': 'deterministic', 'reasoning': False}]}}})
    prompt = (f"Staging acceptance only. Read {news_cache.URL} using read_page, then answer "
              f"the RSI survey from the local evidence.\n\n{source}")
    plan = {'condition': 'baseline', 'date': datetime.now(timezone.utc).date().isoformat(),
            'survey': source, 'prompt': prompt, 'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
            'members': [{'key': 'keyless', 'model': MODEL, 'label': 'Keyless staging model'}],
            'web_access': 'full-web', 'blocked_domains': []}
    runs = output / 'results'
    folder = runs / 'keyless'
    folder.mkdir(parents=True)
    write(folder / 'plan.json', plan)

    def real_pi(**kwargs):
        return harness.run(**{**kwargs, 'pi': str(pi), 'thinking': 'off', 'timeout': 300})

    try:
        run.execute(folder, plan, harness_run=real_pi)
    finally:
        model.close()
    if model.errors or not model.read_consumed or not model.submission_consumed or len(model.requests) != 3:
        raise RuntimeError(f'The real pi/tool/model exchange was incomplete: {model.errors}')
    accepted = results.load_runs(runs, source, sections, publication=True)
    if len(accepted) != 1 or accepted[0]['answers']['Q3.2']['probability'] != 37:
        raise RuntimeError('Publication validation did not retain the model-derived forecast')
    events = list(folder.glob('attempts/keyless/attempt-*/events.jsonl'))
    if len(events) != 1:
        raise RuntimeError('Expected one successful real pi attempt')
    parsed = [json.loads(line) for line in events[0].read_text().splitlines() if line.strip().startswith('{')]
    tool_ends = [event for event in parsed if event.get('type') == 'tool_execution_end']
    if [event.get('toolName') for event in tool_ends] != ['read_page', 'submit_forecast']:
        raise RuntimeError('The real extension did not finish both required tool calls')
    publication = output / 'site'
    subprocess.run([sys.executable, '-m', 'rsi_dashboard', '--results', str(runs),
                    '--site', str(publication), '--publication'], check=True)
    html = publication / 'index.html'
    before = html.read_bytes()
    text = before.decode()
    body = text.index('>', text.index('<body')) + 1
    html.write_text(text[:body] + BANNER + text[body:])
    if page.blocks(before) != page.blocks(html.read_bytes()):
        raise RuntimeError('The staging banner changed the forecast payload')
    payload = page.payload(html.read_bytes(), 'SURVEY')
    if payload['runs'][0]['answers']['Q3.2']['probability'] != 37:
        raise RuntimeError('The dashboard builder dropped the model-derived forecast')
    commit = subprocess.check_output(['git', '-C', str(code), 'rev-parse', 'HEAD'], text=True).strip()
    release = 'keyless-' + fixture['nonce'][:12]
    write(publication / 'release.json', {'dashboard': 'rsi', 'release': release, 'commit': commit,
          'frontend_commit': commit, 'publication_commit': commit, 'publication': 'rebuilt', 'fixture': True,
          'data_snapshot': {**site.snapshot(publication, ()), 'origin_commit': commit, 'source_release': release}})
    site.verify(publication)
    verdict = {'pi_version': harness.PINNED_PI_VERSION, 'source_commit': commit, 'fixture': True,
               'model_requests': len(model.requests), 'read_page_executed': True,
               'ambient_credentials': 'excluded', 'coding_login': 'absent',
               'tool_result_consumed': model.read_consumed, 'submission_result_consumed': model.submission_consumed,
               'snapshot_sha256': digest, 'probability': 37, 'rsi_validation': 'verified',
               'publication_validation': 'verified', 'builder': 'verified',
               'results': str(runs), 'site': str(publication), 'trace': str(events[0])}
    write(output / 'verdict.json', verdict)
    print(json.dumps(verdict), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pi', type=Path, required=True, help='installed pinned pi dist/bundle/cli.js')
    parser.add_argument('--out', type=Path, required=True, help='new private output directory')
    parser.add_argument('--node', type=Path, help='Node executable, default the configured PATH')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    pi = args.pi.resolve(strict=True)
    if not pi.is_file() or len(pi.parents) < 8 or pi.parents[7] / PI_RELATIVE != pi:
        parser.error('Provide the installed pinned pi dist/bundle/cli.js')
    output = args.out.resolve()
    if args.worker:
        worker(pi, output)
        return
    node = args.node or shutil.which('node')
    if not node:
        parser.error('The configured Node executable is required')
    node = Path(node).resolve(strict=True)
    os.umask(0o077)
    output.mkdir(mode=0o700)
    for name in ('home', 'agent', 'tmp'):
        (output / name).mkdir()
    (output / 'no-keys').touch()
    env = {'PATH': str(node.parent) + ':/usr/bin:/bin', 'HOME': str(output / 'home'),
           'TMPDIR': str(output / 'tmp'), 'LANG': 'C.UTF-8', 'PI_CODING_AGENT_DIR': str(output / 'agent'),
           'PI_OFFLINE': '1', 'PI_TELEMETRY': '0', 'FORECASTER_ENV_FILE': str(output / 'no-keys')}
    subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker', '--pi', str(pi),
                    '--out', str(output)], env=env, stdin=subprocess.DEVNULL, check=True, timeout=600)


if __name__ == '__main__':
    main()
