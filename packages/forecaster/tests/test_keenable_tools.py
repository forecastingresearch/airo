"""keenable_tools: the Keenable backend for web_search / read_page. No network:
Keenable's replies are stubbed."""
import json
import os
import unittest
from datetime import date
from unittest import mock

from fri_forecaster import keenable_tools, tool_cli, tools

SEARCH_REPLY = {"results": [
    {"title": "A", "url": "https://a.org/1", "snippet": "one", "published_at": "2026-11-16T00:00:00Z"},
    {"title": "B", "url": "https://blocked.org/2", "snippet": "body"},
    {"title": "C", "url": "https://www.kalshi.com/3", "snippet": "body"},
], "usage": {"sku": "search.pro", "credits": 1}}


class Keenable(unittest.TestCase):
    def test_search_body(self):
        b = keenable_tools.search_body("q", 50, recent_days=7, today=date(2026, 11, 17))
        self.assertEqual((b["max_results"], b["published_after"], b["published_before"], b["disable_cards"]),
                         (10, "2026-11-10", "2026-11-17", True))
        self.assertNotIn("published_after", keenable_tools.search_body("q"))

    def test_web_search_shape_and_guard(self):
        env = {tools.BLOCK_ENV: r"blocked\.org", tools.BLOCK_DOMAINS_ENV: "kalshi.com"}
        with mock.patch.dict(os.environ, env), mock.patch.object(keenable_tools, "_call", return_value=SEARCH_REPLY) as call:
            out = keenable_tools.web_search("q", cause="g")
        self.assertEqual(call.call_args[0][2]["exclude_domains"], ["kalshi.com"])
        self.assertEqual(out["results"], [{"title": "A", "url": "https://a.org/1", "content": "one",
                                           "published": "2026-11-16T00:00:00Z"}])
        self.assertEqual((out["answer"], out["cause"], out["credits"]), (None, "g", 1))

    def test_read_page_windows_and_guard(self):
        text = "x" * (tools.PAGE_CHARS + 10)
        with mock.patch.dict(os.environ, {tools.BLOCK_DOMAINS_ENV: "kalshi.com"}), \
                mock.patch.object(keenable_tools, "_call", return_value={"content": text}) as call:
            first = keenable_tools.read_page("https://a.org")
            second = keenable_tools.read_page("https://a.org", offset=first["next_offset"])
            blocked = keenable_tools.read_page("https://kalshi.com/markets/x")
            call.return_value = {"content": text, "url": "https://kalshi.com/markets/x"}
            moved = keenable_tools.read_page("https://short.example/1")      # a redirect to a blocked host
        self.assertEqual((len(first["text"]), first["total_chars"], second["text"]), (tools.PAGE_CHARS, len(text), "x" * 10))
        self.assertNotIn("next_offset", second)
        self.assertEqual(blocked["error"], "page read failed: page unavailable")
        self.assertEqual(moved["error"], blocked["error"])
        self.assertEqual(call.call_count, 3)                       # the blocked page: no request

    def test_a_key_changes_the_path_and_the_header(self):
        reply = mock.MagicMock()
        reply.__enter__.return_value.read.return_value = b"{}"
        for key, path, header in (("", "/v1/fetch/public?url=", "X-keenable-title"), ("k", "/v1/fetch?url=", "X-api-key")):
            with mock.patch.dict(os.environ, {"KEENABLE_API_KEY": key}), \
                    mock.patch("urllib.request.urlopen", return_value=reply) as urlopen:
                keenable_tools._call("fetch", 5, url="https://a.org")
            req = urlopen.call_args[0][0]
            self.assertIn(path, req.full_url)
            self.assertTrue(req.has_header(header))
        with mock.patch("urllib.request.urlopen", return_value=reply) as urlopen:
            keenable_tools._call("search", 5, {"query": "q"})
        self.assertEqual(json.loads(urlopen.call_args[0][0].data), {"query": "q"})   # a search is a POST of the body

    def test_it_is_a_backend(self):
        with mock.patch.dict(os.environ, {tool_cli.SEARCH_ENV: "keenable"}):
            self.assertIs(tool_cli.backend(), keenable_tools)


if __name__ == "__main__":
    unittest.main()
