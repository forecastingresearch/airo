#!/usr/bin/env python3
"""Limit a publication key to the repository receiver and its assigned site."""
import os
import re
import shlex
import sys


def main():
    receiver, root, verifier = sys.argv[1:]
    try:
        command = shlex.split(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
    except ValueError:
        sys.exit('Invalid publication request')
    expected = ['python3', receiver, 'receive', '--root', root, '--verify', verifier]
    if command[:-1] != expected or not re.fullmatch(r'[0-9a-f]{64}', command[-1]):
        sys.exit('Invalid publication request')
    os.execv(sys.executable, [sys.executable, *expected[1:], command[-1]])


if __name__ == '__main__':
    main()
