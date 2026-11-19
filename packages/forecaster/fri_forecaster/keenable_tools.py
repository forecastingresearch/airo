#!/usr/bin/env python3
"""The research tools on Keenable (2026-11-18).

Same two tools as tools.py -- `web_search` and `read_page` -- with the same
arguments and the same result shapes. tool_cli.py uses this module when the
environment sets FORECASTER_SEARCH=keenable.

Keenable works without a key, with a limit of 1,000 requests an hour for each
IP address. With KEENABLE_API_KEY there is no hourly limit, and each result
has `credits`, the credits that Keenable billed for the request.

Keenable writes no summary of a search, so `answer` is always None.

Blocking: the rules of exa_tools.py. The hosts of the domain block go to
Keenable as `exclude_domains`. read_page also refuses a page when the address
after redirects is blocked. A search result on a host that redirects to a
blocked host is not dropped.

Keenable's `published_at` is sometimes the date of a later change to a page.
Thus `recent_days` and `published` are less exact than on Tavily.

On an error a tool returns a result with an `error` string, so the loop
continues. Pure stdlib.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from fri_forecaster.exa_tools import PAGE_FETCH_CHARS, SNIPPET_CHARS
from fri_forecaster.tools import (BLOCK_TITLES, PAGE_CHARS, blocked_domains, blocked_patterns, domain_blocked,
                                  is_blocked)

KEENABLE_URL = "https://api.keenable.ai/v1/"
APP = "research-tools"   # a call without a key must give the name of its application


def _call(path, timeout, body=None, **params):
    """One Keenable request: a POST of `body`, or a GET with `params`.
    Without a key, the request goes to the /public path."""
    key = os.environ.get("KEENABLE_API_KEY")
    url = KEENABLE_URL + path + ("" if key else "/public") + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json",
                                          **({"X-API-Key": key} if key else {"X-Keenable-Title": APP})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _credits(data):
    return ((data or {}).get("usage") or {}).get("credits")


def search_body(query, max_results=5, recent_days=None, today=None, exclude=()):
    """The Keenable request for a search. With `recent_days`, the request asks
    for pages published from today - recent_days to today. tools.search_body
    sets the same limits for Tavily."""
    body = {"query": query, "max_results": max(1, min(int(max_results or 5), 10)),
            "snippet_max_length": SNIPPET_CHARS,
            "disable_cards": True}   # a card is a page that Keenable writes; its URL works for one day only
    if exclude:
        body["exclude_domains"] = list(exclude)
    if recent_days:
        today = today or datetime.now(timezone.utc).date()   # Keenable reads a date as a UTC day
        days = max(1, min(int(recent_days), 3650))
        body.update({"published_after": (today - timedelta(days=days)).isoformat(),
                     "published_before": today.isoformat()})
    return body


def web_search(query, max_results=5, recent_days=None, cause=None):
    """Search the web via Keenable. Returns {query, answer, results:[{title,
    url, content, published?}], credits}, the shape of tools.web_search."""
    tag = {"cause": cause} if cause is not None else {}
    exclude = blocked_domains()
    spec = search_body(query, max_results, recent_days, exclude=exclude)
    try:
        data = _call("search", 45, spec)
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        return {"query": query, **tag, "results": [], "error": f"web search failed: {e}"}
    out = {"query": query, **tag, "answer": None, "results": [], "credits": _credits(data)}
    if exclude:
        out["excluded_domains"] = len(exclude)
    if recent_days:
        out["window"] = {"start": spec["published_after"], "end": spec["published_before"]}
    guard = blocked_patterns()
    for r in data.get("results") or []:
        if domain_blocked(r.get("url"), exclude) or (guard and (
                is_blocked(r.get("url"), guard) or any(t in (r.get("title") or "") for t in BLOCK_TITLES))):
            continue
        item = {"title": r.get("title"), "url": r.get("url"), "content": (r.get("snippet") or "")[:SNIPPET_CHARS]}
        if r.get("published_at"):
            item["published"] = r["published_at"]
        out["results"].append(item)
    return out


def read_page(url, offset=0):
    """The text of one web page via Keenable fetch, as a window of PAGE_CHARS
    characters from `offset`: {url, offset, total_chars, text, next_offset?,
    credits}, the shape of tools.read_page."""
    if domain_blocked(url, blocked_domains()) or is_blocked(url, blocked_patterns()):
        return {"url": url, "text": "", "error": "page read failed: page unavailable"}
    try:
        data = _call("fetch", 60, url=url, max_chars=PAGE_FETCH_CHARS)
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        return {"url": url, "text": "", "error": f"page read failed: {e}"}
    final = data.get("url") or url   # the address after redirects
    if domain_blocked(final, blocked_domains()) or is_blocked(final, blocked_patterns()):
        return {"url": url, "text": "", "error": "page read failed: page unavailable"}
    text = data.get("content") or ""
    if not text:
        return {"url": url, "text": "", "error": "page read failed: no content returned"}
    try:
        start = max(0, int(offset or 0))
    except (TypeError, ValueError):
        start = 0
    out = {"url": url, "offset": start, "total_chars": len(text),
           "text": text[start:start + PAGE_CHARS], "credits": _credits(data)}
    if start + PAGE_CHARS < len(text):
        out["next_offset"] = start + PAGE_CHARS
    return out
