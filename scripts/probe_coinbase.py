"""Probe the exact Coinbase public endpoints from the GitHub runner.

This diagnostic never replaces missing values and never fails the workflow solely
because Coinbase is unreachable. The collector remains responsible for writing
null/error fields when these endpoints cannot be reached.
"""

import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from btc_collector.core import iso, number
from btc_collector.http import Client
from btc_collector.sources import COINBASE
from btc_collector.storage import write_json


def main():
    client = Client()
    checks = []
    cases = [
        ("BTC-USD ticker", "/products/BTC-USD/ticker", {}, lambda data: number(data["price"], 1e-12)),
        ("USDT-USD ticker", "/products/USDT-USD/ticker", {}, lambda data: number(data["price"], 1e-12)),
        ("BTC-USD trades", "/products/BTC-USD/trades", {"limit": 5}, lambda data: data[0]["side"]),
    ]

    for name, path, params, validate in cases:
        row = {"name": name, "base": COINBASE, "path": path, "parameters": params}
        try:
            data, headers, received_at = client.get(COINBASE, path, **params)
            validate(data)
            row.update(status="ok", received_at=iso(received_at))
            if name == "BTC-USD trades":
                cursor = headers.get("cb-after")
                if not cursor:
                    raise ValueError("Missing CB-AFTER pagination header")
                older, _, _ = client.get(COINBASE, path, limit=5, after=cursor)
                if min(int(item["trade_id"]) for item in older) >= min(
                    int(item["trade_id"]) for item in data
                ):
                    raise ValueError("Pagination did not move backwards")
                row["pagination"] = "verified"
        except Exception as exc:
            row.update(status="error", error=str(exc))
        checks.append(row)
        print(f"Coinbase runner probe: {name}: {row['status']}", flush=True)

    write_json(
        "docs/coinbase-runner-check.json",
        {
            "tested_at": iso(time.time()),
            "runner": "GitHub Actions" if "GITHUB_ACTIONS" in __import__("os").environ else "local",
            "results": checks,
            "http_audit": client.audit,
            "fallback_policy": "No Binance or Deribit substitution; unavailable Coinbase values remain null/error.",
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
