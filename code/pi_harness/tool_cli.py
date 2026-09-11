#!/usr/bin/env python3
"""The two research tools as a one-shot CLI for the pi extension
(code/pi_harness/redlines_tools.ts): `tool_cli.py web_search '{"query": ...}'`
prints the same JSON redlines.tools returns to our own loop. One tool
implementation for both harnesses, so the pi trial compares loops, not
search stacks. Keys come from the environment (redlines.llm.load_keys reads
~/.config/redlines/env when they are not set)."""
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from redlines import tools  # noqa: E402
from redlines.llm import load_keys  # noqa: E402

FN = {"web_search": tools.web_search, "read_page": tools.read_page}


def main():
    load_keys()
    tool, args = sys.argv[1], json.loads(sys.argv[2] if len(sys.argv) > 2 else "{}")
    if tool not in FN:
        sys.exit(f"unknown tool {tool!r}")
    print(json.dumps(FN[tool](**args)))


if __name__ == "__main__":
    main()
