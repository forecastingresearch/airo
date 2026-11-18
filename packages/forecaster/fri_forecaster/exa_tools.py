#!/usr/bin/env python3
"""The research tools on Exa instead of Tavily (2026-11-17).

Same two tools as tools.py -- `web_search` and `read_page` -- with the same
arguments and the same result shapes, so pi_tools.ts, the tool descriptions
and the evidence records do not change. tool_cli.py uses this module when the
environment sets FORECASTER_SEARCH=exa; the default is tools.py (Tavily).

Two additions to each result: `cost_usd`, Exa's own `costDollars.total` for
the request, so a run can total its search cost from its evidence; and, on
web_search, `excluded_domains` when the domain block below is on.

Blocking, both OFF unless the environment sets them, with the rules of tools.py:
  * FORECASTER_BLOCK_URL_PATTERNS (the URL guard): web_search drops matching
    results, read_page refuses a matching URL.
  * FORECASTER_BLOCK_DOMAINS (the domain block): the hosts go to Exa as
    `excludeDomains`, so blocked sites never take a result slot. web_search
    also drops a result on one of these hosts, and read_page refuses its URL.

Needs EXA_API_KEY; without it (or on any network error) a tool returns a
result carrying an `error` string, so the loop keeps going. Pure stdlib.
"""
import json
import os
import re
import urllib.error
import urllib.request
from datetime import date, timedelta

from fri_forecaster.tools import (BLOCK_TITLES, PAGE_CHARS, blocked_domains, blocked_patterns, domain_blocked,
                                  is_blocked)

EXA_SEARCH_URL = "https://api.exa.ai/search"
EXA_CONTENTS_URL = "https://api.exa.ai/contents"
SNIPPET_CHARS = 1500       # one result's `content`, as tools.py caps Tavily's
# Exa writes about 1000 characters for this (4 live searches, 2026-11-18). Tavily's `answer` is about 250.
ANSWER = "A short answer to the query, in two or three sentences."
PAGE_FETCH_CHARS = 200000  # the most of one page read_page asks Exa for


def _post(url, body, timeout):
    key = os.environ.get("EXA_API_KEY")
    if not key:
        raise LookupError("no EXA_API_KEY set")
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "x-api-key": key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _cost(data):
    return ((data or {}).get("costDollars") or {}).get("total")


def search_body(query, max_results=5, recent_days=None, today=None, exclude=()):
    """The Exa request for a search. With `recent_days`, news published in
    [today - recent_days, today], as tools.search_body bounds Tavily's."""
    body = {"query": query, "type": "auto", "numResults": max(1, min(int(max_results or 5), 10)),
            "contents": {"highlights": {"maxCharacters": SNIPPET_CHARS}}}
    if exclude:
        body["excludeDomains"] = list(exclude)
    if recent_days:
        today = today or date.today()
        days = max(1, min(int(recent_days), 3650))
        body.update({"category": "news",
                     "startPublishedDate": f"{(today - timedelta(days=days)).isoformat()}T00:00:00.000Z",
                     "endPublishedDate": f"{today.isoformat()}T23:59:59.999Z"})
    elif not exclude:   # as tools.py: no summary on a news search or with the domain block
        body["outputSchema"] = {"type": "text", "description": ANSWER}
    return body


def _snippet(r):
    hl = r.get("highlights") or []
    text = " … ".join(h for h in hl if isinstance(h, str)) if hl else (r.get("text") or "")
    return text[:SNIPPET_CHARS]


def web_search(query, max_results=5, recent_days=None, cause=None):
    """Search the web via Exa. Returns {query, answer, results:[{title, url,
    content, published?}], cost_usd}, the shape of tools.web_search. `answer`
    is the text that Exa writes from the results (`outputSchema`), with the
    rules of tools.py: a general search only, and None when the guard drops
    a result. The function removes Exa's citation marks ([1]): they refer to
    a source list that is not in the result."""
    tag = {"cause": cause} if cause is not None else {}
    exclude = blocked_domains()
    spec = search_body(query, max_results, recent_days, exclude=exclude)
    try:
        data = _post(EXA_SEARCH_URL, spec, timeout=45)
    except LookupError:
        return {"query": query, **tag, "results": [], "error": "web search unavailable (no EXA_API_KEY set)"}
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        return {"query": query, **tag, "results": [], "error": f"web search failed: {e}"}
    answer = re.sub(r" ?\[\d+\]", "", (data.get("output") or {}).get("content") or "")
    out = {"query": query, **tag, "answer": answer or None, "results": [], "cost_usd": _cost(data)}
    if exclude:
        out["excluded_domains"] = len(exclude)
    if recent_days:
        out["window"] = {"start": spec["startPublishedDate"][:10], "end": spec["endPublishedDate"][:10]}
    guard = blocked_patterns()
    for r in data.get("results") or []:
        if domain_blocked(r.get("url"), exclude) or (guard and (
                is_blocked(r.get("url"), guard) or any(t in (r.get("title") or "") for t in BLOCK_TITLES))):
            out["answer"] = None   # Exa wrote it from the results before the guard
            continue
        item = {"title": r.get("title"), "url": r.get("url"), "content": _snippet(r)}
        if r.get("publishedDate"):
            item["published"] = r["publishedDate"]
        out["results"].append(item)
    return out


def read_page(url, offset=0):
    """The text of one web page via Exa contents, as a window of PAGE_CHARS
    characters from `offset`: {url, offset, total_chars, text, next_offset?,
    cost_usd}, the shape of tools.read_page."""
    if domain_blocked(url, blocked_domains()) or is_blocked(url, blocked_patterns()):
        return {"url": url, "text": "", "error": "page read failed: page unavailable"}
    try:
        data = _post(EXA_CONTENTS_URL, {"urls": [url], "text": {"maxCharacters": PAGE_FETCH_CHARS}}, timeout=60)
    except LookupError:
        return {"url": url, "text": "", "error": "page reading unavailable (no EXA_API_KEY set)"}
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        return {"url": url, "text": "", "error": f"page read failed: {e}"}
    results = data.get("results") or []
    text = (results[0].get("text") or "") if results else ""
    if not text:
        st = [s for s in data.get("statuses") or [] if isinstance(s, dict)]
        why = ((st[0].get("error") or {}).get("tag") if st and isinstance(st[0].get("error"), dict)
               else (st[0].get("status") if st else None)) or "no content returned"
        return {"url": url, "text": "", "error": f"page read failed: {why}", "cost_usd": _cost(data)}
    try:
        start = max(0, int(offset or 0))
    except (TypeError, ValueError):
        start = 0
    out = {"url": url, "offset": start, "total_chars": len(text),
           "text": text[start:start + PAGE_CHARS], "cost_usd": _cost(data)}
    if start + PAGE_CHARS < len(text):
        out["next_offset"] = start + PAGE_CHARS
    return out
