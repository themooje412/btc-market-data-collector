import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from btc_collector.bybit_demo import quantize_down
from btc_collector.bybit_demo_executor import (
    MIN_SWING_RISK_PCT,
    execute_asset,
    signal_key,
    size_quantity,
    validate_signal,
)


def fresh_state(asset="BTC", state="LONG_TRIGGERED", direction="long", risk_pct=None):
    risk_pct = risk_pct if risk_pct is not None else MIN_SWING_RISK_PCT[asset]
    entry = 100.0
    stop = entry * (1 - risk_pct / 100) if direction == "long" else entry * (1 + risk_pct / 100)
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": "1.3.0",
        "asset": asset,
        "state": state,
        "direction": direction,
        "signature": f"{asset}|{state}|{direction}|x",
        "state_changed_at": ts,
        "fast_timestamp": ts,
        "execution": {
            "entry_reference": entry,
            "hard_stop": stop,
            "hard_stop_type": "core_swing_structure_volatility",
            "risk_pct": risk_pct,
        },
    }


class FakeClient:
    def __init__(self):
        self.position = None
        self.orders = []

    def get_position(self, symbol):
        return self.position

    def get_instrument(self, symbol):
        return {"lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"}}

    def place_market_order(self, symbol, side, qty, order_link_id, stop_loss=None, reduce_only=False):
        self.orders.append({
            "symbol": symbol, "side": side, "qty": qty, "order_link_id": order_link_id,
            "stop_loss": stop_loss, "reduce_only": reduce_only,
        })
        return {"retCode": 0, "result": {"orderId": f"oid-{len(self.orders)}"}}


class BybitDemoExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history_patch = patch(
            "btc_collector.bybit_demo_executor.HISTORY_PATH",
            Path(self.tmp.name) / "history.csv",
        )
        self.history_patch.start()

    def tearDown(self):
        self.history_patch.stop()
        self.tmp.cleanup()

    def test_quantize_down(self):
        self.assertEqual(quantize_down(1.2349, "0.001"), "1.234")
        self.assertEqual(quantize_down(0.019, "0.01"), "0.01")

    def test_rejects_old_schema(self):
        state = fresh_state()
        state["schema_version"] = "1.2.0"
        ok, reason = validate_signal(state)
        self.assertFalse(ok)
        self.assertEqual(reason, "schema_before_1.3")

    def test_rejects_early_and_armed_states(self):
        for state_name in ("EARLY_SETUP", "ARMED_LONG"):
            state = fresh_state(state=state_name)
            ok, reason = validate_signal(state)
            self.assertFalse(ok)
            self.assertEqual(reason, "non_actionable_state")

    def test_rejects_scalp_like_stop(self):
        state = fresh_state(asset="ZEC", risk_pct=0.2)
        ok, reason = validate_signal(state)
        self.assertFalse(ok)
        self.assertEqual(reason, "scalp_stop_rejected")

    def test_position_size_is_risk_based_and_notional_capped(self):
        qty = size_quantity(
            equity=1000,
            risk_r=0.5,
            entry=100,
            stop=99,
            qty_step="0.01",
            min_qty=0.01,
            account_r_pct=0.01,
            max_notional_multiple=2.0,
        )
        self.assertEqual(qty, "5.00")

    def test_trigger_opens_demo_order_with_exchange_stop(self):
        client = FakeClient()
        ledger = {"mode": "bybit_demo", "assets": {}}
        state = fresh_state("BTC")
        result = execute_asset("BTC", state, client, ledger, equity=1000, account_r_pct=0.01)
        self.assertEqual(result["status"], "open_sent")
        self.assertEqual(len(client.orders), 1)
        self.assertEqual(client.orders[0]["side"], "Buy")
        self.assertFalse(client.orders[0]["reduce_only"])
        self.assertIsNotNone(client.orders[0]["stop_loss"])
        self.assertEqual(ledger["assets"]["BTC"]["risk_r"], 0.5)

    def test_duplicate_signal_does_not_order_twice(self):
        client = FakeClient()
        state = fresh_state("ETH")
        ledger = {"mode": "bybit_demo", "assets": {"ETH": {"last_signal_key": signal_key(state)}}}
        result = execute_asset("ETH", state, client, ledger, equity=1000, account_r_pct=0.01)
        self.assertEqual(result["reason"], "duplicate_signal")
        self.assertEqual(client.orders, [])

    def test_invalidated_closes_existing_position_reduce_only(self):
        client = FakeClient()
        client.position = {"side": "Buy", "size": "0.25"}
        state = fresh_state("SOL", state="INVALIDATED", direction="long")
        ledger = {"mode": "bybit_demo", "assets": {"SOL": {"risk_r": 0.5}}}
        result = execute_asset("SOL", state, client, ledger, equity=1000, account_r_pct=0.01)
        self.assertEqual(result["status"], "close_sent")
        self.assertTrue(client.orders[0]["reduce_only"])
        self.assertEqual(client.orders[0]["side"], "Sell")


if __name__ == "__main__":
    unittest.main()
