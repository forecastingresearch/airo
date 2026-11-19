"""Remove prediction market prices from the results of the research tools.

tool_cli.py applies clean() to each tool result when the environment sets
FORECASTER_BLOCK_MARKET_PRICES. The block of the market sites (tools.BLOCK_ENV)
is not sufficient: other sites repeat the prices. In a recorded run with the
market sites blocked (2026-11-18, 191 questions), a price got to the model for
144 questions. With this filter the number was 30.

The rule: when a text, its title or its URL is about a prediction market, the
filter removes each sentence and each line that has a price number. In a
sample of 320 sentences with labels, it removed 66 of the 69 that gave a market
price. It also removes other numbers on such a page, about 0.6% of all text.
It cannot find a price in words ("a heavy favourite") or a price on a page
that does not name a market. Standard library only.
"""
import re

ENV = "FORECASTER_BLOCK_MARKET_PRICES"
# A text is about a prediction market if it has one of these.
MARKET = re.compile(
    r"kalshi|polymarket|metaculus|manifold|predictit|good judgment|gjopen|"  # the name of a platform
    r"(?:prediction|betting|forecasting)[- ]?markets?|event contracts?|"       # a general term
    r"market (?:will )?resolves?|resolves? (?:to )?\W?(?:yes|no)\b|"           # contract words
    r"\b(?:yes|no)\W{0,3}[\d.]+\s?¢",                                           # a quote: "Yes 62¢"
    re.I)
# A number that can be a price: a percent, cents, or a dollar price below $1.
PRICE = re.compile(r"\d\s?(?:%|¢|percent\b|cents?\b)|\$0?\.\d\d\b", re.I)
# A text divides after each newline and after each sentence end.
SPLIT = re.compile(r"(?<=\n)|(?<=[.!?])(?=\s)")


def redact(text, context=""):
    """Remove each sentence or line that has a price number, if the text or
    its context (the title and the URL of the same result) is about a market."""
    if not MARKET.search(f"{text} {context}"):
        return text
    return "".join(unit for unit in SPLIT.split(text) if not PRICE.search(unit))


def clean(res):
    """A tool result without prediction market prices. web_search: drop a
    result when its title gives a price, and redact each snippet. read_page:
    redact the text."""
    if isinstance(res.get("results"), list):
        kept = []
        for r in res["results"]:
            title, context = r.get("title") or "", f"{r.get('title')} {r.get('url')} {r.get('content')}"
            if redact(title, context) == title:
                kept.append({**r, "content": redact(r.get("content") or "", context)})
        if res.get("answer"):   # the summary also used a dropped result: remove it, as the URL guard does
            res["answer"] = redact(res["answer"]) if len(kept) == len(res["results"]) else None
        res["results"] = kept
    elif res.get("text"):
        res["text"] = redact(res["text"], res.get("url") or "")
    return res
