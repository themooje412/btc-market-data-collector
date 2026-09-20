#!/usr/bin/env python3
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def epoch(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def require_metric(snapshot, path):
    obj = snapshot
    for part in path:
        obj = obj[part]
    if obj.get("status") != "ok" or obj.get("value") is None:
        raise SystemExit(f"Required metric not OK: {'.'.join(path)} -> {obj.get('status')} {obj.get('reason','')}")
    return obj


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("asset", choices=("ETH", "SOL", "ZEC"))
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    root = args.root or Path("assets") / args.asset.lower()
    path = root / "latest.json"
    if not path.exists():
        raise SystemExit(f"Missing {path}")
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if snapshot.get("asset") != args.asset:
        raise SystemExit(f"Wrong asset in {path}: {snapshot.get('asset')}")
    age = datetime.now(timezone.utc).timestamp() - epoch(snapshot["timestamp"])
    if age < -30 or age > 1800:
        raise SystemExit(f"Snapshot timestamp is not fresh: age={age:.1f}s")
    require_metric(snapshot, ("spot", "binance"))
    require_metric(snapshot, ("cvd", "binance", "15m"))
    require_metric(snapshot, ("cvd", "binance", "1h"))
    require_metric(snapshot, ("market_structure", "rolling_24h", "vwap"))
    for required in ("history.csv", "options_chain.json", "state/market_structure.json", "docs/latest-endpoint-check.json"):
        if not (root / required).exists():
            raise SystemExit(f"Missing {root / required}")
    print(f"{args.asset}: output valid; status={snapshot['status']}; age={age:.1f}s")


if __name__ == "__main__":
    main()
