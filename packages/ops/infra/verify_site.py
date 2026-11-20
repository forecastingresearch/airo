#!/usr/bin/env python3
"""Verify the candidate's bytes and access policy at its HTTPS ingress."""
import argparse
import base64
from hashlib import file_digest
import json
from pathlib import Path
import ssl
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def basic(user, password):
    value = base64.b64encode(f'{user}:{password}'.encode()).decode()
    return {'Authorization': f'Basic {value}'}


def verify(config, candidate):
    url = config['url'].rstrip('/') + '/'
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path != '/' or parsed.query or parsed.fragment):
        raise ValueError('Expected a site-root HTTPS URL without credentials')
    tls = ssl.create_default_context(cafile=config.get('ca_file'))
    opener = build_opener(NoRedirect(), HTTPSHandler(context=tls))
    headers = {}
    if 'credentials_file' in config:
        credentials_path = Path(config['credentials_file'])
        with credentials_path.open() as source:
            if credentials_path.stat().st_mode & 0o077:
                raise ValueError('Verifier credentials must have mode 0600')
            credentials = json.load(source)
        headers = basic(credentials['username'], credentials['password'])

    for name in ('index.html', 'release.json', *config.get('downloads', [])):
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or path == Path('.'):
            raise ValueError('Expected an ingress-relative file name')
        endpoint = url + ('' if name == 'index.html' else quote(name))
        with (candidate / path).open('rb') as expected, opener.open(
            Request(endpoint, headers=headers), timeout=10,
        ) as served:
            if served.status != 200 or file_digest(served, 'sha256').hexdigest() != file_digest(expected, 'sha256').hexdigest():
                raise ValueError(f'Ingress content differs for {name}')
        if headers:
            for denied in ({}, basic('invalid-verifier', 'invalid-verifier')):
                try:
                    with opener.open(Request(endpoint, headers=denied), timeout=10):
                        raise ValueError(f'Ingress allowed unauthorized access to {name}')
                except HTTPError as exc:
                    exc.close()
                    if exc.code != 401:
                        raise ValueError(f'Ingress did not return 401 for {name}') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    parser.add_argument('candidate', type=Path)
    args = parser.parse_args()
    try:
        verify(json.loads(args.config.read_text()), args.candidate)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f'Public verification failed: {type(exc).__name__}\n')


if __name__ == '__main__':
    main()
