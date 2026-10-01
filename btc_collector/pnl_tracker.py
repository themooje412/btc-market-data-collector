"""Reconstruct and report execution-watcher PnL for BTC/ETH/SOL/ZEC.

PnL is signal PnL, not exchange/account PnL. Historical rows before geometry fields
were persisted can only report price-return PnL. New rows also report R-multiple
when the trigger row contains a frozen hard stop.
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

ASSETS = {"BTC": Path("."), "ETH": Path("assets/eth"), "SOL": Path("assets/sol"), "ZEC": Path("assets/zec")}
TRIGGERS = {"LONG_TRIGGERED": "long", "SHORT_TRIGGERED": "short"}
ACTIVE = {"LONG_TRIGGERED", "SHORT_TRIGGERED", "ADD_ALLOWED", "HOLD_MANAGE"}
CUTOFF = datetime.fromisoformat("2026-09-24T15:03:33+00:00")  # swing schema 1.3.0 production start


def dt(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_rows(path, asset):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    out = []
    for row in rows:
        t = dt(row.get("timestamp"))
        if t and t >= CUTOFF:
            row["asset"] = row.get("asset") or asset
            out.append(row)
    return out


def latest_spot(root):
    p = root / "execution_state.json"
    if not p.exists():
        return None, None
    data = json.loads(p.read_text(encoding="utf-8"))
    return num(data.get("fast_spot")), data.get("fast_timestamp")


def pnl_pct(direction, entry, exit_):
    raw = (exit_ / entry - 1.0) * 100.0
    return raw if direction == "long" else -raw


def r_multiple(direction, entry, exit_, stop):
    if stop is None:
        return None
    risk = entry - stop if direction == "long" else stop - entry
    reward = exit_ - entry if direction == "long" else entry - exit_
    return reward / risk if risk > 0 else None


def reconstruct(asset, root):
    rows = load_rows(root / "execution_history.csv", asset)
    trades, open_trade = [], None
    for row in rows:
        state = row.get("state") or ""
        spot = num(row.get("fast_spot"))
        if spot is None:
            continue
        if state in TRIGGERS:
            direction = TRIGGERS[state]
            if open_trade is not None:
                # A fresh trigger is an unambiguous replacement; close prior mark here.
                close_trade(open_trade, row, spot, "REPLACED_BY_NEW_TRIGGER")
                trades.append(open_trade)
            open_trade = {
                "asset": asset, "direction": direction,
                "entry_time": row.get("timestamp"), "entry": spot,
                "hard_stop": num(row.get("hard_stop")),
                "risk_pct": num(row.get("risk_pct")),
                "entry_state": state, "adds": 0,
            }
        elif open_trade is not None and state == "ADD_ALLOWED":
            open_trade["adds"] += 1
        elif open_trade is not None and state == "INVALIDATED":
            close_trade(open_trade, row, spot, "INVALIDATED")
            trades.append(open_trade)
            open_trade = None

    if open_trade is not None:
        mark, mark_time = latest_spot(root)
        if mark is not None:
            open_trade.update({
                "status": "OPEN", "exit_time": mark_time, "exit": mark,
                "exit_reason": "MARK_TO_MARKET",
                "pnl_pct": pnl_pct(open_trade["direction"], open_trade["entry"], mark),
                "r_multiple": r_multiple(open_trade["direction"], open_trade["entry"], mark, open_trade["hard_stop"]),
            })
        trades.append(open_trade)
    return trades


def close_trade(trade, row, exit_, reason):
    trade.update({
        "status": "CLOSED", "exit_time": row.get("timestamp"), "exit": exit_, "exit_reason": reason,
        "pnl_pct": pnl_pct(trade["direction"], trade["entry"], exit_),
        "r_multiple": r_multiple(trade["direction"], trade["entry"], exit_, trade["hard_stop"]),
    })


def period_key(value, kind):
    t = dt(value)
    if kind == "week":
        y, w, _ = t.isocalendar(); return f"{y}-W{w:02d}"
    return t.strftime("%Y-%m")


def aggregate(trades, kind=None):
    closed = [t for t in trades if t.get("status") == "CLOSED"]
    groups = {"all": closed} if kind is None else {}
    if kind:
        for trade in closed:
            groups.setdefault(period_key(trade["exit_time"], kind), []).append(trade)
    result = {}
    for key, items in groups.items():
        pnls = [t["pnl_pct"] for t in items if t.get("pnl_pct") is not None]
        rs = [t["r_multiple"] for t in items if t.get("r_multiple") is not None]
        result[key] = {
            "closed_trades": len(items),
            "wins": sum(1 for x in pnls if x > 0), "losses": sum(1 for x in pnls if x < 0),
            "win_rate_pct": round(100 * sum(1 for x in pnls if x > 0) / len(pnls), 2) if pnls else None,
            "sum_signal_return_pct": round(sum(pnls), 4),
            "avg_signal_return_pct": round(sum(pnls) / len(pnls), 4) if pnls else None,
            "sum_r": round(sum(rs), 4) if rs else None,
        }
    return result


def write_reports(repo_root=Path(".")):
    all_trades = []
    for asset, rel in ASSETS.items():
        all_trades.extend(reconstruct(asset, repo_root / rel))
    all_trades.sort(key=lambda x: x.get("entry_time") or "")
    out = repo_root / "performance"; out.mkdir(exist_ok=True)
    fields = ["asset","direction","entry_time","entry","hard_stop","risk_pct","adds","status","exit_time","exit","exit_reason","pnl_pct","r_multiple"]
    with (out / "trades.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", lineterminator="\n"); w.writeheader(); w.writerows(all_trades)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "method": "signal_return_from_production_swing_triggers; excludes pre-schema-1.3 scalp era; no fees/slippage/funding",
        "cutoff": CUTOFF.isoformat(),
        "all_time": aggregate(all_trades).get("all", {}),
        "weekly": aggregate(all_trades, "week"),
        "monthly": aggregate(all_trades, "month"),
        "by_asset": {a: aggregate([t for t in all_trades if t["asset"] == a]).get("all", {}) for a in ASSETS},
        "open_trades": [t for t in all_trades if t.get("status") == "OPEN"],
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--root", type=Path, default=Path(".")); args = parser.parse_args()
    print(json.dumps(write_reports(args.root), indent=2))

if __name__ == "__main__":
    main()
