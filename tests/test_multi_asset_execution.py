import unittest

from btc_collector.multi_asset_execution import (
    A_PLUS_EARLY_MIN_RR,
    CORE_BIAS_SCORE,
    MAX_LEVEL_DISTANCE_PCT,
    NORMAL_MIN_RR,
    PROFILE,
    build_plan,
    classify_state,
    default_root,
    flow_score,
    nearest_cluster,
)


def metric(value, status="ok"):
    return {"value": value, "status": status, "timestamp": "2026-09-24T07:00:00.000Z"}


def snapshot(asset="SOL"):
    return {
        "schema_version": "1.0.0-multi-asset",
        "asset": asset,
        "timestamp": "2026-09-24T07:00:00.000Z",
        "spot": {"binance": metric(100.0)},
        "market_structure": {
            "utc_session": {
                "vwap": metric(99.5), "poc": metric(100.0), "vah": metric(101.0),
                "val": metric(98.5), "spot_location": metric("inside_value"),
            },
            "rolling_24h": {
                "vwap": metric(98.5), "poc": metric(99.5), "vah": metric(100.9),
                "val": metric(97.5), "spot_location": metric("above_value"),
            },
            "rolling_7d": {
                "vwap": metric(97.0), "poc": metric(98.0), "vah": metric(101.1),
                "val": metric(96.0), "spot_location": metric("above_value"),
            },
        },
        "cvd": {
            "binance": {
                "1h": {"status": "ok", "value": 1.0, "quote_cvd": metric(500_000)},
                "4h": {"status": "ok", "value": 1.0, "quote_cvd": metric(1_000_000)},
            },
            "coinbase": {
                "1h": {"status": "ok", "value": 1.0, "quote_cvd": metric(300_000)},
                "4h": {"status": "ok", "value": 1.0, "quote_cvd": metric(800_000)},
            },
        },
        "fx_adjusted_coinbase_premium": {"bps": metric(0.2)},
        "options": {
            "zero_gamma_flip_cumulative_strike": metric(99.0),
            "zero_gamma_flip_repriced": metric(90.0),
            "gamma_regime": metric("short_gamma"),
        },
        "call_wall": metric(102.0),
        "put_wall": metric(97.0),
    }


def fast(cvd15=10_000, cvd1h=0, fx=0.0, spot=100.2, candles=None, candles15=None, asset="SOL"):
    if candles is None:
        candles = [
            {"open": 100.18, "high": 100.24, "low": 100.14, "close": 100.20, "quote_volume": 1_000_000},
            {"open": 100.20, "high": 100.25, "low": 100.16, "close": 100.21, "quote_volume": 1_000_000},
        ]
    return {
        "schema_version": "1.2.0",
        "asset": asset,
        "timestamp": "2026-09-24T07:10:00.000Z",
        "spot": {"binance": metric(spot)},
        "binance_cvd": {
            "15m": {"status": "ok", "value": 1.0, "quote_cvd": metric(cvd15)},
            "1h": {"status": "ok", "value": 1.0, "quote_cvd": metric(cvd1h)},
        },
        "premium": {"fx_adjusted": {"bps": metric(fx)}},
        "candles_5m": candles,
        "candles_15m": candles15 or [],
    }


class MultiAssetExecutionTests(unittest.TestCase):
    def test_plan_exposes_plus30_policy_and_core_candidate(self):
        plan = build_plan(snapshot("SOL"), "SOL")
        self.assertEqual(plan["asset"], "SOL")
        self.assertEqual(plan["profile"], PROFILE)
        self.assertEqual(plan["rules"]["normal_min_rr"], NORMAL_MIN_RR)
        self.assertEqual(plan["rules"]["a_plus_early_probe_min_rr"], A_PLUS_EARLY_MIN_RR)
        self.assertEqual(plan["rules"]["core_bias_score_threshold"], CORE_BIAS_SCORE)
        self.assertEqual(plan["core_swing"]["direction"], "long")
        self.assertEqual(plan["core_swing"]["status"], "TREND_ACTIVE")

    def test_flow_score_is_cross_asset_volume_normalized(self):
        candles = [
            {"quote_volume": 1_000_000, "open": 1, "high": 1, "low": 1, "close": 1}
            for _ in range(12)
        ]
        market = fast(cvd15=150_000, cvd1h=200_000, candles=candles)
        self.assertEqual(flow_score(market), 3)

    def test_aligned_early_setup_needs_only_score_one_near_structure(self):
        plan = build_plan(snapshot("ETH"), "ETH")
        market = fast(cvd15=10_000, cvd1h=0, spot=100.2, asset="ETH")
        anchor = nearest_cluster(plan, 100.2)
        self.assertIsNotNone(anchor)
        self.assertLessEqual(abs(anchor["value"] - 100.2) / 100.2, MAX_LEVEL_DISTANCE_PCT)
        state = classify_state(plan, market)
        self.assertEqual(state["state"], "EARLY_SETUP")
        self.assertEqual(state["direction"], "long")
        self.assertEqual(state["bias_alignment"], "aligned")
        self.assertEqual(state["asset"], "ETH")

    def test_countertrend_is_not_relaxed_by_plus30_profile(self):
        plan = build_plan(snapshot("ZEC"), "ZEC")
        anchor = nearest_cluster(plan, 100.0)
        level = anchor["value"]
        candles = [
            {"open": level + 0.20, "high": level + 0.25, "low": level + 0.12, "close": level + 0.18, "quote_volume": 1_000_000},
            {"open": level + 0.10, "high": level + 0.20, "low": level - 0.30, "close": level - 0.20, "quote_volume": 1_000_000},
        ]
        market = fast(cvd15=-20_000, cvd1h=0, fx=0.0, spot=level - 0.20, candles=candles, asset="ZEC")
        state = classify_state(plan, market)
        self.assertEqual(state["state"], "NO_TRADE")
        self.assertIsNone(state["direction"])
        self.assertIn("counter-trend", state["reason"])

    def test_default_roots_keep_asset_state_isolated(self):
        self.assertEqual(str(default_root("BTC")), ".")
        self.assertEqual(str(default_root("ETH")), "assets/eth")
        self.assertEqual(str(default_root("SOL")), "assets/sol")
        self.assertEqual(str(default_root("ZEC")), "assets/zec")


if __name__ == "__main__":
    unittest.main()
