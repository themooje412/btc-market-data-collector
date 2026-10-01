import csv
import json
import tempfile
import unittest
from pathlib import Path

from btc_collector.pnl_tracker import reconstruct, aggregate


class PnlTrackerTests(unittest.TestCase):
    def test_long_trigger_to_invalidation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fields = ["timestamp","asset","state","direction","fast_spot","hard_stop","risk_pct"]
            rows = [
                {"timestamp":"2026-09-25T00:00:00Z","asset":"BTC","state":"LONG_TRIGGERED","direction":"long","fast_spot":"100","hard_stop":"99","risk_pct":"1"},
                {"timestamp":"2026-09-25T02:00:00Z","asset":"BTC","state":"ADD_ALLOWED","direction":"long","fast_spot":"101"},
                {"timestamp":"2026-09-25T05:00:00Z","asset":"BTC","state":"INVALIDATED","direction":"long","fast_spot":"99"},
            ]
            with (root / "execution_history.csv").open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)
            trades = reconstruct("BTC", root)
            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0]["adds"], 1)
            self.assertAlmostEqual(trades[0]["pnl_pct"], -1.0)
            self.assertAlmostEqual(trades[0]["r_multiple"], -1.0)

    def test_aggregate(self):
        trades = [
            {"status":"CLOSED","exit_time":"2026-09-25T00:00:00Z","pnl_pct":2.0,"r_multiple":1.0},
            {"status":"CLOSED","exit_time":"2026-09-26T00:00:00Z","pnl_pct":-1.0,"r_multiple":-0.5},
        ]
        result = aggregate(trades)["all"]
        self.assertEqual(result["closed_trades"], 2)
        self.assertEqual(result["win_rate_pct"], 50.0)
        self.assertEqual(result["sum_signal_return_pct"], 1.0)


if __name__ == "__main__":
    unittest.main()
