"""exa_tools: the Exa backend for web_search / read_page, and tool_cli's choice
of backend. No network: Exa's replies are stubbed."""
import os
import unittest
from datetime import date
from unittest import mock

from fri_forecaster import exa_tools, tool_cli, tools


class SearchBody(unittest.TestCase):
    def test_plain(self):
        b = exa_tools.search_body("q", 3)
        self.assertEqual(b["numResults"], 3)
        self.assertNotIn("startPublishedDate", b)
        self.assertNotIn("excludeDomains", b)
        self.assertEqual(b["outputSchema"]["type"], "text")        # a general search asks for the summary

    def test_recent_and_excluded(self):
        b = exa_tools.search_body("q", 50, recent_days=7, today=date(2026, 11, 17), exclude=["kalshi.com"])
        self.assertEqual(b["numResults"], 10)
        self.assertEqual(b["category"], "news")
        self.assertEqual(b["startPublishedDate"], "2026-11-10T00:00:00.000Z")
        self.assertEqual(b["endPublishedDate"], "2026-11-17T23:59:59.999Z")
        self.assertEqual(b["excludeDomains"], ["kalshi.com"])
        self.assertNotIn("outputSchema", b)                        # as Tavily: no summary here


SEARCH_REPLY = {"results": [
    {"title": "A", "url": "https://a.org/1", "highlights": ["one", "two"], "publishedDate": "2026-11-16T00:00:00.000Z"},
    {"title": "B", "url": "https://blocked.org/2", "text": "body"},
    {"title": "C", "url": "https://www.kalshi.com/3", "text": "body"},
], "costDollars": {"total": 0.007}}


class WebSearch(unittest.TestCase):
    def test_shape_cost_and_guard(self):
        env = {"EXA_API_KEY": "k", tools.BLOCK_ENV: r"blocked\.org", tools.BLOCK_DOMAINS_ENV: "kalshi.com"}
        with mock.patch.dict(os.environ, env), mock.patch.object(exa_tools, "_post", return_value=SEARCH_REPLY) as post:
            out = exa_tools.web_search("q", 5, recent_days=3, cause="g")
        body = post.call_args[0][1]
        self.assertEqual(body["excludeDomains"], ["kalshi.com"])
        self.assertEqual(out["cause"], "g")
        self.assertEqual(out["cost_usd"], 0.007)
        self.assertEqual([r["url"] for r in out["results"]], ["https://a.org/1"])
        self.assertEqual(out["results"][0]["content"], "one … two")
        self.assertEqual(out["results"][0]["published"], "2026-11-16T00:00:00.000Z")
        self.assertIn("window", out)

    def test_answer(self):
        reply = dict(SEARCH_REPLY, output={"content": "A summary [1][2]."})
        with mock.patch.dict(os.environ, {"EXA_API_KEY": "k"}), mock.patch.object(exa_tools, "_post", return_value=reply):
            self.assertEqual(exa_tools.web_search("q")["answer"], "A summary.")   # no citation marks
            with mock.patch.dict(os.environ, {tools.BLOCK_ENV: r"blocked\.org"}):
                self.assertIsNone(exa_tools.web_search("q")["answer"])     # the guard dropped a result

    def test_no_key(self):
        with mock.patch.dict(os.environ, {"EXA_API_KEY": ""}):
            out = exa_tools.web_search("q")
        self.assertEqual(out["results"], [])
        self.assertIn("EXA_API_KEY", out["error"])


class ReadPage(unittest.TestCase):
    def test_windows(self):
        text = "x" * (tools.PAGE_CHARS + 10)
        reply = {"results": [{"text": text}], "costDollars": {"total": 0.001}}
        with mock.patch.dict(os.environ, {"EXA_API_KEY": "k"}), mock.patch.object(exa_tools, "_post", return_value=reply):
            first = exa_tools.read_page("https://a.org")
            second = exa_tools.read_page("https://a.org", offset=first["next_offset"])
        self.assertEqual(len(first["text"]), tools.PAGE_CHARS)
        self.assertEqual(first["total_chars"], len(text))
        self.assertEqual(second["text"], "x" * 10)
        self.assertNotIn("next_offset", second)
        self.assertEqual(second["cost_usd"], 0.001)

    def test_excluded_domain_never_fetched(self):
        with mock.patch.dict(os.environ, {"EXA_API_KEY": "k", tools.BLOCK_DOMAINS_ENV: "kalshi.com"}), \
                mock.patch.object(exa_tools, "_post") as post:
            out = exa_tools.read_page("https://kalshi.com/markets/x")
        post.assert_not_called()
        self.assertEqual(out["error"], "page read failed: page unavailable")

    def test_failed_status(self):
        reply = {"results": [], "statuses": [{"id": "u", "status": "error", "error": {"tag": "CRAWL_NOT_FOUND"}}]}
        with mock.patch.dict(os.environ, {"EXA_API_KEY": "k"}), mock.patch.object(exa_tools, "_post", return_value=reply):
            out = exa_tools.read_page("https://a.org")
        self.assertEqual(out["error"], "page read failed: CRAWL_NOT_FOUND")


class Backend(unittest.TestCase):
    def test_choice(self):
        with mock.patch.dict(os.environ, {tool_cli.SEARCH_ENV: ""}):
            self.assertIs(tool_cli.backend(), tools)                 # the default is Tavily
        with mock.patch.dict(os.environ, {tool_cli.SEARCH_ENV: "Exa"}), \
                mock.patch.object(exa_tools, "web_search", return_value="s"), \
                mock.patch.object(exa_tools, "read_page", return_value="p"):
            self.assertEqual(tool_cli.FN["web_search"](query="q"), "s")       # the two tools go to Exa
            self.assertEqual(tool_cli.FN["read_page"]("https://a.org/1"), "p")
        with mock.patch.dict(os.environ, {tool_cli.SEARCH_ENV: "bing"}), self.assertRaises(SystemExit):
            tool_cli.backend()


if __name__ == "__main__":
    unittest.main()
