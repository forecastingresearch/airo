"""Manifest-driven page assembly for dashboard pages under web/<name>/.

Mirrors an on-disk dashboard page (index.html today; timeline.html
later) as ordered source chunks plus a manifest, so the page becomes a build
ARTIFACT instead of a hand-edited source. See web/demo/manifest.json for the
layout this module reads:

    {"target": "index.html",
     "blobs": ["GRAPH4", "GRAPH1", "GRAPH2", "DATABANK", "GRAPH3"],
     "chunks": ["00-head.html", "01-shell-open.html", ...]}

Two functions matter to callers:

    assemble(page_dir) -> str
        Concatenates page_dir's chunks (manifest order) into the page
        SKELETON: the full page text with every window.__NAME__ data blob's
        marker block collapsed to the empty pair it normally surrounds,
        "<!--NAME_DATA--><!--/NAME_DATA-->" -- each such pair lives inside
        whichever chunk spans that point in the page, verbatim.

    hydrate_page(page_dir, blobs) -> str
        assemble()s the skeleton, then splices each blob into its marker
        pair using the exact block format redlines.hydrate.inject() writes
        to disk (start marker, newline, the window.__NAME__ <script> tag,
        newline, end marker) -- so the result is byte-identical to what
        inject() would produce against the real page, without touching
        disk. `blobs` needs one entry per name in the manifest's "blobs".

write_skeleton(page_dir) writes assemble()'s output (no blobs spliced in)
straight to the manifest's target page. It exists for future CLI wiring and
is not called anywhere yet -- callers invoke it explicitly.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import REPO_ROOT


def _resolve(page_dir) -> Path:
    p = Path(page_dir)
    return p if p.is_absolute() else REPO_ROOT / p


def _manifest(page_dir) -> dict:
    d = _resolve(page_dir)
    return json.loads((d / "manifest.json").read_text(encoding="utf-8"))


def assemble(page_dir) -> str:
    """Concatenate page_dir's chunks (manifest order) into the skeleton."""
    d = _resolve(page_dir)
    manifest = _manifest(page_dir)
    return "".join((d / name).read_text(encoding="utf-8") for name in manifest["chunks"])


def _block(name: str, blob) -> str:
    """The exact block redlines.hydrate.inject() writes for window.__NAME__."""
    start, end = f"<!--{name}_DATA-->", f"<!--/{name}_DATA-->"
    blob_json = json.dumps(blob).replace("</", "<\\/")
    return f"{start}\n<script>window.__{name}__ = {blob_json};</script>\n{end}"


def hydrate_page(page_dir, blobs: dict) -> str:
    """assemble() page_dir's skeleton, then inject every manifest blob.

    Returns the final page text; writes nothing. Raises ValueError if a
    manifest blob name's marker pair isn't found in the assembled skeleton
    (e.g. a chunking bug dropped or duplicated it).
    """
    manifest = _manifest(page_dir)
    html = assemble(page_dir)
    for name in manifest["blobs"]:
        start, end = f"<!--{name}_DATA-->", f"<!--/{name}_DATA-->"
        block = _block(name, blobs[name])
        pattern = re.escape(start) + r".*?" + re.escape(end)
        html, n = re.subn(pattern, lambda m: block, html, count=1, flags=re.S)
        if n != 1:
            raise ValueError(
                f"hydrate_page({page_dir!r}): marker pair for {name!r} "
                f"not found exactly once in assembled skeleton (found {n})"
            )
    return html


def write_skeleton(page_dir, out_path=None) -> Path:
    """Write assemble(page_dir)'s output to the manifest's target page, with
    no blobs spliced in. Only writes when a caller explicitly asks."""
    manifest = _manifest(page_dir)
    target = Path(out_path) if out_path is not None else REPO_ROOT / manifest["target"]
    target.write_text(assemble(page_dir), encoding="utf-8")
    return target
