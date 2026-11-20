#!/usr/bin/env python3
"""Create an encrypted host snapshot or restore one into fresh quarantine."""
import argparse
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess


def restic(config, *argv, capture=False):
    return subprocess.run(
        [config.get('restic', '/usr/bin/restic'), '--repository-file', config['repository_file'],
         '--password-file', config['password_file'], *argv],
        check=True, capture_output=capture, text=capture,
    )


def backup(config):
    with ExitStack() as locks:
        for path in sorted(set(config['locks'])):
            descriptor = locks.enter_context(Path(path).open('rb'))
            fcntl.flock(descriptor, fcntl.LOCK_SH)
        excludes = [arg for name in config['exclude'] for arg in ('--exclude', name)]
        restic(config, 'backup', '--tag', config['role'], *excludes, '--', *config['paths'])


def restore(config, snapshot, target, staging):
    if not re.fullmatch(r'[0-9a-f]{8,64}', snapshot):
        raise ValueError('Choose an explicit snapshot ID from the documented role')
    if not target.is_absolute() or target.resolve() != target:
        raise ValueError('Restore target must be a new absolute directory without symlinks')
    snapshots = json.loads(restic(config, 'snapshots', '--tag', config['role'], '--json', capture=True).stdout)
    matches = [item['id'] for item in snapshots if item['id'].startswith(snapshot)]
    if len(matches) != 1:
        raise ValueError('Snapshot ID does not identify one backup for this host role')
    # Always restore into a new quarantine. Never overwrite an installation in place.
    target.mkdir(mode=0o700)
    old_umask = os.umask(0o077)
    try:
        if staging:
            (target / 'STAGING').touch()
        restored = target / 'snapshot'
        exclude = [arg for name in config['staging_exclude'] for arg in ('--exclude', name)] if staging else []
        restic(config, 'restore', matches[0], '--verify', '--target', str(restored), *exclude)
    finally:
        os.umask(old_umask)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('backup')
    commands.add_parser('snapshots')
    commands.add_parser('check')
    restore_args = commands.add_parser('restore')
    restore_args.add_argument('snapshot')
    restore_args.add_argument('target', type=Path)
    modes = restore_args.add_mutually_exclusive_group(required=True)
    modes.add_argument('--staging', action='store_true')
    modes.add_argument('--recovery', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if args.command == 'restore':
        restore(config, args.snapshot, args.target, args.staging)
    elif args.command == 'backup':
        backup(config)
    elif args.command == 'snapshots':
        restic(config, 'snapshots', '--tag', config['role'])
    else:
        restic(config, 'check')


if __name__ == '__main__':
    main()
