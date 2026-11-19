"""price_filter: research results without prediction market prices."""
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from fri_forecaster import price_filter, tool_cli

PRICED = "Kalshi traders see a rate increase. The price is 86.3%.\nThe Fed meets on Dec. 9."


class PriceFilter(unittest.TestCase):
    def test_redact(self):
        self.assertEqual(price_filter.redact(PRICED), "Kalshi traders see a rate increase.\nThe Fed meets on Dec. 9.")
        plain = "Inflation was 3.2% in October."
        self.assertEqual(price_filter.redact(plain), plain)                         # no market: no change
        self.assertEqual(price_filter.redact(plain, "polymarket.com/event/x"), "")   # the context is a market

    def test_clean(self):
        search = {"answer": "A summary.", "results": [
            {"title": "Fed minutes", "url": "https://a.org/1", "content": PRICED},
            {"title": "Kalshi puts a rate increase at 86.3%", "url": "https://b.org/2", "content": "x"}]}
        out = price_filter.clean(search)
        self.assertEqual([r["url"] for r in out["results"]], ["https://a.org/1"])   # the title gave a price
        self.assertNotIn("86.3", out["results"][0]["content"])
        self.assertIsNone(out["answer"])
        page = price_filter.clean({"url": "https://a.org/1", "text": PRICED})
        self.assertNotIn("86.3", page["text"])

    def test_tool_cli_applies_it_only_with_the_setting(self):
        for setting, has_price in (("", True), ("1", False)):
            out = io.StringIO()
            with mock.patch.dict(os.environ, {price_filter.ENV: setting}), \
                    mock.patch.dict(tool_cli.FN, {"read_page": lambda url: {"url": url, "text": PRICED}}), \
                    mock.patch("sys.argv", ["tool_cli.py", "read_page", '{"url": "https://a.org/1"}']), \
                    mock.patch.object(tool_cli, "load_keys"), contextlib.redirect_stdout(out):
                tool_cli.main()
            self.assertEqual("86.3" in json.loads(out.getvalue())["text"], has_price)


if __name__ == "__main__":
    unittest.main()
