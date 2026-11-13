"""The page from its parts: web/'s chunks in manifest order, with the data spliced in.

web/manifest.json names the chunks and the data blobs. A blob NAME goes where
the page has the marker pair <!--NAME_DATA--><!--/NAME_DATA-->, as
window.__NAME__. (AIRO's page is assembled the same way, by its own code.)
"""
import json
import re
from pathlib import Path


def hydrate_page(page_dir, blobs):
    """-> the page's text. Raises ValueError unless each blob's marker pair is there exactly once."""
    page_dir = Path(page_dir)
    manifest = json.loads((page_dir / "manifest.json").read_text(encoding="utf-8"))
    html = "".join((page_dir / name).read_text(encoding="utf-8") for name in manifest["chunks"])
    for name in manifest["blobs"]:
        start, end = f"<!--{name}_DATA-->", f"<!--/{name}_DATA-->"
        # "</" is escaped so a string in the data cannot close the script element.
        data = json.dumps(blobs[name]).replace("</", "<\\/")
        block = f"{start}\n<script>window.__{name}__ = {data};</script>\n{end}"
        html, n = re.subn(re.escape(start) + r".*?" + re.escape(end), lambda m: block, html, count=1, flags=re.S)
        if n != 1:
            raise ValueError(f"{page_dir}: the marker pair for {name!r} is not in the page exactly once (found {n})")
    return html
