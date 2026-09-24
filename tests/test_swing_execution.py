import unittest

from btc_collector import swing_execution as swing


def metric(value, status="ok"):
    return {"value": value, "status": status, "timestamp": "2026-09-24T15:00:00.000Z"}


def plan(asset="ETH"):
    return {
        "schema_version": swing.SCHEMA_VERSION,
        "asset": asset,
        "profile": swing.PROFILE,
        "context_timestamp": "2026-09-24T15:00:00.000Z",
        "strategic_bias": {"value": "bullish", "score": 7.0},
        "gamma_regime": "short_gamma",
        "rules": {"normal_min_rr": 1.60},
        "levels": [
            {"value": 1500.0, "id": "24h_vwap", "labels": ["24h_vwap"]},
            {"value": 1518.97, "id": "session_poc", "labels": ["session_poc"]},
            {"value": 1528.02, "id": "session_vah", "labels": ["session_vah"]},
        ],
        "core_swing": {"status": "TREND_ACTIVE", "direction": "long"},
    }


def fast(spot=1518.97, cvd15=100_000, cvd1h=100_000, asset="ETH", last_close=None):
    last_close = spot if last_close is None else last_close
    candles = []
    for i in range(12):
        close = last_close if i == 11 else spot
        candles.append({
            "open": close,
            "high": close * 1.0004,
            "low": close * 0.9996,
            "close": close,
            "quote_volume": 1_000_000,
        })
    return {
        "schema_version": swing.SCHEMA_VERSION,
        "asset": asset,
        "timestamp": "2026-09-24T15:10:00.000Z",
        "spot": {"binance": metric(spot)},
        "binance_cvd": {
            "15m": {"quote_cvd": metric(cvd15)},
            "1h": {"quote_cvd": metric(cvd1h)},
        },
        "premium": {"fx_adjusted": {"bps": metric(0.0)}},
        "candles_5m": candles,
        "candles_15m": [
            {"open": spot, "high": spot * 1.001, "low": spot * 0.999, "close": last_close}
        ],
    }


class SwingExecutionTests(unittest.TestCase):
    def setUp(self):
        swing.install_policy()

    def test_eth_geometry_cannot_use_scalp_sized_hard_stop(self):
        p = plan("ETH")
        market = fast(1518.97, asset="ETH")
        anchor = {"value": 1518.97, "id": "session_poc", "labels": ["session_poc"]}
        geo = swing.swing_trade_geometry(p, market, anchor, "long")
        self.assertIsNotNone(geo)
        self.assertGreaterEqual(geo["risk_pct"], 0.85)
        self.assertLessEqual(geo["hard_stop"], 1518.97 * (1 - 0.0085) + 1e-6)
        self.assertEqual(geo["checkpoint1"], 1528.02)
        self.assertNotEqual(geo["target1"], 1528.02)
        self.assertGreaterEqual(geo["rr_to_target1"], 1.60)
        self.assertIn(geo["target1_type"], ("structural", "open_path_2r_projection"))

    def test_local_anchor_loss_does_not_kill_open_swing_before_hard_stop(self):
        p = plan("ETH")
        market = fast(1515.0, cvd15=-100_000, cvd1h=0, asset="ETH", last_close=1515.0)
        previous = {
            "schema_version": swing.SCHEMA_VERSION,
            "asset": "ETH",
            "state": "LONG_TRIGGERED",
            "direction": "long",
            "setup_type": "breakout-retest/trend-pullback",
            "event": "retest_hold_long",
            "anchor": {"value": 1518.97, "id": "session_poc", "labels": ["session_poc"]},
            "flow_score": 1,
            "strategic_bias": "bullish",
            "strategic_bias_score": 7.0,
            "bias_alignment": "aligned",
            "core_swing": {"status": "TREND_ACTIVE", "direction": "long"},
            "fast_timestamp": "2026-09-24T15:05:00.000Z",
            "fast_spot": 1518.97,
            "execution": {"entry_reference": 1518.97, "hard_stop": 1506.0},
            "signature": "ETH|LONG_TRIGGERED|long|session_poc|breakout-retest/trend-pullback|bullish",
        }
        state = swing.swing_classify_state(p, market, previous)
        self.assertEqual(state["state"], "HOLD_MANAGE")
        self.assertEqual(state["execution"]["hard_stop"], 1506.0)
        self.assertIn("hard stop has not been breached", state["reason"])

    def test_core_hard_stop_breach_still_invalidates(self):
        p = plan("ETH")
        market = fast(1505.5, cvd15=-100_000, cvd1h=-100_000, asset="ETH", last_close=1505.5)
        previous = {
            "schema_version": swing.SCHEMA_VERSION,
            "asset": "ETH",
            "state": "LONG_TRIGGERED",
            "direction": "long",
            "setup_type": "breakout-retest/trend-pullback",
            "event": "retest_hold_long",
            "anchor": {"value": 1518.97, "id": "session_poc", "labels": ["session_poc"]},
            "flow_score": 1,
            "strategic_bias": "bullish",
            "strategic_bias_score": 7.0,
            "bias_alignment": "aligned",
            "core_swing": {"status": "TREND_ACTIVE", "direction": "long"},
            "fast_timestamp": "2026-09-24T15:05:00.000Z",
            "fast_spot": 1518.97,
            "execution": {"entry_reference": 1518.97, "hard_stop": 1506.0},
            "signature": "ETH|LONG_TRIGGERED|long|session_poc|breakout-retest/trend-pullback|bullish",
        }
        state = swing.swing_classify_state(p, market, previous)
        self.assertEqual(state["state"], "INVALIDATED")
        self.assertEqual(state["reason"], "Core swing hard stop breached")


if __name__ == "__main__":
    unittest.main()
