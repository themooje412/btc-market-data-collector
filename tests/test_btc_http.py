import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from btc_collector.http import Client, FetchError


class HttpTests(unittest.TestCase):
    def fake_curl(self, status, body, headers=""):
        def run(args, **kwargs):
            Path(args[args.index("--dump-header") + 1]).write_text(
                "HTTP/1.1 200 Connection established\r\n\r\nHTTP/2 "
                + str(status)
                + "\r\n"
                + headers
                + "\r\n"
            )
            Path(args[args.index("--output") + 1]).write_text(body)
            return SimpleNamespace(returncode=0, stdout=str(status), stderr="")

        return run

    def test_success_parses_json_and_pagination_headers(self):
        with patch(
            "btc_collector.http.subprocess.run",
            side_effect=self.fake_curl(200, '[{"side":"sell"}]', "CB-AFTER: 123\r\n"),
        ):
            data, headers, _ = Client().get(
                "https://api.exchange.coinbase.com", "/products/BTC-USD/trades"
            )
            self.assertEqual(headers["cb-after"], "123")
            self.assertEqual(data[0]["side"], "sell")

    def test_geo_denial_is_not_retried_or_bypassed(self):
        client = Client()
        with patch(
            "btc_collector.http.subprocess.run", side_effect=self.fake_curl(451, "{}")
        ) as run:
            with self.assertRaises(FetchError):
                client.get("https://fapi.binance.com", "/fapi/v1/openInterest")
            with self.assertRaises(FetchError):
                client.get("https://fapi.binance.com", "/fapi/v1/premiumIndex")
            self.assertEqual(run.call_count, 1)

    def test_long_retry_after_defers_instead_of_hammering(self):
        client = Client()
        with patch(
            "btc_collector.http.subprocess.run",
            side_effect=self.fake_curl(429, "{}", "Retry-After: 120\r\n"),
        ) as run:
            with self.assertRaises(FetchError):
                client.get(
                    "https://api.exchange.coinbase.com", "/products/BTC-USD/trades"
                )
            self.assertEqual(run.call_count, 1)

    def test_json_rpc_error_never_becomes_valid_data(self):
        client = Client(attempts=1)
        with patch(
            "btc_collector.http.subprocess.run",
            side_effect=self.fake_curl(200, '{"error":{"code":10028}}'),
        ):
            with self.assertRaises(FetchError):
                client.get("https://www.deribit.com/api/v2", "/public/ticker")
        self.assertEqual(client.audit[0]["status"], "error")
