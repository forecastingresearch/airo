#!/usr/bin/env python3
"""Install the pinned Node archive or preflight a project's release toolchain."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
from tempfile import TemporaryDirectory


MIN_NODE = (22, 21, 0)


def version(text):
    if not re.fullmatch(r'v?\d+\.\d+\.\d+', text):
        raise ValueError('Expected a three-part Node.js version')
    return tuple(map(int, text.removeprefix('v').split('.')))


def node_version(program, env=None):
    return version(subprocess.check_output([program, '--version'], env=env, text=True).strip())


def install_node(archive, sha, release, destination):
    if not re.fullmatch(r'[0-9a-f]{64}', sha) or version(release) < MIN_NODE:
        raise ValueError('Expected a SHA-256 and Node.js 22.21.0 or later')
    if destination.is_dir():
        marker = destination / '.archive-sha256'
        if marker.read_text().strip() != sha or node_version(destination / 'bin/node') != version(release):
            raise ValueError('Installed Node.js directory does not match the configured pin')
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with archive.open('rb') as source, TemporaryDirectory(dir=destination.parent) as staging:
        if hashlib.file_digest(source, 'sha256').hexdigest() != sha:
            raise ValueError('Node.js archive SHA-256 mismatch')
        source.seek(0)
        with tarfile.open(fileobj=source, mode='r:xz') as distribution:
            distribution.extractall(staging, filter='data')
        payload, = Path(staging).iterdir()
        if node_version(payload / 'bin/node') != version(release) or not (payload / 'bin/npm').is_file():
            raise ValueError('Node.js distribution version or npm differs from the requested toolchain')
        (payload / '.archive-sha256').write_text(sha + '\n')
        payload.chmod(0o755)
        payload.rename(destination)


def check(config):
    env = {**os.environ, 'PATH': config['path']}
    if node_version('node', env) < MIN_NODE:
        raise ValueError('Node.js 22.21.0 or later is required')
    for command in ('npm', 'git', 'gh'):
        subprocess.run([command, '--version'], env=env, check=True, stdout=subprocess.DEVNULL)
    with TemporaryDirectory(prefix='fri-toolchain-') as venv:
        subprocess.run(['python3', '-m', 'venv', venv], env=env, check=True)
    print('Node.js, npm, Git, gh and Python venv are available')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    install = commands.add_parser('install-node')
    install.add_argument('archive', type=Path)
    install.add_argument('sha256')
    install.add_argument('version')
    install.add_argument('destination', type=Path)
    preflight = commands.add_parser('check')
    preflight.add_argument('config', type=Path)
    args = parser.parse_args()
    if args.command == 'install-node':
        install_node(args.archive, args.sha256, args.version, args.destination)
    else:
        check(json.loads(args.config.read_text()))


if __name__ == '__main__':
    main()
