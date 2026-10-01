"""Deterministic Bybit demo executor for swing signals.

Safety properties:
- demo endpoint only; no production Bybit URL exists in this module;
- only schema >= 1.3 swing geometry is accepted;
- EARLY_SETUP and ARMED states are never auto-entered;
- exchange-side stop loss is attached to every demo entry/add;
- stale signals, duplicate signatures and opposite-position conflicts fail closed;
- account risk is position-sized from the frozen swing stop.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import os
from datetime import datetime, timezone
from pathlib import Path

from .bybit_demo import BybitDemoClient, BybitDemoError, instrument_qty_rules, quantize_down
from .multi_asset_execution import ASSETS, default_root
from .storage import atomic_write, read_json, write_json

SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "ZEC": "ZECUSDT"}
OPEN_STATES = {"LONG_TRIGGERED", "SHORT_TRIGGERED"}
ADD_STATES = {"ADD_ALLOWED"}
MAX_SIGNAL_AGE_SECONDS = 15 * 60
DEFAULT_ACCOUNT_R_PCT = 0.01   # 1R = 1% of account equity
INITIAL_R = 0.50
ADD_R = 0.35
MAX_TOTAL_R = 1.25
MAX_NOTIONAL_MULTIPLE = 2.0
LEDGER_PATH = Path("bybit_demo_state.json")
HISTORY_PATH = Path("bybit_demo_history.csv")


def _dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _schema_ok(value):
    try:
        major, minor, *_ = [int(part) for part in str(value).split(".")]
    except (TypeError, ValueError):
        return False
    return (major, minor) >= (1, 3)


def signal_key(state):
    raw = "|".join([
        str(state.get("asset") or ""),
        str(state.get("signature") or ""),
        str(state.get("state_changed_at") or state.get("fast_timestamp") or ""),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def validate_signal(state, now=None):
    now = now or datetime.now(timezone.utc)
    if not _schema_ok(state.get("schema_version")):
        return False, "schema_before_1.3"
    timestamp = _dt(state.get("fast_timestamp"))
    if timestamp is None or (now - timestamp).total_seconds() > MAX_SIGNAL_AGE_SECONDS:
        return False, "stale_signal"
    action = state.get("state")
    if action not in OPEN_STATES | ADD_STATES | {"INVALIDATED", "HOLD_MANAGE"}:
        return False, "non_actionable_state"
    direction = state.get("direction")
    if direction not in ("long", "short"):
        return False, "missing_direction"
    geometry = state.get("execution") or {}
    if action in OPEN_STATES | ADD_STATES:
        try:
            entry = float(geometry["entry_reference"])
            stop = float(geometry["hard_stop"])
            risk_pct = float(geometry["risk_pct"])
        except (KeyError, TypeError, ValueError):
            return False, "missing_swing_geometry"
        if geometry.get("hard_stop_type") != "core_swing_structure_volatility":
            return False, "non_swing_stop"
        if entry <= 0 or stop <= 0 or risk_pct <= 0:
            return False, "invalid_geometry"
        if direction == "long" and stop >= entry:
            return False, "invalid_long_stop"
        if direction == "short" and stop <= entry:
            return False, "invalid_short_stop"
    return True, "ok"


def size_quantity(equity, risk_r, entry, stop, qty_step, min_qty, account_r_pct=DEFAULT_ACCOUNT_R_PCT,
                  max_notional_multiple=MAX_NOTIONAL_MULTIPLE):
    equity = float(equity)
    risk_cash = equity * float(account_r_pct) * float(risk_r)
    per_unit_risk = abs(float(entry) - float(stop))
    if equity <= 0 or risk_cash <= 0 or per_unit_risk <= 0:
        return None
    qty_by_risk = risk_cash / per_unit_risk
    qty_by_notional = (equity * float(max_notional_multiple)) / float(entry)
    qty = min(qty_by_risk, qty_by_notional)
    rounded = quantize_down(qty, qty_step)
    if float(rounded) < float(min_qty):
        return None
    return rounded


def _position_direction(position):
    if not position:
        return None
    side = str(position.get("side") or "").lower()
    return "long" if side == "buy" else "short" if side == "sell" else None


def _history(event):
    fields = [
        "timestamp", "asset", "event", "signal_state", "direction", "signal_key", "symbol",
        "qty", "entry_reference", "hard_stop", "risk_r", "equity", "order_id", "note",
    ]
    rows = []
    if HISTORY_PATH.exists():
        with HISTORY_PATH.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    rows.append({key: event.get(key, "") for key in fields})
    rows = rows[-2000:]
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    atomic_write(HISTORY_PATH, buf.getvalue())


def _order_id(asset, action, key):
    label = "open" if action in OPEN_STATES else "add" if action in ADD_STATES else "close"
    return f"cg-{asset.lower()}-{label}-{key[:12]}"[:36]


def load_states():
    out = {}
    for asset in ASSETS:
        root = default_root(asset)
        out[asset] = read_json(root / "execution_state.json", {})
    return out


def execute_asset(asset, state, client, ledger, equity, account_r_pct=DEFAULT_ACCOUNT_R_PCT):
    asset = asset.upper()
    symbol = SYMBOLS[asset]
    valid, reason = validate_signal(state)
    if not valid:
        return {"asset": asset, "status": "skip", "reason": reason}

    key = signal_key(state)
    record = (ledger.get("assets") or {}).get(asset) or {}
    if record.get("last_signal_key") == key:
        return {"asset": asset, "status": "skip", "reason": "duplicate_signal"}

    position = client.get_position(symbol)
    position_direction = _position_direction(position)
    action = state.get("state")
    direction = state.get("direction")
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    if action == "INVALIDATED":
        if not position:
            event = {"timestamp": now, "asset": asset, "event": "flat", "signal_state": action,
                     "direction": direction, "signal_key": key, "symbol": symbol, "note": "No open demo position"}
            _history(event)
            record.update({"last_signal_key": key, "status": "flat", "risk_r": 0.0})
            ledger.setdefault("assets", {})[asset] = record
            return {"asset": asset, "status": "flat"}
        qty = str(position.get("size"))
        close_side = "Sell" if position_direction == "long" else "Buy"
        response = client.place_market_order(symbol, close_side, qty, _order_id(asset, action, key), reduce_only=True)
        order_id = ((response.get("result") or {}).get("orderId") or "")
        event = {"timestamp": now, "asset": asset, "event": "close", "signal_state": action,
                 "direction": position_direction, "signal_key": key, "symbol": symbol, "qty": qty,
                 "risk_r": record.get("risk_r", 0.0), "equity": equity, "order_id": order_id,
                 "note": "Swing state invalidated"}
        _history(event)
        record.update({"last_signal_key": key, "status": "closing", "risk_r": 0.0, "order_id": order_id})
        ledger.setdefault("assets", {})[asset] = record
        return {"asset": asset, "status": "close_sent", "order_id": order_id}

    if action == "HOLD_MANAGE":
        record["last_signal_key"] = key
        ledger.setdefault("assets", {})[asset] = record
        return {"asset": asset, "status": "hold"}

    if position and position_direction != direction:
        return {"asset": asset, "status": "skip", "reason": "opposite_position_conflict"}

    is_add = action in ADD_STATES
    if is_add and not position:
        return {"asset": asset, "status": "skip", "reason": "add_without_position"}
    if not is_add and position:
        record["last_signal_key"] = key
        ledger.setdefault("assets", {})[asset] = record
        return {"asset": asset, "status": "skip", "reason": "position_already_open"}

    current_r = float(record.get("risk_r") or 0.0)
    risk_r = ADD_R if is_add else INITIAL_R
    if current_r + risk_r > MAX_TOTAL_R + 1e-9:
        return {"asset": asset, "status": "skip", "reason": "max_total_r"}

    geometry = state.get("execution") or {}
    entry = float(geometry["entry_reference"])
    stop = float(geometry["hard_stop"])
    instrument = client.get_instrument(symbol)
    qty_step, min_qty = instrument_qty_rules(instrument)
    qty = size_quantity(equity, risk_r, entry, stop, qty_step, min_qty, account_r_pct)
    if qty is None:
        return {"asset": asset, "status": "skip", "reason": "size_below_minimum"}

    side = "Buy" if direction == "long" else "Sell"
    response = client.place_market_order(
        symbol, side, qty, _order_id(asset, action, key), stop_loss=str(stop), reduce_only=False
    )
    order_id = ((response.get("result") or {}).get("orderId") or "")
    new_r = current_r + risk_r
    event = {
        "timestamp": now, "asset": asset, "event": "add" if is_add else "open",
        "signal_state": action, "direction": direction, "signal_key": key, "symbol": symbol,
        "qty": qty, "entry_reference": entry, "hard_stop": stop, "risk_r": risk_r,
        "equity": equity, "order_id": order_id,
        "note": "Bybit demo only; exchange-side MarkPrice stop attached",
    }
    _history(event)
    record.update({
        "last_signal_key": key, "status": "open", "direction": direction, "risk_r": new_r,
        "hard_stop": stop, "order_id": order_id, "updated_at": now,
    })
    ledger.setdefault("assets", {})[asset] = record
    return {"asset": asset, "status": "add_sent" if is_add else "open_sent", "qty": qty, "order_id": order_id}


def run_all(client, account_r_pct=DEFAULT_ACCOUNT_R_PCT):
    ledger = read_json(LEDGER_PATH, {"mode": "bybit_demo", "assets": {}})
    if ledger.get("mode") not in (None, "bybit_demo"):
        raise ValueError("Refusing non-demo ledger mode")
    ledger["mode"] = "bybit_demo"
    equity = client.get_total_equity()
    results = []
    for asset, state in load_states().items():
        try:
            results.append(execute_asset(asset, state, client, ledger, equity, account_r_pct))
        except BybitDemoError as exc:
            results.append({"asset": asset, "status": "error", "reason": str(exc)})
    ledger["last_run_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    ledger["last_equity"] = equity
    ledger["last_results"] = results
    write_json(LEDGER_PATH, ledger)
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Send orders to Bybit DEMO only")
    args = parser.parse_args()
    if not args.execute:
        raise SystemExit("Demo executor requires --execute; use tests/shadow inspection before enabling")
    api_key = os.environ.get("BYBIT_DEMO_API_KEY", "")
    api_secret = os.environ.get("BYBIT_DEMO_API_SECRET", "")
    if not api_key or not api_secret:
        raise SystemExit("BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET are required")
    account_r_pct = float(os.environ.get("BYBIT_DEMO_ACCOUNT_R_PCT", str(DEFAULT_ACCOUNT_R_PCT)))
    if not (0 < account_r_pct <= 0.02):
        raise SystemExit("BYBIT_DEMO_ACCOUNT_R_PCT must be >0 and <=0.02")
    client = BybitDemoClient(api_key, api_secret)
    for result in run_all(client, account_r_pct):
        print(result)


if __name__ == "__main__":
    main()
